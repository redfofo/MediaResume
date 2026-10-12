import asyncio

from mediaresume.plex import PlexClient


class Recorder(PlexClient):
    def __init__(self):
        super().__init__(None, "http://p", "t")
        self.calls = []

    async def _get(self, path, params=None, token=None):
        self.calls.append((path, params.get("time")))
        return {}


def test_clearing_position_unscrobbles():
    # Plex 忽略 time=0 的 /:/progress，原进度会保留
    c = Recorder()
    asyncio.run(c.set_position("1", 0))
    assert c.calls == [("/:/unscrobble", None)]


def test_setting_position_reports_progress():
    c = Recorder()
    asyncio.run(c.set_position("1", 60_000))
    assert c.calls == [("/:/progress", 60_000)]
