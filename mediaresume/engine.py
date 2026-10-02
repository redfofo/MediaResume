from __future__ import annotations

import asyncio
import logging
import time
from collections import defaultdict
from dataclasses import dataclass, field, replace
from typing import Optional

from .config import Config, Mapping
from .emby import EmbyClient
from .models import POSITION_TOLERANCE_MS, MediaItem, MediaKey, WatchState, merge_states
from .plex import PlexClient
from .store import StateStore

log = logging.getLogger("engine")

# 定向查找确认“另一端没有”的条目，在此时间内不再查找（秒）；全量对账重建索引时也会清空
MISSING_TTL = 600


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

    @property
    def id(self) -> str:
        return f"{self.mapping.plex_user or '@owner'}|{self.emby_user_id}"

    def accepts_plex_user(self, username: Optional[str]) -> bool:
        if self.mapping.plex_user is None:
            return True
        return username is not None and username.lower() == self.mapping.plex_user.lower()


class SyncEngine:
    def __init__(self, cfg: Config, plex: PlexClient, emby: EmbyClient, store: StateStore):
        self.cfg = cfg
        self.plex = plex
        self.emby = emby
        self.store = store
        self.pairs: list[Pair] = []
        # 所有同步操作在单个 worker 中串行执行，避免竞态
        self.queue: asyncio.Queue[tuple] = asyncio.Queue()
        # (server, item_id, pair_id) -> 截止时间；在此之前忽略该端该条目的事件（防回环）
        self._echo: dict[tuple[str, str, str], float] = {}
        self._plex_sessions: dict[str, str] = {}
        self._plex_last_enqueue: dict[tuple[str, str], float] = defaultdict(float)
        # ratingKey -> 最近一次收到 playing 通知的时间
        self._plex_playing: dict[str, float] = {}
        # pair_id -> 最近一次对账结果，供 Web 状态页展示
        self.reconcile_stats: dict[str, dict] = {}
        self.reconciling = False
        self.last_poll: Optional[float] = None
        self.poll_error: Optional[str] = None

    async def setup(self) -> None:
        for m in self.cfg.mappings:
            user = await self.emby.resolve_user(m.emby_user)
            pair = Pair(m, m.plex_token or self.cfg.plex.token, user["id"], user["name"])
            self.pairs.append(pair)
            log.info("用户映射: Plex[%s] <-> Emby[%s] (%s)", m.plex_user or "所有者", user["name"], user["id"])

    async def run(self) -> None:
        await self.setup()
        now = time.time()
        for pair in self.pairs:
            pair.plex_since = pair.emby_since = now
        self.queue.put_nowait(("reconcile",))
        await asyncio.gather(
            self._worker(),
            self._reconcile_timer(),
            self._poll_loop(),
            self.plex.listen(self.on_plex_playing),
        )

    # ---------- 事件入口 ----------

    async def on_plex_playing(self, n: dict) -> None:
        session_key = str(n.get("sessionKey"))
        rating_key = str(n.get("ratingKey"))
        state = n.get("state")
        if session_key not in self._plex_sessions:
            try:
                self._plex_sessions.update(await self.plex.session_users())
            except Exception as e:
                log.debug("获取 Plex 会话失败: %s", e)
        username = self._plex_sessions.get(session_key)
        if state == "playing":
            self._plex_playing[rating_key] = time.monotonic()
        else:
            self._plex_playing.pop(rating_key, None)

        for pair in self.pairs:
            if not pair.accepts_plex_user(username):
                continue
            k = (pair.id, rating_key)
            now = time.monotonic()
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
        for src, prev, cur in (("plex", pair.plex_resume, plex_now), ("emby", pair.emby_resume, emby_now)):
            if prev is None:
                continue  # 首轮只建立基线
            for group in prev.keys() - cur.keys():
                self.queue.put_nowait(("resume_removed", pair, src, group, prev[group]))
        pair.plex_resume, pair.emby_resume = plex_now, emby_now

    async def _handle_resume_removed(self, pair: Pair, src: str, group: str, item_ids: list[str]) -> None:
        title = group
        for item_id in item_ids:
            item = await self._get_item(src, pair, item_id)
            if item is None:
                continue
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
        # 这是我们自己造成的移除，从快照中去掉，避免下一轮反向再同步一次
        snapshot = pair.plex_resume if dst == "plex" else pair.emby_resume
        if snapshot is not None:
            snapshot.pop(group, None)

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
                # 自己造成的移除，避免下一轮轮询再反向同步
                snapshot = pair.plex_resume if dst == "plex" else pair.emby_resume
                if snapshot is not None:
                    snapshot.pop(r["group"], None)
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
                    await self.reconcile_all()
                elif job[0] == "resume_removed":
                    await self._handle_resume_removed(*job[1:])
                elif job[0] == "resume_sync":
                    _, pair, src, fut = job
                    try:
                        fut.set_result(await self._resume_sync(pair, src))
                    except Exception as e:
                        fut.set_exception(e)
                        raise
                else:
                    await self._handle_event(*job)
            except Exception:
                log.exception("处理任务失败: %s", job[:1] + job[2:])

    async def _reconcile_timer(self) -> None:
        while True:
            await asyncio.sleep(self.cfg.sync.reconcile_interval)
            self.queue.put_nowait(("reconcile",))

    def _plex_playing_unstable(self, rating_key: str, state: WatchState) -> bool:
        """Plex 客户端续播时会先上报 0 秒再跳到断点，这期间读到的进度不可信"""
        seen = self._plex_playing.get(rating_key)
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
        if src == "plex" and self._plex_playing_unstable(item_id, src_item.state):
            log.debug("[%s] %s 正在播放且进度接近 0，可能是续播前的瞬时状态，忽略", pair.id, src_item.title)
            return
        # 一个文件包含多集时，对每一集分别同步
        for key in (src_item.key, *src_item.alt_keys):
            await self._sync_key(src, pair, replace(src_item, key=key, alt_keys=()))

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
        for key in plex_groups.keys() & emby_groups.keys():
            p_items, e_items = plex_groups[key], emby_groups[key]
            p, e = merge_states(p_items), merge_states(e_items)
            base = self.store.get(pair.id, key)
            target = decide(p, e, base)
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
                        await self.emby.mark_played(pair.emby_user_id, item.item_id)
                    elif op == "unplayed":
                        await self.emby.mark_unplayed(pair.emby_user_id, item.item_id)
                    else:
                        await self.emby.set_position(pair.emby_user_id, item.item_id, target.position_ms)
        self._prune_echo()

    def _prune_echo(self) -> None:
        now = time.monotonic()
        for k in [k for k, v in self._echo.items() if v <= now]:
            del self._echo[k]


def _fmt(s: WatchState) -> str:
    if s.played:
        return "[已看]"
    m, sec = divmod(s.position_ms // 1000, 60)
    h, m = divmod(m, 60)
    return f"[进度 {h:d}:{m:02d}:{sec:02d}]"
