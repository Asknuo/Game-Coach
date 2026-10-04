"""services/broadcast.py 广播健壮性测试."""

import asyncio
from types import SimpleNamespace

import pytest

from services.broadcast import broadcast_tip_json


class _FakeWS:
    def __init__(self, on_send=None):
        self.sent: list[str] = []
        self._on_send = on_send

    async def send_text(self, text: str) -> None:
        if self._on_send is not None:
            await self._on_send()
        self.sent.append(text)


@pytest.mark.asyncio
async def test_broadcast_survives_concurrent_client_removal():
    """回归：遍历 set 时 await 让出控制权，客户端被并发 add/discard 曾抛
    Set changed size during iteration."""
    ctx = SimpleNamespace(overlay_clients=set())
    late = _FakeWS()

    async def remove_late_during_send():
        ctx.overlay_clients.discard(late)  # 真实场景：ws 处理器 finally: discard

    first = _FakeWS(on_send=remove_late_during_send)
    ctx.overlay_clients = {first, late}

    await broadcast_tip_json(ctx, "tip-1")  # 旧实现此处抛 RuntimeError
    assert late.sent == ["tip-1"]  # 快照包含已移除客户端，多发一次无害


@pytest.mark.asyncio
async def test_broadcast_drops_failing_client():
    class _Broken(_FakeWS):
        async def send_text(self, text):
            raise RuntimeError("closed")

    ctx = SimpleNamespace(overlay_clients=set())
    broken, good = _Broken(), _FakeWS()
    ctx.overlay_clients = {broken, good}

    await broadcast_tip_json(ctx, "tip-2")
    assert broken not in ctx.overlay_clients
    assert good.sent == ["tip-2"]


@pytest.mark.asyncio
async def test_slow_client_does_not_block_fast_client():
    """P1 回归：广播是并发的——挂死的 overlay 客户端（send 卡住）只会
    自己被超时丢弃，不能拖慢其他客户端（串行 + 5s 超时曾把流式润色停摆）."""
    import asyncio as _asyncio

    class _Hanging(_FakeWS):
        async def send_text(self, text):
            await _asyncio.sleep(10)  # 超过 SEND_TIMEOUT 即被丢弃

    ctx = SimpleNamespace(overlay_clients=set())
    hanging, good = _Hanging(), _FakeWS()
    ctx.overlay_clients = {hanging, good}

    await broadcast_tip_json(ctx, "tip-3")
    assert good.sent == ["tip-3"]
    assert hanging not in ctx.overlay_clients


@pytest.mark.asyncio
async def test_stream_broadcaster_coalesces_and_relates_tip_id():
    """流式合并广播：多次 emit 只发最新帧，坏客户端不影响后续广播."""
    from services.broadcast import PolishStreamBroadcaster

    ctx = SimpleNamespace(overlay_clients=set())
    client = _FakeWS()
    ctx.overlay_clients = {client}
    b = PolishStreamBroadcaster(ctx)
    b.start()
    try:
        await b.emit({"message": "第一帧", "tip_id": "t1"}, "t1")
        await b.emit({"message": "第二帧", "tip_id": "t1"}, "t1")
        await asyncio.sleep(0.1)
        await b.emit({"message": "另一条", "tip_id": "t2"}, "t2")
        await asyncio.sleep(0.1)

        assert len(client.sent) == 2  # 同 tip_id 的增量被合并为最新一帧
        assert '"第二帧"' in client.sent[0]
        assert '"另一条"' in client.sent[1]
    finally:
        await b.stop()


@pytest.mark.asyncio
async def test_stream_broadcaster_discard_prevents_orphan_send():
    """discard：流水线异常/超时后丢弃未发出的帧，防 overlay 孤儿卡片."""
    from services.broadcast import PolishStreamBroadcaster

    ctx = SimpleNamespace(overlay_clients=set())
    client = _FakeWS()
    ctx.overlay_clients = {client}
    b = PolishStreamBroadcaster(ctx)
    b.start()
    try:
        await b.emit({"message": "不该上屏", "tip_id": "t9"}, "t9")
        b.discard("t9")
        await asyncio.sleep(0.1)
        assert client.sent == []
    finally:
        await b.stop()
