import asyncio

import pytest

from mediaresume.config import parse_config
from mediaresume.web import Runner


def base(**extra):
    return {
        "plex": {"url": "http://p", "token": "t"},
        "emby": {"url": "http://e", "api_key": "k"},
        "mappings": [{"emby_user": "u"}],
        **extra,
    }


def test_defaults_are_valid():
    cfg = parse_config(base(sync={"dry_run": None}, log_level="info"))
    assert cfg.sync.poll_interval == 30 and cfg.log_level == "INFO" and cfg.sync.dry_run is False


@pytest.mark.parametrize(
    "extra, message",
    [
        ({"sync": {"reconcile_interval": 0}}, "sync.reconcile_interval 不能小于 60"),
        ({"sync": {"poll_interval": "abc"}}, "sync.poll_interval 必须是整数"),
        ({"sync": {"poll_interval": True}}, "sync.poll_interval 必须是整数"),
        ({"sync": {"dry_run": "yes"}}, "sync.dry_run 必须是 true 或 false"),
        ({"log_level": "LOUD"}, "log_level 必须是"),
        ({"trakt": {"push_interval": -1}}, "不能小于 0"),
    ],
)
def test_rejects_invalid_values(extra, message):
    with pytest.raises(ValueError, match=message):
        parse_config(base(**extra))


def test_string_numbers_are_accepted():
    cfg = parse_config(base(sync={"poll_interval": "10", "dry_run": "true"}))
    assert cfg.sync.poll_interval == 10 and cfg.sync.dry_run is True


def test_runner_init_failure_does_not_raise(tmp_path):
    cfg = tmp_path / "config.yaml"
    blocker = tmp_path / "blocker"
    blocker.write_text("")
    # db_path 的父目录是一个文件，StateStore 创建目录会失败
    cfg.write_text(
        "plex: {url: 'http://p', token: t}\nemby: {url: 'http://e', api_key: k}\n"
        f"mappings: [{{emby_user: u}}]\ndb_path: {blocker}/state.db\n",
        encoding="utf-8",
    )
    runner = Runner(None, cfg)
    asyncio.run(runner.start())
    assert runner.error and "初始化失败" in runner.error
    assert runner.task is None and runner.store is None


def test_concurrent_restarts_leave_one_engine(tmp_path, monkeypatch):
    started = []

    async def fake_start(self):
        await asyncio.sleep(0.01)  # 模拟初始化中的 await
        started.append(asyncio.create_task(asyncio.sleep(3600)))
        self.task = started[-1]

    monkeypatch.setattr(Runner, "_start", fake_start)

    async def main():
        runner = Runner(None, tmp_path / "config.yaml")
        await asyncio.gather(runner.restart(), runner.restart(), runner.restart())
        alive = [t for t in started if not t.done()]
        await runner.stop()
        return alive

    assert len(asyncio.run(main())) == 1


def test_saved_config_is_private(tmp_path):
    import os

    from mediaresume.config import save_config

    path = tmp_path / "config.yaml"
    save_config(parse_config(base()), path)
    assert os.stat(path).st_mode & 0o777 == 0o600


def make_app(tmp_path, monkeypatch, **env):
    from mediaresume.web import LogBuffer, create_app

    for k, v in env.items():
        monkeypatch.setenv(k, v)
    runner = Runner(None, tmp_path / "config.yaml")
    return create_app(runner, LogBuffer(), tmp_path / "no-dist")


def test_write_api_requires_json(tmp_path, monkeypatch):
    from aiohttp.test_utils import TestClient, TestServer

    async def main():
        async with TestClient(TestServer(make_app(tmp_path, monkeypatch))) as client:
            forged = await client.post("/api/reconcile", data="{}", headers={"Content-Type": "text/plain"})
            ok = await client.post("/api/reconcile", json={})
            read = await client.get("/api/status")
            return forged.status, ok.status, read.status

    # 引擎未运行时对账返回 409：说明请求已通过中间件
    assert asyncio.run(main()) == (415, 409, 200)


