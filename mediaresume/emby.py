from __future__ import annotations

import json
import logging
import re
from dataclasses import replace
from datetime import datetime, timezone
from typing import Any, Optional

import aiohttp

from .models import MediaItem, MediaKey, WatchState, clean_tmdb, tmdb_from_path

log = logging.getLogger("emby")

PAGE_SIZE = 1000
# 增量轮询的分页大小
CHANGED_PAGE_SIZE = 500
TICKS_PER_MS = 10_000


def _parse_date(value: Optional[str]) -> int:
    """Emby 日期形如 2024-01-01T12:00:00.0000000Z，小数位可能超过 6 位"""
    if not value:
        return 0
    value = re.sub(r"\.\d+", "", value).replace("Z", "+00:00")
    try:
        return int(datetime.fromisoformat(value).timestamp())
    except ValueError:
        return 0


def _tmdb_of(item: dict) -> Optional[str]:
    for k, v in (item.get("ProviderIds") or {}).items():
        if k.lower() in ("tmdb", "moviedb") and (tmdb := clean_tmdb(v)):
            return tmdb
    return None


def _item_tmdb(item: dict) -> Optional[str]:
    """优先取路径中的 {tmdb-} 标记，没有时再用元数据"""
    return tmdb_from_path(item.get("Path")) or _tmdb_of(item)


def _emby_date(ts: int) -> str:
    """unix 秒 -> Emby UserData 中的 UTC 日期；0 表示现在"""
    dt = datetime.fromtimestamp(ts, timezone.utc) if ts else datetime.now(timezone.utc)
    return dt.strftime("%Y-%m-%dT%H:%M:%S.0000000Z")


def _emby_played_date(ts: int) -> str:
    """PlayedItems 的 DatePlayed 参数：yyyyMMddHHmmss，不带时区。
    Emby 按 UTC 解析（ParseExact + AdjustToUniversal），因此按 UTC 格式化，不能用容器本地时区（TZ 可能被设成东八区）"""
    dt = datetime.fromtimestamp(ts, timezone.utc) if ts else datetime.now(timezone.utc)
    return dt.strftime("%Y%m%d%H%M%S")


FIELDS = "ProviderIds,Path"


