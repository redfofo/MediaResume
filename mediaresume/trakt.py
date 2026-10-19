from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Optional

import aiohttp

from . import __version__
from .config import TraktConfig
from .models import POSITION_TOLERANCE_MS, MediaKey, WatchState

log = logging.getLogger("trakt")

API = "https://api.trakt.tv"
USER_AGENT = f"MediaResume/{__version__}"
# 设备码授权不走回调，刷新 token 时 redirect_uri 固定为此值，创建 Trakt App 时也要填它
REDIRECT_URI = "urn:ietf:wg:oauth:2.0:oob"
# access_token 只有 24 小时有效期，提前这么久刷新（秒）
REFRESH_MARGIN = 3600
# Trakt 限制写接口每秒 1 次
WRITE_INTERVAL = 1.1
# 单次提交的最大条目数
BATCH_SIZE = 200
# 检查待推送实时进度的间隔（秒）
FLUSH_INTERVAL = 5
# 推送失败后的重试退避（秒）
RETRY_MIN, RETRY_MAX = 60, 900
# scrobble/stop 在 1%~79% 时只保存进度，>=80% 会直接记一次观看（与观看记录推送重复），因此只推送此区间
PROGRESS_MIN, PROGRESS_MAX = 1.0, 80.0
# 进度变化小于此百分比不重复推送
PROGRESS_STEP = 1.0
# 列表接口的分页大小（Trakt 上限 250）与并发拉取的页数
PAGE_LIMIT, PAGE_CONCURRENCY = 250, 8
# 全量观看记录 / 播放进度的缓存时间（秒）：全量同步的预览和执行、共用一个账户的多个映射会接连读取同样的数据。
# 对该账户的任何写入都会立即作废缓存
READ_CACHE_TTL = 60
# 标记已看 / 清除进度延迟这么久（秒）再推送，期间改回（误点、开始重看）则取消
MARK_DELAY = 60
# scrobble 记录的观看时间由 Trakt 服务器决定，记为已见时多留这么多秒，容忍时钟偏差
SEEN_MARGIN = 300
# 统计标记未看数量的时间窗口（秒），超过 unwatch_max 部电影 / 剧时暂停删除，等待页面确认
UNWATCH_WINDOW = 3600
MARK_NAMES = {"watched": "标记已看", "clear": "清除进度", "unwatch": "标记未看（删除观看记录）"}


class TraktAuthError(Exception):
    """token 无效且无法刷新，需要在 Web 页面重新授权"""


class TraktTokenStore:
    """按 Trakt 用户名保存 OAuth token。token 每天刷新，因此不写进 config.yaml，避免与页面保存配置互相覆盖"""

    def __init__(self, path: Path):
        self.path = path

    def _load(self) -> dict[str, dict]:
        try:
            return json.loads(self.path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return {}

    def get(self, username: str) -> Optional[dict]:
        return self._load().get(username)

    def users(self) -> list[str]:
        return sorted(self._load())

    def set(self, username: str, token: dict) -> None:
        data = self._load()
        data[username] = token
        self._save(data)

    def delete(self, username: str) -> None:
        data = self._load()
        if data.pop(username, None) is not None:
            self._save(data)

    def _save(self, data: dict[str, dict]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, indent=2), encoding="utf-8")
        os.chmod(tmp, 0o600)
        os.replace(tmp, self.path)


def token_record(resp: dict, client_id: str) -> dict:
    """OAuth 响应 -> 保存的 token；记录 client_id，换了 App 后旧 token 不再可用"""
    created = int(resp.get("created_at") or time.time())
    return {
        "access_token": resp["access_token"],
        "refresh_token": resp["refresh_token"],
        "expires_at": created + int(resp.get("expires_in") or 86400),
        "client_id": client_id,
    }


