from __future__ import annotations

import asyncio
import logging
import time
from collections import defaultdict
from dataclasses import dataclass, field, replace
from typing import Any, Optional

from .config import Config, Mapping
from .emby import EmbyClient
from .models import POSITION_TOLERANCE_MS, MediaItem, MediaKey, WatchState, merge_states
from .plex import PlexClient
from .store import StateStore
from .trakt import PROGRESS_MIN, PROGRESS_MAX, PROGRESS_STEP, PushPlan, TraktSync, percent_of

log = logging.getLogger("engine")

# 定向查找确认“另一端没有”的条目，在此时间内不再查找（秒）；全量对账重建索引时也会清空
MISSING_TTL = 600
# 一轮中继续观看列表少了大半（且至少这么多项）时，视为服务器异常而非手动移除
RESUME_BULK_MIN = 3
# 引擎初始化失败（如 Emby 尚未启动）的重试间隔（秒）
SETUP_RETRY_MIN, SETUP_RETRY_MAX = 10, 300
# 播放中 Plex 约每 10 秒推送一次 playing 通知；超过此时间（秒）没收到，视为已不在播放（可能漏了 stopped）
PLAYING_STALE = 120


def decide(plex: WatchState, emby: WatchState, base: Optional[WatchState]) -> Optional[WatchState]:
    """三方比对，返回双方应达到的状态；None 表示已一致无需处理"""
    if plex.same_as(emby):
        return None
    if base is not None:
        if plex.same_as(base):
            return emby  # 只有 Emby 变了
        if emby.same_as(base):
            return plex  # 只有 Plex 变了
        # 双方都变了：最后播放时间新的一方胜出
        return plex if plex.last_played >= emby.last_played else emby
    # 首次见到（无基准）：已看优先，否则取进度更大的一方，避免清掉已有进度
    if plex.played != emby.played:
        return plex if plex.played else emby
    return plex if plex.position_ms >= emby.position_ms else emby


@dataclass
class Pair:
    mapping: Mapping
    plex_token: str
    emby_user_id: str
    emby_user_name: str = ""
    plex_index: dict[MediaKey, list[str]] = field(default_factory=dict)
    emby_index: dict[MediaKey, list[str]] = field(default_factory=dict)
    # 另一端确认没有的条目：(server, key) -> 过期时间
    missing: dict[tuple[str, MediaKey], float] = field(default_factory=dict)
    # 增量轮询水位（unix 秒）
    plex_since: float = 0
    emby_since: float = 0
    unwatch_polled_at: float = 0
    # Plex 未看检测：上一轮快照，以及两轮之间增量轮询看到有活动的电影/剧集
    plex_watched_movies: Optional[set[str]] = None
    plex_show_counts: Optional[dict[str, int]] = None
    plex_active_movies: set[str] = field(default_factory=set)
    plex_active_shows: set[str] = field(default_factory=set)
    # 上一轮的继续观看列表：分组(movie:tmdb:X / show:tmdb:X) -> 条目 id
    plex_resume: Optional[dict[str, list[str]]] = None
    emby_resume: Optional[dict[str, list[str]]] = None
    # 上一轮刚从继续观看中消失的分组，下一轮仍不在才确认为移除：服务器 -> 分组 -> 条目 id
    resume_gone: dict[str, dict[str, list[str]]] = field(default_factory=lambda: {"plex": {}, "emby": {}})
    # 最近一次得到的统一状态：MediaKey -> (状态, 标题, 片长)，供 Trakt 全量同步生成计划
    unified: dict[MediaKey, tuple[WatchState, str, int]] = field(default_factory=dict)

    @property
    def id(self) -> str:
        return f"{self.mapping.plex_user or '@owner'}|{self.emby_user_id}"

    def accepts_plex_user(self, user_id: Optional[str], username: Optional[str]) -> bool:
        if self.mapping.plex_user is None:
            # 所有者映射读取的是所有者的观看状态，只认所有者（账户 id 1）的会话；取不到会话信息时放行
            return user_id is None or user_id == "1"
        return username is not None and username.lower() == self.mapping.plex_user.lower()


