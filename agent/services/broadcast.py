"""tip 广播与建议反馈闭环."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

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
    tip: dict[str, Any],
    result: dict[str, Any],
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
            # 敌方威胁类：记录发出建议时的敌方状态，供后续判定 shut down / 威胁膨胀
            "enemy_name": event_data.get("enemy_name", ""),
            "enemy_deaths": event_data.get("deaths", 0),
            "enemy_kills": event_data.get("kills", 0) or event_data.get("enemy_kills", 0),
        },
    )


async def check_advice_feedback(ctx: "AppContext", payload: dict[str, Any]) -> None:
    """每帧 state 到达时检查建议反馈.

    状态机：followed → 加置信度；not_followed → 降置信度（仅明确违背时）；
    pending → 下帧继续观察；skipped → 中性跳过（不影响置信度，不误导学习信号）。
    """
    metrics = ctx.metrics
    status, skill, reason = await ctx.redis_store.check_advice_followed("default", payload)
    if status == "followed":
        metrics["advice_followed"] += 1
        new_conf = await ctx.redis_store.adjust_skill_confidence("default", skill, True)
        logger.info("Feedback: [%s] advice followed (%s) → conf %.2f", skill, reason, new_conf)
    elif status == "not_followed":
        metrics["advice_expired"] += 1
        new_conf = await ctx.redis_store.adjust_skill_confidence("default", skill, False)
        logger.info("Feedback: [%s] advice not followed (%s) → conf %.2f", skill, reason, new_conf)
    elif status == "skipped":
        logger.debug("Feedback: [%s] not measurable (%s) — confidence unchanged", skill, reason)
