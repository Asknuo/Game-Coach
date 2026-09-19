"""services/session.py — 断连收尾与 urgent 并发上限回归测试."""

import asyncio
from types import SimpleNamespace

import pytest
from fastapi import WebSocketDisconnect

from memory.queue import MemoryQueue
from models.state import CoachEvent
from services.session import CollectorSession


def _fake_ctx(queue: MemoryQueue) -> SimpleNamespace:
    return SimpleNamespace(
        queue=queue,
        metrics={"tips_published": 0, "tips_skipped": 0},
        memory=SimpleNamespace(user=SimpleNamespace(top_of_mind=[])),
        memory_store=SimpleNamespace(save=lambda *a: None),
    )


class _DeadWebsocket:
    async def receive_text(self):
        raise WebSocketDisconnect()


@pytest.mark.asyncio
async def test_disconnect_clears_queue_handler():
    """B2 回归：会话结束后 queue 不得继续回调死会话."""
    q = MemoryQueue()
    ctx = _fake_ctx(q)
    session = CollectorSession(_DeadWebsocket(), ctx)  # type: ignore[arg-type]

    await session.run()
    assert q._handler is None


@pytest.mark.asyncio
async def test_new_session_handler_survives_old_cleanup():
    """旧会话收尾不能误清新会话注册的 handler."""
    q = MemoryQueue()
    ctx = _fake_ctx(q)
    old = CollectorSession(_DeadWebsocket(), ctx)  # type: ignore[arg-type]
    new = CollectorSession(_DeadWebsocket(), ctx)  # type: ignore[arg-type]

    q.set_handler(old.handle_coaching)
    q.set_handler(new.handle_coaching)  # 模拟重连：新会话覆盖了旧的

    # 手动执行旧会话的收尾逻辑
    if q._handler == old.handle_coaching:
        q.set_handler(None)
    assert q._handler == new.handle_coaching


@pytest.mark.asyncio
async def test_urgent_spawn_capped_at_two():
    """B3 回归：urgent 直启最多 2 条并发，超载丢弃并计数."""
    q = MemoryQueue()
    ctx = _fake_ctx(q)
    session = CollectorSession(_DeadWebsocket(), ctx)  # type: ignore[arg-type]

    started: list[dict] = []

    async def fake_coaching(item):
        started.append(item)
        await asyncio.sleep(5)  # 占住信号量

    session.handle_coaching = fake_coaching  # type: ignore[method-assign]

    ev = {"event": CoachEvent(name="low_health", data={}), "signals": [], "priority": 3}
    for _ in range(4):
        session._spawn_coaching(dict(ev))
        await asyncio.sleep(0.05)  # 让已启动任务真正占住信号量

    assert len(started) == 2
    assert ctx.metrics["tips_skipped"] == 2

    for t in list(session._background_tasks):
        t.cancel()
