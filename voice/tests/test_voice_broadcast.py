"""voice_broadcast 单元测试：队列调度 / tip 过滤 / WS 集成."""

import asyncio
import json
import subprocess
import threading
import time
from types import SimpleNamespace

import pytest
import websockets

import voice_broadcast
from voice_broadcast import PrintSpeaker, TipQueue, VoiceBroadcaster, parse_args


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

# ── P1：溢出丢弃按重要性（先丢普通，后丢紧急） ──

def test_overflow_drops_oldest_normal_before_urgent():
    """P1 回归：满员时先丢最旧的普通条；全是紧急条才丢最旧紧急条.
    按到达序一刀切会丢掉团战爆发中最早到达、最该播的紧急条."""
    q = TipQueue(maxsize=3)
    q.put("u1", urgent=True)
    q.put("n1", urgent=False)
    q.put("u2", urgent=True)            # [u1, u2, n1] 恰好满员
    assert q.put("n2", urgent=False) == ["n1"]   # 挤掉最旧普通条
    assert q.put("u3", urgent=True) == ["n2"]    # 新紧急仍挤普通条
    assert q.last_dropped_urgent == 0
    # 全是紧急时才丢最旧紧急条
    assert q.put("u4", urgent=True) == ["u1"]
    assert q.last_dropped_urgent == 1
    texts = [q.get_item()[0] for _ in range(3)]
    assert texts == ["u2", "u3", "u4"]  # 紧急之间保序


def test_handle_tip_counts_urgent_overflow_separately():
    """紧急条被溢出丢弃必须单独计数——agent 25s 冷却内不会补推."""
    b = VoiceBroadcaster("ws://localhost:0", speaker=PrintSpeaker())
    b._queue = TipQueue(maxsize=1)
    b.handle_tip({"message": "紧急一", "skill": "survival", "priority": 3})
    b.handle_tip({"message": "紧急二", "skill": "dragon", "priority": 3})
    stats = b.metrics()
    assert stats["skipped_overflow"] == 1
    assert stats["skipped_overflow_urgent"] == 1


# ── P2：畸形帧防护（不打掉连接） ──

@pytest.mark.asyncio
async def test_listen_ignores_tip_stream_and_malformed(capsys):
    """tip_stream（累计文本）不得入播报；非对象帧/payload 非对象/priority
    非数字都必须安全跳过并计入 malformed，不能让一条畸形帧打掉连接."""

    async def server(ws):
        await ws.send(json.dumps({  # 流式累计文本：朗读它会逐词复读
            "type": "tip_stream",
            "payload": {"message": "快", "tip_id": "t1", "priority": 3},
        }))
        await ws.send("[1, 2]")              # 合法 JSON 但非对象
        await ws.send("{\"type\": \"tip\", \"payload\": [1]}")   # payload 非对象
        await ws.send(json.dumps({
            "type": "tip",
            "payload": {"message": "正常建议", "skill": "dragon", "priority": "high"},
        }))
        await ws.close()

    async with websockets.serve(server, "127.0.0.1", 0) as srv:
        port = srv.sockets[0].getsockname()[1]
        url = f"ws://127.0.0.1:{port}/ws/overlay"
        b = VoiceBroadcaster(url, speaker=PrintSpeaker())
        b.start()
        try:
            async with websockets.connect(url) as ws:
                await b._listen(ws)   # 不抛异常即不断线
            deadline = time.monotonic() + 3
            while b.metrics()["spoken"] < 1 and time.monotonic() < deadline:
                await asyncio.sleep(0.02)
            stats = b.metrics()
            assert stats["malformed"] == 3
            assert stats["received"] == 1      # 只有权威 tip 被受理
            assert stats["spoken"] == 1        # priority 脏值降级为 1 后仍播出
        finally:
            b.stop()


# ── P1：库级 keepalive（半开连接检测） ──

@pytest.mark.asyncio
async def test_connect_uses_library_keepalive(monkeypatch):
    """P1 回归：必须启用库级 ping_interval/ping_timeout——单向应用层
    心跳无法发现半开连接（休眠/切网后永久静默挂死）."""
    captured = {}

    class _FakeConnect:
        """websockets.connect 替身：记录参数，__aenter__ 即失败."""

        def __init__(self, url, **kwargs):
            captured.update(kwargs)
            b._stop.set()  # 请求退出，避免真实退避等待

        async def __aenter__(self):
            raise ConnectionError("no server")

        async def __aexit__(self, *exc):
            return False

    # 只垫片 voice_broadcast 看到的 asyncio（不能 patch 全局 asyncio，
    # 会连带停掉 pytest-asyncio 自己的 sleep）
    shim = SimpleNamespace(
        sleep=lambda _s: asyncio.sleep(0),
        CancelledError=asyncio.CancelledError,
    )
    monkeypatch.setattr(voice_broadcast, "asyncio", shim)
    monkeypatch.setattr(voice_broadcast.websockets, "connect", _FakeConnect)

    b = VoiceBroadcaster("ws://localhost:1", speaker=PrintSpeaker())
    await b.run()

    assert captured.get("ping_interval") == 20
    assert captured.get("ping_timeout") == 20


# ── P2：PowerShell 回退路径 ──

class _FakeProc:
    def __init__(self, returncode=None):
        self.returncode = returncode
        self.killed = False

    def poll(self):
        return self.returncode

    def kill(self):
        self.killed = True


