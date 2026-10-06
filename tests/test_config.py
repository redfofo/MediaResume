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
