"""Collector WebSocket 会话 — state/event 分发 + LangGraph 流水线."""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING

from fastapi import WebSocket, WebSocketDisconnect

from graph.state import build_initial_state
from models.state import CoachEvent, GameState, WSMessage
from services import broadcast, events, lifecycle

if TYPE_CHECKING:
    from context import AppContext

logger = logging.getLogger(__name__)


class CollectorSession:
    """Collector WebSocket 会话：state/event 分发 + LangGraph 流水线。"""

    def __init__(self, websocket: WebSocket, ctx: AppContext):
        self.websocket = websocket
        self.ctx = ctx
        self.latest_state: GameState | None = None
        self._background_tasks: set[asyncio.Task] = set()
        self._urgent_slots = asyncio.Semaphore(2)

    async def run(self) -> None:
        ctx = self.ctx
        ctx.queue.set_handler(self.handle_coaching)
        try:
            while True:
                raw = await self.websocket.receive_text()
                msg = WSMessage.model_validate_json(raw)
                if msg.type == "state":
                    await self._on_state(msg)
                elif msg.type == "event":
                    await self._on_event(msg)
        except WebSocketDisconnect:
            logger.info("collector disconnected")
            await lifecycle.summarize_on_disconnect(ctx, self.latest_state)
            ctx.memory.user.top_of_mind.clear()
        except Exception:
            logger.exception("websocket error")
            ctx.memory_store.save("default", ctx.memory)
        finally:
            # 断连后不能让 queue 继续回调死会话（send 失败 + tips_published 虚增）。
            # == 比较绑定方法安全；用 is 会因每次生成新 bound method 对象而永假。
            if ctx.queue._handler == self.handle_coaching:
                ctx.queue.set_handler(None)

    async def handle_coaching(self, item: dict) -> None:
        ctx = self.ctx
        metrics = ctx.metrics
        event: CoachEvent = item["event"]
        snapshot: GameState | None = item.get("_snapshot") or self.latest_state
        initial = build_initial_state(
            event, snapshot, item.get("signals", []), item.get("priority", 1),
        )
        try:
            result = await ctx.coaching_graph.ainvoke(initial)
        except Exception:
            metrics["graph_errors"] += 1
            logger.exception("graph.ainvoke failed for %s", event.name)
            return

        tip = result.get("tip")
        if not tip:
            metrics["tips_skipped"] += 1
            logger.debug("tip skipped: %s (reason=%s)", event.name, result.get("skip_reason", "?"))
            return

        metrics["tips_published"] += 1
        ctx.engine.update_top_of_mind(event, snapshot)

        tip_json = WSMessage(type="tip", payload=tip).model_dump_json()
        try:
            await self.websocket.send_text(tip_json)
            logger.info("[%s] %s", tip["skill"], tip["message"][:80])
        except Exception:
            logger.warning("Send tip failed (connection closed)")

        await broadcast.broadcast_tip_json(ctx, tip_json)
        await broadcast.record_advice_context(ctx, tip, result, self.latest_state)

    async def _on_state(self, msg: WSMessage) -> None:
        ctx = self.ctx
        self.latest_state = GameState.model_validate(msg.payload)
        self.latest_state.sync_active_player()
        await ctx.redis_store.save_state("default", msg.payload)
        await broadcast.check_advice_feedback(ctx, msg.payload)
        events.update_memory_from_state(ctx, self.latest_state, msg.payload)
        zone = ctx.memory.user.context.get("current_zone", "")
        enemies = ctx.memory.user.context.get("enemy_zones", [])
        logger.debug("Player zone: %s | Enemies visible: %d", zone, len(enemies))

    async def _on_event(self, msg: WSMessage) -> None:
        ctx = self.ctx
        metrics = ctx.metrics
        event = CoachEvent.model_validate(msg.payload)

        # LCU 大厅事件：写入上下文供记忆注入，不进 coaching 流水线
        if events.handle_lcu_event(ctx, event):
            return

        metrics["events_received"] += 1
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
            })
            return

        metrics["events_queued"] += 1
        await ctx.queue.enqueue({
            "event": event,
            "signals": [],
            "priority": events.event_priority(event),
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
            return

        async def _limited() -> None:
            async with self._urgent_slots:
                await self.handle_coaching(item)

        task = asyncio.create_task(_limited())
        self._background_tasks.add(task)
        task.add_done_callback(self._background_tasks.discard)
