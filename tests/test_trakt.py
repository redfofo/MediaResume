import asyncio
import json
import time
from dataclasses import replace

import pytest

from mediaresume.config import Config, EmbyConfig, Mapping, PlexConfig, SyncConfig, TraktConfig, parse_config
from mediaresume.engine import Pair, SyncEngine
from mediaresume.models import MediaItem, MediaKey, WatchState
from mediaresume.store import StateStore
from mediaresume.trakt import (
    Mark,
    Playback,
    PushPlan,
    TraktAuthError,
    TraktClient,
    TraktSync,
    TraktTokenStore,
    history_payload,
    not_found_tmdb,
    parse_playback,
    parse_watched,
    _parse_ts,
    scrobble_body,
)

MOVIE = MediaKey("movie", "603")
EP1 = MediaKey("episode", "1399", 1, 1)
EP2 = MediaKey("episode", "1399", 1, 2)
EP3 = MediaKey("episode", "1399", 1, 3)
EP4 = MediaKey("episode", "1399", 1, 4)
NOW = int(time.time())


class FakeClient:
    def __init__(self, watched=(), not_found=None, fail=None, playback=None, history=None):
        self.remote = {k: NOW for k in watched}
        self.remote_playback = dict(playback or {})
        # MediaKey -> 观看记录；不在其中的视为 Trakt 不认识
        self.history = dict(history or {})
        self.removed_history: list[int] = []
        self.not_found = not_found or {}
        self.fail = fail
        self.posts: list[dict] = []
        self.scrobbles: list[tuple] = []
        self.removed: list[int] = []
        self.watched_calls = 0
        self.playback_calls = 0
        self.activity = "t0"
        self.paused = "p0"

    async def last_activities(self, username):
        return {
            "movies": {"watched_at": self.activity, "paused_at": self.paused},
            "episodes": {"watched_at": self.activity, "paused_at": self.paused},
        }

    async def watched(self, username):
        self.watched_calls += 1
        return dict(self.remote)

    async def add_history(self, username, payload):
        if self.fail:
            raise self.fail
        self.posts.append(payload)
        self.activity += "+"
        return {"added": {"movies": len(payload.get("movies", [])), "episodes": 0}, "not_found": self.not_found}

    async def playback(self, username):
        self.playback_calls += 1
        return dict(self.remote_playback)

    async def remove_playback(self, username, playback_id):
        self.removed.append(playback_id)

    async def item_history(self, username, key):
        return self.history.get(key)

    async def remove_history(self, username, history_ids):
        self.removed_history.extend(history_ids)
        return {"deleted": {"movies": len(history_ids)}}

    async def scrobble(self, username, action, key, progress):
        if self.fail:
            raise self.fail
        self.scrobbles.append((action, key, progress))
        self.paused += "+"


def make(client, tmp_path, dry_run=False, **cfg):
    sync = TraktSync(client, TraktConfig("id", "secret", **cfg), dry_run, tmp_path / "data" / "trakt_removed.jsonl")
    sync.add_account("me")
    return sync, sync.accounts["me"]


def played(ts=1_700_000_000):
    return WatchState(True, 0, ts)


def run(coro):
    return asyncio.run(coro)


# ---------- 数据转换 ----------


def test_parse_watched():
    movies = [{"movie": {"ids": {"tmdb": 603}}, "last_watched_at": "2023-11-14T22:13:20.000Z"}, {"movie": {"ids": {}}}]
    show = {"ids": {"tmdb": 1399}}
    history = [
        {"type": "episode", "watched_at": "2023-11-14T22:13:20.000Z", "episode": {"season": 1, "number": 1}, "show": show},
        {"type": "episode", "watched_at": "2023-11-15T22:13:20.000Z", "episode": {"season": 1, "number": 1}, "show": show},
        {"type": "episode", "episode": {"season": 1, "number": 2}, "show": show},
        {"type": "episode", "episode": {"season": 1, "number": 3}, "show": {"ids": {}}},
    ]
    assert parse_watched(movies, history) == {MOVIE: 1_700_000_000, EP1: 1_700_086_400, EP2: 0}


def test_parse_playback_keeps_latest():
    entries = [
        {"id": 1, "type": "movie", "progress": 10, "paused_at": "2023-11-14T22:13:20.000Z", "movie": {"ids": {"tmdb": 603}}},
        {"id": 2, "type": "movie", "progress": 30, "paused_at": "2023-11-15T22:13:20.000Z", "movie": {"ids": {"tmdb": 603}}},
        {"id": 3, "type": "episode", "progress": 50, "episode": {"season": 1, "number": 2}, "show": {"ids": {"tmdb": 1399}}},
    ]
    pb = parse_playback(entries)
    assert pb[MOVIE].id == 2 and pb[MOVIE].progress == 30
    assert pb[EP2].id == 3


def test_history_payload_groups_episodes():
    payload = history_payload({MOVIE: 0, EP2: 1_700_000_000, EP1: 1_700_000_000})
    assert payload["movies"] == [{"ids": {"tmdb": 603}, "watched_at": "unknown"}]
    (show,) = payload["shows"]
    assert show["ids"] == {"tmdb": 1399}
    assert [e["number"] for e in show["seasons"][0]["episodes"]] == [2, 1]
    assert show["seasons"][0]["episodes"][0]["watched_at"] == "2023-11-14T22:13:20.000Z"


def test_scrobble_body():
    assert scrobble_body(EP1, 12.5) == {
        "show": {"ids": {"tmdb": 1399}}, "episode": {"season": 1, "number": 1}, "progress": 12.5,
    }
    assert scrobble_body(MOVIE, 3) == {"movie": {"ids": {"tmdb": 603}}, "progress": 3}


def test_not_found_tmdb():
    resp = {"not_found": {"movies": [{"ids": {"tmdb": 1}}], "shows": [{"ids": {"tmdb": 2}}]}}
    assert not_found_tmdb(resp) == ({"1"}, {"2"})


# ---------- 实时进度 ----------


def progress(pct, duration=3_600_000, last=NOW):
    return WatchState(False, int(duration * pct / 100), last), duration