class TraktClient:
    """Trakt API。不带 username 的方法用于授权流程，带 username 的方法使用该用户保存的 token"""

    def __init__(self, session: aiohttp.ClientSession, cfg: TraktConfig, tokens: TraktTokenStore):
        self.http = session
        self.cfg = cfg
        self.tokens = tokens
        self._write_lock = asyncio.Lock()
        # 并发请求只刷新一次 token：旧 refresh_token 刷新后即失效
        self._refresh_lock = asyncio.Lock()
        self._last_write = 0.0
        # (用户名, 数据名) -> (读取时间, 数据)
        self._cache: dict[tuple[str, str], tuple[float, Any]] = {}
        # 每个用户的写入计数：读取期间有写入时，读到的可能是旧数据，不放入缓存
        self._writes: dict[str, int] = {}

    def _headers(self, access_token: Optional[str] = None) -> dict:
        headers = {
            "Content-Type": "application/json",
            "User-Agent": USER_AGENT,
            "trakt-api-version": "2",
            "trakt-api-key": self.cfg.client_id,
        }
        if access_token:
            headers["Authorization"] = f"Bearer {access_token}"
        return headers

    async def _raw(
        self, method: str, path: str, body: Any = None, access_token: Optional[str] = None, retry_429: bool = True
    ) -> tuple[int, Any]:
        """retry_429=False 时 429 原样返回（设备码轮询用 429 表示 slow_down，应由页面放慢轮询）"""
        for _ in range(3):
            async with self.http.request(method, API + path, json=body, headers=self._headers(access_token)) as resp:
                if resp.status == 429 and retry_429:
                    wait = float(resp.headers.get("Retry-After") or 10)
                    log.debug("Trakt 限流，%ss 后重试 %s", wait, path)
                    await asyncio.sleep(wait)
                    continue
                text = await resp.text()
                try:
                    return resp.status, json.loads(text) if text.strip() else None
                except ValueError:
                    return resp.status, None
        raise RuntimeError(f"Trakt 持续限流: {path}")

    # ---------- 设备码授权 ----------

    async def device_code(self) -> dict:
        status, data = await self._raw("POST", "/oauth/device/code", {"client_id": self.cfg.client_id})
        if status != 200:
            raise RuntimeError(f"获取设备码失败（HTTP {status}），请检查 Client ID")
        return data

    async def device_token(self, device_code: str) -> tuple[int, Optional[dict]]:
        """返回 (HTTP 状态码, token 响应)；400 表示用户尚未完成授权"""
        body = self._with_secret({"code": device_code, "client_id": self.cfg.client_id})
        return await self._raw("POST", "/oauth/device/token", body, retry_429=False)

    def _with_secret(self, body: dict) -> dict:
        """Trakt 新建的 App 不再发放 Client Secret（已弃用、可选），旧 App 填了才发送"""
        return {**body, "client_secret": self.cfg.client_secret} if self.cfg.client_secret else body

    async def username_of(self, access_token: str) -> str:
        status, data = await self._raw("GET", "/users/settings", access_token=access_token)
        if status != 200:
            raise RuntimeError(f"读取 Trakt 用户信息失败（HTTP {status}）")
        return data["user"]["ids"]["slug"]

    # ---------- 带用户 token 的请求 ----------

    async def _access_token(self, username: str, rejected: Optional[str] = None) -> str:
        """rejected：被服务器拒绝（401）的 access_token，仍是当前 token 时强制刷新"""
        async with self._refresh_lock:
            tok = self.tokens.get(username)
            if not tok or tok.get("client_id") != self.cfg.client_id:
                raise TraktAuthError(f"Trakt 账户 {username} 未授权或授权属于其他 App，请在配置页重新授权")
            if tok["expires_at"] - time.time() < REFRESH_MARGIN or tok["access_token"] == rejected:
                tok = await self._refresh(username, tok)
            return tok["access_token"]

    async def _refresh(self, username: str, tok: dict) -> dict:
        body = self._with_secret({
            "refresh_token": tok["refresh_token"],
            "client_id": self.cfg.client_id,
            "redirect_uri": REDIRECT_URI,
            "grant_type": "refresh_token",
        })
        status, data = await self._raw("POST", "/oauth/token", body)
        if status in (400, 401):
            raise TraktAuthError(f"Trakt 账户 {username} 的授权已失效，请在配置页重新授权")
        if status != 200:
            raise RuntimeError(f"刷新 Trakt token 失败（HTTP {status}）")
        tok = token_record(data, self.cfg.client_id)
        self.tokens.set(username, tok)
        log.info("Trakt 账户 %s 的 token 已刷新", username)
        return tok

    async def _user_request(
        self, username: str, method: str, path: str, body: Any = None, ok: tuple[int, ...] = ()
    ) -> Any:
        """ok 中的状态码不视为错误（例如 scrobble 的 409 重复）"""
        if method != "GET":
            # 写接口限速：串行并保持间隔
            async with self._write_lock:
                await asyncio.sleep(max(0.0, self._last_write + WRITE_INTERVAL - time.monotonic()))
                try:
                    return await self._user_request_once(username, method, path, body, ok)
                finally:
                    self._last_write = time.monotonic()
                    self._invalidate(username)
        return await self._user_request_once(username, method, path, body, ok)

    async def _user_request_once(self, username: str, method: str, path: str, body: Any, ok: tuple[int, ...]) -> Any:
        access_token = await self._access_token(username)
        status, data = await self._raw(method, path, body, access_token)
        if status == 401:
            # token 可能被提前吊销，强制刷新一次
            status, data = await self._raw(method, path, body, await self._access_token(username, access_token))
            if status == 401:
                raise TraktAuthError(f"Trakt 账户 {username} 的授权已失效，请在配置页重新授权")
        if status >= 400 and status not in ok:
            raise RuntimeError(_status_message(status))
        return data

    async def last_activities(self, username: str) -> dict:
        return await self._user_request(username, "GET", "/sync/last_activities") or {}

    async def _user_pages(self, username: str, path: str) -> list:
        """取分页接口的全部结果：不带 page 参数时 Trakt 只返回第一页。按批并发拉取，遇到不满一页即结束"""
        out: list = []
        page = 1
        while True:
            pages = await asyncio.gather(*(
                self._user_request(username, "GET", f"{path}?page={p}&limit={PAGE_LIMIT}")
                for p in range(page, page + PAGE_CONCURRENCY)
            ))
            for data in pages:
                out.extend(data or [])
                if len(data or []) < PAGE_LIMIT:
                    return out
            page += PAGE_CONCURRENCY

    def _invalidate(self, username: str) -> None:
        self._writes[username] = self._writes.get(username, 0) + 1
        for k in [k for k in self._cache if k[0] == username]:
            del self._cache[k]

    async def _cached(self, username: str, name: str, fetch: Callable[[], Any]) -> dict:
        """返回副本，调用方修改不影响缓存"""
        hit = self._cache.get((username, name))
        if hit and time.monotonic() - hit[0] < READ_CACHE_TTL:
            return dict(hit[1])
        writes = self._writes.get(username, 0)
        value = await fetch()
        if self._writes.get(username, 0) == writes:
            self._cache[(username, name)] = (time.monotonic(), value)
        return dict(value)

    async def watched(self, username: str) -> dict[MediaKey, int]:
        async def fetch() -> dict[MediaKey, int]:
            movies = await self._user_pages(username, "/sync/watched/movies")
            # /sync/watched/shows 已不再返回各季各集，剧集的已看从观看历史汇总
            episodes = await self._user_pages(username, "/sync/history/episodes")
            return parse_watched(movies, episodes)

        return await self._cached(username, "watched", fetch)

    async def add_history(self, username: str, payload: dict) -> dict:
        return await self._user_request(username, "POST", "/sync/history", payload) or {}

    async def trakt_id(self, kind: str, tmdb: str) -> Optional[int]:
        """TMDB id -> Trakt id（kind 为 movie / show）；Trakt 不认识时返回 None"""
        status, data = await self._raw("GET", f"/search/tmdb/{tmdb}?type={kind}")
        if status != 200:
            raise RuntimeError(_status_message(status))
        for e in data or []:
            if e.get("type") == kind:
                return ((e.get(kind) or {}).get("ids") or {}).get("trakt")
        return None

    async def item_history(self, username: str, key: MediaKey) -> Optional[list[dict]]:
        """该电影 / 单集在 Trakt 上的全部观看记录；Trakt 不认识该条目时返回 None"""
        kind = "movie" if key.kind == "movie" else "show"
        trakt_id = await self.trakt_id(kind, key.tmdb)
        if trakt_id is None:
            return None
        # 剧集按整部剧查询（单集要再查一次 id），再按季 / 集筛选
        entries = await self._user_pages(username, f"/sync/history/{kind}s/{trakt_id}")
        return [e for e in entries if _entry_key(e) == key]

    async def remove_history(self, username: str, history_ids: list[int]) -> dict:
        return await self._user_request(username, "POST", "/sync/history/remove", {"ids": history_ids}) or {}

    async def playback(self, username: str) -> dict[MediaKey, "Playback"]:
        async def fetch() -> dict[MediaKey, Playback]:
            return parse_playback(await self._user_pages(username, "/sync/playback"))

        return await self._cached(username, "playback", fetch)

    async def remove_playback(self, username: str, playback_id: int) -> None:
        await self._user_request(username, "DELETE", f"/sync/playback/{playback_id}", ok=(404,))

    async def scrobble(self, username: str, action: str, key: MediaKey, progress: float) -> None:
        await self._user_request(username, "POST", f"/scrobble/{action}", scrobble_body(key, progress), ok=(409, 422))