class EmbyClient:
    def __init__(self, session: aiohttp.ClientSession, url: str, api_key: str):
        self.http = session
        self.url = url
        self.api_key = api_key
        # seriesId -> tmdb id
        self._series_tmdb: dict[str, Optional[str]] = {}

    async def _request(self, method: str, path: str, params: Optional[dict] = None, body: Any = None) -> Any:
        headers = {"X-Emby-Token": self.api_key, "Accept": "application/json"}
        async with self.http.request(method, self.url + path, params=params, json=body, headers=headers) as resp:
            resp.raise_for_status()
            text = await resp.text()
            return json.loads(text) if text.strip() else None

    async def server_info(self) -> dict:
        info = await self._request("GET", "/System/Info")
        return {"name": info.get("ServerName", ""), "version": info.get("Version", "")}

    async def users(self) -> list[dict]:
        return [{"id": u["Id"], "name": u["Name"]} for u in await self._request("GET", "/Users")]

    async def resolve_user(self, name_or_id: str) -> dict:
        for u in await self.users():
            if u["id"] == name_or_id or u["name"].lower() == name_or_id.lower():
                return u
        raise ValueError(f"Emby 中找不到用户: {name_or_id}")

    # ---------- 读取 ----------

    @staticmethod
    def _state_of(item: dict) -> WatchState:
        ud = item.get("UserData") or {}
        played = bool(ud.get("Played"))
        return WatchState(
            played=played,
            position_ms=0 if played else int(ud.get("PlaybackPositionTicks") or 0) // TICKS_PER_MS,
            last_played=_parse_date(ud.get("LastPlayedDate")),
        )

    async def _series_tmdb_id(self, user_id: str, series_id: str) -> Optional[str]:
        if series_id not in self._series_tmdb:
            data = await self._request("GET", f"/Users/{user_id}/Items/{series_id}", {"Fields": FIELDS})
            self._series_tmdb[series_id] = _item_tmdb(data) if data else None
        return self._series_tmdb[series_id]

    def _to_item(self, item: dict) -> Optional[MediaItem]:
        kind = item.get("Type")
        alt_keys: tuple[MediaKey, ...] = ()
        if kind == "Movie":
            tmdb = _item_tmdb(item)
            if not tmdb:
                return None
            key = MediaKey("movie", tmdb)
        elif kind == "Episode":
            tmdb = tmdb_from_path(item.get("Path")) or self._series_tmdb.get(str(item.get("SeriesId")))
            if not tmdb or item.get("ParentIndexNumber") is None or item.get("IndexNumber") is None:
                return None
            season, first = int(item["ParentIndexNumber"]), int(item["IndexNumber"])
            key = MediaKey("episode", tmdb, season, first)
            # 一个文件包含多集（S01E01-E02），Emby 只生成一个条目，用 IndexNumberEnd 表示结束集
            last = int(item.get("IndexNumberEnd") or first)
            alt_keys = tuple(MediaKey("episode", tmdb, season, n) for n in range(first + 1, last + 1))
        else:
            return None
        # 一个文件包含多集时片长是整个文件的，无法换算单集进度
        duration = 0 if alt_keys else int(item.get("RunTimeTicks") or 0) // TICKS_PER_MS
        return MediaItem("emby", str(item["Id"]), key, self._state_of(item), item.get("Name", ""), alt_keys, duration)

    async def get_item(self, user_id: str, item_id: str) -> Optional[MediaItem]:
        data = await self._request("GET", f"/Users/{user_id}/Items/{item_id}", {"Fields": FIELDS})
        if not data:
            return None
        if data.get("Type") == "Episode" and data.get("SeriesId"):
            await self._series_tmdb_id(user_id, str(data["SeriesId"]))
        return self._to_item(data)

    async def _list(self, user_id: str, types: str) -> list[dict]:
        out: list[dict] = []
        start = 0
        while True:
            params = {
                "Recursive": "true",
                "IncludeItemTypes": types,
                "Fields": FIELDS,
                "StartIndex": start,
                "Limit": PAGE_SIZE,
            }
            data = await self._request("GET", f"/Users/{user_id}/Items", params)
            items = data.get("Items") or []
            out.extend(items)
            start += len(items)
            if not items or start >= int(data.get("TotalRecordCount", start)):
                return out

    async def list_items(self, user_id: str) -> list[MediaItem]:
        for s in await self._list(user_id, "Series"):
            self._series_tmdb[str(s["Id"])] = _item_tmdb(s)
        raws = await self._list(user_id, "Movie,Episode")
        # 有些单集挂在不出现在剧集列表里的重复 Series 下，单独查询补上
        orphan = {
            str(r["SeriesId"])
            for r in raws
            if r.get("Type") == "Episode" and r.get("SeriesId") and str(r["SeriesId"]) not in self._series_tmdb
            and not tmdb_from_path(r.get("Path"))
        }
        for series_id in orphan:
            await self._series_tmdb_id(user_id, series_id)
        items = []
        for raw in raws:
            if item := self._to_item(raw):
                items.append(item)
                items.extend(replace(item, key=k, alt_keys=()) for k in item.alt_keys)
        return items

    async def find(self, user_id: str, key: MediaKey) -> list[str]:
        """按 tmdb（剧集再加季/集号）查找条目，返回 ItemId 列表"""
        item_type = "Movie" if key.kind == "movie" else "Series"
        params = {
            "Recursive": "true",
            "IncludeItemTypes": item_type,
            "AnyProviderIdEquals": f"tmdb.{key.tmdb}",
            "Fields": FIELDS,
        }
        data = await self._request("GET", f"/Users/{user_id}/Items", params)
        # 再按“路径标记优先”的规则校验一次 tmdb
        found = [i for i in data.get("Items") or [] if _item_tmdb(i) == key.tmdb]
        if key.kind == "movie":
            return [str(i["Id"]) for i in found]
        out = []
        for series in found:
            self._series_tmdb[str(series["Id"])] = _item_tmdb(series)
            eps = await self._request(
                "GET", f"/Shows/{series['Id']}/Episodes", {"UserId": user_id, "Season": key.season, "Fields": FIELDS}
            )
            for raw in eps.get("Items") or []:
                ep = self._to_item(raw)
                if ep and (ep.key == key or key in ep.alt_keys):
                    out.append(ep.item_id)
        return out

    async def changed_since(self, user_id: str, since: float) -> list[str]:
        """用户数据（已看/未看/进度）在 since 之后保存过的条目；分页取完，批量变化时不丢"""
        out: list[str] = []
        start = 0
        while True:
            params = {
                "Recursive": "true",
                "IncludeItemTypes": "Movie,Episode",
                "MinDateLastSavedForUser": datetime.fromtimestamp(since, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
                "StartIndex": start,
                "Limit": CHANGED_PAGE_SIZE,
            }
            data = await self._request("GET", f"/Users/{user_id}/Items", params)
            items = data.get("Items") or []
            out.extend(str(i["Id"]) for i in items)
            start += len(items)
            if len(items) < CHANGED_PAGE_SIZE or start >= int(data.get("TotalRecordCount", start)):
                return out

    async def resume(self, user_id: str) -> list[dict]:
        """继续观看列表，每项 {group, id, title, position_ms}；group 按整部电影 / 整部剧：movie:tmdb:X / show:tmdb:X"""
        params = {"Recursive": "true", "IncludeItemTypes": "Movie,Episode", "Fields": FIELDS, "Limit": 500}
        data = await self._request("GET", f"/Users/{user_id}/Items/Resume", params)
        out = []
        for item in data.get("Items") or []:
            if item.get("Type") == "Movie":
                tmdb = _item_tmdb(item)
                group = f"movie:tmdb:{tmdb}" if tmdb else None
            elif item.get("Type") == "Episode" and item.get("SeriesId"):
                tmdb = tmdb_from_path(item.get("Path")) or await self._series_tmdb_id(user_id, str(item["SeriesId"]))
                group = f"show:tmdb:{tmdb}" if tmdb else None
            else:
                group = None
            if group:
                out.append({
                    "group": group,
                    "id": str(item["Id"]),
                    "title": item.get("SeriesName") or item.get("Name", ""),
                    "position_ms": int((item.get("UserData") or {}).get("PlaybackPositionTicks") or 0) // TICKS_PER_MS,
                })
        return out

    # ---------- 写入 ----------

    async def hide_from_resume(self, user_id: str, item_id: str, hide: bool = True) -> None:
        params = {"Hide": "true" if hide else "false"}
        await self._request("POST", f"/Users/{user_id}/Items/{item_id}/HideFromResume", params)


    async def mark_played(self, user_id: str, item_id: str, played_at: int = 0) -> None:
        # 带上播放时间，Emby 依据它推算继续观看中的“下一集”
        params = {"DatePlayed": _emby_played_date(played_at)}
        await self._request("POST", f"/Users/{user_id}/PlayedItems/{item_id}", params)

    async def mark_unplayed(self, user_id: str, item_id: str) -> None:
        await self._request("DELETE", f"/Users/{user_id}/PlayedItems/{item_id}")

    async def set_position(self, user_id: str, item_id: str, position_ms: int, played_at: int = 0) -> None:
        # 带上完整 UserData，避免覆盖收藏等其他字段
        data = await self._request("GET", f"/Users/{user_id}/Items/{item_id}")
        user_data = dict((data or {}).get("UserData") or {})
        user_data["PlaybackPositionTicks"] = position_ms * TICKS_PER_MS
        # 没有 LastPlayedDate 的条目不会出现在 Emby 的继续观看中；清除进度时保留原值，不冒充刚播放过
        if position_ms > 0:
            user_data["LastPlayedDate"] = _emby_date(played_at)
        await self._request("POST", f"/Users/{user_id}/Items/{item_id}/UserData", body=user_data)