def test_observe_live_thresholds(tmp_path):
    sync, acc = make(FakeClient(), tmp_path)
    sync.observe_live("me", MOVIE, *progress(85))  # >=80% 会被 Trakt 记为看完
    sync.observe_live("me", MOVIE, *progress(0.5))  # 暂停时 <1% 会被拒绝
    sync.observe_live("me", MOVIE, WatchState(False, 60_000, NOW), 0)  # 片长未知
    sync.observe_live(None, MOVIE, *progress(30))
    sync.observe_live("me", MOVIE, played(), 0)  # 本次运行没有播放过，只是被标记已看
    assert acc.pending == {}
    sync.observe_live("me", MOVIE, *progress(0), playing=True)
    assert acc.pending[MOVIE][1] == "start"


def test_live_start_stop_and_finish(tmp_path):
    client = FakeClient()
    sync, acc = make(client, tmp_path)
    sync.observe_live("me", EP1, *progress(10), playing=True)
    run(sync._push_live(acc))
    sync.observe_live("me", EP1, *progress(10.5), playing=True)  # 变化太小
    assert acc.pending == {}
    sync.observe_live("me", EP1, *progress(40))
    run(sync._push_live(acc))
    sync.observe_live("me", EP1, played(), 0)
    run(sync._push_live(acc))
    # 另一端的回传事件不会再记一次
    sync.observe_live("me", EP1, played(), 0)
    assert acc.pending == {}
    assert client.scrobbles == [("start", EP1, 10.0), ("stop", EP1, 40.0), ("stop", EP1, 100.0)]
    assert acc.progress_pushed == 3


def test_live_dry_run_does_not_scrobble(tmp_path):
    client = FakeClient()
    sync, acc = make(client, tmp_path, dry_run=True)
    sync.observe_live("me", EP1, *progress(30))
    run(sync._push_live(acc))
    assert client.scrobbles == [] and acc.pending == {}


def test_scrobble_disabled(tmp_path):
    sync, acc = make(FakeClient(), tmp_path, scrobble=False)
    sync.observe_live("me", MOVIE, *progress(30), playing=True)
    assert acc.pending == {}


def test_live_failure_keeps_pending_for_retry(tmp_path):
    sync, acc = make(FakeClient(fail=RuntimeError("boom")), tmp_path)
    sync.observe_live("me", MOVIE, *progress(30))
    assert not run(sync._guard(acc, sync._push_live(acc)))
    assert MOVIE in acc.pending
    assert acc.error == "boom" and acc.retry_at > 0


def test_auth_failure_stops_account(tmp_path):
    sync, acc = make(FakeClient(fail=TraktAuthError("请重新授权")), tmp_path)
    sync.observe_live("me", MOVIE, *progress(30))
    run(sync._guard(acc, sync._push_live(acc)))
    assert acc.auth_failed
    sync.observe_live("me", EP1, *progress(30))
    assert acc.pending == {}


# ---------- 全量推送 ----------


def test_push_full(tmp_path):
    client = FakeClient(not_found={"shows": [{"ids": {"tmdb": 1399}}]})
    sync, acc = make(client, tmp_path)
    plan = PushPlan(history={MOVIE: 1_700_000_000, EP1: 0}, progress={EP2: 30.0}, clear={MOVIE: 7})
    run(sync.push_full("me", plan))
    assert client.posts == [history_payload(plan.history)]
    assert client.scrobbles == [("stop", EP2, 30.0)]
    assert client.removed == [7]
    assert acc.progress_sent[EP2] == (30.0, False)
    assert not acc.busy and acc.error is None


def test_push_full_dry_run(tmp_path):
    client = FakeClient()
    sync, _ = make(client, tmp_path, dry_run=True)
    run(sync.push_full("me", PushPlan(history={MOVIE: 0}, progress={EP2: 30.0}, clear={MOVIE: 7})))
    assert client.posts == client.scrobbles == client.removed == []


# ---------- 引擎 ----------


class Server:
    def __init__(self, name, items):
        self.name = name
        self.items = {i.item_id: i for i in items}
        self.ops = []

    async def list_items(self, *_):
        return list(self.items.values())

    async def get_item(self, *args):
        return self.items.get(args[0] if self.name == "plex" else args[1])

    async def mark_played(self, *args):
        self.ops.append(("played", args[0] if self.name == "plex" else args[1]))

    async def mark_unplayed(self, *args):
        self.ops.append(("unplayed", args[0] if self.name == "plex" else args[1]))

    async def set_position(self, *args):
        item_id, pos = (args[0], args[1]) if self.name == "plex" else (args[1], args[2])
        self.ops.append(("position", item_id, pos))


def make_engine(tmp_path, plex_items, emby_items, dry_run=False, client=None, servers=("plex", "emby")):
    """servers 只含一个时，另一端的客户端为 None（只与 Trakt 同步）"""
    plex = Server("plex", plex_items) if "plex" in servers else None
    emby = Server("emby", emby_items) if "emby" in servers else None
    mapping = Mapping(None, "u" if emby else "", trakt_user="me")
    cfg = Config(
        PlexConfig("http://p", "t") if plex else None,
        EmbyConfig("http://e", "k") if emby else None,
        [mapping],
        SyncConfig(dry_run=dry_run),
    )
    sync = TraktSync(client or FakeClient(), TraktConfig("id", "s"), dry_run)
    sync.add_account("me")
    eng = SyncEngine(cfg, plex, emby, StateStore(str(tmp_path / "e.db")), sync)
    pair = Pair(mapping, "t" if plex else "", "u" if emby else "")
    eng.pairs.append(pair)
    return eng, pair, plex, emby, sync.accounts["me"]


H = 3_600_000


def test_reconcile_records_state_without_pushing(tmp_path):
    eng, pair, *_, acc = make_engine(
        tmp_path,
        [MediaItem("plex", "1", MOVIE, played()), MediaItem("plex", "2", EP1, WatchState(False, 0))],
        [MediaItem("emby", "a", EP1, played()), MediaItem("emby", "b", EP2, WatchState(False, 900_000, NOW), duration_ms=H)],
        dry_run=True,
    )
    run(eng._reconcile(pair))
    assert acc.pending == {}
    assert pair.unified[EP1][0].played  # 对账后的统一状态
    assert pair.unified[EP2] == (WatchState(False, 900_000, NOW), "", H)


