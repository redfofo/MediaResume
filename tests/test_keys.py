from mediaresume.emby import EmbyClient
from mediaresume.models import MediaKey, tmdb_from_path
from mediaresume.plex import PlexClient


def test_tmdb_from_path():
    assert tmdb_from_path("/data/anime/青春 (2013) {tmdb-65676}/Season 2/a.strm") == "65676"
    assert tmdb_from_path("/data/movie/回魂之夜.(2023)/A.strm") is None
    assert tmdb_from_path(None) is None


def test_plex_prefers_path_tag_over_metadata():
    p = PlexClient(None, "", "")
    movie = {
        "type": "movie", "ratingKey": "1", "Guid": [{"id": "tmdb://26404"}],
        "Media": [{"Part": [{"file": "/data/tv/爱情公寓 (2016) {tmdb-99768}/a.strm"}]}],
    }
    assert p._to_item(movie).key == MediaKey("movie", "99768")
    # 剧没有 tmdb（local:// 未匹配），单集路径有标记
    ep = {
        "type": "episode", "ratingKey": "2", "parentIndex": 2, "index": 13,
        "Media": [{"Part": [{"file": "/data/tv/法证先锋 (2006) {tmdb-254090}/Season 2/x.strm"}]}],
    }
    assert p._to_item(ep, None).key == MediaKey("episode", "254090", 2, 13)


def test_emby_multi_episode_and_path_tag():
    e = EmbyClient(None, "", "")
    raw = {
        "Type": "Episode", "Id": "9", "SeriesId": "orphan", "ParentIndexNumber": 1, "IndexNumber": 1,
        "IndexNumberEnd": 2, "Path": "/115/strm/tv/光环 (2022) {tmdb-52814}/Season 1/光环.S01E01-E02.strm",
    }
    item = e._to_item(raw)
    assert item.key == MediaKey("episode", "52814", 1, 1)
    assert item.alt_keys == (MediaKey("episode", "52814", 1, 2),)


def test_emby_falls_back_to_series_metadata():
    e = EmbyClient(None, "", "")
    e._series_tmdb["s1"] = "45790"
    raw = {"Type": "Episode", "Id": "1", "SeriesId": "s1", "ParentIndexNumber": 5, "IndexNumber": 13, "Path": "/x/a.strm"}
    assert e._to_item(raw).key == MediaKey("episode", "45790", 5, 13)
