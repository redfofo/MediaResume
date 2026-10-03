import asyncio
import time

import pytest

from mediaresume.config import Config, EmbyConfig, Mapping, PlexConfig, SyncConfig, TraktConfig, parse_config
from mediaresume.engine import Pair, SyncEngine
from mediaresume.models import MediaItem, MediaKey, WatchState
from mediaresume.store import StateStore
from mediaresume.trakt import (
    Playback,
    PushPlan,
    TraktAuthError,
    TraktSync,
    history_payload,
    not_found_tmdb,
    parse_playback,
    parse_watched,
    scrobble_body,
)

MOVIE = MediaKey("movie", "603")
EP1 = MediaKey("episode", "1399", 1, 1)
EP2 = MediaKey("episode", "1399", 1, 2)
EP3 = MediaKey("episode", "1399", 1, 3)
EP4 = MediaKey("episode", "1399", 1, 4)
NOW = int(time.time())


class FakeClient:
    def __init__(self, watched=(), not_found=None, fail=None, playback=None):
        self.remote = {k: NOW for k in watched}
        self.remote_playback = dict(playback or {})
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

    async def scrobble(self, username, action, key, progress):
        if self.fail:
            raise self.fail
        self.scrobbles.append((action, key, progress))
        self.paused += "+"


def make(client, tmp_path, dry_run=False, **cfg):
    sync = TraktSync(client, TraktConfig("id", "secret", **cfg), dry_run)
    sync.add_account("me")
    return sync, sync.accounts["me"]


def played(ts=1_700_000_000):
    return WatchState(True, 0, ts)


def run(coro):
    return asyncio.run(coro)


# ---------- 数据转换 ----------


def test_parse_watched():
    movies = [{"movie": {"ids": {"tmdb": 603}}, "last_watched_at": "2023-11-14T22:13:20.000Z"}, {"movie": {"ids": {}}}]
    shows = [{"show": {"ids": {"tmdb": 1399}}, "seasons": [{"number": 1, "episodes": [{"number": 1}, {"number": 2}]}]}]
    assert parse_watched(movies, shows) == {MOVIE: 1_700_000_000, EP1: 0, EP2: 0}


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

    async def set_position(self, *args):
        item_id, pos = (args[0], args[1]) if self.name == "plex" else (args[1], args[2])
        self.ops.append(("position", item_id, pos))


def make_engine(tmp_path, plex_items, emby_items, dry_run=False, client=None):
    plex, emby = Server("plex", plex_items), Server("emby", emby_items)
    mapping = Mapping(None, "u", trakt_user="me")
    cfg = Config(PlexConfig("http://p", "t"), EmbyConfig("http://e", "k"), [mapping], SyncConfig(dry_run=dry_run))
    sync = TraktSync(client or FakeClient(), TraktConfig("id", "s"), dry_run)
    sync.add_account("me")
    eng = SyncEngine(cfg, plex, emby, StateStore(str(tmp_path / "e.db")), sync)
    pair = Pair(mapping, "t", "u")
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
    show, (watched, prog) = run(eng.trakt_plan(pair, "from_trakt"))
    assert watched == {EP1: NOW}
    assert prog == {EP2: (50, NOW)}
    assert show["skipped_newer"] == 1


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
    eng._plex_playing["1"] = time.monotonic()
    run(eng._handle_event("emby", pair, "a"))
    assert acc.pending == {}
    del eng._plex_playing["1"]
    run(eng._handle_event("emby", pair, "a"))
    assert acc.pending[EP1][1] == "stop"


# ---------- 客户端 / 配置 ----------


def test_client_refreshes_expired_token(tmp_path, monkeypatch):
    import aiohttp
    from aiohttp import web
    from aiohttp.test_utils import TestServer

    from mediaresume import trakt
    from mediaresume.trakt import TraktClient, TraktTokenStore

    seen = []

    async def token(request):
        body = await request.json()
        assert body["grant_type"] == "refresh_token" and body["refresh_token"] == "r1"
        return web.json_response({"access_token": "a2", "refresh_token": "r2", "expires_in": 86400, "created_at": int(time.time())})

    async def watched(request):
        seen.append(request.headers["Authorization"])
        if request.match_info["type"] == "movies":
            return web.json_response([{"movie": {"ids": {"tmdb": 603}}}])
        return web.json_response([])

    async def main():
        app = web.Application()
        app.router.add_post("/oauth/token", token)
        app.router.add_get("/sync/watched/{type}", watched)
        async with TestServer(app) as server:
            monkeypatch.setattr(trakt, "API", str(server.make_url("")).rstrip("/"))
            store = TraktTokenStore(tmp_path / "tokens.json")
            store.set("me", {"access_token": "a1", "refresh_token": "r1", "expires_at": int(time.time()) + 60, "client_id": "cid"})
            async with aiohttp.ClientSession() as http:
                client = TraktClient(http, TraktConfig("cid", "secret"), store)
                assert await client.watched("me") == {MOVIE: 0}
            return store

    store = run(main())
    assert seen == ["Bearer a2", "Bearer a2"]
    assert store.get("me")["refresh_token"] == "r2"
    assert (tmp_path / "tokens.json").stat().st_mode & 0o777 == 0o600


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
    cfg = parse_config({
        **BASE,
        "mappings": [{"emby_user": "u", "trakt_user": "me"}],
        "trakt": {"client_id": " a ", "client_secret": "b", "scrobble": False, "pull_progress": True},
    })
    assert cfg.mappings[0].trakt_user == "me"
    assert cfg.trakt.client_id == "a" and not cfg.trakt.scrobble
    assert parse_config({**BASE, "mappings": [{"emby_user": "u"}]}).mappings[0].trakt_user is None

