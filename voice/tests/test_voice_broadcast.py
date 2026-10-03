"""voice_broadcast 单元测试：队列调度 / tip 过滤 / WS 集成."""

import asyncio
import json
import threading
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


def test_tip_queue_urgent_fifo():
    """连续紧急 tip 之间保持到达顺序（后到的不能插到先到的前面）."""
    q = TipQueue()
    q.put("u1", urgent=True)
    q.put("u2", urgent=True)
    q.put("u3", urgent=True)
    assert q.get() == "u1"
    assert q.get() == "u2"
    assert q.get() == "u3"


def test_tip_queue_drops_oldest_when_full():
    q = TipQueue(maxsize=3)
    for i in range(5):
        q.put(f"msg{i}", urgent=False)
    items = [q.get(), q.get(), q.get()]
    assert items == ["msg2", "msg3", "msg4"]


def test_tip_queue_put_returns_dropped():
    """满员丢弃要返回被丢弃的文本，供调用方计数（不再静默）."""
    q = TipQueue(maxsize=2)
    assert q.put("a", urgent=False) == []
    assert q.put("b", urgent=False) == []
    assert q.put("c", urgent=False) == ["a"]


def test_tip_queue_close_clears_backlog():
    """close() 丢弃积压只留退出哨兵（退出即弃播）."""
    q = TipQueue()
    q.put("a", urgent=False)
    q.put("b", urgent=False)
    q.close()
    assert q.get() == "__EXIT__"


def test_handle_tip_priority_filter():
    b = VoiceBroadcaster("ws://localhost:0", min_priority=2, speaker=PrintSpeaker())
    b.handle_tip({"message": "普通建议", "skill": "build", "priority": 1})
    b.handle_tip({"message": "重要建议", "skill": "dragon", "priority": 2})
    stats = b.metrics()
    assert stats["received"] == 2
    assert stats["queued"] == 1
    assert stats["skipped_priority"] == 1
    assert b._queue.get() == "重要建议"


def test_handle_tip_dedup_window():
    b = VoiceBroadcaster("ws://localhost:0", speaker=PrintSpeaker())
    b.handle_tip({"message": "第一条", "skill": "dragon", "priority": 2})
    b.handle_tip({"message": "第二条", "skill": "dragon", "priority": 2})
    stats = b.metrics()
    assert stats["queued"] == 1
    assert stats["skipped_dedup"] == 1


def test_handle_tip_overflow_counts_dropped():
    """队列满员丢弃要计入 skipped_overflow（不再静默）."""
    b = VoiceBroadcaster("ws://localhost:0", speaker=PrintSpeaker())
    b._queue = TipQueue(maxsize=1)  # 替换为小队列触发溢出
    b.handle_tip({"message": "第一条", "skill": "dragon", "priority": 1})
    b.handle_tip({"message": "第二条", "skill": "build", "priority": 1})
    b.handle_tip({"message": "第三条", "skill": "survival", "priority": 1})
    stats = b.metrics()
    assert stats["queued"] == 3
    assert stats["skipped_overflow"] == 2
    # 挤掉了最旧的，留下最后一条
    assert b._queue.get() == "第三条"


def test_handle_tip_empty_message():
    b = VoiceBroadcaster("ws://localhost:0", speaker=PrintSpeaker())
    b.handle_tip({"message": "   ", "skill": "dragon", "priority": 1})
    stats = b.metrics()
    assert stats["received"] == 1
    assert stats["queued"] == 0


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


class InterruptibleSpeaker:
    """匹配 hang_texts 的条目模拟"播报中"：可被打断（返回 False）；
    其余条目立即完整播出（返回 True）。用于驱动打断路径."""

    def __init__(self, hang_texts=(), hang_timeout=0.5):
        self.started = threading.Event()
        self.spoken: list[str] = []
        self._hang_texts = set(hang_texts)
        self._hang_timeout = hang_timeout

    def speak(self, text, interrupt_event=None) -> bool:
        self.started.set()
        if text in self._hang_texts:
            if interrupt_event is not None:
                if interrupt_event.wait(timeout=self._hang_timeout):
                    return False  # 被打断让路
            else:
                time.sleep(self._hang_timeout)  # 紧急条播报中（不可打断）
        self.spoken.append(text)
        return True


def test_tts_urgent_interrupts_current_speech():
    """紧急 tip 入队 → 打断当前非紧急播报 → 插队播紧急条."""
    sp = InterruptibleSpeaker(hang_texts={"长篇低优建议"})
    b = VoiceBroadcaster("ws://localhost:0", speaker=sp)
    b._queue.put("长篇低优建议", urgent=False)
    b.start()
    try:
        assert sp.started.wait(1), "TTS 线程应开始播报"
        b.handle_tip({"message": "快撤", "skill": "survival", "priority": 3})
        deadline = time.monotonic() + 3
        m = b.metrics()
        while m["spoken"] < 1 and time.monotonic() < deadline:
            time.sleep(0.02)
            m = b.metrics()
        assert m["spoken"] == 1      # 紧急条完整播出
        assert m["interrupted"] == 1  # 低优条被打断让路
        assert sp.spoken == ["快撤"]  # 被打断的低优条不再重播
    finally:
        b.stop()


def test_tts_urgent_speech_not_interrupted():
    """紧急条目播报期间不接受打断（避免连续团战 tip 播成半截碎片）."""
    sp = InterruptibleSpeaker(hang_texts={"第一条紧急"})
    b = VoiceBroadcaster("ws://localhost:0", speaker=sp)
    b.handle_tip({"message": "第一条紧急", "skill": "survival", "priority": 3})
    b.start()
    try:
        assert sp.started.wait(1)
        # 第一条紧急在播（interrupt_event=None，挂起模拟），此时第二条
        # 紧急到达并置位 interrupt —— 但当前条不应被打断
        b.handle_tip({"message": "第二条紧急", "skill": "dragon", "priority": 3})
        deadline = time.monotonic() + 5
        m = b.metrics()
        while m["spoken"] < 2 and time.monotonic() < deadline:
            time.sleep(0.05)
            m = b.metrics()
        assert m["interrupted"] == 0
        assert sp.spoken == ["第一条紧急", "第二条紧急"]  # 两条都完整播出
    finally:
        b.stop()


def test_stop_clears_backlog_without_speaking():
    """stop() 清空积压 → TTS 线程拿到 EXIT 直接退出，积压不再消化."""
    sp = InterruptibleSpeaker()
    b = VoiceBroadcaster("ws://localhost:0", speaker=sp)
    b._queue.put("积压一", urgent=False)
    b._queue.put("积压二", urgent=False)
    b.stop()
    b._tts_loop()  # 在当前线程驱动：应立即拿到 EXIT 返回
    assert b.metrics()["spoken"] == 0
    assert sp.spoken == []