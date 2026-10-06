from __future__ import annotations

import asyncio
import base64
import hmac
import logging
import os
import time
from collections import deque
from pathlib import Path
from typing import Optional

import aiohttp
from aiohttp import web

import yaml

from .config import Config, TraktConfig, config_form, config_to_dict, load_config, parse_config, save_config
from .emby import EmbyClient
from .engine import SyncEngine
from .plex import PlexClient
from .store import StateStore
from .trakt import TraktClient, TraktSync, TraktTokenStore, token_record

log = logging.getLogger("web")

DEFAULT_DIST = Path(__file__).resolve().parent.parent / "web" / "dist"
TEST_TIMEOUT = aiohttp.ClientTimeout(total=10)


class LogBuffer(logging.Handler):
    """保存最近的日志，供页面展示"""

    def __init__(self, size: int = 500):
        super().__init__()
        self.records: deque[dict] = deque(maxlen=size)
        self.seq = 0

    def emit(self, record: logging.LogRecord) -> None:
        self.seq += 1
        self.records.append({
            "seq": self.seq,
            "time": record.created,
            "level": record.levelname,
            "name": record.name,
            "message": record.getMessage(),
        })

    def since(self, seq: int) -> list[dict]:
        return [r for r in self.records if r["seq"] > seq]


class Runner:
    """管理同步引擎的生命周期，配置变更后重启"""

    def __init__(self, http: aiohttp.ClientSession, config_path: Path):
        self.http = http
        self.config_path = config_path
        self.cfg: Optional[Config] = None
        self.engine: Optional[SyncEngine] = None
        self.store: Optional[StateStore] = None
        self.task: Optional[asyncio.Task] = None
        self.error: Optional[str] = None
        self.started_at: Optional[float] = None
        # 与配置文件放在同一挂载目录下
        self.trakt_tokens = TraktTokenStore(config_path.parent / "data" / "trakt_tokens.json")
        # 启动 / 停止 / 重启串行执行，避免并发保存配置时启动两个引擎
        self._lock = asyncio.Lock()

    async def start(self) -> None:
        async with self._lock:
            await self._start()

    async def stop(self) -> None:
        async with self._lock:
            await self._stop()

    async def restart(self) -> None:
        async with self._lock:
            await self._stop()
            await self._start()

    async def _start(self) -> None:
        self.error = None
        if not self.config_path.exists():
            log.info("配置文件 %s 不存在，请在 Web 页面中完成配置", self.config_path)
            return
        try:
            self.cfg = load_config(self.config_path)
        except Exception as e:
            self.error = f"配置无效: {e}"
            log.error(self.error)
            return
        # 初始化失败只记录错误，不能让进程退出：否则容器反复重启，也无法进入页面修改配置
        try:
            logging.getLogger().setLevel(self.cfg.log_level)
            self.store = StateStore(self.cfg.db_path)
            trakt = None
            if any(m.trakt_user for m in self.cfg.mappings):
                client = TraktClient(self.http, self.cfg.trakt, self.trakt_tokens)
                trakt = TraktSync(client, self.cfg.trakt, self.cfg.sync.dry_run)
            self.engine = SyncEngine(
                self.cfg,
                PlexClient(self.http, self.cfg.plex.url, self.cfg.plex.token),
                EmbyClient(self.http, self.cfg.emby.url, self.cfg.emby.api_key),
                self.store,
                trakt,
            )
        except Exception as e:
            self.error = f"同步引擎初始化失败: {e}"
            log.exception(self.error)
            await self._stop()
            return
        self.started_at = time.time()
        self.task = asyncio.create_task(self.engine.run())
        self.task.add_done_callback(self._on_done)

    def _on_done(self, task: asyncio.Task) -> None:
        if task.cancelled():
            return
        if exc := task.exception():
            self.error = f"同步引擎异常退出: {_describe(exc)}"
            log.error(self.error, exc_info=exc)

    async def _stop(self) -> None:
        if self.task and not self.task.done():
            self.task.cancel()
            try:
                await self.task
            except asyncio.CancelledError:
                pass
        self.task = None
        self.engine = None
        if self.store:
            self.store.close()
            self.store = None

    def status(self) -> dict:
        running = self.task is not None and not self.task.done()
        eng = self.engine
        return {
            "configured": self.config_path.exists(),
            "running": running,
            "error": self.error or (eng.setup_error if running and eng else None),
            "started_at": self.started_at if running else None,
            "dry_run": bool(self.cfg and self.cfg.sync.dry_run),
            "plex_ws": bool(running and eng and eng.plex.connected),
            "last_poll": eng.last_poll if running and eng else None,
            "poll_error": eng.poll_error if running and eng else None,
            "reconciling": bool(running and eng and eng.reconciling),
            "queue": eng.queue.qsize() if running and eng else 0,
            "pairs": [
                {
                    "id": p.id,
                    "plex_user": p.mapping.plex_user,
                    "emby_user": p.emby_user_name or p.mapping.emby_user,
                    "trakt_user": p.mapping.trakt_user,
                    "reconcile": eng.reconcile_stats.get(p.id),
                }
                for p in (eng.pairs if running and eng else [])
            ],
            "trakt": eng.trakt.status() if running and eng and eng.trakt else [],
            "trakt_next": eng.trakt_next if running and eng else {},
        }


