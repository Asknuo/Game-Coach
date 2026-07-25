"""tip 广播与建议反馈闭环."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from fastapi import WebSocket

from models.state import GameState

if TYPE_CHECKING:
    from context import AppContext

logger = logging.getLogger(__name__)


async def broadcast_tip_json(ctx: "AppContext", tip_json: str) -> None:
    """向所有 overlay 客户端广播 tip，清理已断开的连接."""
    dead: list[WebSocket] = []
    for client in ctx.overlay_clients:
        try:
            await client.send_text(tip_json)
        except Exception:
            dead.append(client)
    for client in dead:
        ctx.overlay_clients.discard(client)


async def record_advice_context(
    ctx: "AppContext",
    tip: dict,
    result: dict,
    latest_state: GameState | None,
) -> None:
    """记录"已给建议"的上下文，供后续帧检查玩家是否采纳（反馈闭环）."""
    event_name = result.get("event_name", "")
    event_data = result.get("event_data", {})
    items = latest_state.active_player.items if latest_state else []
    item_count = len([it for it in items if it.item_id != 0])
    await ctx.redis_store.record_advice_given(
        "default",
        skill=tip["skill"],
        event_name=event_name,
        context={
            "health_pct": event_data.get("health_pct", 0) if event_name == "low_health" else 100,
            "item_count": item_count,
        },
    )


async def check_advice_feedback(ctx: "AppContext", payload: dict) -> None:
    """每帧 state 到达时检查建议反馈.

    状态机：followed → 加置信度；pending → 下帧继续观察；expired → 降置信度。
    """
    metrics = ctx.metrics
    status, skill, reason = await ctx.redis_store.check_advice_followed("default", payload)
    if status == "followed":
        metrics["advice_followed"] += 1
        new_conf = await ctx.redis_store.adjust_skill_confidence("default", skill, True)
        logger.info("Feedback: [%s] advice followed (%s) → conf %.2f", skill, reason, new_conf)
    elif status == "expired":
        metrics["advice_expired"] += 1
        new_conf = await ctx.redis_store.adjust_skill_confidence("default", skill, False)
        logger.info("Feedback: [%s] advice not followed (%s) → conf %.2f", skill, reason, new_conf)
