import asyncio

from mediaresume.emby import EmbyClient


class Recorder(EmbyClient):
    def __init__(self):
        super().__init__(None, "http://e", "k")
        self.posted = None

    async def _request(self, method, path, params=None, body=None):
        if method == "GET":
            return {"UserData": {"LastPlayedDate": "2020-01-01T00:00:00.0000000Z", "IsFavorite": True}}
        self.posted = body


def test_clearing_position_keeps_last_played_date():
    c = Recorder()
    asyncio.run(c.set_position("u", "i", 0))
    assert c.posted["LastPlayedDate"] == "2020-01-01T00:00:00.0000000Z"
    assert c.posted["PlaybackPositionTicks"] == 0 and c.posted["IsFavorite"]


def test_setting_position_writes_last_played_date():
    c = Recorder()
    asyncio.run(c.set_position("u", "i", 60_000, 1_700_000_000))
    assert c.posted["LastPlayedDate"] == "2023-11-14T22:13:20.0000000Z"
