"""memory/queue.py 防抖队列测试."""

import asyncio

import pytest

from memory.queue import MemoryQueue
from models.state import CoachEvent


def _item(name: str, priority: int = 1) -> dict:
    return {"event": CoachEvent(name=name, data={}), "signals": [], "priority": priority}


def test_defaults_match_documented_debounce():
    """默认参数与 README 防抖表 / context.py 实际注入值一致（B6）."""
    q = MemoryQueue()
    assert q.window == 15.0
    assert q.max_per_window == 2
    assert q.skill_cooldown == 25.0


@pytest.mark.asyncio
async def test_urgent_event_rejected():
    """紧急事件不应入队（由 app 层绕过队列直接处理）."""
    q = MemoryQueue(window=0.05)
    q.set_handler(lambda item: None)
    await q.enqueue(_item("low_health"))
    await q.enqueue(_item("death"))
    assert q.pending_count == 0


@pytest.mark.asyncio
async def test_cooldown_is_skill_granular():
    """item_purchased 与 item_sold 同映射 build skill → 应互相冷却."""
    q = MemoryQueue(window=0.05, skill_cooldown=60.0)
    handled: list[dict] = []

    async def handler(item):
        handled.append(item)

    q.set_handler(handler)
    await q.enqueue(_item("item_purchased"))
    await q.enqueue(_item("item_sold"))  # 同 skill (build)，应被冷却拦截
    assert q.pending_count == 1

    await asyncio.sleep(0.15)
    assert len(handled) == 1
    assert handled[0]["event"].name == "item_purchased"


@pytest.mark.asyncio
async def test_window_batches_and_sorts_by_priority():
    """窗口内事件聚合，到期按优先级降序消费."""
    q = MemoryQueue(window=0.05, max_per_window=3, skill_cooldown=0.0)
    handled: list[dict] = []

    async def handler(item):
        handled.append(item)

    q.set_handler(handler)
    await q.enqueue(_item("laning_check", priority=1))
    await q.enqueue(_item("dragon_soon", priority=2))
    await q.enqueue(_item("macro_check", priority=1))

    await asyncio.sleep(0.15)
    names = [it["event"].name for it in handled]
    assert names[0] == "dragon_soon"  # 优先级最高者先处理
    assert len(handled) == 3


@pytest.mark.asyncio
async def test_batch_processed_concurrently():
    """同批事件并发处理，总耗时应接近单个事件而非累加."""
    q = MemoryQueue(window=0.05, max_per_window=3, skill_cooldown=0.0)
    done_at: list[float] = []

    async def handler(item):
        await asyncio.sleep(0.1)
        done_at.append(asyncio.get_event_loop().time())

    q.set_handler(handler)
    for name in ("laning_check", "macro_check", "kill"):
        await q.enqueue(_item(name))

    start = asyncio.get_event_loop().time()
    await asyncio.sleep(0.3)
    assert len(done_at) == 3
    # 并发执行：三个 0.1s 的任务总窗口应远小于 0.3s 串行
    assert max(done_at) - start < 0.25
