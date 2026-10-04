"""Game Coach 语音播报客户端.

连接 Agent 的 /ws/overlay 广播端点，把 coaching tip 用 Windows 语音引擎
实时朗读出来。无界面、后台运行，适合游戏时挂机使用。

用法::

    python voice/voice_broadcast.py
    python voice/voice_broadcast.py --min-priority 2 --rate 2 --voice Huihui

依赖:
    websockets>=13.0   # WebSocket 连接
    pywin32            # Windows SAPI（可选；缺失时自动回退 PowerShell）
"""
from __future__ import annotations

import argparse
import asyncio
import collections
import json
import logging
import os
import subprocess
import sys
import threading
import time

import websockets

logger = logging.getLogger("voice_broadcast")

DEFAULT_URL = os.environ.get("VOICE_WS_URL", "ws://localhost:8000/ws/overlay")
# 协议级 keepalive：服务端（uvicorn）自动应答 ping/pong；单向的应用层心跳
# 无法发现半开连接（休眠/切网/Docker pause 时永久静默挂死）
PING_INTERVAL = 20.0     # 库级心跳间隔（秒）
PING_TIMEOUT = 20.0      # 心跳超时：超时即判定连接坏死，触发重连
RECONNECT_BASE = 2.0      # 断线重连初始退避（秒）
RECONNECT_MAX = 30.0      # 重连退避上限（秒）
QUEUE_MAX = 5             # 播报队列上限，满员丢最旧
DEDUP_WINDOW = 10.0       # 同一 skill 的 tip 在窗口内去重（防御）
URGENT_PRIORITY = 3       # >= 该优先级视为紧急，插队播报
POWERSHELL_TIMEOUT = 45.0  # PowerShell 回退单条播报超时（秒）
SAPI_RETRY_COOLDOWN = 30.0  # SAPI 初始化失败后的重试冷却（秒）

try:
    import win32com.client
    _HAS_PYWIN32 = True
except ImportError:
    _HAS_PYWIN32 = False


class _SpeakNotStarted(Exception):
    """SAPI 播放尚未开始就失败（可安全回退 PowerShell 重播）."""


class TipQueue:
    """线程安全播报队列.

    - 紧急 tip 插队到队首"紧急块"的尾部（紧急之间保持到达顺序，
      避免连续团战 tip 后到先播的碎片化乱序）
    - 满员丢弃策略按重要性：先丢最旧的普通条，全是紧急条时才丢最旧紧急条
      （团战爆发时最早到达的紧急条往往最该播，按到达序一刀切会丢掉它）
    - put() 返回被丢弃的文本列表供调用方计数；last_dropped_urgent 记录
      其中紧急条的数量（丢普通条与丢紧急条严重程度完全不同）
    """

    def __init__(self, maxsize: int = QUEUE_MAX):
        self._items: collections.deque[tuple[int, str, bool]] = collections.deque()
        self._maxsize = maxsize
        self._seq = 0
        self._cond = threading.Condition()
        self.last_dropped_urgent = 0

    def _drop_victim_index(self) -> int:
        """挑选淘汰位：最旧的非紧急条优先；全是紧急条时取最旧紧急条."""
        for idx, item in enumerate(self._items):
            if not item[2]:
                return idx
        return 0  # 全是紧急条（队列有序，0 即最旧）

    def put(self, text: str, urgent: bool) -> list[str]:
        """入队；返回因满员被丢弃的最旧文本（可能为空）."""
        with self._cond:
            item = (self._seq, text, urgent)
            self._seq += 1
            if urgent:
                idx = 0
                while idx < len(self._items):
                    if not self._items[idx][2]:  # 跳过队首连续的紧急块
                        break
                    idx += 1
                self._items.insert(idx, item)
            else:
                self._items.append(item)
            dropped: list[str] = []
            dropped_urgent = 0
            while len(self._items) > self._maxsize:
                victim = self._items[self._drop_victim_index()]
                del self._items[self._drop_victim_index()]
                dropped.append(victim[1])
                if victim[2]:
                    dropped_urgent += 1
            if dropped:
                logger.warning(
                    "播报队列满（%d），丢弃 %d 条（其中紧急 %d）",
                    len(self._items), len(dropped), dropped_urgent,
                )
            self.last_dropped_urgent = dropped_urgent
            self._cond.notify()
            return dropped

    def get(self) -> str:
        return self.get_item()[0]

    def get_item(self) -> tuple[str, bool]:
        with self._cond:
            while not self._items:
                self._cond.wait()
            _, text, urgent = self._items.popleft()
            return text, urgent

    def close(self) -> None:
        """清空积压并放入退出哨兵（退出即弃播，不再消化队列）.

        幂等：重复调用只放一个哨兵（run() 收尾与 stop() 可能各调一次）。
        """
        with self._cond:
            self._items.clear()
            self._items.append((self._seq, "__EXIT__", False))
            self._seq += 1
            self._cond.notify()


