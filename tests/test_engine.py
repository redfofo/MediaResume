import asyncio
import time

from mediaresume.config import Config, EmbyConfig, Mapping, PlexConfig
from mediaresume.engine import Pair, SyncEngine
from mediaresume.models import MediaItem, MediaKey, WatchState

MOVIE = MediaKey("movie", "603")


def run(coro):
    return asyncio.run(coro)


class Resume:
    """继续观看列表与条目读取的假服务器"""

    def __init__(self, groups, items=None):
        self.groups = groups
        self.items = items or {}

    async def continue_watching(self, *_):
        return [{"group": g, "id": i} for g, ids in self.groups.items() for i in ids]

    async def resume(self, *_):
        return await self.continue_watching()

    async def get_item(self, *args):
        item = self.items.get(args[0]) or self.items.get(args[-1])
        if item is None:
            raise RuntimeError("404")
        return item


def make_engine(plex=None, emby=None, mappings=None):
    mappings = mappings or [Mapping(None, "u")]
    cfg = Config(PlexConfig("http://p", "t"), EmbyConfig("http://e", "k"), mappings)
    eng = SyncEngine(cfg, plex, emby, None)
    eng.pairs = [Pair(m, "t", f"u{i}") for i, m in enumerate(mappings)]
    return eng, eng.pairs[0]


def drain(eng):
    jobs = []
    while not eng.queue.empty():
        jobs.append(eng.queue.get_nowait())
    return jobs


def test_resume_removal_needs_two_rounds():
    plex = Resume({"movie:tmdb:1": ["a"], "movie:tmdb:2": ["b"], "movie:tmdb:3": ["c"]})
    eng, pair = make_engine(plex, Resume({}))
    run(eng._poll_resume(pair))  # 基线
    del plex.groups["movie:tmdb:1"]
    run(eng._poll_resume(pair))
    assert drain(eng) == []  # 第一次消失只记为候选
    run(eng._poll_resume(pair))
    assert [j[3] for j in drain(eng)] == ["movie:tmdb:1"]


def test_resume_flicker_is_ignored():
    plex = Resume({"movie:tmdb:1": ["a"], "movie:tmdb:2": ["b"], "movie:tmdb:3": ["c"]})
    eng, pair = make_engine(plex, Resume({}))
    run(eng._poll_resume(pair))
    ids = plex.groups.pop("movie:tmdb:1")
    run(eng._poll_resume(pair))
    plex.groups["movie:tmdb:1"] = ids  # 下一轮又回来了
    run(eng._poll_resume(pair))
    run(eng._poll_resume(pair))
    assert drain(eng) == []


def test_resume_bulk_vanish_is_ignored():
    plex = Resume({f"movie:tmdb:{n}": [str(n)] for n in range(4)})
    eng, pair = make_engine(plex, Resume({}))
    run(eng._poll_resume(pair))
    saved = dict(plex.groups)
    plex.groups.clear()  # 服务器重启时返回空列表
    for _ in range(3):
        run(eng._poll_resume(pair))
    assert drain(eng) == []
    assert pair.plex_resume == saved  # 保留上一轮快照
    plex.groups.update(saved)
    run(eng._poll_resume(pair))
    run(eng._poll_resume(pair))
    assert drain(eng) == []


def test_resume_removed_skips_unreadable_item():
    removed = []

    class Emby(Resume):
        async def hide_from_resume(self, user_id, item_id, hide=True):
            removed.append(item_id)

    eng, pair = make_engine(Resume({}), Emby({"movie:tmdb:1": ["x"]}))
    run(eng._handle_resume_removed(pair, "plex", "movie:tmdb:1", ["a"]))  # 读取失败
    assert removed == []
    eng.plex.items["a"] = MediaItem("plex", "a", MOVIE, WatchState(False, 60_000))
    run(eng._handle_resume_removed(pair, "plex", "movie:tmdb:1", ["a"]))
    assert removed == ["x"]


def test_owner_mapping_ignores_other_users_sessions():
    class Plex:
        async def session_users(self):
            return {"10": ("1", "owner"), "20": ("5", "kid")}

    eng, owner = make_engine(Plex(), mappings=[Mapping(None, "u"), Mapping("kid", "k")])
    kid = eng.pairs[1]
    run(eng.on_plex_playing({"sessionKey": "20", "ratingKey": "99", "state": "playing"}))
    assert (kid.id, "99") in eng._plex_playing
    assert (owner.id, "99") not in eng._plex_playing
    assert [j[1] for j in drain(eng)] == [kid]
    run(eng.on_plex_playing({"sessionKey": "10", "ratingKey": "99", "state": "playing"}))
    assert (owner.id, "99") in eng._plex_playing
    assert [j[1] for j in drain(eng)] == [owner]


def test_reconcile_not_queued_twice():
    eng, _ = make_engine()
    assert eng.request_reconcile()
    assert not eng.request_reconcile()
    assert eng.queue.qsize() == 1