class SyncEngine:
    def __init__(
        self, cfg: Config, plex: PlexClient, emby: EmbyClient, store: StateStore, trakt: Optional[TraktSync] = None
    ):
        self.cfg = cfg
        self.plex = plex
        self.emby = emby
        self.store = store
        # 未绑定 Trakt 账户时为 None
        self.trakt = trakt
        self._background: set[asyncio.Task] = set()
        self.pairs: list[Pair] = []
        # 所有同步操作在单个 worker 中串行执行，避免竞态
        self.queue: asyncio.Queue[tuple] = asyncio.Queue()
        # (server, item_id, pair_id) -> 截止时间；在此之前忽略该端该条目的事件（防回环）
        self._echo: dict[tuple[str, str, str], float] = {}
        # sessionKey -> (Plex 账户 id, 用户名)
        self._plex_sessions: dict[str, tuple[str, str]] = {}
        self._plex_last_enqueue: dict[tuple[str, str], float] = defaultdict(float)
        # (pair_id, ratingKey) -> 该映射的 Plex 用户最近一次收到 playing 通知的时间
        self._plex_playing: dict[tuple[str, str], float] = {}
        # 队列中已有一个待执行的对账，不重复入队
        self._reconcile_queued = False
        # pair_id -> 最近一次对账结果，供 Web 状态页展示
        self.reconcile_stats: dict[str, dict] = {}
        self.reconciling = False
        # Trakt 定时全量同步的下次执行时间（unix 秒）：方向 -> 时间
        self.trakt_next: dict[str, float] = {}
        self.last_poll: Optional[float] = None
        self.poll_error: Optional[str] = None
        self.setup_error: Optional[str] = None

    async def setup(self) -> None:
        self.pairs = []
        for m in self.cfg.mappings:
            user = await self.emby.resolve_user(m.emby_user)
            pair = Pair(m, m.plex_token or self.cfg.plex.token, user["id"], user["name"])
            self.pairs.append(pair)
            log.info("用户映射: Plex[%s] <-> Emby[%s] (%s)", m.plex_user or "所有者", user["name"], user["id"])
            if self.trakt and m.trakt_user:
                self.trakt.add_account(m.trakt_user)
                log.info("用户映射: Plex[%s] / Emby[%s] -> Trakt[%s]", m.plex_user or "所有者", user["name"], m.trakt_user)

    async def _setup_until_ready(self) -> None:
        """容器开机时 Emby 可能还没启动：初始化失败时退避重试，而不是让引擎退出"""
        delay = SETUP_RETRY_MIN
        while True:
            try:
                await self.setup()
                self.setup_error = None
                return
            except Exception as e:
                self.setup_error = f"初始化失败，{delay}s 后重试: {str(e) or e.__class__.__name__}"
                log.warning("%s", self.setup_error)
            await asyncio.sleep(delay)
            delay = min(delay * 2, SETUP_RETRY_MAX)

    async def run(self) -> None:
        await self._setup_until_ready()
        now = time.time()
        for pair in self.pairs:
            pair.plex_since = pair.emby_since = now
        self.request_reconcile()
        tasks = [
            self._worker(),
            self._reconcile_timer(),
            self._poll_loop(),
            self.plex.listen(self.on_plex_playing, self._on_plex_connect),
        ]
        if self.trakt and self.trakt.accounts:
            tasks.append(self.trakt.run())
            for direction, hours in (("to_trakt", self.cfg.trakt.push_interval), ("from_trakt", self.cfg.trakt.pull_interval)):
                if hours > 0:
                    tasks.append(self._trakt_timer(direction, hours * 3600))
        try:
            await asyncio.gather(*tasks)
        finally:
            # 引擎重启时一并停止进行中的 Trakt 全量推送
            for task in self._background:
                task.cancel()

    # ---------- 事件入口 ----------

    def _on_plex_connect(self) -> None:
        """断线期间可能漏掉 stopped 通知：清空播放状态，仍在播放的会话很快会再次推送 playing"""
        self._plex_playing.clear()
        self._plex_sessions.clear()
        self._plex_last_enqueue.clear()

    def _is_playing(self, pair: Pair, rating_key: str) -> bool:
        seen = self._plex_playing.get((pair.id, rating_key))
        if seen is None:
            return False
        if time.monotonic() - seen > PLAYING_STALE:
            del self._plex_playing[(pair.id, rating_key)]
            return False
        return True

    async def on_plex_playing(self, n: dict) -> None:
        session_key = str(n.get("sessionKey"))
        rating_key = str(n.get("ratingKey"))
        state = n.get("state")
        if session_key not in self._plex_sessions:
            try:
                self._plex_sessions.update(await self.plex.session_users())
            except Exception as e:
                log.debug("获取 Plex 会话失败: %s", e)
        user_id, username = self._plex_sessions.get(session_key, (None, None))

        for pair in self.pairs:
            if not pair.accepts_plex_user(user_id, username):
                continue
            k = (pair.id, rating_key)
            now = time.monotonic()
            if state == "playing":
                self._plex_playing[k] = now
            else:
                self._plex_playing.pop(k, None)
            if state in ("paused", "stopped"):
                # 等 Plex 落盘进度后再读取
                self._plex_last_enqueue[k] = now
                asyncio.get_running_loop().call_later(3, self.queue.put_nowait, ("plex", pair, rating_key))
            elif state == "playing" and now - self._plex_last_enqueue[k] >= self.cfg.sync.progress_interval:
                self._plex_last_enqueue[k] = now
                self.queue.put_nowait(("plex", pair, rating_key))
        if state == "stopped":
            self._plex_sessions.pop(session_key, None)
            for pair in self.pairs:
                self._plex_last_enqueue.pop((pair.id, rating_key), None)

    # ---------- 增量轮询 ----------
    # Emby 的 WebSocket 用 API Key 连接时收不到 UserDataChanged，Plex 的 WebSocket 不推送标记已看/未看，
    # 因此用轻量轮询发现这些变化，再交给 worker 按单条事件处理。

    async def _poll_loop(self) -> None:
        while True:
            await asyncio.sleep(self.cfg.sync.poll_interval)
            try:
                for pair in self.pairs:
                    await self._poll(pair)
                self.poll_error = None
            except Exception as e:
                self.poll_error = str(e) or e.__class__.__name__
                log.warning("增量轮询失败: %s", self.poll_error)
            self.last_poll = time.time()

    async def _poll(self, pair: Pair) -> None:
        now = time.time()
        # 留 5 秒余量，重复入队无害（处理是幂等的）
        for item_id in await self.emby.changed_since(pair.emby_user_id, pair.emby_since - 5):
            self.queue.put_nowait(("emby", pair, item_id))
        pair.emby_since = now
        for rk, show_rk in await self.plex.recently_viewed(int(pair.plex_since) - 5, pair.plex_token):
            self.queue.put_nowait(("plex", pair, rk))
            if show_rk:
                pair.plex_active_shows.add(show_rk)
            else:
                pair.plex_active_movies.add(rk)
        pair.plex_since = now
        await self._poll_resume(pair)
        if now - pair.unwatch_polled_at >= self.cfg.sync.unwatch_poll_interval:
            await self._poll_plex_unwatched(pair)
            pair.unwatch_polled_at = now

    async def _poll_plex_unwatched(self, pair: Pair) -> None:
        """Plex 标记未看会清空 lastViewedAt，无法按时间增量发现。以下两类逐条核对：
        1. 已看数量比上一轮快照少的电影/剧集；
        2. 两轮之间被增量轮询看到有活动的（覆盖“标记已看后又标记未看”，此时数量可能不变）。
        核对时 Plex 未看、而本地记录为已同步“已看”的条目，交给 worker 同步。"""
        movies = await self.plex.watched_movies(pair.plex_token)
        counts = await self.plex.show_viewed_counts(pair.plex_token)
        movie_candidates = pair.plex_active_movies - movies
        show_candidates = set(pair.plex_active_shows)
        if pair.plex_watched_movies is not None and pair.plex_show_counts is not None:
            movie_candidates |= pair.plex_watched_movies - movies
            show_candidates |= {rk for rk, n in counts.items() if n < pair.plex_show_counts.get(rk, n)}
        pair.plex_watched_movies, pair.plex_show_counts = movies, counts
        pair.plex_active_movies, pair.plex_active_shows = set(), set()

        for rk in movie_candidates:
            self.queue.put_nowait(("plex", pair, rk))
        for show_rk in show_candidates:
            for ep in await self.plex.show_episodes(show_rk, pair.plex_token):
                base = self.store.get(pair.id, ep.key)
                if not ep.state.played and base is not None and base.played:
                    self.queue.put_nowait(("plex", pair, ep.item_id))

    # ---------- 从继续观看移除 ----------
    # 移除不会改变观看状态，只能通过对比两轮之间的继续观看列表发现：
    # 某部电影/剧整个从列表中消失、且不是因为看完，就视为被手动移除，同步到另一端。

    async def _resume_entries(self, server: str, pair: Pair) -> list[dict]:
        if server == "plex":
            return await self.plex.continue_watching(pair.plex_token)
        return await self.emby.resume(pair.emby_user_id)

    async def _resume_groups(self, server: str, pair: Pair) -> dict[str, list[str]]:
        entries = await self._resume_entries(server, pair)
        groups: dict[str, list[str]] = defaultdict(list)
        for e in entries:
            groups[e["group"]].append(e["id"])
        return dict(groups)

    async def _poll_resume(self, pair: Pair) -> None:
        plex_now = await self._resume_groups("plex", pair)
        emby_now = await self._resume_groups("emby", pair)
        for src, cur in (("plex", plex_now), ("emby", emby_now)):
            prev = pair.plex_resume if src == "plex" else pair.emby_resume
            if prev is not None:  # 首轮只建立基线
                gone = {g: prev[g] for g in prev.keys() - cur.keys()}
                if len(gone) >= RESUME_BULK_MIN and len(gone) * 2 > len(prev):
                    # 多半是服务器重启或媒体库暂不可用时返回了不完整的列表：保留上一轮快照，本轮不处理
                    log.warning(
                        "[%s] %s 的继续观看一次少了 %d/%d 项，疑似服务器异常，本轮忽略（批量移除请使用手动同步继续观看）",
                        pair.id, src, len(gone), len(prev),
                    )
                    pair.resume_gone[src] = {}
                    continue
                # 连续两轮都不在列表中才确认移除，避免列表短暂抖动造成误删
                for group, ids in pair.resume_gone[src].items():
                    if group not in cur:
                        self.queue.put_nowait(("resume_removed", pair, src, group, ids))
                pair.resume_gone[src] = gone
            if src == "plex":
                pair.plex_resume = cur
            else:
                pair.emby_resume = cur

    @staticmethod
    def _forget_resume(pair: Pair, server: str, group: str) -> None:
        """我们自己造成的移除：从快照中去掉，避免下一轮反向再同步一次"""
        snapshot = pair.plex_resume if server == "plex" else pair.emby_resume
        if snapshot is not None:
            snapshot.pop(group, None)
        pair.resume_gone[server].pop(group, None)

    async def _handle_resume_removed(self, pair: Pair, src: str, group: str, item_ids: list[str]) -> None:
        title = group
        for item_id in item_ids:
            item = await self._get_item(src, pair, item_id)
            if item is None:
                # 读取失败或条目已删除（如洗版后换了 id），无法确认是用户手动移除
                log.info("[%s] %s 离开 %s 的继续观看，但无法读取条目 %s，跳过", pair.id, group, src, item_id)
                return
            title = item.title or title
            if item.state.played:
                log.debug("[%s] %s 因看完离开继续观看，忽略", pair.id, title)
                return
        dst = "emby" if src == "plex" else "plex"
        targets = (await self._resume_groups(dst, pair)).get(group, [])
        if not targets:
            log.debug("[%s] %s 不在 %s 的继续观看中，无需处理", pair.id, group, dst)
            return
        log.info("[%s] %s → %s: 从继续观看移除 %s (%s)", pair.id, src, dst, title, group)
        if self.cfg.sync.dry_run:
            log.info("[dry-run] %s 从继续观看移除 %s", dst, targets)
            return
        for item_id in targets:
            if dst == "plex":
                await self.plex.remove_from_continue_watching(item_id, pair.plex_token)
            else:
                await self.emby.hide_from_resume(pair.emby_user_id, item_id)
        self._forget_resume(pair, dst, group)

    # ---------- 手动同步继续观看列表（一次性任务） ----------
    # 以整部电影/剧为单位，让目标端的继续观看尽量与源端一致：
    #   目标有、源没有 -> 从目标移除；
    #   源有、目标没有 -> 源端看到一半的写入进度（目标为 Emby 时同时取消隐藏），
    #                     只是“下一集”推荐的：目标为 Emby 时取消隐藏，目标为 Plex 时无接口可加入。

    async def resume_sync_plan(self, pair: Pair, src: str) -> dict:
        dst = "emby" if src == "plex" else "plex"
        src_entries = await self._resume_entries(src, pair)
        dst_entries = await self._resume_entries(dst, pair)
        src_groups = {e["group"]: e for e in src_entries}
        dst_groups: dict[str, list[dict]] = defaultdict(list)
        for e in dst_entries:
            dst_groups[e["group"]].append(e)

        remove = [
            {"group": g, "title": es[0]["title"], "ids": [e["id"] for e in es]}
            for g, es in dst_groups.items()
            if g not in src_groups
        ]
        add, unsupported = [], []
        for g, e in src_groups.items():
            if g in dst_groups:
                continue
            item = await self._get_item(src, pair, e["id"])
            if item is None:
                unsupported.append({"group": g, "title": e["title"], "reason": f"读取 {src} 条目失败"})
                continue
            dst_ids = await self._lookup(dst, pair, item.key)
            entry = {"group": g, "title": e["title"], "episode": item.title, "key": str(item.key), "ids": dst_ids}
            if not dst_ids:
                unsupported.append({**entry, "reason": f"{dst} 中没有对应条目"})
            elif not item.state.played and item.state.position_ms > 0:
                add.append({**entry, "action": "progress", "position_ms": item.state.position_ms})
            elif dst == "emby":
                add.append({**entry, "action": "unhide"})
            else:
                unsupported.append({**entry, "reason": "Plex 没有接口可以手动加入“下一集”推荐"})
        return {
            "src": src,
            "dst": dst,
            "remove": remove,
            "add": add,
            "unsupported": unsupported,
            "same": len(src_groups.keys() & dst_groups.keys()),
        }

    async def resume_sync_execute(self, pair: Pair, src: str) -> dict:
        """在 worker 中串行执行，避免与实时同步交错"""
        fut: asyncio.Future = asyncio.get_running_loop().create_future()
        self.queue.put_nowait(("resume_sync", pair, src, fut))
        return await fut

    async def _resume_sync(self, pair: Pair, src: str) -> dict:
        plan = await self.resume_sync_plan(pair, src)
        dst = plan["dst"]
        errors: list[str] = []
        done = {"removed": 0, "added": 0}
        log.info(
            "[%s] 手动同步继续观看 %s → %s：移除 %d 项，补充 %d 项，无法补充 %d 项",
            pair.id, src, dst, len(plan["remove"]), len(plan["add"]), len(plan["unsupported"]),
        )
        if self.cfg.sync.dry_run:
            log.info("[dry-run] 未实际执行")
            return {**plan, "dry_run": True, "errors": [], **done, "dst_count": None}

        for r in plan["remove"]:
            try:
                for item_id in r["ids"]:
                    if dst == "plex":
                        await self.plex.remove_from_continue_watching(item_id, pair.plex_token)
                    else:
                        await self.emby.hide_from_resume(pair.emby_user_id, item_id)
                done["removed"] += 1
                self._forget_resume(pair, dst, r["group"])
            except Exception as e:
                errors.append(f"移除 {r['title']} 失败: {e}")

        for a in plan["add"]:
            try:
                if a["action"] == "progress":
                    dst_items = [i for i in [await self._get_item(dst, pair, d) for d in a["ids"]] if i]
                    target = WatchState(False, a["position_ms"], int(time.time()))
                    await self._apply(dst, pair, dst_items, target)
                    if dst_items:
                        self.store.set(pair.id, dst_items[0].key, target)
                if dst == "emby":
                    for item_id in a["ids"]:
                        await self.emby.hide_from_resume(pair.emby_user_id, item_id, hide=False)
                done["added"] += 1
            except Exception as e:
                errors.append(f"补充 {a['title']} 失败: {e}")

        dst_count = len(await self._resume_entries(dst, pair))
        log.info("[%s] 手动同步继续观看完成：移除 %d 项，补充 %d 项，失败 %d 项，%s 现有 %d 项",
                 pair.id, done["removed"], done["added"], len(errors), dst, dst_count)
        return {**plan, "dry_run": False, "errors": errors, **done, "dst_count": dst_count}

    # ---------- worker ----------

    async def _worker(self) -> None:
        while True:
            job = await self.queue.get()
            try:
                if job[0] == "reconcile":
                    self._reconcile_queued = False
                    await self.reconcile_all()
                elif job[0] == "resume_removed":
                    await self._handle_resume_removed(*job[1:])
                elif job[0] == "resume_sync":
                    await self._run_resume_sync(*job[1:])
                elif job[0] == "trakt_pull":
                    await self._trakt_pull(*job[1:])
                else:
                    await self._handle_event(*job)
            except Exception:
                log.exception("处理任务失败: %s", job[:1] + job[2:])

    async def _run_resume_sync(self, pair: Pair, src: str, fut: asyncio.Future) -> None:
        # 页面请求可能已超时（future 被取消），此时结果无人接收，不能再设置
        try:
            result = await self._resume_sync(pair, src)
        except asyncio.CancelledError:
            if not fut.done():
                fut.set_exception(RuntimeError("同步引擎已停止，任务未完成"))
            raise
        except Exception as e:
            if not fut.done():
                fut.set_exception(e)
            raise
        if not fut.done():
            fut.set_result(result)

    async def _reconcile_timer(self) -> None:
        while True:
            await asyncio.sleep(self.cfg.sync.reconcile_interval)
            self.request_reconcile()

    def request_reconcile(self) -> bool:
        """对账入队；队列里已有待执行的对账时不重复入队（对账耗时超过间隔时避免越积越多）"""
        if self._reconcile_queued:
            return False
        self._reconcile_queued = True
        self.queue.put_nowait(("reconcile",))
        return True

    def _plex_playing_unstable(self, pair: Pair, rating_key: str, state: WatchState) -> bool:
        """Plex 客户端续播时会先上报 0 秒再跳到断点，这期间读到的进度不可信"""
        seen = self._plex_playing.get((pair.id, rating_key))
        if seen is None or time.monotonic() - seen > 60:
            return False
        return not state.played and state.position_ms < 30_000

    def _echo_guarded(self, server: str, item_id: str, pair: Pair) -> bool:
        until = self._echo.get((server, item_id, pair.id))
        return until is not None and until > time.monotonic()

    async def _handle_event(self, src: str, pair: Pair, item_id: str) -> None:
        if self._echo_guarded(src, item_id, pair):
            log.debug("忽略回环事件 %s:%s", src, item_id)
            return

        src_item = await self._get_item(src, pair, item_id)
        if src_item is None:
            return
        if src == "plex" and self._plex_playing_unstable(pair, item_id, src_item.state):
            log.debug("[%s] %s 正在播放且进度接近 0，可能是续播前的瞬时状态，忽略", pair.id, src_item.title)
            return
        playing = src == "plex" and self._is_playing(pair, item_id)
        # 一个文件包含多集时，对每一集分别同步
        for key in (src_item.key, *src_item.alt_keys):
            await self._sync_key(src, pair, replace(src_item, key=key, alt_keys=()))
            # Plex 正在播放时，Emby 侧的变化是我们同步过去的，不能当成“暂停”推给 Trakt
            live = src == "plex" or not any(self._is_playing(pair, i) for i in pair.plex_index.get(key, []))
            self._observe(pair, key, src_item.state, src_item.title, src_item.duration_ms, playing, live)

    async def _sync_key(self, src: str, pair: Pair, src_item: MediaItem) -> None:
        dst = "emby" if src == "plex" else "plex"
        dst_ids = await self._lookup(dst, pair, src_item.key)
        if not dst_ids:
            log.debug("[%s] %s 在 %s 中无对应条目，跳过", pair.id, src_item.key, dst)
            return
        dst_items = [i for i in [await self._get_item(dst, pair, d) for d in dst_ids] if i]
        if not dst_items:
            return

        new = src_item.state
        base = self.store.get(pair.id, src_item.key)
        if new.same_as(merge_states(dst_items)):
            self.store.set(pair.id, src_item.key, new)
            return
        if new.same_as(base):
            # 源端相对基准没变化而目标端不同，交给对账处理
            return
        log.info("[%s] %s → %s: %s %s", pair.id, src, dst, src_item.title or src_item.key, _fmt(new))
        await self._apply(dst, pair, dst_items, new)
        if not self.cfg.sync.dry_run:
            self.store.set(pair.id, src_item.key, new)

    # ---------- 对账 ----------

    async def reconcile_all(self) -> None:
        self.reconciling = True
        try:
            for pair in self.pairs:
                try:
                    await self._reconcile(pair)
                except Exception as e:
                    log.exception("[%s] 对账失败", pair.id)
                    self.reconcile_stats[pair.id] = {"at": time.time(), "error": str(e)}
        finally:
            self.reconciling = False

    async def _reconcile(self, pair: Pair) -> None:
        started = time.monotonic()
        plex_groups = await self._index("plex", pair)
        emby_groups = await self._index("emby", pair)
        changed = 0
        # 只在一端存在的条目不参与双向同步，但它们的状态同样是用户的观看记录
        for key in plex_groups.keys() ^ emby_groups.keys():
            items = plex_groups.get(key) or emby_groups[key]
            self._observe(pair, key, merge_states(items), items[0].title, _duration(items))
        for key in plex_groups.keys() & emby_groups.keys():
            p_items, e_items = plex_groups[key], emby_groups[key]
            p, e = merge_states(p_items), merge_states(e_items)
            base = self.store.get(pair.id, key)
            target = decide(p, e, base)
            self._observe(pair, key, target or p, p_items[0].title, _duration(p_items + e_items))
            if target is None:
                if not p.same_as(base):
                    self.store.set(pair.id, key, p)
                continue
            title = p_items[0].title or str(key)
            if not target.same_as(p):
                log.info("[%s] 对账 emby → plex: %s %s", pair.id, title, _fmt(target))
                await self._apply("plex", pair, p_items, target)
            if not target.same_as(e):
                log.info("[%s] 对账 plex → emby: %s %s", pair.id, title, _fmt(target))
                await self._apply("emby", pair, e_items, target)
            if not self.cfg.sync.dry_run:
                self.store.set(pair.id, key, target)
            changed += 1
        stats = {
            "at": time.time(),
            "plex": len(plex_groups),
            "emby": len(emby_groups),
            "matched": len(plex_groups.keys() & emby_groups.keys()),
            "changed": changed,
            "seconds": round(time.monotonic() - started, 1),
        }
        self.reconcile_stats[pair.id] = stats
        log.info(
            "[%s] 对账完成：Plex %d 项，Emby %d 项，匹配 %d 项，同步 %d 项，用时 %.1fs",
            pair.id, stats["plex"], stats["emby"], stats["matched"], changed, stats["seconds"],
        )

    def _observe(
        self,
        pair: Pair,
        key: MediaKey,
        state: WatchState,
        title: str = "",
        duration_ms: int = 0,
        playing: bool = False,
        live: bool = False,
    ) -> None:
        """记录统一状态供 Trakt 全量同步使用；实时事件中的状态推送到 Trakt"""
        if not self.trakt or not pair.mapping.trakt_user:
            return
        pair.unified[key] = (state, title, duration_ms)
        if live:
            self.trakt.observe_live(pair.mapping.trakt_user, key, state, duration_ms, title, playing)

    # ---------- Trakt 全量同步（手动） ----------

    async def _items_for(self, pair: Pair, key: MediaKey) -> tuple[list[MediaItem], list[MediaItem]]:
        """两端对应条目；只查对账建立的索引，不做定向查找（条目可能很多）"""
        out = []
        for server in ("plex", "emby"):
            index = pair.plex_index if server == "plex" else pair.emby_index
            out.append([i for i in [await self._get_item(server, pair, d) for d in index.get(key, [])] if i])
        return out[0], out[1]

    async def trakt_plan(self, pair: Pair, direction: str) -> tuple[dict, Any]:
        """基于最近一次对账的统一状态与 Trakt 当前数据生成同步计划，返回 (页面展示用, 执行用)。
        to_trakt：本地已看而 Trakt 没有的补观看记录；本地进度写入 Trakt；本地已看完的清除 Trakt 进度。
        from_trakt：Trakt 上新增（之前拉取时没见过）的观看记录、而本地未看的标记已看；
        Trakt 进度更新且差距明显的写入本地。都不会改为未看。"""
        user = pair.mapping.trakt_user
        if not self.trakt or not user:
            raise ValueError("该映射未绑定 Trakt 账户")
        if not pair.unified:
            raise ValueError("请等待首次对账完成")
        watched = await self.trakt.client.watched(user)
        playback = await self.trakt.client.playback(user)

        def entry(key: MediaKey, **extra) -> dict:
            return {"key": str(key), "title": pair.unified[key][1] or str(key), **extra}

        if direction == "to_trakt":
            plan = PushPlan()
            for key, (state, _, duration) in pair.unified.items():
                pb = playback.get(key)
                if state.played:
                    if key not in watched:
                        plan.history[key] = state.last_played
                    if pb:
                        plan.clear[key] = pb.id
                    continue
                pct = percent_of(state, duration)
                if PROGRESS_MIN <= pct < PROGRESS_MAX and (pb is None or abs(pb.progress - pct) >= 2 * PROGRESS_STEP):
                    plan.progress[key] = pct
            show = {
                "history": [entry(k, watched_at=ts) for k, ts in plan.history.items()],
                "progress": [
                    entry(k, local_pct=pct, trakt_pct=playback[k].progress if k in playback else None)
                    for k, pct in plan.progress.items()
                ],
                "clear": [entry(k, trakt_pct=playback[k].progress) for k in plan.clear],
            }
        elif direction == "from_trakt":
            # 之前拉取时已见过、且之后没有新观看的记录不再拉取：本地在那之后标记的未看（如准备重看）要保留
            seen = self.store.trakt_seen(pair.id)
            pull_watched: dict[MediaKey, int] = {}
            kept_unwatched = 0
            for k, ts in watched.items():
                if k not in pair.unified or pair.unified[k][0].played:
                    continue
                if str(k) in seen and ts <= seen[str(k)]:
                    kept_unwatched += 1
                    continue
                pull_watched[k] = ts
            pull_progress: dict[MediaKey, tuple[float, int]] = {}
            skipped = 0
            for key, pb in playback.items():
                if key not in pair.unified or key in pull_watched:
                    continue
                state, _, duration = pair.unified[key]
                if state.played or duration <= 0 or not _progress_differs(state, duration, pb.progress):
                    continue
                if pb.paused_at <= state.last_played:
                    skipped += 1  # 本地更新
                    continue
                pull_progress[key] = (pb.progress, pb.paused_at)
            plan = (pull_watched, pull_progress, watched)
            show = {
                "watched": [entry(k, watched_at=ts) for k, ts in pull_watched.items()],
                "progress": [
                    entry(k, local_pct=percent_of(pair.unified[k][0], pair.unified[k][2]), trakt_pct=pct)
                    for k, (pct, _) in pull_progress.items()
                ],
                "skipped_newer": skipped,
                "kept_unwatched": kept_unwatched,
            }
        else:
            raise ValueError(f"未知的同步方向: {direction}")
        for items in show.values():
            if isinstance(items, list):
                items.sort(key=lambda i: i["title"])
        return {"direction": direction, "trakt_user": user, **show}, plan

    async def trakt_execute(self, pair: Pair, direction: str) -> dict:
        """推送在后台任务中执行（只写 Trakt，不占用 worker）；拉回交给 worker 串行写入 Plex / Emby"""
        user = pair.mapping.trakt_user
        if self.trakt and user and self.trakt.accounts[user].busy:
            raise ValueError(f"Trakt[{user}] 已有全量同步在执行")
        show, plan = await self.trakt_plan(pair, direction)
        if direction == "to_trakt":
            task = asyncio.create_task(self.trakt.push_full(user, plan))
            self._background.add(task)
            task.add_done_callback(self._background.discard)
        else:
            self.queue.put_nowait(("trakt_pull", pair, *plan))
        return {k: len(v) if isinstance(v, list) else v for k, v in show.items()}

    async def _trakt_timer(self, direction: str, interval: float) -> None:
        while True:
            self.trakt_next[direction] = time.time() + interval
            await asyncio.sleep(interval)
            await self.trakt_scheduled(direction)

    async def trakt_scheduled(self, direction: str) -> None:
        """定时全量同步：与页面手动执行相同；尚未完成首次对账或已有全量同步在执行时跳过本轮"""
        name = "全量同步到 Trakt" if direction == "to_trakt" else "从 Trakt 全量同步"
        for pair in self.pairs:
            if not pair.mapping.trakt_user:
                continue
            if not pair.unified:
                log.info("[%s] 定时%s跳过：尚未完成首次对账", pair.id, name)
                continue
            log.info("[%s] 定时%s", pair.id, name)
            try:
                await self.trakt_execute(pair, direction)
            except Exception as e:
                log.warning("[%s] 定时%s失败: %s", pair.id, name, e)

    async def _trakt_pull(
        self,
        pair: Pair,
        watched: dict[MediaKey, int],
        progress: dict[MediaKey, tuple[float, int]],
        seen: Optional[dict[MediaKey, int]] = None,
    ) -> None:
        """seen：生成计划时 Trakt 上的全部观看记录，执行后记为已见，之后只拉取新增的观看"""
        log.info(
            "[%s] 从 Trakt[%s] 全量同步开始：标记已看 %d 条，写入进度 %d 条%s",
            pair.id, pair.mapping.trakt_user, len(watched), len(progress), "（dry-run）" if self.cfg.sync.dry_run else "",
        )
        n_watched = n_progress = 0
        for key, watched_at in watched.items():
            p_items, e_items = await self._items_for(pair, key)
            if all(i.state.played for i in p_items + e_items):
                continue  # 包括两端都没有
            target = WatchState(True, 0, watched_at)
            log.info("[%s] trakt → plex/emby: %s %s", pair.id, (p_items + e_items)[0].title or key, _fmt(target))
            await self._apply("plex", pair, p_items, target)
            await self._apply("emby", pair, e_items, target)
            if not self.cfg.sync.dry_run:
                self.store.set(pair.id, key, target)
            n_watched += 1
        acc = self.trakt.accounts.get(pair.mapping.trakt_user)
        for key, (pct, paused_at) in progress.items():
            # 执行时重新读取，跳过计划生成后本地又有变化的条目
            p_items, e_items = await self._items_for(pair, key)
            items = p_items + e_items
            duration = _duration(items)
            if not items or duration <= 0:
                continue
            cur = merge_states(items)
            if cur.played or paused_at <= cur.last_played or not _progress_differs(cur, duration, pct):
                continue
            target = WatchState(False, int(duration * pct / 100), paused_at)
            log.info("[%s] trakt → plex/emby: %s %s", pair.id, items[0].title or key, _fmt(target))
            await self._apply("plex", pair, p_items, target)
            await self._apply("emby", pair, e_items, target)
            if not self.cfg.sync.dry_run:
                self.store.set(pair.id, key, target)
            if acc:
                # 写入后两端的回传事件不应再把同样的进度推回 Trakt
                acc.progress_sent[key] = (pct, False)
            n_progress += 1
        if seen and not self.cfg.sync.dry_run:
            self.store.set_trakt_seen(pair.id, seen)
        log.info("[%s] 从 Trakt 全量同步完成：标记已看 %d 条，写入进度 %d 条", pair.id, n_watched, n_progress)

    # ---------- 服务器操作封装 ----------

    async def _index(self, server: str, pair: Pair) -> dict[MediaKey, list[MediaItem]]:
        if server == "plex":
            items = await self.plex.list_items(pair.plex_token)
        else:
            items = await self.emby.list_items(pair.emby_user_id)
        groups: dict[MediaKey, list[MediaItem]] = defaultdict(list)
        for i in items:
            groups[i.key].append(i)
        index = {k: [i.item_id for i in v] for k, v in groups.items()}
        pair.missing = {k: v for k, v in pair.missing.items() if k[0] != server}
        if server == "plex":
            pair.plex_index = index
        else:
            pair.emby_index = index
        return groups

    async def _lookup(self, server: str, pair: Pair, key: MediaKey) -> list[str]:
        index = pair.plex_index if server == "plex" else pair.emby_index
        if key in index:
            return index[key]
        now = time.monotonic()
        if pair.missing.get((server, key), 0) > now:
            return []
        # 可能是新入库的媒体：只按 tmdb 定向查找这一部，不全量重建索引
        if server == "plex":
            ids = await self.plex.find(key, pair.plex_token)
        else:
            ids = await self.emby.find(pair.emby_user_id, key)
        if ids:
            log.info("[%s] 在 %s 中找到新条目 %s", pair.id, server, key)
            index[key] = ids
        else:
            log.info("[%s] %s 中没有 %s，%d 分钟内不再查找", pair.id, server, key, MISSING_TTL // 60)
            pair.missing[(server, key)] = now + MISSING_TTL
        return ids

    async def _get_item(self, server: str, pair: Pair, item_id: str) -> Optional[MediaItem]:
        try:
            if server == "plex":
                return await self.plex.get_item(item_id, pair.plex_token)
            return await self.emby.get_item(pair.emby_user_id, item_id)
        except Exception as e:
            log.warning("读取 %s 条目 %s 失败: %s", server, item_id, e)
            return None

    async def _apply(self, server: str, pair: Pair, items: list[MediaItem], target: WatchState) -> None:
        for item in items:
            cur = item.state
            ops: list[str] = []
            if target.played and not cur.played:
                ops.append("played")
            elif not target.played:
                if cur.played:
                    ops.append("unplayed")
                    cur = WatchState(False, 0)
                if abs(cur.position_ms - target.position_ms) >= POSITION_TOLERANCE_MS:
                    ops.append("position")
            if not ops:
                continue
            if self.cfg.sync.dry_run:
                log.info("[dry-run] %s %s %s", server, item.item_id, ops)
                continue
            self._echo[(server, item.item_id, pair.id)] = time.monotonic() + self.cfg.sync.echo_window
            for op in ops:
                if server == "plex":
                    if op == "played":
                        await self.plex.mark_played(item.item_id, pair.plex_token)
                    elif op == "unplayed":
                        await self.plex.mark_unplayed(item.item_id, pair.plex_token)
                    else:
                        await self.plex.set_position(item.item_id, target.position_ms, pair.plex_token)
                else:
                    if op == "played":
                        await self.emby.mark_played(pair.emby_user_id, item.item_id, target.last_played)
                    elif op == "unplayed":
                        await self.emby.mark_unplayed(pair.emby_user_id, item.item_id)
                    else:
                        await self.emby.set_position(pair.emby_user_id, item.item_id, target.position_ms, target.last_played)
        self._prune_echo()

    def _prune_echo(self) -> None:
        now = time.monotonic()
        for k in [k for k, v in self._echo.items() if v <= now]:
            del self._echo[k]


def _progress_differs(state: WatchState, duration_ms: int, pct: float) -> bool:
    """本地进度与 Trakt 百分比的差距超过容差（Trakt 只有百分比，至少按 2% 片长计）"""
    return abs(int(duration_ms * pct / 100) - state.position_ms) >= max(POSITION_TOLERANCE_MS, duration_ms // 50)


def _duration(items: list[MediaItem]) -> int:
    return max((i.duration_ms for i in items), default=0)


def _fmt(s: WatchState) -> str:
    if s.played:
        return "[已看]"
    m, sec = divmod(s.position_ms // 1000, 60)
    h, m = divmod(m, 60)
    return f"[进度 {h:d}:{m:02d}:{sec:02d}]"
