"""services/broadcast.py 广播健壮性测试."""

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
