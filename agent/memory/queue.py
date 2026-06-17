"""防抖队列 — 聚合事件窗口，批量触发 coaching 生成."""

import asyncio
import logging
import time

logger = logging.getLogger(__name__)


class MemoryQueue:
    """时间窗口内聚合事件，按优先级排序，控制推送频率.

    window:       防抖窗口 (秒)，窗口结束后批量处理
    max_per_window: 每窗口最多推送的建议数
    skill_cooldown: 同一 skill 的最小间隔 (秒)
    """

    def __init__(
        self,
        window: float = 30.0,
        max_per_window: int = 3,
        skill_cooldown: float = 30.0,
    ):
        self.window = window
        self.max_per_window = max_per_window
        self.skill_cooldown = skill_cooldown

        self._pending: list[dict] = []
        self._last_skill_time: dict[str, float] = {}
        self._drain_task: asyncio.Task | None = None
        self._handler = None  # 回调：async fn(item) -> CoachingTip | None

    def set_handler(self, handler):
        """设置消费回调：接收队列排出的 item，返回 CoachingTip 或 None."""
        self._handler = handler

    async def enqueue(self, item: dict):
        event_name = item["event"].name
        now = time.time()

        # 同 skill 去重
        if event_name in self._last_skill_time:
            if now - self._last_skill_time[event_name] < self.skill_cooldown:
                return

        self._pending.append(item)
        self._last_skill_time[event_name] = now

        # 启动/重置防抖定时器
        if self._drain_task is None or self._drain_task.done():
            self._drain_task = asyncio.create_task(self._drain())

    async def _drain(self):
        """窗口到期后消费队列."""
        await asyncio.sleep(self.window)

        if not self._pending:
            return

        batch = list(self._pending)
        self._pending.clear()

        # 按优先级降序 + 截断
        batch.sort(key=lambda x: x.get("priority", 1), reverse=True)
        batch = batch[: self.max_per_window]

        for item in batch:
            if self._handler:
                try:
                    await self._handler(item)
                except Exception:
                    logger.exception("queue handler failed for event=%s", item["event"].name)

    @property
    def pending_count(self) -> int:
        return len(self._pending)