def test_plan_to_trakt(tmp_path):
    client = FakeClient(watched={EP1}, playback={MOVIE: Playback(7, 50, NOW), EP2: Playback(8, 25.5, NOW)})
    eng, pair, *_ = make_engine(
        tmp_path,
        [
            MediaItem("plex", "1", MOVIE, played(), "M"),
            MediaItem("plex", "2", EP1, played(), "E1"),
            MediaItem("plex", "3", EP2, WatchState(False, 900_000, NOW), "E2", duration_ms=H),  # 25%，与 Trakt 一致
            MediaItem("plex", "4", EP3, WatchState(False, 1_800_000, NOW), "E3", duration_ms=H),
            MediaItem("plex", "5", EP4, WatchState(False, 3_200_000, NOW), "E4", duration_ms=H),  # 89%，不推送
        ],
        [],
        client=client,
    )
    run(eng._reconcile(pair))
    show, plan = run(eng.trakt_plan(pair, "to_trakt"))
    assert plan.history == {MOVIE: 1_700_000_000}
    assert plan.progress == {EP3: 50.0}
    assert plan.clear == {MOVIE: 7}
    assert [i["title"] for i in show["history"]] == ["M"]
    assert show["progress"][0]["trakt_pct"] is None


def test_scheduled_waits_for_reconcile_then_executes(tmp_path):
    client = FakeClient()
    eng, pair, *_ = make_engine(tmp_path, [MediaItem("plex", "1", MOVIE, played(), "M")], [], client=client)
    run(eng.trakt_scheduled("to_trakt"))  # 尚未对账：跳过
    assert client.posts == [] and not eng._background
    run(eng._reconcile(pair))

    async def scheduled():
        await eng.trakt_scheduled("to_trakt")
        await asyncio.gather(*eng._background)

    run(scheduled())
    assert client.posts and client.posts[0]["movies"][0]["ids"] == {"tmdb": 603}


def test_plan_from_trakt(tmp_path):
    client = FakeClient(
        watched={EP1},
        playback={EP2: Playback(1, 50, NOW), EP3: Playback(2, 50, NOW - 1000), EP4: Playback(3, 25.2, NOW)},
    )
    eng, pair, *_ = make_engine(
        tmp_path,
        [
            MediaItem("plex", "1", EP1, WatchState(False, 0)),
            MediaItem("plex", "2", EP2, WatchState(False, 0, NOW - 100), duration_ms=H),
            MediaItem("plex", "3", EP3, WatchState(False, 0, NOW), duration_ms=H),  # 本地更新
            MediaItem("plex", "4", EP4, WatchState(False, 900_000, NOW - 100), duration_ms=H),  # 差距太小
        ],
        [],
        client=client,
    )
    run(eng._reconcile(pair))
    show, (watched, prog, seen) = run(eng.trakt_plan(pair, "from_trakt"))
    assert watched == {EP1: NOW}
    assert prog == {EP2: (50, NOW)}
    assert show["skipped_newer"] == 1
    assert seen == {EP1: NOW}


def test_pull_keeps_local_unwatched_after_seen(tmp_path):
    # 上次拉取已见过 EP1 的观看记录，之后本地标为未看（准备重看）：不再改回已看
    client = FakeClient(watched={EP1, EP2})
    eng, pair, plex, *_ = make_engine(
        tmp_path,
        [MediaItem("plex", "1", EP1, WatchState(False, 0)), MediaItem("plex", "2", EP2, WatchState(False, 0))],
        [],
        client=client,
    )
    eng.store.set_trakt_seen(pair.id, {EP1: NOW, EP2: NOW - 100})
    run(eng._reconcile(pair))
    show, (watched, _, _) = run(eng.trakt_plan(pair, "from_trakt"))
    assert watched == {EP2: NOW}  # EP2 在 Trakt 上有新的观看
    assert show["kept_unwatched"] == 1


def test_pull_records_seen(tmp_path):
    eng, pair, plex, *_ = make_engine(tmp_path, [MediaItem("plex", "1", EP1, WatchState(False, 0))], [])
    pair.plex_index[EP1] = ["1"]
    run(eng._trakt_pull(pair, {EP1: NOW}, {}, {EP1: NOW, EP2: NOW - 5}))
    assert eng.store.trakt_seen(pair.id) == {str(EP1): NOW, str(EP2): NOW - 5}


def test_trakt_pull_applies_to_both_sides(tmp_path):
    eng, pair, plex, emby, acc = make_engine(
        tmp_path,
        [MediaItem("plex", "1", EP1, WatchState(False, 0)), MediaItem("plex", "2", EP2, WatchState(False, 0, NOW - 100), duration_ms=H)],
        [MediaItem("emby", "a", EP1, played()), MediaItem("emby", "b", EP2, WatchState(False, 0, NOW - 100), duration_ms=H)],
    )
    for item in list(plex.items.values()) + list(emby.items.values()):
        (pair.plex_index if item.server == "plex" else pair.emby_index)[item.key] = [item.item_id]
    run(eng._trakt_pull(pair, {EP1: 1_700_000_000}, {EP2: (50.0, NOW)}))
    assert plex.ops == [("played", "1"), ("position", "2", 1_800_000)]
    assert emby.ops == [("position", "b", 1_800_000)]
    assert eng.store.get(pair.id, EP1).played
    assert acc.progress_sent[EP2] == (50.0, False)


def test_live_events_push_progress(tmp_path):
    item = MediaItem("emby", "a", EP1, WatchState(False, 600_000, NOW), duration_ms=H)
    eng, pair, plex, emby, acc = make_engine(tmp_path, [], [item])
    pair.plex_index[EP1] = ["1"]
    # Plex 正在播放时，Emby 的变化是同步过去的，不推送“暂停”
    eng._plex_playing[(pair.id, "1")] = time.monotonic()
    run(eng._handle_event("emby", pair, "a"))
    assert acc.pending == {}
    del eng._plex_playing[(pair.id, "1")]
    run(eng._handle_event("emby", pair, "a"))
    assert acc.pending[EP1][1] == "stop"


class UnwatchPlex(Server):
    """Plex 标记未看检测所需的接口：已看电影集合、每部剧的已看集数、剧的全部单集"""

    def __init__(self, items, counts):
        super().__init__("plex", items)
        self.counts = counts

    async def watched_movies(self, *_):
        return set()

    async def show_viewed_counts(self, *_):
        return dict(self.counts)

    async def show_episodes(self, *_):
        return [i for i in self.items.values() if i.key.kind == "episode"]


