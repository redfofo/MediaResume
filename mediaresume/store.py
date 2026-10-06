from __future__ import annotations

import sqlite3
import time
from pathlib import Path
from typing import Optional

from .models import MediaKey, WatchState


class StateStore:
    """记录每个用户对、每个媒体最后一次同步后的状态（三方比对的基准）"""

    def __init__(self, path: str):
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(path)
        # WAL + NORMAL：每次提交不必等待完整 fsync，在 NAS 机械盘上写入快很多，断电最多丢最后几次提交
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA synchronous=NORMAL")
        self.conn.execute(
            """
            CREATE TABLE IF NOT EXISTS sync_state (
                pair TEXT NOT NULL,
                media_key TEXT NOT NULL,
                played INTEGER NOT NULL,
                position_ms INTEGER NOT NULL,
                last_played INTEGER NOT NULL,
                updated_at INTEGER NOT NULL,
                PRIMARY KEY (pair, media_key)
            )
            """
        )
        # 少量运行信息（如 Trakt 定时全量同步的上次执行时间），重启后保留
        self.conn.execute("CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
        # 已见过的 Trakt 观看记录（最后观看时间），从 Trakt 拉取时只同步之后新增的观看
        self.conn.execute(
            """
            CREATE TABLE IF NOT EXISTS trakt_seen (
                pair TEXT NOT NULL,
                media_key TEXT NOT NULL,
                watched_at INTEGER NOT NULL,
                PRIMARY KEY (pair, media_key)
            )
            """
        )
        self.conn.commit()

    def get(self, pair: str, key: MediaKey) -> Optional[WatchState]:
        row = self.conn.execute(
            "SELECT played, position_ms, last_played FROM sync_state WHERE pair=? AND media_key=?",
            (pair, str(key)),
        ).fetchone()
        if row is None:
            return None
        return WatchState(played=bool(row[0]), position_ms=row[1], last_played=row[2])

    def all(self, pair: str) -> dict[str, WatchState]:
        """该用户对的全部基准，对账时一次读出，不必逐条查询"""
        rows = self.conn.execute("SELECT media_key, played, position_ms, last_played FROM sync_state WHERE pair=?", (pair,))
        return {k: WatchState(played=bool(p), position_ms=pos, last_played=last) for k, p, pos, last in rows}

    def set(self, pair: str, key: MediaKey, state: WatchState, commit: bool = True) -> None:
        """commit=False 时由调用方批量提交（见 commit）"""
        self.conn.execute(
            """
            INSERT INTO sync_state (pair, media_key, played, position_ms, last_played, updated_at)
            VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(pair, media_key) DO UPDATE SET
                played=excluded.played, position_ms=excluded.position_ms,
                last_played=excluded.last_played, updated_at=excluded.updated_at
            """,
            (pair, str(key), int(state.played), state.position_ms, state.last_played, int(time.time())),
        )
        if commit:
            self.conn.commit()

    def commit(self) -> None:
        self.conn.commit()

    def meta_get(self, key: str) -> Optional[str]:
        row = self.conn.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
        return row[0] if row else None

    def meta_set(self, key: str, value: str) -> None:
        self.conn.execute(
            "INSERT INTO meta (key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, value)
        )
        self.conn.commit()

    def trakt_seen(self, pair: str) -> dict[str, int]:
        rows = self.conn.execute("SELECT media_key, watched_at FROM trakt_seen WHERE pair=?", (pair,))
        return {k: ts for k, ts in rows}

    def set_trakt_seen(self, pair: str, seen: dict[MediaKey, int]) -> None:
        self.conn.executemany(
            """
            INSERT INTO trakt_seen (pair, media_key, watched_at) VALUES (?, ?, ?)
            ON CONFLICT(pair, media_key) DO UPDATE SET watched_at=max(watched_at, excluded.watched_at)
            """,
            [(pair, str(k), ts) for k, ts in seen.items()],
        )
        self.conn.commit()

    def close(self) -> None:
        self.conn.close()