def _basic_auth_middleware(password: str):
    @web.middleware
    async def middleware(request: web.Request, handler):
        header = request.headers.get("Authorization", "")
        if header.startswith("Basic "):
            try:
                _, _, given = base64.b64decode(header[6:]).decode().partition(":")
                if hmac.compare_digest(given, password):
                    return await handler(request)
            except Exception:
                pass
        return web.Response(status=401, headers={"WWW-Authenticate": 'Basic realm="mediaresume"'})

    return middleware


@web.middleware
async def _csrf_middleware(request: web.Request, handler):
    """写接口只接受 JSON 请求。跨站页面只能发出 text/plain / 表单这类“简单请求”（浏览器会带上缓存的登录凭据），
    要发 application/json 必须先通过 CORS 预检，而本服务不返回 CORS 头，因此跨站请求会被拦下"""
    if request.method not in ("GET", "HEAD", "OPTIONS") and request.path.startswith("/api/"):
        if request.content_type != "application/json":
            return web.json_response({"error": "请求的 Content-Type 必须是 application/json"}, status=415)
    return await handler(request)


def _allowed_hosts_middleware(hosts: set[str]):
    """只响应这些主机名的请求，防御 DNS rebinding（恶意域名解析到本服务后，以同源身份读取配置）"""

    @web.middleware
    async def middleware(request: web.Request, handler):
        if (request.url.host or "").lower() not in hosts:
            return web.Response(status=421, text="Host not allowed")
        return await handler(request)

    return middleware


