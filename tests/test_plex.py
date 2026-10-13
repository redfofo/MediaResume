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


import aiohttp
import pytest

from mediaresume import plex as plex_mod


def _error(status):
    return aiohttp.ClientResponseError(None, (), status=status, message="err")


class Library(PlexClient):
    """模拟分页的资料库：bad 中的位置会让包含它的页返回 502；drop 中的位置不会出现在结果里（Plex 的短页）"""

    def __init__(self, n, bad=(), drop=(), root_status=None, page_status=None):
        super().__init__(None, "http://p", "t")
        self.n, self.bad, self.drop = n, set(bad), set(drop)
        self.root_status, self.page_status = root_status, page_status
        self.pages = []

    async def _get(self, path, params=None, token=None):
        if path == "/":
            if self.root_status:
                raise _error(self.root_status)
            return {}
        start, size = params["X-Plex-Container-Start"], params["X-Plex-Container-Size"]
        self.pages.append((start, size))
        if self.page_status:
            raise _error(self.page_status)
        rng = range(start, min(start + size, self.n))
        if self.bad & set(rng):
            raise _error(502)
        metas = [{"ratingKey": i} for i in rng if i not in self.drop]
        return {"MediaContainer": {"totalSize": self.n, "Metadata": metas}}


def _keys(c):
    return [m["ratingKey"] for m in asyncio.run(c._list_section("1", 2, None, {}))]


def test_list_section_skips_bad_item(monkeypatch):
    monkeypatch.setattr(plex_mod, "PAGE_SIZE", 8)
    c = Library(20, bad={11})
    assert _keys(c) == [i for i in range(20) if i != 11]


def test_list_section_short_page_does_not_repeat(monkeypatch):
    monkeypatch.setattr(plex_mod, "PAGE_SIZE", 8)
    c = Library(20, drop={3})
    assert _keys(c) == [i for i in range(20) if i != 3]


def test_list_section_server_down_raises_without_splitting(monkeypatch):
    monkeypatch.setattr(plex_mod, "PAGE_SIZE", 8)
    c = Library(20, page_status=502, root_status=502)
    with pytest.raises(aiohttp.ClientResponseError):
        _keys(c)
    assert c.pages == [(0, 8)]


def test_list_section_client_error_not_retried(monkeypatch):
    monkeypatch.setattr(plex_mod, "PAGE_SIZE", 8)
    c = Library(20, page_status=401)
    with pytest.raises(aiohttp.ClientResponseError):
        _keys(c)
    assert c.pages == [(0, 8)]


def test_list_section_gives_up_after_too_many_bad_items(monkeypatch):
    monkeypatch.setattr(plex_mod, "PAGE_SIZE", 8)
    monkeypatch.setattr(plex_mod, "MAX_SKIPPED_ITEMS", 2)
    c = Library(20, bad={1, 2, 3})
    with pytest.raises(aiohttp.ClientResponseError):
        _keys(c)
