"""tip 广播与建议反馈闭环."""

from __future__ import annotations

import asyncio
import logging
import time
from typing import TYPE_CHECKING, Any

from models.state import GameState, WSMessage

if TYPE_CHECKING:
    from context import AppContext

logger = logging.getLogger(__name__)

# 单客户端发送超时：串行广播时一个挂死客户端会阻塞整个流式流水线
SEND_TIMEOUT = 2.0


async def broadcast_tip_json(ctx: AppContext, tip_json: str) -> None:
    """向所有 overlay 客户端广播 tip，清理已断开的连接.

    并发发送：串行 await 时最慢的客户端决定整条流水线的节奏
    （流式路径每 0.1s 一次 emitter，等不起 5s 的慢消费者）。
    """
    clients = list(ctx.overlay_clients)
    if not clients:
        return
    results = await asyncio.gather(
        *(asyncio.wait_for(c.send_text(tip_json), timeout=SEND_TIMEOUT) for c in clients),
        return_exceptions=True,
    )
    for client, res in zip(clients, results):
        if isinstance(res, Exception):
            logger.debug("overlay send failed (%s) — dropping client", res)
            ctx.overlay_clients.discard(client)


class PolishStreamBroadcaster:
    """流式润色增量的合并广播器.

    llm_polish 每 ~0.1s 产出一条累计文本；若直接 await 广播，LLM 迭代
    会被网络 IO 绑架（一个卡住的 overlay 就能让润色停摆数秒）。这里用
    「最新值覆盖 + 单 worker」把网络 IO 与生成彻底解耦：

    - emit() 只更新内存中的最新帧（O(1)，不碰 socket）
    - worker 每次醒来把待发帧一次性广播，多个 tip 的增量独立键控

    异常兜底：帧带单调时间戳，超过 PENDING_TTL 未被 worker 带走
    （worker 卡死/事件循环饱和）则丢弃，避免把过期文本推上屏。
    """

    PENDING_TTL = 10.0  # 孤儿帧过期时间（秒）

    def __init__(self, ctx: AppContext):
        self._ctx = ctx
        self._pending: dict[str, tuple[float, str]] = {}
        self._wake = asyncio.Event()
        self._task: asyncio.Task | None = None

    def start(self) -> None:
        if self._task is None or self._task.done():
            self._task = asyncio.create_task(self._run())

    async def stop(self) -> None:
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None
        self._pending.clear()

    async def emit(self, payload: dict[str, Any], tip_id: str) -> None:
        """记录某 tip 的最新累计帧（调用方不等待网络 IO）."""
        self._pending[tip_id] = (time.monotonic(), payload)
        self._wake.set()

    def discard(self, tip_id: str) -> None:
        """丢弃未发出的帧（流水线异常/超时后调用，防孤儿卡片）."""
        self._pending.pop(tip_id, None)

    async def _run(self) -> None:
        while True:
            await self._wake.wait()
            self._wake.clear()
            batch = self._pending
            self._pending = {}
            now = time.monotonic()
            for tip_id, (ts, payload) in batch.items():
                if now - ts > self.PENDING_TTL:
                    logger.debug("dropping stale stream frame for %s", tip_id)
                    continue
                try:
                    await broadcast_tip_json(
                        self._ctx,
                        WSMessage(type="tip_stream", payload=payload).model_dump_json(),
                    )
                except Exception:
                    logger.exception("polish stream broadcast failed")


async def record_advice_context(
    ctx: AppContext,
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


async def check_advice_feedback(ctx: AppContext, payload: dict[str, Any]) -> None:
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