def create_app(runner: Runner, logs: LogBuffer, dist: Path) -> web.Application:
    password = os.environ.get("MEDIARESUME_PASSWORD")
    middlewares = [_csrf_middleware]
    if password:
        middlewares.insert(0, _basic_auth_middleware(password))
    # 逗号分隔的主机名（不含端口），如 192.168.1.5,nas.lan；未设置时不限制
    if allowed := os.environ.get("MEDIARESUME_ALLOWED_HOSTS", "").strip():
        middlewares.insert(0, _allowed_hosts_middleware({h.strip().lower() for h in allowed.split(",") if h.strip()}))
    app = web.Application(middlewares=middlewares)
    routes = web.RouteTableDef()

    @routes.get("/api/config")
    async def get_config(_: web.Request) -> web.Response:
        if not runner.config_path.exists():
            return web.json_response({"exists": False, "config": None})
        try:
            cfg = load_config(runner.config_path)
        except Exception as e:
            # 校验不通过时仍把文件里的值回填到表单，只需改正出错的项
            try:
                form = config_form(yaml.safe_load(runner.config_path.read_text(encoding="utf-8")))
            except Exception:
                form = None
            return web.json_response({"exists": True, "config": form, "error": str(e)})
        return web.json_response({"exists": True, "config": config_to_dict(cfg)})

    @routes.put("/api/config")
    async def put_config(request: web.Request) -> web.Response:
        try:
            cfg = parse_config(await request.json())
        except ValueError as e:
            return web.json_response({"ok": False, "error": str(e)}, status=400)
        save_config(cfg, runner.config_path)
        log.info("配置已保存，重启同步引擎")
        await runner.restart()
        return web.json_response({"ok": True, "status": runner.status()})

    @routes.post("/api/test/plex")
    async def test_plex(request: web.Request) -> web.Response:
        body = await request.json()
        async with aiohttp.ClientSession(timeout=TEST_TIMEOUT) as http:
            client = PlexClient(http, str(body.get("url", "")).strip().rstrip("/"), str(body.get("token", "")).strip())
            try:
                info = await client.server_info()
                accounts = await client.accounts()
            except Exception as e:
                return web.json_response({"ok": False, "error": _err(e)})
        return web.json_response({"ok": True, **info, "accounts": accounts})

    @routes.post("/api/test/emby")
    async def test_emby(request: web.Request) -> web.Response:
        body = await request.json()
        async with aiohttp.ClientSession(timeout=TEST_TIMEOUT) as http:
            client = EmbyClient(http, str(body.get("url", "")).strip().rstrip("/"), str(body.get("api_key", "")).strip())
            try:
                info = await client.server_info()
                users = await client.users()
            except Exception as e:
                return web.json_response({"ok": False, "error": _err(e)})
        return web.json_response({"ok": True, **info, "users": users})

    # ---------- Trakt 设备码授权 ----------
    # 页面上的 Client ID / Secret 可能尚未保存，因此由请求携带；Secret 可选

    def _trakt_client(body: dict) -> TraktClient:
        cfg = TraktConfig(str(body.get("client_id") or "").strip(), str(body.get("client_secret") or "").strip())
        if not cfg.client_id:
            raise web.HTTPBadRequest(text='{"error": "请先填写 Trakt Client ID"}', content_type="application/json")
        return TraktClient(runner.http, cfg, runner.trakt_tokens)

    @routes.get("/api/trakt/accounts")
    async def trakt_accounts(_: web.Request) -> web.Response:
        return web.json_response({"accounts": runner.trakt_tokens.users()})

    @routes.post("/api/trakt/device-code")
    async def trakt_device_code(request: web.Request) -> web.Response:
        client = _trakt_client(await request.json())
        try:
            data = await client.device_code()
        except Exception as e:
            return web.json_response({"ok": False, "error": _err(e)})
        return web.json_response({"ok": True, **data})

    @routes.post("/api/trakt/device-token")
    async def trakt_device_token(request: web.Request) -> web.Response:
        body = await request.json()
        client = _trakt_client(body)
        try:
            status, data = await client.device_token(str(body.get("device_code") or ""))
            if status == 200:
                username = await client.username_of(data["access_token"])
                runner.trakt_tokens.set(username, token_record(data, client.cfg.client_id))
                log.info("Trakt 账户 %s 授权成功", username)
                if runner.engine and runner.engine.trakt:
                    runner.engine.trakt.reauthorized(username)
                return web.json_response({"status": "ok", "username": username})
        except Exception as e:
            return web.json_response({"status": "error", "error": _err(e)})
        result = {400: "pending", 429: "slow_down", 404: "invalid", 409: "used", 410: "expired", 418: "denied"}
        return web.json_response({"status": result.get(status, "error"), "error": f"HTTP {status}"})

    def _running_engine():
        if not runner.engine or not runner.task or runner.task.done():
            raise web.HTTPConflict(text='{"error": "同步引擎未运行"}', content_type="application/json")
        return runner.engine

    def _trakt_direction(body: dict) -> str:
        direction = body.get("direction")
        if direction not in ("to_trakt", "from_trakt"):
            raise web.HTTPBadRequest(text='{"error": "direction 必须是 to_trakt 或 from_trakt"}', content_type="application/json")
        return direction

    @routes.post("/api/trakt/sync/preview")
    async def trakt_sync_preview(request: web.Request) -> web.Response:
        direction = _trakt_direction(await request.json())
        eng = _running_engine()
        pairs = []
        for p in eng.pairs:
            if not p.mapping.trakt_user:
                continue
            try:
                pairs.append({**_pair_info(p), "plan": (await eng.trakt_plan(p, direction))[0]})
            except Exception as e:
                pairs.append({**_pair_info(p), "error": _err(e)})
        return web.json_response({"pairs": pairs, "dry_run": eng.cfg.sync.dry_run})

    @routes.post("/api/trakt/sync/execute")
    async def trakt_sync_execute(request: web.Request) -> web.Response:
        # 条目可能很多，计划生成后在后台执行，进度和结果看日志
        direction = _trakt_direction(await request.json())
        eng = _running_engine()
        pairs = []
        for p in eng.pairs:
            if not p.mapping.trakt_user:
                continue
            try:
                pairs.append({**_pair_info(p), "counts": await eng.trakt_execute(p, direction)})
            except Exception as e:
                pairs.append({**_pair_info(p), "error": _err(e)})
        return web.json_response({"pairs": pairs})

    @routes.delete("/api/trakt/accounts/{username}")
    async def trakt_delete_account(request: web.Request) -> web.Response:
        runner.trakt_tokens.delete(request.match_info["username"])
        return web.json_response({"ok": True})

    @routes.get("/api/status")
    async def status(_: web.Request) -> web.Response:
        return web.json_response(runner.status())

    @routes.post("/api/reconcile")
    async def reconcile(_: web.Request) -> web.Response:
        if not runner.engine or not runner.task or runner.task.done():
            return web.json_response({"ok": False, "error": "同步引擎未运行"}, status=409)
        runner.engine.request_reconcile()
        return web.json_response({"ok": True})

    def _resume_sync_args(body: dict) -> str:
        direction = body.get("direction")
        if direction not in ("plex_to_emby", "emby_to_plex"):
            raise web.HTTPBadRequest(text='{"error": "direction 必须是 plex_to_emby 或 emby_to_plex"}', content_type="application/json")
        if not runner.engine or not runner.task or runner.task.done():
            raise web.HTTPConflict(text='{"error": "同步引擎未运行"}', content_type="application/json")
        return "plex" if direction == "plex_to_emby" else "emby"

    def _pair_info(p) -> dict:
        return {"id": p.id, "plex_user": p.mapping.plex_user, "emby_user": p.emby_user_name or p.mapping.emby_user}

    @routes.post("/api/resume-sync/preview")
    async def resume_sync_preview(request: web.Request) -> web.Response:
        src = _resume_sync_args(await request.json())
        eng = runner.engine
        pairs = []
        for p in eng.pairs:
            try:
                pairs.append({**_pair_info(p), "plan": await eng.resume_sync_plan(p, src)})
            except Exception as e:
                pairs.append({**_pair_info(p), "error": _err(e)})
        return web.json_response({"pairs": pairs, "dry_run": eng.cfg.sync.dry_run})

    @routes.post("/api/resume-sync/execute")
    async def resume_sync_execute(request: web.Request) -> web.Response:
        src = _resume_sync_args(await request.json())
        eng = runner.engine
        pairs = []
        for p in eng.pairs:
            try:
                result = await asyncio.wait_for(eng.resume_sync_execute(p, src), timeout=300)
                pairs.append({**_pair_info(p), "result": result})
            except Exception as e:
                pairs.append({**_pair_info(p), "error": _err(e)})
        return web.json_response({"pairs": pairs})

    @routes.post("/api/restart")
    async def restart(_: web.Request) -> web.Response:
        await runner.restart()
        return web.json_response({"ok": True, "status": runner.status()})

    @routes.get("/api/logs")
    async def get_logs(request: web.Request) -> web.Response:
        try:
            after = int(request.query.get("after", 0))
        except ValueError:
            after = 0
        return web.json_response({"seq": logs.seq, "logs": logs.since(after)})

    app.add_routes(routes)

    if dist.is_dir():
        index = dist / "index.html"

        async def spa(request: web.Request) -> web.StreamResponse:
            rel = request.match_info.get("path", "")
            target = (dist / rel).resolve()
            if rel and target.is_file() and dist.resolve() in target.parents:
                return web.FileResponse(target)
            return web.FileResponse(index)

        app.router.add_get("/{path:.*}", spa)
    else:
        log.warning("未找到前端构建目录 %s，仅提供 API", dist)
    return app


def _describe(exc: BaseException) -> str:
    """TaskGroup 抛出的 ExceptionGroup 只有一句“unhandled errors in a TaskGroup”，展开成其中的原始异常"""
    if isinstance(exc, BaseExceptionGroup):
        return "; ".join(_describe(e) for e in exc.exceptions)
    return f"{exc.__class__.__name__}: {exc}" if str(exc) else exc.__class__.__name__


def _err(e: Exception) -> str:
    if isinstance(e, aiohttp.ClientResponseError):
        if e.status == 401:
            return "认证失败（401），请检查 token / API 密钥"
        return f"HTTP {e.status}: {e.message}"
    if isinstance(e, asyncio.TimeoutError):
        return "连接超时"
    return str(e) or e.__class__.__name__
