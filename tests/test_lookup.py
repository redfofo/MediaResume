import asyncio

from mediaresume.config import Config, EmbyConfig, Mapping, PlexConfig
from mediaresume.engine import Pair, SyncEngine
from mediaresume.models import MediaKey


class FakePlex:
    def __init__(self, result):
        self.result = result
        self.calls = 0

    async def find(self, key, token=None):
        self.calls += 1
        return self.result


def make_engine(plex):
    cfg = Config(PlexConfig("http://p", "t"), EmbyConfig("http://e", "k"), [Mapping(None, "u")])
    return SyncEngine(cfg, plex, None, None), Pair(Mapping(None, "u"), "t", "u")


KEY = MediaKey("episode", "1399", 1, 1)


def test_lookup_finds_new_item_and_caches_in_index():
    plex = FakePlex(["42"])
    eng, pair = make_engine(plex)
    assert asyncio.run(eng._lookup("plex", pair, KEY)) == ["42"]
    assert asyncio.run(eng._lookup("plex", pair, KEY)) == ["42"]
    assert plex.calls == 1
    assert pair.plex_index[KEY] == ["42"]


def test_lookup_missing_is_negative_cached():
    plex = FakePlex([])
    eng, pair = make_engine(plex)
    for _ in range(5):
        assert asyncio.run(eng._lookup("plex", pair, KEY)) == []
    assert plex.calls == 1
