from mediaresume.engine import decide
from mediaresume.models import WatchState, parse_tmdb


def ws(played=False, pos=0, last=0):
    return WatchState(played, pos, last)


def test_consistent_returns_none():
    assert decide(ws(True), ws(True), None) is None
    assert decide(ws(pos=60_000), ws(pos=65_000), None) is None  # 容差内


def test_only_plex_changed():
    base = ws()
    assert decide(ws(True), base, base).played is True


def test_only_emby_changed_unwatch():
    base = ws(True)
    t = decide(base, ws(False, 0), base)
    assert t.played is False


def test_both_changed_newer_wins():
    base = ws(pos=0)
    t = decide(ws(pos=600_000, last=100), ws(pos=1_200_000, last=200), base)
    assert t.position_ms == 1_200_000


def test_first_seen_played_wins():
    t = decide(ws(pos=300_000, last=999), ws(True, last=1), None)
    assert t.played is True


def test_parse_tmdb():
    assert parse_tmdb("tmdb://1399") == "1399"
    assert parse_tmdb("com.plexapp.agents.themoviedb://603?lang=en") == "603"
    assert parse_tmdb("imdb://tt0133093") is None


def test_first_seen_keeps_larger_position():
    # Plex 播放时间更新但进度为 0，不应清掉 Emby 的进度
    t = decide(ws(pos=0, last=200), ws(pos=900_000, last=100), None)
    assert t.position_ms == 900_000
