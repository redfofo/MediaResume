from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

import aiohttp

from . import __version__
from .config import TraktConfig
from .models import MediaKey, WatchState

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
        self._last_write = 0.0

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

    async def _raw(self, method: str, path: str, body: Any = None, access_token: Optional[str] = None) -> tuple[int, Any]:
        for _ in range(3):
            async with self.http.request(method, API + path, json=body, headers=self._headers(access_token)) as resp:
                if resp.status == 429:
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
        body = {"code": device_code, "client_id": self.cfg.client_id, "client_secret": self.cfg.client_secret}
        return await self._raw("POST", "/oauth/device/token", body)

    async def username_of(self, access_token: str) -> str:
        status, data = await self._raw("GET", "/users/settings", access_token=access_token)
        if status != 200:
            raise RuntimeError(f"读取 Trakt 用户信息失败（HTTP {status}）")
        return data["user"]["ids"]["slug"]

    # ---------- 带用户 token 的请求 ----------

    async def _access_token(self, username: str) -> str:
        tok = self.tokens.get(username)
        if not tok or tok.get("client_id") != self.cfg.client_id:
            raise TraktAuthError(f"Trakt 账户 {username} 未授权或授权属于其他 App，请在配置页重新授权")
        if tok["expires_at"] - time.time() < REFRESH_MARGIN:
            tok = await self._refresh(username, tok)
        return tok["access_token"]

    async def _refresh(self, username: str, tok: dict) -> dict:
        body = {
            "refresh_token": tok["refresh_token"],
            "client_id": self.cfg.client_id,
            "client_secret": self.cfg.client_secret,
            "redirect_uri": REDIRECT_URI,
            "grant_type": "refresh_token",
        }
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
        return await self._user_request_once(username, method, path, body, ok)

    async def _user_request_once(self, username: str, method: str, path: str, body: Any, ok: tuple[int, ...]) -> Any:
        status, data = await self._raw(method, path, body, await self._access_token(username))
        if status == 401:
            # token 可能被提前吊销，强制刷新一次
            tok = await self._refresh(username, self.tokens.get(username) or {})
            status, data = await self._raw(method, path, body, tok["access_token"])
            if status == 401:
                raise TraktAuthError(f"Trakt 账户 {username} 的授权已失效，请在配置页重新授权")
        if status >= 400 and status not in ok:
            raise RuntimeError(_status_message(status))
        return data

    async def last_activities(self, username: str) -> dict:
        return await self._user_request(username, "GET", "/sync/last_activities") or {}

    async def watched(self, username: str) -> dict[MediaKey, int]:
        movies = await self._user_request(username, "GET", "/sync/watched/movies")
        shows = await self._user_request(username, "GET", "/sync/watched/shows")
        return parse_watched(movies or [], shows or [])

    async def add_history(self, username: str, payload: dict) -> dict:
        return await self._user_request(username, "POST", "/sync/history", payload) or {}

    async def playback(self, username: str) -> dict[MediaKey, "Playback"]:
        return parse_playback(await self._user_request(username, "GET", "/sync/playback") or [])

    async def remove_playback(self, username: str, playback_id: int) -> None:
        await self._user_request(username, "DELETE", f"/sync/playback/{playback_id}", ok=(404,))

    async def scrobble(self, username: str, action: str, key: MediaKey, progress: float) -> None:
        await self._user_request(username, "POST", f"/scrobble/{action}", scrobble_body(key, progress), ok=(409, 422))


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


def parse_watched(movies: list[dict], shows: list[dict]) -> dict[MediaKey, int]:
    """Trakt 已看列表 -> {MediaKey: 最后观看时间}"""
    out: dict[MediaKey, int] = {}
    for m in movies:
        tmdb = ((m.get("movie") or {}).get("ids") or {}).get("tmdb")
        if tmdb:
            out[MediaKey("movie", str(tmdb))] = _parse_ts(m.get("last_watched_at"))
    for s in shows:
        tmdb = ((s.get("show") or {}).get("ids") or {}).get("tmdb")
        if not tmdb:
            continue
        for season in s.get("seasons") or []:
            for ep in season.get("episodes") or []:
                key = MediaKey("episode", str(tmdb), int(season["number"]), int(ep["number"]))
                out[key] = _parse_ts(ep.get("last_watched_at"))
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


@dataclass
class TraktAccount:
    username: str
    # 待推送的实时进度：MediaKey -> (百分比, scrobble 动作, 标题)
    pending: dict[MediaKey, tuple[float, str, str]] = field(default_factory=dict)
    # 本次运行中已推送的进度：MediaKey -> (百分比, 是否正在播放)
    progress_sent: dict[MediaKey, tuple[float, bool]] = field(default_factory=dict)
    # 本次运行中已通过 stop 100% 记录观看的条目，避免回传事件重复记录
    scrobbled: set[MediaKey] = field(default_factory=set)
    progress_pushed: int = 0
    last_push: Optional[float] = None
    error: Optional[str] = None
    auth_failed: bool = False
    retry_at: float = 0
    retry_delay: float = RETRY_MIN
    # 手动全量推送进行中
    busy: bool = False


class TraktSync:
    """自动部分只有一件事：实时播放进度推送到 Trakt（scrobble）。
    已看记录和进度的全量同步由页面手动触发：引擎生成计划，推送由 push_full 执行，拉回由引擎写入 Plex / Emby。"""

    def __init__(self, client: TraktClient, cfg: TraktConfig, dry_run: bool):
        self.client = client
        self.cfg = cfg
        self.dry_run = dry_run
        self.accounts: dict[str, TraktAccount] = {}

    def add_account(self, username: str) -> None:
        self.accounts.setdefault(username, TraktAccount(username))

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

    async def run(self) -> None:
        # 启动时检查一次授权，问题尽早显示在状态页
        for acc in self.accounts.values():
            await self._guard(acc, self.client.last_activities(acc.username))
        while True:
            await asyncio.sleep(FLUSH_INTERVAL)
            now = time.monotonic()
            for acc in self.accounts.values():
                if acc.pending and not acc.auth_failed and now >= acc.retry_at:
                    await self._guard(acc, self._push_live(acc))

    async def _guard(self, acc: TraktAccount, coro) -> bool:
        try:
            await coro
            acc.error = None
            acc.retry_delay = RETRY_MIN
            return True
        except TraktAuthError as e:
            acc.error, acc.auth_failed = str(e), True
            acc.pending.clear()
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
            else:
                acc.progress_sent[key] = (pct, action == "start")

    # ---------- 手动全量推送 ----------

    async def push_full(self, username: str, plan: PushPlan) -> None:
        acc = self.accounts[username]
        if acc.busy:
            raise RuntimeError(f"Trakt[{username}] 已有全量同步在执行")
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
                "pending": len(a.pending),
                "progress_pushed": a.progress_pushed,
                "last_push": a.last_push,
                "busy": a.busy,
                "error": a.error,
            }
            for a in self.accounts.values()
        ]