def test_episode_unwatch_only_in_plex_is_detected(tmp_path):
    # 双端模式下只在 Plex 中的剧集没有基准，之前标记未看检测不到，Trakt 收不到
    eng, pair, *_ = make_engine(tmp_path, [], [])
    plex = UnwatchPlex([MediaItem("plex", "1", EP1, WatchState(False, 0))], {"show": 1})
    eng.plex = plex
    run(eng._poll_plex_unwatched(pair))  # 建立快照
    pair.unified[EP1] = (played(), "", H)
    plex.counts["show"] = 0
    run(eng._poll_plex_unwatched(pair))
    assert [j[2] for j in drain(eng)] == ["1"]


def test_episode_unwatch_without_any_record_is_ignored(tmp_path):
    eng, pair, *_ = make_engine(tmp_path, [], [])
    plex = UnwatchPlex([MediaItem("plex", "1", EP1, WatchState(False, 0))], {"show": 1})
    eng.plex = plex
    run(eng._poll_plex_unwatched(pair))
    plex.counts["show"] = 0
    run(eng._poll_plex_unwatched(pair))
    assert drain(eng) == []


def test_trakt_pull_does_not_store_base_for_one_sided_item(tmp_path):
    # 只在 Plex 中的条目写入基准后，Emby 之后入库的未看条目会被对账当成“标记未看”同步回 Plex
    eng, pair, plex, *_ = make_engine(tmp_path, [MediaItem("plex", "1", EP1, WatchState(False, 0))], [])
    pair.plex_index[EP1] = ["1"]
    run(eng._trakt_pull(pair, {EP1: NOW}, {}))
    assert plex.ops == [("played", "1")]
    assert eng.store.get(pair.id, EP1) is None
    assert pair.unified[EP1][0].played


@pytest.mark.parametrize("server", ["plex", "emby"])
def test_single_server_reconcile_feeds_trakt(tmp_path, server):
    items = [MediaItem(server, "1", MOVIE, played()), MediaItem(server, "2", EP1, WatchState(False, 900_000, NOW), duration_ms=H)]
    eng, pair, *_ = make_engine(tmp_path, items, items, servers=(server,))
    run(eng._reconcile(pair))
    assert pair.unified[MOVIE][0].played and pair.unified[EP1][0].position_ms == 900_000
    stats = eng.reconcile_stats[pair.id]
    assert stats[server] == 2 and "matched" not in stats
    assert eng.store.all(pair.id) == {}  # 没有另一端，不写基准


def test_emby_only_live_event_pushes_to_trakt(tmp_path):
    item = MediaItem("emby", "a", EP1, WatchState(False, 600_000, NOW), duration_ms=H)
    eng, pair, _, emby, acc = make_engine(tmp_path, [], [item], servers=("emby",))
    run(eng._handle_event("emby", pair, "a"))  # 不能去查找不存在的 Plex
    assert acc.pending[EP1][1] == "stop"
    assert emby.ops == []


def test_plex_only_trakt_pull_writes_plex(tmp_path):
    eng, pair, plex, *_ = make_engine(tmp_path, [MediaItem("plex", "1", EP1, WatchState(False, 0))], [], servers=("plex",))
    run(eng._reconcile(pair))
    run(eng._trakt_pull(pair, {EP1: NOW}, {}))
    assert plex.ops == [("played", "1")]


def test_emby_only_poll_skips_plex(tmp_path):
    class Emby(Server):
        async def changed_since(self, *_):
            return ["a"]

    eng, pair, *_ = make_engine(tmp_path, [], [], servers=("emby",))
    eng.emby = Emby("emby", [])
    run(eng._poll(pair))
    assert [j[:1] + j[2:] for j in drain(eng)] == [("emby", "a")]


def test_single_server_rejects_resume_sync(tmp_path):
    eng, pair, *_ = make_engine(tmp_path, [], [], servers=("plex",))
    with pytest.raises(ValueError, match="同时配置"):
        run(eng.resume_sync_plan(pair, "plex"))


def drain(eng):
    jobs = []
    while not eng.queue.empty():
        jobs.append(eng.queue.get_nowait())
    return jobs


# ---------- 客户端 / 配置 ----------


def test_client_refreshes_expired_token(tmp_path, monkeypatch):
    import aiohttp
    from aiohttp import web
    from aiohttp.test_utils import TestServer

    from mediaresume import trakt
    from mediaresume.trakt import TraktClient, TraktTokenStore

    seen = []
    refreshed = 0

    async def token(request):
        body = await request.json()
        assert body["grant_type"] == "refresh_token" and body["refresh_token"] == "r1"
        assert body["client_secret"] == "secret"
        nonlocal refreshed
        refreshed += 1
        return web.json_response({"access_token": "a2", "refresh_token": "r2", "expires_in": 86400, "created_at": int(time.time())})

    async def watched(request):
        seen.append(request.headers["Authorization"])
        return web.json_response([{"movie": {"ids": {"tmdb": 603}}}])

    async def history(request):
        # 第 1 页满页、第 2 页不满：应取到两页且不再请求后续页
        page = int(request.query["page"])
        pages.append(page)
        n = trakt.PAGE_LIMIT if page == 1 else 1 if page == 2 else 0
        ep = {"type": "episode", "episode": {"season": 1, "number": page}, "show": {"ids": {"tmdb": 1399}}}
        return web.json_response([ep] * n)

    pages = []

    async def main():
        app = web.Application()
        app.router.add_post("/oauth/token", token)
        app.router.add_get("/sync/watched/movies", watched)
        app.router.add_get("/sync/history/episodes", history)
        async with TestServer(app) as server:
            monkeypatch.setattr(trakt, "API", str(server.make_url("")).rstrip("/"))
            store = TraktTokenStore(tmp_path / "tokens.json")
            store.set("me", {"access_token": "a1", "refresh_token": "r1", "expires_at": int(time.time()) + 60, "client_id": "cid"})
            async with aiohttp.ClientSession() as http:
                client = TraktClient(http, TraktConfig("cid", "secret"), store)
                assert await client.watched("me") == {MOVIE: 0, EP1: 0, EP2: 0}
            return store

    monkeypatch.setattr(trakt, "PAGE_LIMIT", 3)
    monkeypatch.setattr(trakt, "PAGE_CONCURRENCY", 2)
    store = run(main())
    # 两个接口各并发 2 页，token 只刷新一次
    assert seen == ["Bearer a2"] * 2 and refreshed == 1
    assert sorted(pages) == [1, 2]
    assert store.get("me")["refresh_token"] == "r2"
    assert (tmp_path / "tokens.json").stat().st_mode & 0o777 == 0o600


