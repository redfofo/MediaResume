from __future__ import annotations

import asyncio
import json
import logging
import time
from typing import Any, Awaitable, Callable, Optional

import aiohttp

from .models import MediaItem, MediaKey, WatchState, parse_tmdb, tmdb_from_path

log = logging.getLogger("plex")

PAGE_SIZE = 1000
IDENTIFIER = "com.plexapp.plugins.library"
# 轮询时去掉用不到的字段，减小响应体
SLIM = {
    "excludeElements": "Media,Genre,Country,Director,Writer,Role,Guid,Image,UltraBlurColors,Rating,Collection,Label",
    "excludeFields": "summary,thumb,art,theme,file",
}
# 同上，但保留 Media（文件路径，用于取 {tmdb-} 标记）
SLIM_KEEP_MEDIA = {**SLIM, "excludeElements": SLIM["excludeElements"].replace("Media,", "")}
# 保留 Media 和 Guid
SLIM_WITH_GUID = {**SLIM_KEEP_MEDIA, "includeGuids": 1, "excludeElements": SLIM_KEEP_MEDIA["excludeElements"].replace("Guid,", "")}
# 定向查找时刷新剧集/电影目录的最小间隔（秒）
CATALOG_REFRESH_INTERVAL = 60


class PlexClient:
    def __init__(self, session: aiohttp.ClientSession, url: str, token: str):
        self.http = session
        self.url = url
        self.token = token
        # show ratingKey -> tmdb id
        self._show_tmdb: dict[str, Optional[str]] = {}
        # 电影 ratingKey -> tmdb id
        self._movie_tmdb: dict[str, str] = {}
        self._catalog_refreshed: dict[str, float] = {}
        self.connected = False

    async def _get(self, path: str, params: Optional[dict] = None, token: Optional[str] = None) -> dict:
        headers = {"Accept": "application/json", "X-Plex-Token": token or self.token}
        async with self.http.get(self.url + path, params=params, headers=headers) as resp:
            resp.raise_for_status()
            if resp.content_length == 0:
                return {}
            text = await resp.text()
            return json.loads(text) if text.strip() else {}

    async def server_info(self) -> dict:
        mc = (await self._get("/")).get("MediaContainer", {})
        return {"name": mc.get("friendlyName", ""), "version": mc.get("version", "")}

    async def accounts(self) -> list[dict]:
        """服务器上的账户列表，id=1 为服务器所有者"""
        data = await self._get("/accounts")
        return [
            {"id": int(a["id"]), "name": a.get("name", ""), "owner": int(a["id"]) == 1}
            for a in data.get("MediaContainer", {}).get("Account") or []
            if a.get("name")
        ]

    # ---------- 读取 ----------

    @staticmethod
    def _tmdb_of(meta: dict) -> Optional[str]:
        for g in meta.get("Guid") or []:
            tmdb = parse_tmdb(g.get("id", ""))
            if tmdb:
                return tmdb
        return parse_tmdb(meta.get("guid", ""))

    @staticmethod
    def _path_tmdb(meta: dict) -> Optional[str]:
        """文件路径中的 {tmdb-} 标记（单集/电影看文件，剧看 Location）"""
        for media in meta.get("Media") or []:
            for part in media.get("Part") or []:
                if tmdb := tmdb_from_path(part.get("file")):
                    return tmdb
        for loc in meta.get("Location") or []:
            if tmdb := tmdb_from_path(loc.get("path")):
                return tmdb
        return None

    @staticmethod
    def _state_of(meta: dict) -> WatchState:
        played = int(meta.get("viewCount") or 0) > 0
        return WatchState(
            played=played,
            position_ms=0 if played else int(meta.get("viewOffset") or 0),
            last_played=int(meta.get("lastViewedAt") or 0),
        )

    async def _show_tmdb_id(self, show_rk: str) -> Optional[str]:
        if show_rk not in self._show_tmdb:
            data = await self._get(f"/library/metadata/{show_rk}", {"includeGuids": 1})
            metas = data.get("MediaContainer", {}).get("Metadata") or []
            self._show_tmdb[show_rk] = (self._path_tmdb(metas[0]) or self._tmdb_of(metas[0])) if metas else None
        return self._show_tmdb[show_rk]

    def _to_item(self, meta: dict, show_tmdb: Optional[str] = None) -> Optional[MediaItem]:
        """tmdb 优先取文件路径中的 {tmdb-} 标记，没有时再用元数据"""
        kind = meta.get("type")
        if kind == "movie":
            tmdb = self._path_tmdb(meta) or self._tmdb_of(meta)
            if not tmdb:
                return None
            key = MediaKey("movie", tmdb)
        elif kind == "episode":
            tmdb = self._path_tmdb(meta) or show_tmdb
            if not tmdb or meta.get("parentIndex") is None or meta.get("index") is None:
                return None
            key = MediaKey("episode", tmdb, int(meta["parentIndex"]), int(meta["index"]))
        else:
            return None
        return MediaItem(
            "plex", str(meta["ratingKey"]), key, self._state_of(meta), meta.get("title", ""),
            duration_ms=int(meta.get("duration") or 0),
        )

    async def get_item(self, rating_key: str, token: Optional[str] = None) -> Optional[MediaItem]:
        data = await self._get(f"/library/metadata/{rating_key}", {"includeGuids": 1}, token)
        metas = data.get("MediaContainer", {}).get("Metadata") or []
        if not metas:
            return None
        meta = metas[0]
        show_tmdb = None
        if meta.get("type") == "episode":
            show_tmdb = await self._show_tmdb_id(str(meta.get("grandparentRatingKey")))
        return self._to_item(meta, show_tmdb)

    async def _list_section(self, section: str, type_id: int, token: Optional[str]) -> list[dict]:
        out: list[dict] = []
        start = 0
        while True:
            params = {
                "type": type_id,
                "includeGuids": 1,
                "X-Plex-Container-Start": start,
                "X-Plex-Container-Size": PAGE_SIZE,
            }
            data = await self._get(f"/library/sections/{section}/all", params, token)
            mc = data.get("MediaContainer", {})
            metas = mc.get("Metadata") or []
            out.extend(metas)
            start += len(metas)
            if not metas or start >= int(mc.get("totalSize", start)):
                return out

    async def list_items(self, token: Optional[str] = None) -> list[MediaItem]:
        data = await self._get("/library/sections", token=token)
        sections = data.get("MediaContainer", {}).get("Directory") or []
        items: list[MediaItem] = []
        for sec in sections:
            sid = str(sec["key"])
            if sec.get("type") == "movie":
                for meta in await self._list_section(sid, 1, token):
                    if item := self._to_item(meta):
                        self._movie_tmdb[item.item_id] = item.key.tmdb
                        items.append(item)
            elif sec.get("type") == "show":
                for show in await self._list_section(sid, 2, token):
                    self._show_tmdb[str(show["ratingKey"])] = self._tmdb_of(show)
                for meta in await self._list_section(sid, 4, token):
                    show_rk = str(meta.get("grandparentRatingKey"))
                    # 单集文件路径上的标记比剧的元数据更可靠，顺便更新剧的缓存
                    if tmdb := self._path_tmdb(meta):
                        self._show_tmdb[show_rk] = tmdb
                    if item := self._to_item(meta, self._show_tmdb.get(show_rk)):
                        items.append(item)
        return items

    # ---------- 定向查找 ----------

    async def _refresh_catalog(self, kind: str, token: Optional[str]) -> None:
        """只刷新剧集或电影本身的列表（不含单集），用于发现新入库的剧/电影"""
        if time.monotonic() - self._catalog_refreshed.get(kind, 0) < CATALOG_REFRESH_INTERVAL:
            return
        self._catalog_refreshed[kind] = time.monotonic()
        for sec in await self._sections(token):
            if kind == "show" and sec["type"] == "show":
                # 剧集列表不带文件夹路径，只对缓存里没有的新剧单独查询（会读取 Location）
                data = await self._get(f"/library/sections/{sec['key']}/all", {"type": 2, **SLIM}, token)
                for m in data.get("MediaContainer", {}).get("Metadata") or []:
                    if str(m["ratingKey"]) not in self._show_tmdb:
                        await self._show_tmdb_id(str(m["ratingKey"]))
            elif kind == "movie" and sec["type"] == "movie":
                data = await self._get(f"/library/sections/{sec['key']}/all", {"type": 1, **SLIM_WITH_GUID}, token)
                for m in data.get("MediaContainer", {}).get("Metadata") or []:
                    if tmdb := self._path_tmdb(m) or self._tmdb_of(m):
                        self._movie_tmdb[str(m["ratingKey"])] = tmdb

    async def find(self, key: MediaKey, token: Optional[str] = None) -> list[str]:
        """按 tmdb（剧集再加季/集号）查找条目，返回 ratingKey 列表"""
        kind = "movie" if key.kind == "movie" else "show"
        cache = self._movie_tmdb if kind == "movie" else self._show_tmdb
        rks = [rk for rk, t in cache.items() if t == key.tmdb]
        if not rks:
            await self._refresh_catalog(kind, token)
            rks = [rk for rk, t in cache.items() if t == key.tmdb]
        if kind == "movie":
            return rks
        out = []
        for show_rk in rks:
            for ep in await self.show_episodes(show_rk, token):
                if ep.key == key:
                    out.append(ep.item_id)
        return out

    # ---------- 增量轮询 ----------

    async def _sections(self, token: Optional[str]) -> list[dict]:
        data = await self._get("/library/sections", token=token)
        return [s for s in data.get("MediaContainer", {}).get("Directory") or [] if s.get("type") in ("movie", "show")]

    async def recently_viewed(self, since: int, token: Optional[str] = None) -> list[tuple[str, Optional[str]]]:
        """lastViewedAt >= since 的电影/单集（覆盖标记已看和进度变化），返回 (ratingKey, 剧集 ratingKey 或 None)"""
        out: list[tuple[str, Optional[str]]] = []
        for sec in await self._sections(token):
            type_id = 1 if sec["type"] == "movie" else 4
            start = 0
            while True:
                params = {
                    "type": type_id,
                    "sort": "lastViewedAt:desc",
                    "X-Plex-Container-Start": start,
                    "X-Plex-Container-Size": 50,
                    **SLIM,
                }
                data = await self._get(f"/library/sections/{sec['key']}/all", params, token)
                metas = data.get("MediaContainer", {}).get("Metadata") or []
                recent = [m for m in metas if int(m.get("lastViewedAt") or 0) >= since]
                for m in recent:
                    show = m.get("grandparentRatingKey")
                    out.append((str(m["ratingKey"]), str(show) if show else None))
                if len(recent) < len(metas) or not metas:
                    break
                start += len(metas)
        return out

    async def watched_movies(self, token: Optional[str] = None) -> set[str]:
        out: set[str] = set()
        for sec in await self._sections(token):
            if sec["type"] == "movie":
                params = {"type": 1, "viewCount>>": 0, **SLIM}
                data = await self._get(f"/library/sections/{sec['key']}/all", params, token)
                out.update(str(m["ratingKey"]) for m in data.get("MediaContainer", {}).get("Metadata") or [])
        return out

    async def show_viewed_counts(self, token: Optional[str] = None) -> dict[str, int]:
        """剧集 ratingKey -> 已看集数"""
        out: dict[str, int] = {}
        for sec in await self._sections(token):
            if sec["type"] == "show":
                data = await self._get(f"/library/sections/{sec['key']}/all", {"type": 2, **SLIM}, token)
                for m in data.get("MediaContainer", {}).get("Metadata") or []:
                    out[str(m["ratingKey"])] = int(m.get("viewedLeafCount") or 0)
        return out

    async def show_episodes(self, show_rk: str, token: Optional[str] = None) -> list[MediaItem]:
        data = await self._get(f"/library/metadata/{show_rk}/allLeaves", SLIM_KEEP_MEDIA, token)
        show_tmdb = await self._show_tmdb_id(show_rk)
        items = []
        for meta in data.get("MediaContainer", {}).get("Metadata") or []:
            if item := self._to_item(meta, show_tmdb):
                items.append(item)
        return items

    async def continue_watching(self, token: Optional[str] = None) -> list[dict]:
        """继续观看列表，每项 {group, id, title, position_ms}；group 按整部电影 / 整部剧：movie:tmdb:X / show:tmdb:X"""
        params = {"includeGuids": 1, "X-Plex-Container-Start": 0, "X-Plex-Container-Size": 500}
        data = await self._get("/hubs/continueWatching/items", params, token)
        out = []
        for meta in data.get("MediaContainer", {}).get("Metadata") or []:
            if meta.get("type") == "movie":
                tmdb = self._path_tmdb(meta) or self._tmdb_of(meta)
                group = f"movie:tmdb:{tmdb}" if tmdb else None
            elif meta.get("type") == "episode":
                tmdb = self._path_tmdb(meta) or await self._show_tmdb_id(str(meta.get("grandparentRatingKey")))
                group = f"show:tmdb:{tmdb}" if tmdb else None
            else:
                group = None
            if group:
                out.append({
                    "group": group,
                    "id": str(meta["ratingKey"]),
                    "title": meta.get("grandparentTitle") or meta.get("title", ""),
                    "position_ms": int(meta.get("viewOffset") or 0),
                })
        return out

    async def session_users(self) -> dict[str, tuple[str, str]]:
        """sessionKey -> (账户 id, 用户名)；账户 id 为 "1" 的是服务器所有者"""
        data = await self._get("/status/sessions")
        out = {}
        for meta in data.get("MediaContainer", {}).get("Metadata") or []:
            user = meta.get("User") or {}
            out[str(meta.get("sessionKey"))] = (str(user.get("id", "")), user.get("title", ""))
        return out

    # ---------- 写入 ----------

    async def remove_from_continue_watching(self, rating_key: str, token: Optional[str] = None) -> None:
        headers = {"Accept": "application/json", "X-Plex-Token": token or self.token}
        url = self.url + "/actions/removeFromContinueWatching"
        async with self.http.put(url, params={"ratingKey": rating_key}, headers=headers) as resp:
            resp.raise_for_status()

    async def mark_played(self, rating_key: str, token: Optional[str] = None) -> None:
        await self._get("/:/scrobble", {"key": rating_key, "identifier": IDENTIFIER}, token)

    async def mark_unplayed(self, rating_key: str, token: Optional[str] = None) -> None:
        await self._get("/:/unscrobble", {"key": rating_key, "identifier": IDENTIFIER}, token)

    async def set_position(self, rating_key: str, position_ms: int, token: Optional[str] = None) -> None:
        params = {"key": rating_key, "identifier": IDENTIFIER, "time": position_ms, "state": "stopped"}
        await self._get("/:/progress", params, token)

    # ---------- 实时通知 ----------

    async def listen(
        self, on_playing: Callable[[dict], Awaitable[None]], on_connect: Optional[Callable[[], None]] = None
    ) -> None:
        """监听 Plex WebSocket，断线自动重连；on_connect 在每次（重新）连接后调用"""
        ws_url = self.url.replace("http", "ws", 1) + "/:/websockets/notifications"
        backoff = 1
        while True:
            try:
                async with self.http.ws_connect(
                    ws_url, params={"X-Plex-Token": self.token}, heartbeat=30
                ) as ws:
                    log.info("Plex WebSocket 已连接")
                    self.connected = True
                    backoff = 1
                    if on_connect:
                        on_connect()
                    async for msg in ws:
                        if msg.type != aiohttp.WSMsgType.TEXT:
                            continue
                        container: dict[str, Any] = json.loads(msg.data).get("NotificationContainer", {})
                        if container.get("type") == "playing":
                            for n in container.get("PlaySessionStateNotification") or []:
                                await on_playing(n)
            except asyncio.CancelledError:
                raise
            except Exception as e:
                log.warning("Plex WebSocket 断开: %s", e)
            finally:
                self.connected = False
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, 60)