def test_allowed_hosts(tmp_path, monkeypatch):
    from aiohttp.test_utils import TestClient, TestServer

    async def main():
        app = make_app(tmp_path, monkeypatch, MEDIARESUME_ALLOWED_HOSTS="127.0.0.1, NAS.lan")
        async with TestClient(TestServer(app, host="127.0.0.1")) as client:
            ok = await client.get("/api/status")
            evil = await client.get("/api/status", headers={"Host": "evil.example:8095"})
            lan = await client.get("/api/status", headers={"Host": "nas.lan:8095"})
            return ok.status, evil.status, lan.status

    assert asyncio.run(main()) == (200, 421, 200)


def test_shared_plex_user_requires_own_token():
    with pytest.raises(ValueError, match="需要填写该用户自己的 Token"):
        parse_config(base(mappings=[{"plex_user": "kid", "emby_user": "k"}]))
    cfg = parse_config(base(mappings=[{"plex_user": " kid ", "plex_token": " tk ", "emby_user": "k"}]))
    assert (cfg.mappings[0].plex_user, cfg.mappings[0].plex_token) == ("kid", "tk")


def test_duplicate_mappings_rejected():
    with pytest.raises(ValueError, match="用户映射重复"):
        parse_config(base(mappings=[{"emby_user": "U"}, {"emby_user": "u"}]))
    parse_config(base(mappings=[{"emby_user": "u"}, {"plex_user": "kid", "plex_token": "t", "emby_user": "u"}]))


def test_engine_crash_reason_is_unwrapped():
    from mediaresume.web import _describe

    eg = ExceptionGroup("unhandled errors in a TaskGroup", [ValueError("Emby 401")])
    assert _describe(eg) == "ValueError: Emby 401"
    assert _describe(ExceptionGroup("x", [KeyError("a"), RuntimeError()])) == "KeyError: 'a'; RuntimeError"


def test_logs_tolerates_bad_cursor(tmp_path, monkeypatch):
    from aiohttp.test_utils import TestClient, TestServer

    async def main():
        async with TestClient(TestServer(make_app(tmp_path, monkeypatch))) as client:
            return (await client.get("/api/logs?after=abc")).status

    assert asyncio.run(main()) == 200


def test_invalid_config_is_still_prefilled(tmp_path, monkeypatch):
    from aiohttp.test_utils import TestClient, TestServer

    (tmp_path / "config.yaml").write_text(
        "plex: {url: 'http://p', token: secret}\nemby: {url: 'http://e', api_key: k}\n"
        "mappings: [{plex_user: kid, emby_user: u}]\nsync: {poll_interval: 10}\n",
        encoding="utf-8",
    )

    async def main():
        async with TestClient(TestServer(make_app(tmp_path, monkeypatch))) as client:
            return await (await client.get("/api/config")).json()

    res = asyncio.run(main())
    assert "需要填写该用户自己的 Token" in res["error"]
    cfg = res["config"]
    assert cfg["plex"]["token"] == "secret" and cfg["sync"]["poll_interval"] == 10
    assert cfg["sync"]["reconcile_interval"] == 900  # 缺的项用默认值
    assert cfg["mappings"] == [{"plex_user": "kid", "emby_user": "u", "plex_token": None, "trakt_user": None}]


def test_sigterm_exits_gracefully(tmp_path):
    import signal
    import socket
    import subprocess
    import sys
    import time

    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    proc = subprocess.Popen(
        [sys.executable, "-m", "mediaresume", "-c", str(tmp_path / "config.yaml"), "--host", "127.0.0.1", "-p", str(port)],
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
    )
    try:
        deadline = time.time() + 10
        while time.time() < deadline:
            try:
                socket.create_connection(("127.0.0.1", port), timeout=0.2).close()
                break
            except OSError:
                time.sleep(0.1)
        proc.send_signal(signal.SIGTERM)
        out, _ = proc.communicate(timeout=5)
    finally:
        proc.kill()
    assert proc.returncode == 0 and "收到停止信号" in out
