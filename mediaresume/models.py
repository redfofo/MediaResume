from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Optional

# 进度比较容差（毫秒），差值小于此视为相同，避免来回抖动
POSITION_TOLERANCE_MS = 15_000


@dataclass(frozen=True)
class MediaKey:
    """跨服务器的媒体唯一标识，以 TMDB 为准。

    电影: kind="movie", tmdb=电影 tmdb id
    剧集: kind="episode", tmdb=剧集(Show) tmdb id, season/episode=季号/集号
    """

    kind: str
    tmdb: str
    season: Optional[int] = None
    episode: Optional[int] = None

    def __str__(self) -> str:
        if self.kind == "episode":
            return f"episode:tmdb:{self.tmdb}:s{self.season}e{self.episode}"
        return f"movie:tmdb:{self.tmdb}"


@dataclass
class WatchState:
    played: bool
    position_ms: int = 0
    # 最后播放时间（unix 秒），用于双方都改变时的冲突裁决
    last_played: int = 0

    def same_as(self, other: Optional["WatchState"]) -> bool:
        if other is None:
            return False
        if self.played != other.played:
            return False
        # 已看状态下忽略进度
        if self.played:
            return True
        return abs(self.position_ms - other.position_ms) < POSITION_TOLERANCE_MS


@dataclass
class MediaItem:
    """某个服务器上的一个具体条目"""

    server: str  # "plex" | "emby"
    item_id: str
    key: MediaKey
    state: WatchState
    title: str = ""
    # 一个文件包含多集时（如 S01E01-E02），除 key 外对应的其他集
    alt_keys: tuple[MediaKey, ...] = ()


def merge_states(items: list[MediaItem]) -> WatchState:
    """同一服务器上同一媒体可能有多个版本，取“最靠前”的状态"""
    played = any(i.state.played for i in items)
    position = max(i.state.position_ms for i in items)
    last = max(i.state.last_played for i in items)
    return WatchState(played=played, position_ms=0 if played else position, last_played=last)


def parse_tmdb(value: str) -> Optional[str]:
    """解析 Plex guid（新/旧 agent）中的 tmdb id"""
    if value.startswith("tmdb://"):
        return value[len("tmdb://"):]
    if value.startswith("com.plexapp.agents.themoviedb://"):
        return value.split("://", 1)[1].split("?", 1)[0].split("/", 1)[0]
    return None


_TMDB_TAG = re.compile(r"\{tmdb-(\d+)\}")


def tmdb_from_path(path: Optional[str]) -> Optional[str]:
    """从路径中的 {tmdb-12345} 取 tmdb id；取最外层（文件夹）的标记"""
    if not path:
        return None
    m = _TMDB_TAG.search(path)
    return m.group(1) if m else None