def test_client_omits_missing_secret(tmp_path):
    from mediaresume.trakt import TraktClient, TraktTokenStore

    store = TraktTokenStore(tmp_path / "tokens.json")
    assert TraktClient(None, TraktConfig("cid", ""), store)._with_secret({"client_id": "cid"}) == {"client_id": "cid"}
    assert TraktClient(None, TraktConfig("cid", "s"), store)._with_secret({})["client_secret"] == "s"


def test_client_rejects_token_from_other_app(tmp_path):
    from mediaresume.trakt import TraktClient, TraktTokenStore

    store = TraktTokenStore(tmp_path / "tokens.json")
    store.set("me", {"access_token": "a", "refresh_token": "r", "expires_at": 2**40, "client_id": "old"})
    client = TraktClient(None, TraktConfig("new", "s"), store)
    with pytest.raises(TraktAuthError):
        run(client._access_token("me"))


BASE = {"plex": {"url": "http://p", "token": "t"}, "emby": {"url": "http://e", "api_key": "k"}}


def test_config_requires_trakt_credentials_when_bound():
    with pytest.raises(ValueError):
        parse_config({**BASE, "mappings": [{"emby_user": "u", "trakt_user": "me"}]})
    # 新建的 Trakt App 没有 Client Secret
    assert parse_config({**BASE, "mappings": [{"emby_user": "u", "trakt_user": "me"}], "trakt": {"client_id": "a"}}).trakt.client_secret == ""
    cfg = parse_config({
        **BASE,
        "mappings": [{"emby_user": "u", "trakt_user": "me"}],
        "trakt": {"client_id": " a ", "client_secret": "b", "scrobble": False, "pull_progress": True},
    })
    assert cfg.mappings[0].trakt_user == "me"
    assert cfg.trakt.client_id == "a" and not cfg.trakt.scrobble
    assert parse_config({**BASE, "mappings": [{"emby_user": "u"}]}).mappings[0].trakt_user is None
    assert cfg.trakt.push_interval == cfg.trakt.pull_interval == 0


def test_config_trakt_schedule():
    cfg = parse_config({**BASE, "mappings": [{"emby_user": "u"}], "trakt": {"push_interval": "6", "pull_interval": 24}})
    assert (cfg.trakt.push_interval, cfg.trakt.pull_interval) == (6, 24)
    for bad in (-1, "x"):
        with pytest.raises(ValueError):
            parse_config({**BASE, "mappings": [{"emby_user": "u"}], "trakt": {"push_interval": bad}})



def test_reconcile_drops_deleted_items_from_unified(tmp_path):
    eng, pair, plex, *_ = make_engine(tmp_path, [MediaItem("plex", "1", MOVIE, played())], [])
    pair.unified[EP4] = (played(), "gone", 0)
    run(eng._reconcile(pair))
    assert set(pair.unified) == {MOVIE}


def test_reconcile_reports_earliest_watch_time(tmp_path):
    eng, pair, *_ = make_engine(
        tmp_path,
        [MediaItem("plex", "1", MOVIE, WatchState(True, 0, NOW))],  # 同步写入时 Plex 记录的是同步时间
        [MediaItem("emby", "a", MOVIE, WatchState(True, 0, NOW - 3600))],
    )
    run(eng._reconcile(pair))
    assert pair.unified[MOVIE][0].last_played == NOW - 3600


def test_concurrent_push_requests_run_once(tmp_path):
    client = FakeClient()
    eng, pair, *_ = make_engine(tmp_path, [MediaItem("plex", "1", MOVIE, played(), "M")], [], client=client)
    run(eng._reconcile(pair))

    async def main():
        results = await asyncio.gather(
            eng.trakt_execute(pair, "to_trakt"), eng.trakt_execute(pair, "to_trakt"), return_exceptions=True
        )
        await asyncio.gather(*eng._background)
        return results

    results = run(main())
    assert sum(isinstance(r, ValueError) for r in results) == 1
    assert len(client.posts) == 1


def test_reauthorize_resumes_account(tmp_path):
    sync, acc = make(FakeClient(fail=TraktAuthError("请重新授权")), tmp_path)
    sync.observe_live("me", MOVIE, *progress(30))
    run(sync._guard(acc, sync._push_live(acc)))
    assert acc.auth_failed
    sync.reauthorized("me")
    assert not acc.auth_failed and acc.error is None
    sync.observe_live("me", EP1, *progress(30))
    assert EP1 in acc.pending


def test_pull_does_not_mark_missing_items_seen(tmp_path):
    # MOVIE 在 Trakt 上看过但本地还没入库：不能记为已见，否则入库后永远不会拉取
    client = FakeClient(watched={EP1, MOVIE})
    eng, pair, *_ = make_engine(tmp_path, [MediaItem("plex", "1", EP1, WatchState(False, 0))], [], client=client)
    run(eng._reconcile(pair))
    _, (watched, _, seen) = run(eng.trakt_plan(pair, "from_trakt"))
    assert set(watched) == {EP1} and set(seen) == {EP1}


class FlakyServer(Server):
    """对指定条目的写入抛异常"""

    def __init__(self, name, items, broken):
        super().__init__(name, items)
        self.broken = broken

    async def mark_played(self, *args):
        item_id = args[0] if self.name == "plex" else args[1]
        if item_id in self.broken:
            raise RuntimeError("404 Not Found")
        await super().mark_played(*args)


def test_reconcile_continues_after_item_failure(tmp_path):
    eng, pair, *_ = make_engine(tmp_path, [], [])
    eng.plex = FlakyServer(
        "plex",
        [MediaItem("plex", "1", EP1, WatchState(False, 0)), MediaItem("plex", "2", EP2, WatchState(False, 0))],
        broken={"1"},
    )
    eng.emby = Server("emby", [MediaItem("emby", "a", EP1, played()), MediaItem("emby", "b", EP2, played())])
    run(eng._reconcile(pair))
    assert eng.plex.ops == [("played", "2")]
    assert eng.reconcile_stats[pair.id]["failed"] == 1
    assert eng.store.get(pair.id, EP2).played and eng.store.get(pair.id, EP1) is None


