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

    def set(self, pair: str, key: MediaKey, state: WatchState) -> None:
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
