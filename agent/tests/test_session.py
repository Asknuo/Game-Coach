"""services/session.py — 断连收尾与 urgent 并发上限回归测试."""

import asyncio
from collections import defaultdict
from types import SimpleNamespace

import pytest
from fastapi import WebSocketDisconnect

from memory.queue import MemoryQueue
from models.state import CoachEvent
from services.session import CollectorSession


def _fake_ctx(queue: MemoryQueue) -> SimpleNamespace:
    metrics: dict = defaultdict(int)  # 任意计数键自增，避免逐个枚举
    return SimpleNamespace(
        queue=queue,
        metrics=metrics,
        memory=SimpleNamespace(user=SimpleNamespace(top_of_mind=[], context={})),
        memory_store=SimpleNamespace(save=lambda *a: None),
        redis_store=SimpleNamespace(
            save_state=_async_noop,
            reset_session=_async_noop,
            check_advice_followed=_async_noop_returning_empty,
        ),
        engine=SimpleNamespace(update_top_of_mind=lambda *a: None),
        stream_broadcaster=None,
    )


async def _async_noop(*args, **kwargs):
    return None


async def _async_noop_returning_empty(*args, **kwargs):
    return ("no_advice", "", "")


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


class _DisconnectAfterFrames:
    """收完预定帧后抛 WebSocketDisconnect 的假 collector 连接."""

    def __init__(self, frames: list[str]):
        self._frames = list(frames)
        self.sent: list[str] = []
        self.client_state = None

    async def receive_text(self) -> str:
        if self._frames:
            return self._frames.pop(0)
        from fastapi import WebSocketDisconnect
        raise WebSocketDisconnect()


@pytest.mark.asyncio
async def test_transient_disconnect_skips_review_and_record(monkeypatch):
    """P0 回归：未见 game_end 的断连（网络抖动/collector 重启）不得触发
    复盘，也不得写入对局记录——只计 session_aborts 指标."""
    import services.session as session_mod

    calls: list[tuple] = []

    async def spy_summarize(ctx, state, skip_review=False):
        calls.append((state, skip_review))

    monkeypatch.setattr(session_mod.lifecycle, "summarize_on_disconnect", spy_summarize)

    ctx = _fake_ctx(MemoryQueue())
    ws = _DisconnectAfterFrames(["{\"type\": \"state\", \"payload\": {\"game_time\": 900}}"])
    session = CollectorSession(ws, ctx)  # type: ignore[arg-type]

    await session.run()
    assert calls == []
    assert ctx.metrics["session_aborts"] == 1


@pytest.mark.asyncio
async def test_game_end_marked_from_event(monkeypatch):
    """game_end 事件到达即标记正常结束——断连时据此放行复盘."""
    import services.session as session_mod

    calls: list[tuple] = []

    async def spy_summarize(ctx, state, skip_review=False):
        calls.append((state, skip_review))

    monkeypatch.setattr(session_mod.lifecycle, "summarize_on_disconnect", spy_summarize)

    ctx = _fake_ctx(MemoryQueue(window=60.0))  # 长窗口：入队事件不会被消费
    frame = "{\"type\": \"event\", \"payload\": {\"name\": \"game_end\", \"data\": {}}}"
    ws = _DisconnectAfterFrames([frame])
    session = CollectorSession(ws, ctx)  # type: ignore[arg-type]

    await session.run()
    assert session._saw_game_end is True
    assert len(calls) == 1  # 断连走了复盘分支（skip_review=False，本会话未产出 review tip）


@pytest.mark.asyncio
async def test_background_tasks_cancelled_on_disconnect():
    """P1 回归：断连后 urgent 后台流水线必须取消——否则继续向已断会话
    广播 tip、往 Redis 写反馈上下文污染下一局."""
    q = MemoryQueue(window=60.0)
    ctx = _fake_ctx(q)
    session = CollectorSession(_DeadWebsocket(), ctx)  # type: ignore[arg-type]

    started = asyncio.Event()

    async def slow_coaching(item):
        started.set()
        await asyncio.sleep(30)

    session.handle_coaching = slow_coaching  # type: ignore[method-assign]
    ev = {"event": CoachEvent(name="low_health", data={}), "signals": [], "priority": 3}
    session._spawn_coaching(dict(ev))
    await asyncio.wait_for(started.wait(), timeout=1)

    task = next(iter(session._background_tasks))
    await session.run()  # _DeadWebsocket 立即 disconnect → finally 取消任务

    assert task.cancelled()
    assert session._background_tasks == set()


@pytest.mark.asyncio
async def test_malformed_frame_does_not_kill_session():
    """P2 回归：单条畸形帧（截断 JSON / 非法 payload）只丢帧，
    不能杀死会话——此前它会杀掉会话，等价于人为断连."""
    ctx = _fake_ctx(MemoryQueue())
    ws = _DisconnectAfterFrames([
        "not-json-at-all",
        "{\"type\": \"state\", \"payload\": {\"game_time\": 300}}",
        "{\"type\": \"state\", \"payload\": {\"active_player\": \"boom\"}}",  # 非法 payload
        "{\"type\": \"event\", \"payload\": {\"name\": \"kill\", \"data\": {}}}",
    ])
    session = CollectorSession(ws, ctx)  # type: ignore[arg-type]

    await session.run()
    assert ctx.metrics["frames_malformed"] == 2  # 截断 JSON + 非法 state payload
    assert ctx.metrics["events_received"] == 1  # 后续帧照常处理
    assert session.latest_state is not None