def test_failed_pull_is_not_marked_seen(tmp_path):
    eng, pair, *_ = make_engine(tmp_path, [], [])
    eng.plex = FlakyServer(
        "plex",
        [MediaItem("plex", "1", EP1, WatchState(False, 0)), MediaItem("plex", "2", EP2, WatchState(False, 0))],
        broken={"1"},
    )
    pair.plex_index.update({EP1: ["1"], EP2: ["2"]})
    run(eng._trakt_pull(pair, {EP1: NOW, EP2: NOW}, {}, {EP1: NOW, EP2: NOW}))
    assert eng.plex.ops == [("played", "2")]
    assert eng.store.trakt_seen(pair.id) == {str(EP2): NOW}  # EP1 下次重试


# ---------- 实时标记 ----------


def unwatched():
    return WatchState(False, 0, 0)


def test_mark_watched_adds_history_and_clears_progress(tmp_path):
    client = FakeClient(playback={MOVIE: Playback(7, 40.0, NOW)})
    sync, acc = make(client, tmp_path)
    seen = []
    sync.on_history = lambda user, items: seen.append((user, items))
    sync.observe_mark("me", MOVIE, progress(40)[0], played(NOW - 10), "Matrix")
    mark = acc.marks[MOVIE]
    assert mark.kind == "watched" and mark.due > time.monotonic() + 30  # 延迟推送
    run(sync._push_marks(acc, dict(acc.marks)))
    assert client.posts == [history_payload({MOVIE: NOW - 10})]
    assert client.removed == [7]
    assert seen == [("me", {MOVIE: NOW - 10})]
    assert acc.marks == {} and MOVIE in acc.scrobbled and acc.progress_pushed == 1


def test_mark_watched_without_time_uses_now(tmp_path):
    client = FakeClient()
    sync, acc = make(client, tmp_path)
    sync.observe_mark("me", EP1, unwatched(), WatchState(True, 0, 0))
    before = int(time.time())
    run(sync._push_marks(acc, dict(acc.marks)))
    ts = client.posts[0]["shows"][0]["seasons"][0]["episodes"][0]["watched_at"]
    assert ts != "unknown" and _parse_ts(ts) >= before


def test_mark_clear_progress(tmp_path):
    client = FakeClient(playback={EP1: Playback(9, 55.0, NOW)})
    sync, acc = make(client, tmp_path)
    acc.progress_sent[EP1] = (55.0, False)
    sync.observe_mark("me", EP1, progress(55)[0], unwatched())
    assert acc.marks[EP1].kind == "clear"
    run(sync._push_marks(acc, dict(acc.marks)))
    assert client.posts == [] and client.removed == [9]
    assert EP1 not in acc.progress_sent  # 之后再看到同样的进度仍会推送


def test_mark_ignored_cases(tmp_path):
    sync, acc = make(FakeClient(), tmp_path)
    sync.observe_mark("me", MOVIE, None, played())  # 首次对账前，没有之前的状态
    sync.observe_mark("me", MOVIE, played(), unwatched())  # 已看 -> 未看：不删除观看记录
    sync.observe_mark("me", MOVIE, WatchState(False, 5_000, NOW), unwatched())  # 进度太小
    sync.observe_mark("me", MOVIE, unwatched(), progress(30)[0])  # 只是进度变化
    sync.observe_mark(None, MOVIE, unwatched(), played())
    assert acc.marks == {}
    # 实时进度推送已用 stop 100% 记录这次观看
    acc.pending[EP1] = (100.0, "stop", "")
    sync.observe_mark("me", EP1, progress(40)[0], played())
    assert acc.marks == {}
    off, off_acc = make(FakeClient(), tmp_path, sync_marks=False)
    off.observe_mark("me", MOVIE, unwatched(), played())
    assert off_acc.marks == {}


def test_mark_cancelled_when_state_changes(tmp_path):
    sync, acc = make(FakeClient(), tmp_path)
    sync.observe_mark("me", MOVIE, unwatched(), played())
    sync.observe_mark("me", MOVIE, played(), played())  # 另一端的回传事件
    assert MOVIE in acc.marks
    sync.observe_mark("me", MOVIE, played(), unwatched())  # 误点后改回
    assert acc.marks == {}
    sync.observe_mark("me", EP1, progress(40)[0], unwatched())
    sync.observe_mark("me", EP1, unwatched(), progress(2)[0])  # 从头重看
    assert acc.marks == {}


class PlaybackFails(FakeClient):
    async def playback(self, username):
        if not self.playback_calls:
            self.playback_calls += 1
            raise RuntimeError("boom")
        return await super().playback(username)


def test_mark_retry_does_not_add_history_twice(tmp_path):
    client = PlaybackFails(playback={MOVIE: Playback(7, 40.0, NOW)})
    sync, acc = make(client, tmp_path)
    sync.observe_mark("me", MOVIE, progress(40)[0], played())
    assert not run(sync._guard(acc, sync._push_marks(acc, dict(acc.marks))))
    assert acc.marks[MOVIE].kind == "clear"  # 观看记录已写入，只剩清除进度
    run(sync._push_marks(acc, dict(acc.marks)))
    assert len(client.posts) == 1 and client.removed == [7] and acc.marks == {}


def test_mark_dry_run(tmp_path):
    client = FakeClient(playback={MOVIE: Playback(7, 40.0, NOW)})
    sync, acc = make(client, tmp_path, dry_run=True)
    sync.observe_mark("me", MOVIE, progress(40)[0], played())
    run(sync._push_marks(acc, dict(acc.marks)))
    assert client.posts == client.removed == [] and acc.marks == {}


def test_live_finish_records_seen(tmp_path):
    sync, acc = make(FakeClient(), tmp_path)
    seen = []
    sync.on_history = lambda user, items: seen.append(items)
    acc.progress_sent[EP1] = (40.0, False)
    sync.observe_live("me", EP1, played(), 0)
    run(sync._push_live(acc))
    assert list(seen[0]) == [EP1] and seen[0][EP1] > time.time()


