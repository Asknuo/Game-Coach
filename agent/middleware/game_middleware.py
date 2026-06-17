"""游戏事件拦截中间件 — DeerFlow 风格的 MemoryMiddleware 映射到游戏场景."""

import logging

from memory.event_processor import EventProcessor
from memory.queue import MemoryQueue
from models.state import CoachEvent, GameState, WSMessage

logger = logging.getLogger(__name__)


class GameMiddleware:
    """拦截 WebSocket 消息，路由到预处理层 → 队列。

    职责:
    - state 消息 → 实时更新用户上下文
    - event 消息 → 清洗 + 信号检测 → 入队
    """

    def __init__(self, processor: EventProcessor, queue: MemoryQueue):
        self.processor = processor
        self.queue = queue

    async def handle(self, msg: WSMessage, state: GameState | None) -> None:
        if msg.type == "state":
            game_state = GameState.model_validate(msg.payload)
            self.processor.update_context(game_state)
            return

        if msg.type == "event":
            event = CoachEvent.model_validate(msg.payload)
            processed = self.processor.process(event, state)
            if processed:
                logger.debug(
                    "enqueue event=%s signals=%s priority=%d",
                    event.name,
                    processed.get("signals", []),
                    processed.get("priority", 1),
                )
                await self.queue.enqueue(processed)
