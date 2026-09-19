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
PING_INTERVAL = 15.0      # 应用层心跳，与 README 协议约定一致
RECONNECT_BASE = 2.0      # 断线重连初始退避（秒）
RECONNECT_MAX = 30.0      # 重连退避上限（秒）
QUEUE_MAX = 5             # 播报队列上限，满员丢最旧
DEDUP_WINDOW = 10.0       # 同一 skill 的 tip 在窗口内去重（防御）
URGENT_PRIORITY = 3       # >= 该优先级视为紧急，插队播报

try:
    import win32com.client
    _HAS_PYWIN32 = True
except ImportError:
    _HAS_PYWIN32 = False


class TipQueue:
    """线程安全播报队列：紧急 tip 插队到队首，满员时按到达时间丢弃最旧的."""

    def __init__(self, maxsize: int = QUEUE_MAX):
        self._items: collections.deque[tuple[int, str]] = collections.deque()
        self._maxsize = maxsize
        self._seq = 0
        self._cond = threading.Condition()

    def put(self, text: str, urgent: bool) -> None:
        with self._cond:
            item = (self._seq, text)
            self._seq += 1
            if urgent:
                self._items.appendleft(item)
            else:
                self._items.append(item)
            while len(self._items) > self._maxsize:
                oldest = min(self._items, key=lambda it: it[0])
                self._items.remove(oldest)
            self._cond.notify()

    def get(self) -> str:
        with self._cond:
            while not self._items:
                self._cond.wait()
            return self._items.popleft()[1]


class PrintSpeaker:
    """调试用：只打印不发声（--dry-run）."""

    def __init__(self, rate: int = 0, voice: str | None = None) -> None:
        self.rate = rate
        self.voice = voice

    def speak(self, text: str) -> None:
        print(f"[TTS] {text}", flush=True)


class SapiSpeaker:
    """Windows SAPI 语音引擎：pywin32 优先，缺失时回退 PowerShell."""

    def __init__(self, rate: int = 0, voice: str | None = None) -> None:
        self.rate = max(-10, min(10, rate))
        self.voice = voice
        self._sapi = None
        if not _HAS_PYWIN32:
            logger.warning("pywin32 未安装，将使用 PowerShell 回退播报")
            return
        try:
            self._sapi = win32com.client.Dispatch("SAPI.SpVoice")
            self._sapi.Rate = self.rate
            self._select_voice()
            logger.info("SAPI 就绪（%s）", self._current_voice())
        except Exception as exc:  # noqa: BLE001 - COM 异常不可控
            logger.warning("SAPI 初始化失败（%s），将使用 PowerShell 回退播报", exc)
            self._sapi = None

    def _current_voice(self) -> str:
        try:
            return self._sapi.GetVoices().Item(0).GetDescription()
        except Exception:  # noqa: BLE001
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
        except Exception:  # noqa: BLE001
            logger.exception("语音选择失败，使用默认语音")

    def speak(self, text: str) -> None:
        if not text:
            return
        if self._sapi is not None:
            try:
                self._sapi.Speak(text)
                return
            except Exception as exc:  # noqa: BLE001
                logger.warning("SAPI 播报失败（%s），回退 PowerShell", exc)
        self._speak_powershell(text)

    @staticmethod
    def _speak_powershell(text: str) -> None:
        script = (
            "Add-Type -AssemblyName System.Speech;"
            " $s = New-Object System.Speech.Synthesis.SpeechSynthesizer;"
            " $s.Speak($env:VOICE_TEXT)"
        )
        env = dict(os.environ, VOICE_TEXT=text)
        subprocess.run(
            ["powershell", "-NoProfile", "-NonInteractive", "-Command", script],
            env=env,
            timeout=120,
            check=False,
        )


class VoiceBroadcaster:
    """WS 监听 + TTS 播报调度.

    结构：asyncio 事件循环负责连接/心跳/收消息，独立守护线程负责
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
        self.speaker = speaker or SapiSpeaker(rate=rate, voice=voice)
        self._queue: TipQueue = TipQueue()
        self._last_spoken: dict[str, float] = {}
        self._stop = threading.Event()
        self._metrics = {"received": 0, "spoken": 0, "skipped_priority": 0, "skipped_dedup": 0}
        self._tts_thread = threading.Thread(target=self._tts_loop, daemon=True, name="tts")

    # ── 对外接口 ──

    def start(self) -> None:
        self._tts_thread.start()

    def stop(self) -> None:
        self._stop.set()
        self._queue.put("__EXIT__", False)

    def metrics(self) -> dict[str, int]:
        return dict(self._metrics)

    # ── tip 处理 ──

    def handle_tip(self, payload: dict) -> None:
        self._metrics["received"] += 1
        message = str(payload.get("message") or "").strip()
        if not message:
            return
        priority = int(payload.get("priority", 1))
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

        self._queue.put(message, urgent=priority >= URGENT_PRIORITY)
        self._metrics["spoken"] += 1
        logger.info("[%s] 播报：%s", skill or "tip", message)

    # ── 异步连接 ──

    async def run(self) -> None:
        """主循环：连接 → 监听 → 断线重连（指数退避）."""
        self.start()
        backoff = RECONNECT_BASE
        while not self._stop.is_set():
            try:
                logger.info("连接 Agent：%s", self.url)
                async with websockets.connect(self.url, ping_interval=None) as ws:
                    logger.info("已连接，等待 coaching tip...")
                    backoff = RECONNECT_BASE
                    await self._listen(ws)
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 - 任意断线都要重连
                logger.warning("连接断开（%s），%.0fs 后重连", exc, backoff)
                if self._stop.wait(backoff):
                    break
                backoff = min(backoff * 2, RECONNECT_MAX)
        self._queue.put("__EXIT__", False)

    async def _listen(self, ws) -> None:
        """接收 tip 消息 + 定期应用层心跳."""

        async def heartbeat() -> None:
            while True:
                await asyncio.sleep(PING_INTERVAL)
                try:
                    await ws.send(json.dumps({"type": "ping"}))
                except Exception:  # noqa: BLE001
                    return

        hb = asyncio.create_task(heartbeat())
        try:
            async for raw in ws:
                try:
                    msg = json.loads(raw)
                except json.JSONDecodeError:
                    continue
                if msg.get("type") == "tip":
                    self.handle_tip(msg.get("payload") or {})
        finally:
            hb.cancel()

    # ── TTS 工作线程 ──

    def _tts_loop(self) -> None:
        while True:
            item = self._queue.get()
            if item == "__EXIT__":
                return
            try:
                self.speaker.speak(item)
            except Exception:  # noqa: BLE001 - 播报失败不致命
                logger.exception("播报失败")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Game Coach 语音播报客户端（Windows TTS，无需界面）",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument("--url", default=DEFAULT_URL, help="Agent /ws/overlay 地址")
    parser.add_argument("--min-priority", type=int, default=1, help="最低播报优先级（1-3）")
    parser.add_argument("--rate", type=int, default=0, help="语速（SAPI -10~10，默认 0）")
    parser.add_argument("--voice", default="Huihui", help="语音名称子串；默认 Huihui（微软中文慧慧），找不到时回退系统首选")
    parser.add_argument("--dry-run", action="store_true", help="只打印不发声（调试）")
    parser.add_argument("--verbose", action="store_true", help="调试日志")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
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
        stats = broadcaster.metrics()
        logger.info("本次统计：接收 %d / 播报 %d / 低优先级跳过 %d / 去重跳过 %d",
                    stats["received"], stats["spoken"], stats["skipped_priority"], stats["skipped_dedup"])


if __name__ == "__main__":
    sys.exit(main())