def test_engine_mark_watched_and_seen(tmp_path):
    p_item = MediaItem("plex", "1", MOVIE, WatchState(False, 1_200_000, NOW - 100), duration_ms=H)
    e_item = MediaItem("emby", "a", MOVIE, WatchState(False, 1_200_000, NOW - 100), duration_ms=H)
    client = FakeClient()
    eng, pair, plex, emby, acc = make_engine(tmp_path, [p_item], [e_item], client=client)
    run(eng._reconcile(pair))
    assert acc.marks == {}
    p_item.state = WatchState(True, 0, NOW - 5)
    run(eng._handle_event("plex", pair, "1"))
    assert emby.ops == [("played", "a")]
    assert acc.marks[MOVIE].kind == "watched"
    run(sync_marks(acc, eng))
    assert client.posts == [history_payload({MOVIE: NOW - 5})]
    # 之后在本地改为未看，从 Trakt 全量同步时不会拉回已看
    assert eng.store.trakt_seen(pair.id) == {str(MOVIE): NOW - 5}


def sync_marks(acc, eng):
    return eng.trakt._push_marks(acc, dict(acc.marks))


def test_engine_pull_echo_is_not_marked(tmp_path):
    p_item = MediaItem("plex", "1", EP1, WatchState(False, 0))
    e_item = MediaItem("emby", "a", EP1, WatchState(False, 0))
    eng, pair, plex, emby, acc = make_engine(tmp_path, [p_item], [e_item])
    run(eng._reconcile(pair))
    run(eng._trakt_pull(pair, {EP1: 1_700_000_000}, {}))
    # 拉回写入后，超过防回环窗口才轮询到的回传事件
    eng._echo.clear()
    e_item.state = played(1_700_000_000)
    run(eng._handle_event("emby", pair, "a"))
    assert acc.marks == {}


# ---------- 标记未看：删除观看记录 ----------


def plays(key, *ids):
    entry = {"type": "movie", "movie": {"ids": {"tmdb": int(key.tmdb)}}} if key.kind == "movie" else {
        "type": "episode",
        "show": {"ids": {"tmdb": int(key.tmdb)}},
        "episode": {"season": key.season, "number": key.episode},
    }
    return [{**entry, "id": i, "watched_at": "2024-01-01T00:00:00.000Z"} for i in ids]


def due_now(acc):
    return {k: m for k, m in acc.marks.items()}


def test_unwatch_removes_history_after_backup(tmp_path):
    client = FakeClient(history={MOVIE: plays(MOVIE, 11, 12)}, playback={MOVIE: Playback(7, 5.0, NOW)})
    sync, acc = make(client, tmp_path, sync_unwatch=True)
    acc.scrobbled.add(MOVIE)
    sync.observe_mark("me", MOVIE, played(), unwatched(), "Matrix")
    assert acc.marks[MOVIE].kind == "unwatch"
    run(sync._push_marks(acc, due_now(acc)))
    assert client.removed_history == [11, 12] and client.removed == [7]
    assert MOVIE not in acc.scrobbled  # 之后再看完仍会记录
    lines = (tmp_path / "data" / "trakt_removed.jsonl").read_text(encoding="utf-8").splitlines()
    record = json.loads(lines[0])
    assert record["key"] == str(MOVIE) and record["title"] == "Matrix" and [e["id"] for e in record["history"]] == [11, 12]


def test_unwatch_disabled_or_readded(tmp_path):
    sync, acc = make(FakeClient(), tmp_path)  # 默认关闭
    sync.observe_mark("me", MOVIE, played(), unwatched())
    assert acc.marks == {}
    sync, acc = make(FakeClient(), tmp_path, sync_unwatch=True)
    sync.observe_mark("me", MOVIE, played(), unwatched(), readded=True)
    assert acc.marks == {}
    sync.observe_mark("me", MOVIE, played(), progress(30)[0])  # 重看到一半，不是标记未看
    assert acc.marks == {}
    # 只开删除观看记录时，标记已看不推送
    sync, acc = make(FakeClient(), tmp_path, sync_marks=False, sync_unwatch=True)
    sync.observe_mark("me", MOVIE, unwatched(), played())
    assert acc.marks == {}


def test_unwatch_backup_failure_removes_nothing(tmp_path):
    client = FakeClient(history={MOVIE: plays(MOVIE, 11)})
    sync, acc = make(client, tmp_path, sync_unwatch=True)
    sync.removed_log = tmp_path  # 是目录，写入失败
    sync.observe_mark("me", MOVIE, played(), unwatched())
    assert not run(sync._guard(acc, sync._push_marks(acc, due_now(acc))))
    assert client.removed_history == [] and acc.marks[MOVIE].kind == "unwatch"


def test_unwatch_unknown_item_only_clears_progress(tmp_path):
    client = FakeClient(playback={MOVIE: Playback(7, 5.0, NOW)})
    sync, acc = make(client, tmp_path, sync_unwatch=True)
    sync.observe_mark("me", MOVIE, played(), unwatched())
    run(sync._push_marks(acc, due_now(acc)))
    assert client.removed_history == [] and client.removed == [7] and acc.marks == {}
    assert not (tmp_path / "data" / "trakt_removed.jsonl").exists()


def test_unwatch_retry_after_removal_only_clears_progress(tmp_path):
    client = PlaybackFails(history={MOVIE: plays(MOVIE, 11)}, playback={MOVIE: Playback(7, 5.0, NOW)})
    sync, acc = make(client, tmp_path, sync_unwatch=True)
    sync.observe_mark("me", MOVIE, played(), unwatched())
    assert not run(sync._guard(acc, sync._push_marks(acc, due_now(acc))))
    assert acc.marks[MOVIE].kind == "clear"
    run(sync._push_marks(acc, due_now(acc)))
    assert client.removed_history == [11] and client.removed == [7]


def test_unwatch_bulk_is_held_until_confirmed(tmp_path):
    movies = [MediaKey("movie", str(i)) for i in (1, 2, 3)]
    client = FakeClient(history={k: plays(k, int(k.tmdb)) for k in movies})
    sync, acc = make(client, tmp_path, sync_unwatch=True, unwatch_max=2)
    for k in movies:
        sync.observe_mark("me", k, played(), unwatched(), f"M{k.tmdb}")
    run(sync._push_marks(acc, due_now(acc)))
    assert client.removed_history == [] and set(acc.held) == set(movies)
    assert client.playback_calls == 0
    assert sync.status()[0]["held"] == ["M1", "M2", "M3"]
    acc.held[EP1] = Mark("unwatch", unwatched(), "Pilot", 0)
    assert "Pilot S01E01" in sync.status()[0]["held"]
    del acc.held[EP1]
    # 暂停期间又标记的也一并暂停；改回已看的取消
    extra = MediaKey("movie", "4")
    sync.observe_mark("me", extra, played(), unwatched())
    run(sync._push_marks(acc, due_now(acc)))
    sync.observe_mark("me", extra, unwatched(), played())
    assert extra not in acc.held
    assert sync.release_held("me", confirm=True) == 3
    assert acc.held == {} and all(acc.marks[k].confirmed and acc.marks[k].due == 0 for k in movies)
    assert acc.marks[extra].kind == "watched"  # 改回已看按标记已看处理
    run(sync._push_marks(acc, due_now(acc)))
    assert sorted(client.removed_history) == [1, 2, 3]
    # 确认执行的不计入数量上限
    assert acc.unwatch_recent == {}


