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
    assert q.window == 6.0
    assert q.max_per_window == 2
    assert q.skill_cooldown == 25.0
    assert q.burst_flush_at == 3


@pytest.mark.asyncio
async def test_burst_flushes_before_window_expiry():
    """窗口内攒满 burst_flush_at 条 → 立即消费，不等窗口到期（团战场景）."""
    handled: list[str] = []

    async def handler(item: dict) -> None:
        handled.append(item["event"].name)

    q = MemoryQueue(window=10.0, max_per_window=3, skill_cooldown=0.0, burst_flush_at=3)
    q.set_handler(handler)
    for name in ("kill", "enemy_gold_lead", "teamfight_detected"):
        await q.enqueue(_item(name))

    # 0.5s 远小于 10s 窗口：若突发触发失效，此刻 handled 应为空
    await asyncio.sleep(0.5)
    assert handled == ["kill", "enemy_gold_lead", "teamfight_detected"]
    assert q.pending_count == 0


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


@pytest.mark.asyncio
async def test_handler_checked_before_batch_taken():
    """P1 回归：断连竞态下 drain 醒来发现无 handler 时，必须把批次留在
    pending——早先的实现先取批再判空，事件被静默吞掉且无任何计数."""
    q = MemoryQueue(window=0.05, skill_cooldown=0.0)
    handled: list[str] = []

    async def handler(item):
        handled.append(item["event"].name)

    q.set_handler(handler)
    await q.enqueue(_item("kill"))
    q.set_handler(None)  # 模拟 session 断连清 handler（窗口尚未到期）

    await asyncio.sleep(0.15)
    assert handled == []
    assert q.pending_count == 1  # 批次完好保留，未被吞


@pytest.mark.asyncio
async def test_max_per_window_excess_counted():
    """超窗口条数截断时必须计入 metrics.tips_dropped（此前静默丢弃）."""
    metrics: dict = {}
    q = MemoryQueue(window=0.05, max_per_window=2, skill_cooldown=0.0,
                    burst_flush_at=0, metrics=metrics)
    handled: list[str] = []

    async def handler(item):
        handled.append(item["event"].name)

    q.set_handler(handler)
    for name in ("kill", "gold_spike", "laning_check", "macro_check"):
        await q.enqueue(_item(name))

    await asyncio.sleep(0.15)
    assert len(handled) == 2
    assert metrics["tips_dropped"] == 2