class PrintSpeaker:
    """调试用：只打印不发声（--dry-run）."""

    def __init__(self, rate: int = 0, voice: str | None = None) -> None:
        self.rate = rate
        self.voice = voice

    def speak(self, text: str, interrupt_event: threading.Event | None = None) -> bool:
        print(f"[TTS] {text}", flush=True)
        return True


class SapiSpeaker:
    """Windows SAPI 语音引擎：pywin32 优先，缺失时回退 PowerShell.

    speak() 返回 True 表示完整播出；False 表示被 interrupt_event 打断
    （紧急 tip 插队时当前非紧急播报让路）。COM 对象惰性创建在 TTS
    线程内并 CoInitialize —— 跨线程调用 STA 对象会失败。
    """

    POLL_INTERVAL = 0.05  # 打断轮询间隔（秒）
    SVSFlagsAsync = 1            # 异步发起，不阻塞调用线程
    SVSFPurgeBeforeSpeak = 2     # 播报前清空 SAPI 请求队列残余

    def __init__(self, rate: int = 0, voice: str | None = None) -> None:
        self.rate = max(-10, min(10, rate))
        self.voice = voice
        self._sapi = None
        self._sapi_retry_at = 0.0  # 初始化失败后的冷却截止（非永久闭锁）
        self._com_initialized = False
        if not _HAS_PYWIN32:
            logger.warning("pywin32 未安装，将使用 PowerShell 回退播报")

    def _ensure_sapi(self):
        """在调用线程（TTS 线程）内惰性初始化 SAPI.

        失败不永久闭锁——音频设备临时不可用这类瞬时故障应允许冷却后重试。
        """
        if self._sapi is not None or not _HAS_PYWIN32:
            return self._sapi
        if time.monotonic() < self._sapi_retry_at:
            return None
        try:
            import pythoncom

            pythoncom.CoInitialize()
            self._com_initialized = True
            self._sapi = win32com.client.Dispatch("SAPI.SpVoice")
            self._sapi.Rate = self.rate
            self._select_voice()
            logger.info("SAPI 就绪（%s）", self._current_voice())
        except Exception as exc:  # COM 异常不可控
            self._sapi = None
            self._sapi_retry_at = time.monotonic() + SAPI_RETRY_COOLDOWN
            logger.warning("SAPI 初始化失败（%s），%.0fs 内回退 PowerShell 后重试",
                           exc, SAPI_RETRY_COOLDOWN)
        return self._sapi

    def uninitialize(self) -> None:
        """TTS 线程退出前调用：释放 COM 线程引用."""
        if self._com_initialized:
            try:
                import pythoncom

                pythoncom.CoUninitialize()
            except Exception:
                logger.debug("CoUninitialize failed", exc_info=True)
            self._com_initialized = False
        self._sapi = None

    def _current_voice(self) -> str:
        try:
            return self._sapi.GetVoices().Item(0).GetDescription()
        except Exception:
            return "default"

    def _select_voice(self) -> None:
        if not self.voice:
            return
        try:
            voices = self._sapi.GetVoices()
            for i in range(voices.Count):
                desc = voices.Item(i).GetDescription()
                if self.voice.lower() in desc.lower():
                    self._sapi.Voice = voices.Item(i)
                    logger.info("已选择语音：%s", desc)
                    return
            logger.warning("未找到语音 %r，使用默认语音", self.voice)
        except Exception:
            logger.exception("语音选择失败，使用默认语音")

    def speak(self, text: str, interrupt_event: threading.Event | None = None) -> bool:
        if not text:
            return False
        sapi = self._ensure_sapi()
        if sapi is not None:
            try:
                return self._speak_sapi(sapi, text, interrupt_event)
            except _SpeakNotStarted as exc:
                # Speak 发起阶段就失败（未播出任何内容）→ 可安全回退重播
                logger.warning("SAPI 未开始播报即失败（%s），回退 PowerShell", exc)
                return self._speak_powershell(text, interrupt_event)
            except Exception as exc:
                # 播报中途异常（设备切换/COM 抖动）：已播部分不可撤销，
                # 回退重播全文会造成半截重复朗读——放弃该条
                logger.warning("SAPI 播报中断（%s），放弃该条", exc)
                return False
        return self._speak_powershell(text, interrupt_event)

    def _speak_sapi(self, sapi, text: str, interrupt_event: threading.Event | None) -> bool:
        """SVSFlagsAsync 异步发起 + 轮询等待；打断时 Skip 掉剩余句子.

        紧急条目（interrupt_event=None）加 SVSFPurgeBeforeSpeak：
        清空 SAPI 语音请求队列的残余，确保紧急条不会被前面未播完的流挡住。
        """
        flags = self.SVSFlagsAsync if interrupt_event is not None else (
            self.SVSFlagsAsync | self.SVSFPurgeBeforeSpeak)
        try:
            sapi.Speak(text, flags)
        except Exception as exc:  # 发起失败可回退
            raise _SpeakNotStarted(str(exc)) from exc
        while True:
            if interrupt_event is not None and interrupt_event.is_set():
                try:
                    sapi.Skip("Sentence", 1_000_000)
                except Exception:  # Skip 失败只能等它自然播完
                    logger.debug("SAPI Skip 失败，等当前条目自然播完")
                    return True
                return False
            # SPRS_DONE == 1：空闲（不再播报）
            if sapi.Status.RunningState == 1:
                return True
            time.sleep(self.POLL_INTERVAL)

    @staticmethod
    def _speak_powershell(text: str, interrupt_event: threading.Event | None = None) -> bool:
        """PowerShell 回退播报.

        - CREATE_NO_WINDOW：后台常驻场景下 powershell 自建控制台窗口会
          在游戏时反复抢焦点
        - 可打断：Popen + 轮询，紧急 tip 插队时直接 kill
        - 返回 False = 未完整播出（失败/被打断），调用方据此计数
        """
        script = (
            "Add-Type -AssemblyName System.Speech;"
            " $s = New-Object System.Speech.Synthesis.SpeechSynthesizer;"
            " $s.Speak($env:VOICE_TEXT)"
        )
        creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0
        env = dict(os.environ, VOICE_TEXT=text)
        try:
            proc = subprocess.Popen(
                ["powershell", "-NoProfile", "-NonInteractive", "-Command", script],
                env=env,
                creationflags=creationflags,
            )
        except Exception:  # 起不来就不是"完整播出"
            logger.exception("PowerShell 播报启动失败")
            return False

        deadline = time.monotonic() + POWERSHELL_TIMEOUT
        while True:
            if proc.poll() is not None:
                return True
            if interrupt_event is not None and interrupt_event.is_set():
                logger.debug("PowerShell 播报被紧急 tip 打断")
                proc.kill()
                return False
            if time.monotonic() > deadline:
                logger.warning("PowerShell 播报超时（%.0fs），终止", POWERSHELL_TIMEOUT)
                proc.kill()
                return False
            if interrupt_event is not None:
                interrupt_event.wait(0.05)
            else:
                time.sleep(0.05)


