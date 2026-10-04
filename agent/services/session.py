"""Collector WebSocket 会话 — state/event 分发 + LangGraph 流水线."""

from __future__ import annotations

import asyncio
import logging
import time
from typing import TYPE_CHECKING

from fastapi import WebSocket, WebSocketDisconnect
from starlette.websockets import WebSocketState

from graph.state import build_initial_state
from models.state import CoachEvent, GameState, WSMessage
from services import broadcast, events, lifecycle

if TYPE_CHECKING:
    from context import AppContext

logger = logging.getLogger(__name__)

# 单条流水线的端到端预算：LLM 内部 3 次尝试最坏 ~63s，超过预算即放弃——
# overlay 是瞬时提示，迟到的建议不如不发，且槽位/连接不能被长期占死
PIPELINE_TIMEOUT_S = 30.0


class CollectorSession:
    """Collector WebSocket 会话：state/event 分发 + 流水线."""

    def __init__(self, websocket: WebSocket, ctx: AppContext):
        self.websocket = websocket
        self.ctx = ctx
        self.latest_state: GameState | None = None
        self._background_tasks: set[asyncio.Task] = set()
        self._urgent_slots = asyncio.Semaphore(2)
        # 正常对局结束的信号：只有见过 game_end，断连才等价于"打完了一局"。
        # 网络抖动/collector 重启造成的断连也必须走这条，但此时没有
        # game_end → 只做轻量清理，绝不生成复盘或写入对局记录
        self._saw_game_end = False
        # 本会话是否已产出过 review tip（game_end 流水线与断连复盘去重）
        self._review_published = False

    async def run(self) -> None:
        ctx = self.ctx
        metrics = ctx.metrics
        ctx.queue.set_handler(self.handle_coaching)
        try:
            while True:
                raw = await self.websocket.receive_text()
                try:
                    msg = WSMessage.model_validate_json(raw)
                except Exception:
                    # 单条畸形帧不该杀死会话：collector 协议演进/截断的 JSON
                    # 帧此前会直接杀掉会话（等价于人为断连），丢帧即可
                    metrics["frames_malformed"] = metrics.get("frames_malformed", 0) + 1
                    logger.warning("malformed frame dropped: %.80s", raw)
                    continue
                if msg.type == "state":
                    await self._on_state(msg)
                elif msg.type == "event":
                    await self._on_event(msg)
        except WebSocketDisconnect:
            logger.info("collector disconnected (game_end=%s)", self._saw_game_end)
            if self._saw_game_end:
                await lifecycle.summarize_on_disconnect(
                    ctx, self.latest_state, skip_review=self._review_published)
            else:
                # 异常断连：无复盘、无对局记录（一次网络抖动不该产出脏数据）
                ctx.metrics["session_aborts"] = ctx.metrics.get("session_aborts", 0) + 1
                logger.info("collector 异常断连（未见 game_end）— 跳过复盘与对局记录")
            ctx.memory.user.top_of_mind.clear()
        except Exception:
            logger.exception("websocket error")
            ctx.memory_store.save("default", ctx.memory)
        finally:
            # urgent 流水线可能还在跑（入队→LLM 润色 1-3s 窗口）：任其完成
            # 会向已断会话广播 tip、往 Redis 写反馈上下文污染下一局。
            # 必须先取消再清 handler，否则取消前到达的批次仍会回调死会话
            for task in list(self._background_tasks):
                task.cancel()
            if self._background_tasks:
                await asyncio.gather(*self._background_tasks, return_exceptions=True)
            self._background_tasks.clear()
            if ctx.queue._handler == self.handle_coaching:
                ctx.queue.set_handler(None)

    def _ws_closed(self) -> bool:
        return getattr(self.websocket, "client_state", None) == WebSocketState.DISCONNECTED

    async def handle_coaching(self, item: dict) -> None:
        ctx = self.ctx
        metrics = ctx.metrics
        event: CoachEvent = item["event"]
        snapshot: GameState | None = item.get("_snapshot") or self.latest_state
        initial = build_initial_state(
            event, snapshot, item.get("signals", []), item.get("priority", 1),
        )
        ingest_ts = item.get("_ingest_ts")  # _on_event 打点，无则跳过埋点
        graph_t0 = time.monotonic()
        try:
            result = await asyncio.wait_for(
                ctx.coaching_graph.ainvoke(initial), timeout=PIPELINE_TIMEOUT_S)
        except asyncio.TimeoutError:
            metrics["graph_errors"] += 1
            logger.warning("pipeline timeout (%.0fs) for %s — dropping tip",
                           PIPELINE_TIMEOUT_S, event.name)
            return
        except Exception:
            metrics["graph_errors"] += 1
            logger.exception("graph.ainvoke failed for %s", event.name)
            return

        tip = result.get("tip")
        if not tip:
            reason = result.get("skip_reason", "unknown")
            metrics["tips_skipped"] += 1
            metrics[f"skipped_{_skip_bucket(reason)}"] = (
                metrics.get(f"skipped_{_skip_bucket(reason)}", 0) + 1)
            logger.debug("tip skipped: %s (reason=%s)", event.name, reason)
            return

        metrics["tips_published"] += 1
        ctx.engine.update_top_of_mind(event, snapshot)
        if tip.get("skill") == "review":
            self._review_published = True

        tip_json = WSMessage(type="tip", payload=tip).model_dump_json()
        if not self._ws_closed():
            try:
                await self.websocket.send_text(tip_json)
                logger.info("[%s] %s", tip["skill"], tip["message"][:80])
            except Exception:
                logger.warning("Send tip failed (connection closed)")

        await broadcast.broadcast_tip_json(ctx, tip_json)

        # ── 延迟埋点：事件进入 agent（_on_event 打点）→ overlay 收到 tip ──
        # total 拆成 queue（防抖窗口 + 调度）与 pipeline（图执行，含 RAG 与 LLM 润色）
        if ingest_ts is not None:
            pipeline = time.monotonic() - graph_t0
            total = time.monotonic() - ingest_ts
            metrics["latency_total_s"] = metrics.get("latency_total_s", 0.0) + total
            metrics["latency_pipeline_s"] = metrics.get("latency_pipeline_s", 0.0) + pipeline
            logger.info(
                "tip latency: %s total=%.2fs (queue=%.2fs, pipeline=%.2fs)",
                event.name, total, total - pipeline, pipeline,
            )

        await broadcast.record_advice_context(ctx, tip, result, self.latest_state)

    async def _on_state(self, msg: WSMessage) -> None:
        ctx = self.ctx
        try:
            state = GameState.model_validate(msg.payload)
        except Exception:
            ctx.metrics["frames_malformed"] = ctx.metrics.get("frames_malformed", 0) + 1
            logger.warning("malformed state payload dropped")
            return
        self.latest_state = state
        self.latest_state.sync_active_player()
        # 两路 Redis 操作并发：串行时最慢的一个决定 WS 循环节奏
        await asyncio.gather(
            ctx.redis_store.save_state("default", msg.payload),
            broadcast.check_advice_feedback(ctx, msg.payload),
        )
        events.update_memory_from_state(ctx, self.latest_state, msg.payload)
        zone = ctx.memory.user.context.get("current_zone", "")
        enemies = ctx.memory.user.context.get("enemy_zones", [])
        logger.debug("Player zone: %s | Enemies visible: %d", zone, len(enemies))

    async def _on_event(self, msg: WSMessage) -> None:
        ctx = self.ctx
        metrics = ctx.metrics
        try:
            event = CoachEvent.model_validate(msg.payload)
        except Exception:
            metrics["frames_malformed"] = metrics.get("frames_malformed", 0) + 1
            logger.warning("malformed event payload dropped")
            return

        # LCU 大厅事件：写入上下文供记忆注入，不进 coaching 流水线
        if await events.handle_lcu_event(ctx, event):
            return

        metrics["events_received"] += 1
        if event.name == "game_end":
            # LCU 的 EndOfGame 信号：断连时据此区分"打完一局"与"网络抖动"
            self._saw_game_end = True
        if events.should_skip_dead_event(self.latest_state, event):
            return

        if events.is_urgent(event):
            metrics["events_urgent"] += 1
            logger.info("URGENT event: %s — bypassing queue", event.name)
            self._spawn_coaching({
                "event": event,
                "signals": [],
                "priority": 3,
                "_snapshot": self.latest_state,
                "_ingest_ts": time.monotonic(),
            })
            return

        metrics["events_queued"] += 1
        await ctx.queue.enqueue({
            "event": event,
            "signals": [],
            "priority": events.event_priority(event),
            "_ingest_ts": time.monotonic(),
        })

    def _spawn_coaching(self, item: dict) -> None:
        """urgent 事件直启流水线，最多 2 条并发，超载直接丢弃.

        overlay 是瞬时提示不是消息队列，宁可丢也不让紧急事件风暴
        拉起任意多个 LLM 请求。
        """
        if self._urgent_slots.locked():
            logger.warning(
                "urgent coaching at capacity — dropping %s", item["event"].name,
            )
            self.ctx.metrics["tips_skipped"] += 1
            self.ctx.metrics["urgent_dropped"] = (
                self.ctx.metrics.get("urgent_dropped", 0) + 1)
            return

        async def _limited() -> None:
            async with self._urgent_slots:
                await self.handle_coaching(item)

        task = asyncio.create_task(_limited())
        self._background_tasks.add(task)
        task.add_done_callback(self._background_tasks.discard)


def _skip_bucket(reason: str) -> str:
    """skip_reason → metrics 计数键（duplicate/low_confidence/stale 分维度，
    其余归入 other，避免键空间无限膨胀）."""
    if reason == "duplicate":
        return "duplicate"
    if reason == "low_confidence":
        return "low_confidence"
    if reason in ("hp_recovered", "objective_gone", "items_gone"):
        return "stale"
    return "other"