def _group(key: MediaKey) -> str:
    """条目所属的电影 / 剧"""
    return f"{key.kind}:{key.tmdb}" if key.kind == "movie" else f"show:{key.tmdb}"


def _label(key: MediaKey, title: str) -> str:
    """页面展示用：单集标题通常只有集名，附上季 / 集号"""
    if key.kind == "episode":
        return f"{title or key.tmdb} S{key.season:02d}E{key.episode:02d}"
    return title or str(key)


def _status_message(status: int) -> str:
    return {
        420: "Trakt 账户额度已满（HTTP 420），免费账户请检查观看记录上限",
        423: "Trakt 账户已被锁定（HTTP 423），请登录 Trakt 网站处理",
        426: "该功能需要 Trakt VIP（HTTP 426）",
    }.get(status, f"Trakt 返回 HTTP {status}")


# ---------- 数据转换 ----------


def _parse_ts(value: Optional[str]) -> int:
    if not value:
        return 0
    try:
        return int(datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp())
    except ValueError:
        return 0


def _iso(ts: int) -> str:
    # 没有观看时间时（例如只被标记为已看）让 Trakt 记为未知日期，不冒充“现在”
    if ts <= 0:
        return "unknown"
    return datetime.fromtimestamp(ts, timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z")


def parse_watched(movies: list[dict], history: list[dict]) -> dict[MediaKey, int]:
    """Trakt 已看电影列表 + 剧集观看历史 -> {MediaKey: 最后观看时间}"""
    out: dict[MediaKey, int] = {}
    for m in movies:
        tmdb = ((m.get("movie") or {}).get("ids") or {}).get("tmdb")
        if tmdb:
            out[MediaKey("movie", str(tmdb))] = _parse_ts(m.get("last_watched_at"))
    for e in history:
        key = _entry_key(e)
        if key is not None:
            out[key] = max(out.get(key, 0), _parse_ts(e.get("watched_at")))
    return out


@dataclass(frozen=True)
class Playback:
    id: int
    progress: float
    paused_at: int


def _entry_key(e: dict) -> Optional[MediaKey]:
    if e.get("type") == "movie":
        tmdb = ((e.get("movie") or {}).get("ids") or {}).get("tmdb")
        return MediaKey("movie", str(tmdb)) if tmdb else None
    if e.get("type") == "episode":
        tmdb = ((e.get("show") or {}).get("ids") or {}).get("tmdb")
        ep = e.get("episode") or {}
        if tmdb and ep.get("season") is not None and ep.get("number") is not None:
            return MediaKey("episode", str(tmdb), int(ep["season"]), int(ep["number"]))
    return None


def parse_playback(entries: list[dict]) -> dict[MediaKey, Playback]:
    """Trakt 播放进度 -> {MediaKey: Playback}；同一条目有多条时取最新"""
    out: dict[MediaKey, Playback] = {}
    for e in entries:
        key = _entry_key(e)
        if key is None:
            continue
        pb = Playback(int(e["id"]), float(e.get("progress") or 0), _parse_ts(e.get("paused_at")))
        if key not in out or pb.paused_at > out[key].paused_at:
            out[key] = pb
    return out


def history_payload(items: dict[MediaKey, int]) -> dict:
    """{MediaKey: 观看时间} -> POST /sync/history 请求体，剧集按剧/季分组"""
    movies = []
    shows: dict[str, dict[int, list[dict]]] = {}
    for key, ts in items.items():
        if key.kind == "movie":
            movies.append({"ids": {"tmdb": int(key.tmdb)}, "watched_at": _iso(ts)})
        else:
            shows.setdefault(key.tmdb, {}).setdefault(key.season, []).append(
                {"number": key.episode, "watched_at": _iso(ts)}
            )
    payload: dict[str, list] = {}
    if movies:
        payload["movies"] = movies
    if shows:
        payload["shows"] = [
            {
                "ids": {"tmdb": int(tmdb)},
                "seasons": [{"number": n, "episodes": eps} for n, eps in sorted(seasons.items())],
            }
            for tmdb, seasons in shows.items()
        ]
    return payload


def scrobble_body(key: MediaKey, progress: float) -> dict:
    if key.kind == "movie":
        return {"movie": {"ids": {"tmdb": int(key.tmdb)}}, "progress": progress}
    return {
        "show": {"ids": {"tmdb": int(key.tmdb)}},
        "episode": {"season": key.season, "number": key.episode},
        "progress": progress,
    }


def not_found_tmdb(resp: dict) -> tuple[set[str], set[str]]:
    """Trakt 不认识的电影 / 剧 tmdb id"""
    nf = resp.get("not_found") or {}

    def ids(entries: list[dict]) -> set[str]:
        return {str(e["ids"]["tmdb"]) for e in entries or [] if (e.get("ids") or {}).get("tmdb")}

    return ids(nf.get("movies")), ids(nf.get("shows"))


def percent_of(state: WatchState, duration_ms: int) -> float:
    return round(min(100.0, state.position_ms * 100 / duration_ms), 2) if duration_ms > 0 else 0.0


# ---------- 同步 ----------


@dataclass
class PushPlan:
    """MediaResume -> Trakt 的全量同步内容"""

    history: dict[MediaKey, int] = field(default_factory=dict)  # 补观看记录：观看时间
    progress: dict[MediaKey, float] = field(default_factory=dict)  # 写入进度：百分比
    clear: dict[MediaKey, int] = field(default_factory=dict)  # 清除进度：Trakt playback id


@dataclass(frozen=True)
class Mark:
    """待推送的标记：watched 补一条观看记录并清除进度，clear 只清除进度，unwatch 删除全部观看记录并清除进度"""

    kind: str
    state: WatchState
    title: str
    due: float  # time.monotonic()
    # 标记未看：已在页面确认，不受数量上限限制
    confirmed: bool = False


@dataclass
class TraktAccount:
    username: str
    # 待推送的实时进度：MediaKey -> (百分比, scrobble 动作, 标题)
    pending: dict[MediaKey, tuple[float, str, str]] = field(default_factory=dict)
    # 本次运行中已推送的进度：MediaKey -> (百分比, 是否正在播放)
    progress_sent: dict[MediaKey, tuple[float, bool]] = field(default_factory=dict)
    # 本次运行中已通过 stop 100% 记录观看的条目，避免回传事件重复记录
    scrobbled: set[MediaKey] = field(default_factory=set)
    # 待推送的标记（在 Plex / Emby 中标记已看 / 未看、清除进度）
    marks: dict[MediaKey, Mark] = field(default_factory=dict)
    # 短时间内标记未看过多而暂停、等待页面确认的标记
    held: dict[MediaKey, Mark] = field(default_factory=dict)
    # 最近删除过观看记录的电影 / 剧 -> time.monotonic()，用于数量上限
    unwatch_recent: dict[str, float] = field(default_factory=dict)
    progress_pushed: int = 0
    last_push: Optional[float] = None
    error: Optional[str] = None
    auth_failed: bool = False
    retry_at: float = 0
    retry_delay: float = RETRY_MIN
    # 手动全量推送进行中
    busy: bool = False


class TraktSync:
    """自动部分：实时播放进度推送到 Trakt（scrobble），以及实时发现的标记已看 / 未看、清除进度。
    已看记录和进度的全量同步由页面手动或引擎定时触发：引擎生成计划，推送由 push_full 执行，拉回由引擎写入 Plex / Emby。"""

    def __init__(self, client: TraktClient, cfg: TraktConfig, dry_run: bool, removed_log: Optional[Path] = None):
        self.client = client
        self.cfg = cfg
        self.dry_run = dry_run
        # 删除观看记录前追加备份到此文件（每行一个条目的原始观看记录），可据此恢复
        self.removed_log = removed_log
        self.accounts: dict[str, TraktAccount] = {}
        # 实时写入观看记录后回调 (Trakt 用户名, {MediaKey: 观看时间})，引擎据此记为已见：
        # 之后在本地改为未看的，从 Trakt 全量同步时不会再被拉回已看
        self.on_history: Optional[Callable[[str, dict[MediaKey, int]], None]] = None

    def add_account(self, username: str) -> None:
        self.accounts.setdefault(username, TraktAccount(username))

    def reauthorized(self, username: str) -> None:
        """页面重新授权后恢复该账户的推送，无需重启引擎"""
        acc = self.accounts.get(username)
        if acc and acc.auth_failed:
            acc.auth_failed, acc.error = False, None
            acc.retry_at, acc.retry_delay = 0, RETRY_MIN
            log.info("Trakt 账户 %s 已重新授权，恢复推送", username)

    def _account(self, username: Optional[str]) -> Optional[TraktAccount]:
        acc = self.accounts.get(username) if username else None
        return None if acc is None or acc.auth_failed else acc

    # ---------- 实时进度 ----------

    def observe_live(
        self,
        username: Optional[str],
        key: MediaKey,
        state: WatchState,
        duration_ms: int,
        title: str = "",
        playing: bool = False,
    ) -> None:
        """实时事件中的观看状态：正在播放 -> start，暂停/停止 -> stop 保存进度，
        本次运行中推送过进度的条目看完 -> stop 100%（Trakt 记一次观看）"""
        acc = self._account(username)
        if acc is None or not self.cfg.scrobble:
            return
        if state.played:
            if key in acc.progress_sent and key not in acc.scrobbled:
                acc.pending[key] = (100.0, "stop", title)
            return
        if duration_ms <= 0:
            return
        pct = percent_of(state, duration_ms)
        # 暂停时 <1% 会被 Trakt 拒绝，>=80% 会被记为看完（本地却还没看完），都不推送
        if not playing and not PROGRESS_MIN <= pct < PROGRESS_MAX:
            return
        prev = acc.progress_sent.get(key)
        if prev is not None and prev[1] == playing and abs(prev[0] - pct) < PROGRESS_STEP:
            return
        acc.scrobbled.discard(key)  # 重看
        acc.pending[key] = (pct, "start" if playing else "stop", title)

    def observe_mark(
        self,
        username: Optional[str],
        key: MediaKey,
        before: Optional[WatchState],
        after: WatchState,
        title: str = "",
        readded: bool = False,
    ) -> None:
        """实时事件中的状态变化（before 为之前记录的统一状态）：未看 -> 已看，补一条观看记录；
        有进度 -> 没有进度（清除进度），删除 Trakt 上的进度；已看 -> 未看，删除 Trakt 上的全部观看记录（需开启 sync_unwatch）。
        readded：条目是在之前记录的状态之后才入库的（重新入库显示为未看，不是用户标记的），不删除观看记录。
        延迟 MARK_DELAY 秒推送，期间状态又变了就取消"""
        acc = self._account(username)
        if acc is None or not (self.cfg.sync_marks or self.cfg.sync_unwatch):
            return
        for marks in (acc.marks, acc.held):
            mark = marks.get(key)
            if mark is not None and not after.same_as(mark.state):
                del marks[key]
                log.info("Trakt[%s] %s 状态又有变化，取消%s", acc.username, title or key, MARK_NAMES[mark.kind])
        if before is None:
            return
        if after.played and not before.played:
            if acc.pending.get(key, (0.0,))[0] >= 100:
                return  # 实时进度推送已用 stop 100% 记录这次观看
            kind, enabled = "watched", self.cfg.sync_marks
        elif before.played and not after.played:
            # 标记未看会清零进度；带进度的“未看”可能是重看到一半，不是标记未看
            if after.position_ms:
                return
            kind, enabled = "unwatch", self.cfg.sync_unwatch
            if enabled and readded:
                log.info("Trakt[%s] %s 显示为未看，但条目是新入库的（疑似重新入库），不删除 Trakt 观看记录", acc.username, title or key)
                return
        elif not (after.played or before.played) and after.position_ms == 0 and before.position_ms >= POSITION_TOLERANCE_MS:
            kind, enabled = "clear", self.cfg.sync_marks
        else:
            return
        if enabled:
            acc.marks[key] = Mark(kind, after, title, time.monotonic() + MARK_DELAY)

    def release_held(self, username: str, confirm: bool) -> int:
        """页面确认（confirm=True）或丢弃暂停的标记未看，返回条数"""
        acc = self.accounts.get(username)
        if acc is None:
            raise ValueError(f"Trakt 账户 {username} 不在同步中")
        held, acc.held = acc.held, {}
        if confirm:
            for key, mark in held.items():
                acc.marks[key] = replace(mark, confirmed=True, due=0.0)
            log.info("Trakt[%s] 已确认，删除 %d 个条目的观看记录", username, len(held))
        else:
            log.info("Trakt[%s] 已丢弃 %d 个待确认的标记未看，不删除观看记录", username, len(held))
        return len(held)

    async def run(self) -> None:
        # 启动时检查一次授权，问题尽早显示在状态页
        for acc in self.accounts.values():
            await self._guard(acc, self.client.last_activities(acc.username))
        while True:
            await asyncio.sleep(FLUSH_INTERVAL)
            for acc in self.accounts.values():
                if acc.pending and self._ready(acc):
                    await self._guard(acc, self._push_live(acc))
                now = time.monotonic()
                due = {k: m for k, m in acc.marks.items() if m.due <= now}
                if due and self._ready(acc):
                    await self._guard(acc, self._push_marks(acc, due))

    @staticmethod
    def _ready(acc: TraktAccount) -> bool:
        return not acc.auth_failed and time.monotonic() >= acc.retry_at

    async def _guard(self, acc: TraktAccount, coro) -> bool:
        try:
            await coro
            acc.error = None
            acc.retry_delay = RETRY_MIN
            return True
        except TraktAuthError as e:
            acc.error, acc.auth_failed = str(e), True
            acc.pending.clear()
            acc.marks.clear()
            acc.held.clear()
            log.error("%s", e)
        except Exception as e:
            acc.error = str(e) or e.__class__.__name__
            acc.retry_at = time.monotonic() + acc.retry_delay
            log.warning("与 Trakt[%s] 同步失败，%ds 后重试: %s", acc.username, acc.retry_delay, acc.error)
            acc.retry_delay = min(acc.retry_delay * 2, RETRY_MAX)
        return False

    async def _push_live(self, acc: TraktAccount) -> None:
        todo = dict(acc.pending)
        acc.pending.clear()
        for i, (key, (pct, action, title)) in enumerate(todo.items()):
            if self.dry_run:
                log.info("[dry-run] Trakt[%s] %s %s %.0f%%", acc.username, action, title or key, pct)
            else:
                try:
                    await self.client.scrobble(acc.username, action, key, pct)
                except Exception:
                    for k, v in list(todo.items())[i:]:
                        acc.pending.setdefault(k, v)
                    raise
                acc.progress_pushed += 1
                acc.last_push = time.time()
                what = "正在观看" if action == "start" else "看完" if pct >= 100 else "保存进度"
                log.info("Trakt[%s] %s %s %.0f%%", acc.username, what, title or key, pct)
            if pct >= 100:
                acc.scrobbled.add(key)
                acc.progress_sent.pop(key, None)
                if not self.dry_run:
                    self._history_added(acc, {key: int(time.time()) + SEEN_MARGIN})
            else:
                acc.progress_sent[key] = (pct, action == "start")

    def _history_added(self, acc: TraktAccount, items: dict[MediaKey, int]) -> None:
        if self.on_history and items:
            try:
                self.on_history(acc.username, items)
            except Exception as e:
                log.warning("Trakt[%s] 记录已见的观看失败: %s", acc.username, str(e) or e.__class__.__name__)

    # ---------- 实时标记 ----------

    async def _push_marks(self, acc: TraktAccount, due: dict[MediaKey, Mark]) -> None:
        for key in due:
            acc.marks.pop(key, None)
        # 完成的从 todo 中移除；失败时剩余的放回，等待重试（期间又有新标记的以新的为准）
        todo = self._hold_bulk_unwatch(acc, due)
        if not todo:
            return
        try:
            await self._push_marks_todo(acc, todo)
        except Exception:
            for key, mark in todo.items():
                acc.marks.setdefault(key, mark)
            raise

    def _hold_bulk_unwatch(self, acc: TraktAccount, due: dict[MediaKey, Mark]) -> dict[MediaKey, Mark]:
        """短时间内大量条目被标记未看，多半是媒体库重建 / 重新扫描，而不是用户操作：暂停删除，等待页面确认。
        按电影 / 剧计数，标记整季未看只算一部"""
        unwatch = {k: m for k, m in due.items() if m.kind == "unwatch" and not m.confirmed}
        if not unwatch:
            return dict(due)
        now = time.monotonic()
        acc.unwatch_recent = {g: t for g, t in acc.unwatch_recent.items() if now - t < UNWATCH_WINDOW}
        groups = {_group(k) for k in unwatch} | acc.unwatch_recent.keys()
        if not acc.held and len(groups) <= self.cfg.unwatch_max:
            return dict(due)
        acc.held.update(unwatch)
        log.warning(
            "Trakt[%s] %d 分钟内有 %d 部电影 / 剧被标记未看（上限 %d），疑似媒体库异常，暂停删除 Trakt 观看记录，"
            "共 %d 个条目等待在运行状态页确认",
            acc.username, UNWATCH_WINDOW // 60, len(groups | {_group(k) for k in acc.held}), self.cfg.unwatch_max, len(acc.held),
        )
        return {k: m for k, m in due.items() if k not in unwatch}

    def _backup_removed(self, user: str, found: dict[MediaKey, list[dict]], todo: dict[MediaKey, Mark]) -> None:
        if self.removed_log is None:
            return
        self.removed_log.parent.mkdir(parents=True, exist_ok=True)
        removed_at = _iso(int(time.time()))
        with open(self.removed_log, "a", encoding="utf-8") as f:
            for key, entries in found.items():
                if entries:
                    record = {"removed_at": removed_at, "user": user, "key": str(key), "title": todo[key].title, "history": entries}
                    f.write(json.dumps(record, ensure_ascii=False) + "\n")
            f.flush()
            os.fsync(f.fileno())

    async def _push_marks_todo(self, acc: TraktAccount, todo: dict[MediaKey, Mark]) -> None:
        user = acc.username
        if self.dry_run:
            for key, m in todo.items():
                log.info("[dry-run] Trakt[%s] %s %s", user, MARK_NAMES[m.kind], m.title or key)
            todo.clear()
            return
        unwatch = [k for k, m in todo.items() if m.kind == "unwatch"]
        if unwatch:
            found: dict[MediaKey, list[dict]] = {}
            for key in unwatch:
                entries = await self.client.item_history(user, key)
                if entries is None:
                    log.warning("Trakt[%s] 无法识别 %s 的 TMDB ID，跳过删除观看记录", user, todo[key].title or key)
                found[key] = entries or []
            history_ids = [e["id"] for entries in found.values() for e in entries]
            if history_ids:
                # 先备份再删除：备份失败时不删除
                self._backup_removed(user, found, todo)
                for i in range(0, len(history_ids), BATCH_SIZE):
                    await self.client.remove_history(user, history_ids[i:i + BATCH_SIZE])
            for key, entries in found.items():
                # 观看记录已删除，重试时只需清除进度
                todo[key] = replace(todo[key], kind="clear")
                acc.scrobbled.discard(key)
                acc.progress_sent.pop(key, None)
                if not todo[key].confirmed:
                    acc.unwatch_recent[_group(key)] = time.monotonic()
                if entries:
                    acc.progress_pushed += 1
                    acc.last_push = time.time()
                    log.info("Trakt[%s] 标记未看 %s：删除 %d 条观看记录", user, todo[key].title or key, len(entries))
                else:
                    log.debug("Trakt[%s] %s 在 Trakt 上没有观看记录", user, todo[key].title or key)
        now = int(time.time())
        # 没有观看时间时（部分客户端标记已看不更新）记为现在；不晚于现在，Trakt 不接受未来的观看
        watched = {k: min(m.state.last_played, now) or now for k, m in todo.items() if m.kind == "watched"}
        if watched:
            resp = await self.client.add_history(user, history_payload(watched))
            unknown_movies, unknown_shows = not_found_tmdb(resp)
            recorded: dict[MediaKey, int] = {}
            for key, ts in watched.items():
                # 观看记录已写入，重试时只需清除进度，不能再补一次
                todo[key] = replace(todo[key], kind="clear")
                if key.tmdb in (unknown_movies if key.kind == "movie" else unknown_shows):
                    continue
                recorded[key] = ts
                acc.scrobbled.add(key)
                acc.progress_sent.pop(key, None)
                log.info("Trakt[%s] 标记已看 %s", user, todo[key].title or key)
            if unknown_movies or unknown_shows:
                log.warning(
                    "Trakt[%s] 无法识别以下 TMDB ID（电影 %s，剧 %s），已跳过",
                    user, sorted(unknown_movies) or "-", sorted(unknown_shows) or "-",
                )
            acc.progress_pushed += len(recorded)
            acc.last_push = time.time()
            self._history_added(acc, recorded)
        playback = await self.client.playback(user)
        for key in list(todo):
            pb = playback.get(key)
            if pb is not None:
                await self.client.remove_playback(user, pb.id)
                if key not in watched and key not in unwatch:
                    acc.progress_pushed += 1
                    acc.last_push = time.time()
                    log.info("Trakt[%s] 清除进度 %s（原 %.0f%%）", user, todo[key].title or key, pb.progress)
            acc.progress_sent.pop(key, None)
            del todo[key]

    # ---------- 手动全量推送 ----------

    async def push_full(self, username: str, plan: PushPlan) -> None:
        """调用方（引擎）负责检查并占用 busy，这里只在结束时释放"""
        acc = self.accounts[username]
        acc.busy = True
        try:
            if not await self._guard(acc, self._push_full(acc, plan)):
                log.error("Trakt[%s] 全量同步中断: %s", username, acc.error)
        finally:
            acc.busy = False

    async def _push_full(self, acc: TraktAccount, plan: PushPlan) -> None:
        user = acc.username
        log.info(
            "Trakt[%s] 全量同步开始：补观看记录 %d 条，写入进度 %d 条，清除进度 %d 条%s",
            user, len(plan.history), len(plan.progress), len(plan.clear), "（dry-run）" if self.dry_run else "",
        )
        if self.dry_run:
            return
        added = 0
        unknown_movies: set[str] = set()
        unknown_shows: set[str] = set()
        keys = list(plan.history)
        for i in range(0, len(keys), BATCH_SIZE):
            batch = {k: plan.history[k] for k in keys[i:i + BATCH_SIZE]}
            resp = await self.client.add_history(user, history_payload(batch))
            movies, shows = not_found_tmdb(resp)
            unknown_movies |= movies
            unknown_shows |= shows
            a = resp.get("added") or {}
            added += int(a.get("movies") or 0) + int(a.get("episodes") or 0)
            # 与实时标记一样记为已见：之后在本地改为未看的，从 Trakt 全量同步时不会再被拉回已看。
            # 没有观看时间的以 unknown 写入，Trakt 记录的时间不确定，按现在计
            now = int(time.time())
            self._history_added(acc, {
                k: ts if ts > 0 else now + SEEN_MARGIN
                for k, ts in batch.items()
                if k.tmdb not in (movies if k.kind == "movie" else shows)
            })
            log.info("Trakt[%s] 补观看记录 %d/%d", user, min(i + BATCH_SIZE, len(keys)), len(keys))
        if unknown_movies or unknown_shows:
            log.warning(
                "Trakt[%s] 无法识别以下 TMDB ID（电影 %s，剧 %s），已跳过",
                user, sorted(unknown_movies) or "-", sorted(unknown_shows) or "-",
            )
        for key, pct in plan.progress.items():
            await self.client.scrobble(user, "stop", key, pct)
            acc.progress_sent[key] = (pct, False)
        for playback_id in plan.clear.values():
            await self.client.remove_playback(user, playback_id)
        acc.last_push = time.time()
        log.info(
            "Trakt[%s] 全量同步完成：添加观看记录 %d 条，写入进度 %d 条，清除进度 %d 条",
            user, added, len(plan.progress), len(plan.clear),
        )

    def status(self) -> list[dict]:
        return [
            {
                "user": a.username,
                "pending": len(a.pending) + len(a.marks),
                "held": sorted(_label(k, m.title) for k, m in a.held.items()),
                "progress_pushed": a.progress_pushed,
                "last_push": a.last_push,
                "busy": a.busy,
                "error": a.error,
            }
            for a in self.accounts.values()
        ]
