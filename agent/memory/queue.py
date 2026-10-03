"""防抖队列 — 聚合事件窗口，批量触发 coaching 生成.

紧急事件（low_health / death，以及 ≤30s 的 dragon_soon / baron_soon，
判定见 services/events.py is_urgent）由会话层直接绕过本队列处理；
本队列只做防御性过滤（URGENT_EVENTS 无条件项）避免误入。

注意：窗口语义为「固定窗口 + 突发提前」— 从窗口内首个事件入队起计时，
到期后批量消费；窗口期间后续事件只入队、不延长窗口。唯一例外：事件攒满
burst_flush_at 条（团战爆发）时立即提前消费，突发的 tip 延迟从 window 降到接近 0。
"""

import asyncio
import logging
import time

logger = logging.getLogger(__name__)

# 紧急事件列表（不应入队）
URGENT_EVENTS = frozenset({"low_health", "death"})


class MemoryQueue:
    """时间窗口内聚合事件，按优先级排序，控制推送频率.

    window:         防抖窗口 (秒)，窗口结束后批量处理
    max_per_window: 每窗口最多推送的建议数
    skill_cooldown: 同一 skill 的最小间隔 (秒)（按映射后的 skill 粒度，
                    而非事件名 — item_purchased 与 gold_spike 同属 build）
    burst_flush_at: 窗口内事件攒到该条数立即提前消费（团战爆发）；
                    置 0 关闭突发触发
    """

    def __init__(
        self,
        window: float = 6.0,  # 6s ≈ 一波团战节奏；过长的窗口会让建议到时已过期
        max_per_window: int = 2,
        skill_cooldown: float = 25.0,
        burst_flush_at: int = 3,
    ):
        self.window = window
        self.max_per_window = max_per_window
        self.skill_cooldown = skill_cooldown
        self.burst_flush_at = burst_flush_at
        # 突发提前消费信号：drain 在窗口内等待此信号，攒批即醒（3.10+ 无需运行循环即可创建）
        self._flush_event = asyncio.Event()

        self._pending: list[dict] = []
        self._last_skill_time: dict[str, float] = {}
        self._drain_task: asyncio.Task | None = None
        self._handler = None  # 回调：async fn(item) -> None

    def set_handler(self, handler):
        """设置消费回调：接收队列排出的 item."""
        self._handler = handler

    @staticmethod
    def _cooldown_key(event_name: str) -> str:
        """冷却按 skill 粒度聚合：多个事件映射到同一 skill 时共享冷却."""
        from planner.planner import EVENT_TO_SKILL

        return EVENT_TO_SKILL.get(event_name, event_name)

    async def enqueue(self, item: dict):
        event_name = item["event"].name
        now = time.time()

        # ★ 防御性过滤：紧急事件不应入此队列
        if event_name in URGENT_EVENTS:
            logger.warning("queue: rejected urgent event %s (should bypass queue)", event_name)
            return

        # 同 skill 去重（不同事件、同一 skill 也互相冷却）
        cd_key = self._cooldown_key(event_name)
        if cd_key in self._last_skill_time:
            if now - self._last_skill_time[cd_key] < self.skill_cooldown:
                return

        self._pending.append(item)
        self._last_skill_time[cd_key] = now

        # 突发检测：窗口内事件攒满阈值 → 唤醒 drain 立即消费（不等窗口到期）
        if self.burst_flush_at and len(self._pending) >= self.burst_flush_at:
            self._flush_event.set()

        # 窗口已在计时则沿用（固定窗口语义）；否则启动新窗口
        if self._drain_task is None or self._drain_task.done():
            self._drain_task = asyncio.create_task(self._drain())

    async def _drain(self):
        """窗口到期或攒批信号触发时消费队列（同批事件并发处理，避免串行等待 LLM）.

        突发场景（团战：kill → gold_lead → tower 几秒内连续到达）不再干等
        6s 窗口：攒满 burst_flush_at 条立即消费。
        """
        # 可中断等待：等攒批信号，最多 window 秒
        burst = False
        try:
            await asyncio.wait_for(self._flush_event.wait(), timeout=self.window)
            burst = True
        except asyncio.TimeoutError:
            pass
        self._flush_event.clear()

        if not self._pending:
            return

        batch = list(self._pending)
        self._pending.clear()

        # 按优先级降序 + 截断
        batch.sort(key=lambda x: x.get("priority", 1), reverse=True)
        batch = batch[: self.max_per_window]

        # flush 诊断日志：突发/窗口触发 + 队内最长等待（session 埋点缺失时跳过）
        waits = [
            time.monotonic() - it["_ingest_ts"]
            for it in batch
            if it.get("_ingest_ts") is not None
        ]
        if waits:
            logger.info(
                "queue flush: %d events via %s, waited=%.2fs",
                len(batch), "burst" if burst else "window", max(waits),
            )

        if not self._handler:
            return

        results = await asyncio.gather(
            *(self._safe_handle(item) for item in batch),
            return_exceptions=True,
        )
        for item, res in zip(batch, results):
            if isinstance(res, Exception):
                logger.error("queue handler failed for event=%s: %s",
                             item["event"].name, res)

        # 消费期间（LLM 润色可达 1-3s）新入队的事件没有对应计时器，
        # 会滞留到下一条事件才被带走 — 补启一个 drain 收尾
        if self._pending:
            self._drain_task = asyncio.create_task(self._drain())

    async def _safe_handle(self, item: dict):
        try:
            await self._handler(item)
        except Exception:
            logger.exception("queue handler failed for event=%s", item["event"].name)

    @property
    def pending_count(self) -> int:
        return len(self._pending)
