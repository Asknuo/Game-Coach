"""voice_broadcast 单元测试：队列调度 / tip 过滤 / WS 集成."""

import asyncio
import json
import time

import pytest
import websockets

from voice_broadcast import PrintSpeaker, TipQueue, VoiceBroadcaster


def test_tip_queue_urgent_first():
    q = TipQueue()
    q.put("a", urgent=False)
    q.put("b", urgent=True)
    q.put("c", urgent=False)
    assert q.get() == "b"
    assert q.get() == "a"
    assert q.get() == "c"


def test_tip_queue_drops_oldest_when_full():
    q = TipQueue(maxsize=3)
    for i in range(5):
        q.put(f"msg{i}", urgent=False)
    items = [q.get(), q.get(), q.get()]
    assert items == ["msg2", "msg3", "msg4"]


def test_handle_tip_priority_filter():
    b = VoiceBroadcaster("ws://localhost:0", min_priority=2, speaker=PrintSpeaker())
    b.handle_tip({"message": "普通建议", "skill": "build", "priority": 1})
    b.handle_tip({"message": "重要建议", "skill": "dragon", "priority": 2})
    stats = b.metrics()
    assert stats["received"] == 2
    assert stats["spoken"] == 1
    assert stats["skipped_priority"] == 1
    assert b._queue.get() == "重要建议"


def test_handle_tip_dedup_window():
    b = VoiceBroadcaster("ws://localhost:0", speaker=PrintSpeaker())
    b.handle_tip({"message": "第一条", "skill": "dragon", "priority": 2})
    b.handle_tip({"message": "第二条", "skill": "dragon", "priority": 2})
    stats = b.metrics()
    assert stats["spoken"] == 1
    assert stats["skipped_dedup"] == 1


def test_handle_tip_empty_message():
    b = VoiceBroadcaster("ws://localhost:0", speaker=PrintSpeaker())
    b.handle_tip({"message": "   ", "skill": "dragon", "priority": 1})
    stats = b.metrics()
    assert stats["received"] == 1
    assert stats["spoken"] == 0


@pytest.mark.asyncio
async def test_listen_receives_and_speaks(capsys):
    async def server(ws):
        await ws.send(json.dumps({
            "type": "tip",
            "payload": {"message": "第一条建议", "skill": "dragon", "priority": 2},
        }))
        await asyncio.sleep(0.05)
        await ws.send(json.dumps({
            "type": "tip",
            "payload": {"message": "第二条建议", "skill": "build", "priority": 3},
        }))
        await asyncio.sleep(0.05)
        await ws.send(json.dumps({"type": "state", "payload": {}}))  # 非 tip 消息应忽略
        await ws.close()

    async with websockets.serve(server, "127.0.0.1", 0) as srv:
        port = srv.sockets[0].getsockname()[1]
        url = f"ws://127.0.0.1:{port}/ws/overlay"
        b = VoiceBroadcaster(url, speaker=PrintSpeaker())
        b.start()
        try:
            async with websockets.connect(url) as ws:
                await b._listen(ws)
            deadline = time.monotonic() + 3
            while b.metrics()["spoken"] < 2 and time.monotonic() < deadline:
                await asyncio.sleep(0.02)
            assert b.metrics()["spoken"] == 2
            time.sleep(0.2)  # 等 TTS 线程打印完成
            out = capsys.readouterr().out
            assert "第一条建议" in out
            assert "第二条建议" in out
        finally:
            b.stop()