def test_powershell_returns_false_when_start_fails(monkeypatch):
    """Popen 起不来 → 返回 False（未完整播出），不能谎报 spoken."""
    def boom(*a, **kw):
        raise OSError("no powershell")

    monkeypatch.setattr(voice_broadcast.subprocess, "Popen", boom)
    assert voice_broadcast.SapiSpeaker._speak_powershell("测试") is False


def test_powershell_killed_on_interrupt(monkeypatch):
    """紧急插队时 kill PowerShell 子进程并返回 False（可打断）."""
    proc = _FakeProc(returncode=None)

    monkeypatch.setattr(voice_broadcast.subprocess, "Popen", lambda *a, **kw: proc)
    interrupt = threading.Event()
    interrupt.set()

    assert voice_broadcast.SapiSpeaker._speak_powershell("测试", interrupt) is False
    assert proc.killed is True


def test_powershell_hides_window_on_windows(monkeypatch):
    """后台常驻场景：不能弹控制台窗口抢游戏焦点（Windows）."""
    captured = {}

    def fake_popen(*a, **kw):
        captured.update(kw)
        return _FakeProc(returncode=0)

    monkeypatch.setattr(voice_broadcast.subprocess, "Popen", fake_popen)
    monkeypatch.setattr(voice_broadcast.os, "name", "nt", raising=False)

    assert voice_broadcast.SapiSpeaker._speak_powershell("测试") is True
    assert captured["creationflags"] == getattr(subprocess, "CREATE_NO_WINDOW", 0)


# ── P2：SAPI 路径（假 COM，跨平台可跑） ──

class _FakeSapi:
    def __init__(self):
        self.speak_calls = []
        self.skip_calls = []
        self.status = SimpleNamespace(RunningState=0)  # 0=播放中
        self.rate = None

    def Speak(self, text, flags):
        self.speak_calls.append((text, flags))

    def Skip(self, kind, count):
        self.skip_calls.append((kind, count))

    @property
    def Status(self):
        return self.status

    def GetVoices(self):
        return self


@pytest.fixture
def fake_sapi(monkeypatch):
    """注入假 win32com/pythoncom，让 SAPI 路径在非 Windows 上可测."""
    sapi = _FakeSapi()
    monkeypatch.setattr(voice_broadcast, "_HAS_PYWIN32", True)
    monkeypatch.setattr(voice_broadcast, "win32com",
                        SimpleNamespace(client=SimpleNamespace(Dispatch=lambda name: sapi)),
                        raising=False)  # 非 Windows 上模块本就不存在
    import sys
    import types
    pythoncom = types.ModuleType("pythoncom")
    pythoncom.CoInitialize = lambda: None
    pythoncom.CoUninitialize = lambda: None
    monkeypatch.setitem(sys.modules, "pythoncom", pythoncom)
    return sapi


def test_sapi_interrupt_skips_and_returns_false(fake_sapi):
    """打断置位 → Skip 剩余句子 → 返回 False（让路计数）."""
    sp = voice_broadcast.SapiSpeaker()
    interrupt = threading.Event()
    interrupt.set()
    assert sp.speak("低优建议", interrupt_event=interrupt) is False
    assert fake_sapi.skip_calls == [("Sentence", 1_000_000)]


def test_sapi_urgent_purges_queue(fake_sapi):
    """紧急条用 SVSFlagsAsync|SVSFPurgeBeforeSpeak，清掉 SAPI 请求队列残余."""
    sp = voice_broadcast.SapiSpeaker()
    fake_sapi.status.RunningState = 1  # 立即"播完"
    assert sp.speak("紧急建议", interrupt_event=None) is True
    text, flags = fake_sapi.speak_calls[0]
    assert text == "紧急建议"
    assert flags == (sp.SVSFlagsAsync | sp.SVSFPurgeBeforeSpeak)


def test_sapi_start_failure_falls_back_to_powershell(fake_sapi, monkeypatch):
    """Speak 发起即失败（未播出内容）→ 回退 PowerShell 重播全文."""
    calls = []

    def failing_speak(text, flags):
        raise RuntimeError("device busy")

    fake_sapi.Speak = failing_speak
    monkeypatch.setattr(
        voice_broadcast.SapiSpeaker, "_speak_powershell",
        staticmethod(lambda text, interrupt_event=None: calls.append(text) or True))

    sp = voice_broadcast.SapiSpeaker()
    assert sp.speak("建议") is True
    assert calls == ["建议"]


def test_sapi_init_failure_is_not_permanent(fake_sapi, monkeypatch):
    """P3 回归：SAPI 初始化失败只是冷却重试，不是永久闭锁——
    音频设备临时不可用不该让整个会话降级到 PowerShell."""
    monkeypatch.setattr(
        voice_broadcast.win32com.client, "Dispatch",
        lambda name: (_ for _ in ()).throw(RuntimeError("no audio")))

    sp = voice_broadcast.SapiSpeaker()
    assert sp._ensure_sapi() is None
    assert sp._sapi_retry_at > 0  # 冷却截止，而非 _sapi_failed=True 永久闭锁


# ── P3：命令行与生命周期 ──

def test_min_priority_validated():
    with pytest.raises(SystemExit):
        parse_args(["--min-priority", "5"])
    with pytest.raises(SystemExit):
        parse_args(["--min-priority", "abc"])
    assert parse_args(["--min-priority", "2"]).min_priority == 2


def test_run_stop_is_idempotent():
    b = VoiceBroadcaster("ws://localhost:0", speaker=PrintSpeaker())
    b.stop()
    b.stop()  # 重复调用不炸（哨兵幂等）
    assert b.metrics()["spoken"] == 0