def test_setup_retries_until_emby_ready(monkeypatch):
    class Emby:
        calls = 0

        async def resolve_user(self, name):
            Emby.calls += 1
            if Emby.calls < 3:
                raise ConnectionError("Emby 未启动")
            return {"id": "e1", "name": name}

    async def no_sleep(_):
        pass

    eng, _ = make_engine(emby=Emby())
    monkeypatch.setattr("mediaresume.engine.asyncio.sleep", no_sleep)
    run(eng._setup_until_ready())
    assert Emby.calls == 3 and len(eng.pairs) == 1 and eng.setup_error is None


def test_stale_playing_expires_and_reconnect_clears(monkeypatch):
    eng, pair = make_engine()
    eng._plex_playing[(pair.id, "1")] = time.monotonic()
    assert eng._is_playing(pair, "1")
    eng._plex_playing[(pair.id, "1")] = time.monotonic() - 500  # 漏了 stopped
    assert not eng._is_playing(pair, "1")
    assert (pair.id, "1") not in eng._plex_playing
    eng._plex_playing[(pair.id, "2")] = time.monotonic()
    eng._plex_sessions["10"] = ("1", "owner")
    eng._on_plex_connect()
    assert eng._plex_playing == {} and eng._plex_sessions == {}


def test_resume_sync_result_after_request_timeout():
    eng, pair = make_engine()

    async def fake_sync(*_):
        return {"ok": True}

    eng._resume_sync = fake_sync

    async def main():
        fut = asyncio.get_running_loop().create_future()
        fut.cancel()  # 页面请求已超时
        await eng._run_resume_sync(pair, "plex", fut)  # 不应抛 InvalidStateError
        fut = asyncio.get_running_loop().create_future()
        await eng._run_resume_sync(pair, "plex", fut)
        return fut.result()

    assert run(main()) == {"ok": True}


def test_resume_sync_fails_future_when_engine_stops():
    eng, pair = make_engine()

    async def main():
        started = asyncio.Event()

        async def slow_sync(*_):
            started.set()
            await asyncio.sleep(10)

        eng._resume_sync = slow_sync
        fut = asyncio.get_running_loop().create_future()
        task = asyncio.create_task(eng._run_resume_sync(pair, "plex", fut))
        await started.wait()
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        return fut.exception()

    assert isinstance(run(main()), RuntimeError)


def test_agreed_uses_earliest_watch_time():
    from mediaresume.engine import _agreed

    assert _agreed(WatchState(True, 0, 200), WatchState(True, 0, 100)).last_played == 100
    assert _agreed(WatchState(True, 0, 200), WatchState(True, 0, 0)).last_played == 200
    assert _agreed(WatchState(False, 5, 200), WatchState(False, 5, 100)).last_played == 200


def test_store_bulk_read_and_batched_commit(tmp_path):
    from mediaresume.store import StateStore

    path = str(tmp_path / "s.db")
    store = StateStore(path)
    store.set("p", MOVIE, WatchState(False, 1000, 5), commit=False)
    assert StateStore(path).all("p") == {}  # 未提交，其他连接看不到
    store.commit()
    assert StateStore(path).all("p") == {str(MOVIE): WatchState(False, 1000, 5)}


def test_timed_out_resume_sync_is_not_executed():
    eng, pair = make_engine()
    called = []

    async def fake_sync(*_):
        called.append(1)
        return {}

    eng._resume_sync = fake_sync

    async def main():
        fut = asyncio.get_running_loop().create_future()
        fut.cancel()
        await eng._run_resume_sync(pair, "plex", fut)

    run(main())
    assert called == []


def make_timer_engine(tmp_path):
    from mediaresume.store import StateStore

    eng, _ = make_engine()
    eng.store = StateStore(str(tmp_path / "s.db"))
    calls = []

    async def scheduled(direction):
        calls.append(direction)

    eng.trakt_scheduled = scheduled
    return eng, calls


def test_trakt_timer_resumes_after_restart(tmp_path):
    eng, calls = make_timer_engine(tmp_path)
    eng.store.meta_set("trakt_last_to_trakt", str(time.time() - 7200))  # 停机期间已到期

    async def main():
        task = asyncio.create_task(eng._trakt_timer("to_trakt", 3600))
        await asyncio.sleep(0.05)
        before = list(calls)  # 首次对账前不执行
        eng._reconciled.set()
        await asyncio.sleep(0.05)
        task.cancel()
        return before

    assert run(main()) == [] and calls == ["to_trakt"]
    assert time.time() - float(eng.store.meta_get("trakt_last_to_trakt")) < 5
    assert abs(eng.trakt_next["to_trakt"] - (time.time() + 3600)) < 5


def test_trakt_timer_first_start_records_baseline(tmp_path):
    eng, calls = make_timer_engine(tmp_path)

    async def main():
        task = asyncio.create_task(eng._trakt_timer("from_trakt", 3600))
        await asyncio.sleep(0.05)
        task.cancel()

    run(main())
    assert calls == [] and eng.store.meta_get("trakt_last_from_trakt") is not None
    assert abs(eng.trakt_next["from_trakt"] - (time.time() + 3600)) < 5