class VoiceBroadcaster:
    """WS 监听 + TTS 播报调度.

    结构：asyncio 事件循环负责连接/收消息，独立守护线程负责
    同步的 SAPI 播报（避免阻塞事件循环）。
    """

    def __init__(
        self,
        url: str,
        min_priority: int = 1,
        rate: int = 0,
        voice: str | None = None,
        speaker=None,
    ) -> None:
        self.url = url
        self.min_priority = min_priority
        self.speaker = speaker if speaker is not None else SapiSpeaker(rate=rate, voice=voice)
        self._queue: TipQueue = TipQueue()
        self._last_spoken: dict[str, float] = {}
        self._stop = threading.Event()
        # 紧急 tip 入队时置位 → TTS 线程打断当前非紧急播报
        self._interrupt = threading.Event()
        self._metrics = {
            "received": 0,
            "queued": 0,
            "spoken": 0,               # 完整播出
            "interrupted": 0,          # 被紧急 tip 打断让路
            "skipped_overflow": 0,     # 队列满员被丢弃（普通+紧急）
            "skipped_overflow_urgent": 0,  # 其中紧急条——丢援信息比丢普通条严重
            "skipped_priority": 0,
            "skipped_dedup": 0,
            "malformed": 0,            # 畸形帧/字段被安全跳过
        }
        self._tts_thread = threading.Thread(target=self._tts_loop, daemon=True, name="tts")

    # ── 对外接口 ──

    def start(self) -> None:
        if self._tts_thread.is_alive():
            raise RuntimeError("TTS thread already started")
        self._tts_thread.start()

    def stop(self) -> None:
        """停止播报：唤醒 TTS 线程（打断当前普通播报立即让路）并退出.

        等待线程结束有超时上限——被 interrupt 的普通播报应在毫秒级退出，
        超时仅可能是引擎卡死，不能把退出流程拖死。
        """
        self._stop.set()
        self._interrupt.set()
        self._queue.close()
        if self._tts_thread.is_alive() and self._tts_thread is not threading.current_thread():
            self._tts_thread.join(timeout=5.0)

    def metrics(self) -> dict[str, int]:
        return dict(self._metrics)

    # ── tip 处理 ──

    def handle_tip(self, payload: dict) -> None:
        self._metrics["received"] += 1
        message = str(payload.get("message") or "").strip()
        if not message:
            return
        try:
            priority = int(payload.get("priority", 1))
        except (TypeError, ValueError):
            # 协议演进/上游脏数据：降级为最低优先级并计数，不能让一条
            # 畸形帧打掉连接（曾直接 AttributeError/ValueError 逃逸）
            self._metrics["malformed"] += 1
            priority = 1
        skill = str(payload.get("skill") or "")

        if priority < self.min_priority:
            self._metrics["skipped_priority"] += 1
            logger.debug("跳过低优先级 tip（skill=%s, priority=%d）", skill, priority)
            return

        now = time.monotonic()
        last = self._last_spoken.get(skill, -1e9)
        if skill and now - last < DEDUP_WINDOW:
            self._metrics["skipped_dedup"] += 1
            logger.debug("去重跳过 tip（skill=%s）", skill)
            return
        self._last_spoken[skill] = now

        urgent = priority >= URGENT_PRIORITY
        dropped = self._queue.put(message, urgent=urgent)
        self._metrics["queued"] += 1
        if dropped:
            self._metrics["skipped_overflow"] += len(dropped)
            self._metrics["skipped_overflow_urgent"] += self._queue.last_dropped_urgent
            if self._queue.last_dropped_urgent:
                logger.warning("紧急 tip 因队列满被丢弃 %d 条（agent 25s 冷却内不会补推）",
                               self._queue.last_dropped_urgent)
        if urgent:
            # 唤醒 TTS 线程：Skip 当前非紧急播报，插队播这条
            self._interrupt.set()
        logger.info("[%s] 入队（%s）：%s", skill or "tip", "紧急" if urgent else "普通", message)

    # ── 异步连接 ──

    async def run(self) -> None:
        """主循环：连接 → 监听 → 断线重连（指数退避）.

        退避用 asyncio.sleep（可被取消、可被事件循环感知）；早先的
        threading.Event.wait 会在事件循环线程里同步阻塞最多 30s。
        """
        self.start()
        backoff = RECONNECT_BASE
        try:
            while not self._stop.is_set():
                try:
                    logger.info("连接 Agent：%s", self.url)
                    async with websockets.connect(
                        self.url,
                        ping_interval=PING_INTERVAL,
                        ping_timeout=PING_TIMEOUT,
                    ) as ws:
                        logger.info("已连接，等待 coaching tip...")
                        backoff = RECONNECT_BASE
                        await self._listen(ws)
                except asyncio.CancelledError:
                    raise
                except Exception as exc:  # 任意断线都要重连
                    logger.warning("连接断开（%s），%.0fs 后重连", exc, backoff)
                    await asyncio.sleep(backoff)
                    backoff = min(backoff * 2, RECONNECT_MAX)
        finally:
            # 任何退出路径（取消/异常/正常结束）都确保清理
            self.stop()

    async def _listen(self, ws) -> None:
        """接收 tip 消息（仅消费权威 tip；tip_stream 等流式消息忽略）.

        库级 keepalive（ping_interval/ping_timeout）负责半开连接检测，
        不再需要应用层单向心跳。
        """
        async for raw in ws:
            try:
                msg = json.loads(raw)
            except json.JSONDecodeError:
                continue
            if not isinstance(msg, dict):
                self._metrics["malformed"] += 1
                logger.debug("非对象帧已跳过：%r", raw[:80])
                continue
            if msg.get("type") != "tip":
                # tip_stream 是累计文本增量，逐词朗读无意义；权威 tip 才是终稿
                continue
            payload = msg.get("payload")
            if not isinstance(payload, dict):
                self._metrics["malformed"] += 1
                logger.debug("payload 非对象已跳过：%r", raw[:80])
                continue
            self.handle_tip(payload)

    # ── TTS 工作线程 ──

    def _tts_loop(self) -> None:
        try:
            while True:
                # 先清上一轮遗留的打断置位，再阻塞等新条目：
                # 若放在 get_item() 之后，取件与 clear 之间到达的紧急置位
                # 会被无条件擦掉——刚送达的紧急 tip 就得等 5-10s
                self._interrupt.clear()
                text, urgent = self._queue.get_item()
                if text == "__EXIT__":
                    return
                # 紧急条目播报期间不允许被打断（半截碎片比多等几秒更糟），
                # 只打断非紧急播报
                try:
                    completed = self.speaker.speak(
                        text, interrupt_event=None if urgent else self._interrupt)
                    if completed:
                        self._metrics["spoken"] += 1
                    else:
                        self._metrics["interrupted"] += 1
                except Exception:  # 播报失败不致命
                    logger.exception("播报失败")
        finally:
            uninit = getattr(self.speaker, "uninitialize", None)
            if uninit is not None:
                uninit()