def test_unwatch_discard_held(tmp_path):
    client = FakeClient(history={MOVIE: plays(MOVIE, 11)})
    sync, acc = make(client, tmp_path, sync_unwatch=True, unwatch_max=1)
    acc.unwatch_recent["movie:1"] = time.monotonic()  # 1 小时内已删除过一部
    sync.observe_mark("me", MOVIE, played(), unwatched())
    run(sync._push_marks(acc, due_now(acc)))
    assert MOVIE in acc.held
    assert sync.release_held("me", confirm=False) == 1
    assert acc.held == {} and acc.marks == {} and client.removed_history == []


def test_unwatch_limit_counts_shows_not_episodes(tmp_path):
    eps = [EP1, EP2, EP3, EP4]
    client = FakeClient(history={k: plays(k, k.episode) for k in eps})
    sync, acc = make(client, tmp_path, sync_unwatch=True, unwatch_max=1)
    for k in eps:  # 标记整季未看
        sync.observe_mark("me", k, played(), unwatched())
    run(sync._push_marks(acc, due_now(acc)))
    assert acc.held == {} and sorted(client.removed_history) == [1, 2, 3, 4]


def test_unwatch_dry_run(tmp_path):
    client = FakeClient(history={MOVIE: plays(MOVIE, 11)})
    sync, acc = make(client, tmp_path, dry_run=True, sync_unwatch=True)
    sync.observe_mark("me", MOVIE, played(), unwatched())
    run(sync._push_marks(acc, due_now(acc)))
    assert client.removed_history == [] and acc.marks == {}


def test_client_item_history_filters_episode(tmp_path):
    client = TraktClient(None, TraktConfig("id"), TraktTokenStore(tmp_path / "t.json"))
    calls = []

    async def trakt_id(kind, tmdb):
        calls.append((kind, tmdb))
        return 42

    async def pages(username, path):
        calls.append(path)
        return plays(EP1, 1) + plays(EP2, 2)

    client.trakt_id, client._user_pages = trakt_id, pages
    assert [e["id"] for e in run(client.item_history("me", EP1))] == [1]
    assert calls == [("show", "1399"), "/sync/history/shows/42"]


def test_engine_readded_detection(tmp_path):
    item = MediaItem("plex", "1", MOVIE, WatchState(False, 0), added_at=NOW - 86_400)
    eng, pair, *_ = make_engine(tmp_path, [item], [])
    pair.unified[MOVIE] = (played(NOW - 3_600), "", 0)
    assert not eng._readded(pair, MOVIE, item)  # 入库后才看的
    assert eng._readded(pair, MOVIE, replace(item, added_at=NOW))  # 看过之后重新入库
    assert eng._readded(pair, MOVIE, replace(item, added_at=0))  # 入库时间未知
    # 基准在入库之后写入（如 Emby 晚于 Plex 加入、按原观看时间同步为已看），以基准时间为准
    eng.store.set(pair.id, MOVIE, played(NOW - 10 * 86_400))
    assert not eng._readded(pair, MOVIE, replace(item, added_at=NOW - 3 * 86_400))


def test_engine_unwatch_event(tmp_path):
    p_item = MediaItem("plex", "1", MOVIE, played(NOW - 3_600), added_at=NOW - 86_400)
    e_item = MediaItem("emby", "a", MOVIE, played(NOW - 3_600))
    eng, pair, plex, emby, acc = make_engine(tmp_path, [p_item], [e_item])
    eng.trakt.cfg.sync_unwatch = True
    run(eng._reconcile(pair))
    p_item.state = unwatched()
    run(eng._handle_event("plex", pair, "1"))
    assert emby.ops == [("unplayed", "a")]
    assert acc.marks[MOVIE].kind == "unwatch"


def test_config_unwatch_defaults_and_validation():
    base = {"plex": {"url": "http://p", "token": "t"}, "emby": {"url": "http://e", "api_key": "k"}, "mappings": [{"emby_user": "u"}]}
    cfg = parse_config(base)
    assert cfg.trakt.sync_marks and not cfg.trakt.sync_unwatch and cfg.trakt.unwatch_max == 10
    with pytest.raises(ValueError):
        parse_config({**base, "trakt": {"unwatch_max": 0}})


def test_config_single_server():
    plex_only = {"plex": BASE["plex"], "trakt": {"client_id": "a"}}
    cfg = parse_config({**plex_only, "mappings": [{"emby_user": "u", "trakt_user": "me"}]})
    assert cfg.emby is None and cfg.servers == ("plex",)
    assert cfg.mappings[0].emby_user == ""  # 未配置的一端的用户被清空
    # 只有一个服务器时 Trakt 是唯一的同步对象
    with pytest.raises(ValueError, match="都需要选择 Trakt 账户"):
        parse_config({**plex_only, "mappings": [{}]})
    with pytest.raises(ValueError, match="用户映射重复：Plex\\[所有者\\]$"):
        parse_config({**plex_only, "mappings": [{"trakt_user": "me"}, {"trakt_user": "you"}]})
    emby_only = {"plex": None, "emby": BASE["emby"], "trakt": {"client_id": "a"}}
    cfg = parse_config({**emby_only, "mappings": [{"plex_user": "kid", "emby_user": "u", "trakt_user": "me"}]})
    assert cfg.plex is None and cfg.servers == ("emby",)
    assert cfg.mappings[0].plex_user is None  # 不会要求填写 Plex 用户 Token
    with pytest.raises(ValueError, match="至少需要配置"):
        parse_config({"mappings": [{"trakt_user": "me"}], "trakt": {"client_id": "a"}})