def _min_priority(value: str) -> int:
    try:
        prio = int(value)
    except ValueError:
        raise argparse.ArgumentTypeError(f"必须是整数，得到 {value!r}") from None
    if prio not in (1, 2, 3):
        raise argparse.ArgumentTypeError(f"必须在 1-3 之间，得到 {prio}")
    return prio


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Game Coach 语音播报客户端（Windows TTS，无需界面）",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument("--url", default=DEFAULT_URL, help="Agent /ws/overlay 地址")
    parser.add_argument("--min-priority", type=_min_priority, default=1,
                        help="最低播报优先级（1-3）")
    parser.add_argument("--rate", type=int, default=0, help="语速（SAPI -10~10，默认 0）")
    parser.add_argument("--voice", default="Huihui",
                        help="语音名称子串；默认 Huihui（微软中文慧慧），找不到时回退系统首选")
    parser.add_argument("--dry-run", action="store_true", help="只打印不发声（调试）")
    parser.add_argument("--verbose", action="store_true", help="调试日志")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> None:
    args = parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        datefmt="%H:%M:%S",
    )
    speaker = PrintSpeaker() if args.dry_run else None
    broadcaster = VoiceBroadcaster(
        args.url,
        min_priority=args.min_priority,
        rate=args.rate,
        voice=args.voice,
        speaker=speaker,
    )
    try:
        asyncio.run(broadcaster.run())
    except KeyboardInterrupt:
        logger.info("已停止")
    finally:
        broadcaster.stop()
        stats = broadcaster.metrics()
        logger.info(
            "本次统计：接收 %d / 入队 %d / 完整播报 %d / 被紧急打断 %d / "
            "溢出丢弃 %d（紧急 %d）/ 低优先级跳过 %d / 去重跳过 %d / 畸形帧 %d",
            stats["received"], stats["queued"], stats["spoken"], stats["interrupted"],
            stats["skipped_overflow"], stats["skipped_overflow_urgent"],
            stats["skipped_priority"], stats["skipped_dedup"], stats["malformed"])


if __name__ == "__main__":
    sys.exit(main())
