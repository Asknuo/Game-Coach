"""
TTS 引擎 — Edge TTS (首选) + pyttsx3 (回退)
从原 companion.py 移植并适配 PyQt6 异步模型。
"""

import asyncio
import logging
import os
import subprocess
import sys
import tempfile
import threading
import time

logger = logging.getLogger("desktop_pet.tts")

# ── TTS 依赖检测 ─────────────────────────────────
HAS_EDGE_TTS = False
HAS_PYTTSX3 = False

try:
    import edge_tts  # type: ignore
    HAS_EDGE_TTS = True
except ImportError:
    pass

try:
    import pyttsx3
    HAS_PYTTSX3 = True
except ImportError:
    pass

# ── 配置 ─────────────────────────────────────────
EDGE_VOICE = os.getenv("EDGE_VOICE", "zh-CN-XiaoxiaoNeural")
EDGE_RATE = os.getenv("EDGE_RATE", "+15%")


# ── 音频播放工具 ──────────────────────────────────

def _play_audio_file(filepath: str) -> None:
    """跨平台播放音频文件。"""
    try:
        if sys.platform == "win32":
            subprocess.run(
                ["powershell", "-c",
                 f'(New-Object Media.SoundPlayer "{filepath}").PlaySync()'],
                capture_output=True, timeout=15,
            )
        elif sys.platform == "darwin":
            subprocess.run(["afplay", filepath], capture_output=True, timeout=15)
        else:
            subprocess.run(
                ["ffplay", "-nodisp", "-autoexit", filepath],
                capture_output=True, timeout=15,
            )
    except Exception:
        pass


# ── Edge TTS 引擎 ─────────────────────────────────

class EdgeTTSEngine:
    """使用微软 Edge TTS 免费 API，中文语音效果极佳。"""

    def __init__(self):
        self.muted = False

    def speak(self, text: str):
        if self.muted or not HAS_EDGE_TTS:
            return
        try:
            asyncio.run(self._speak_async(text))
        except Exception as e:
            logger.debug("Edge TTS failed: %s", e)

    async def _speak_async(self, text: str):
        try:
            communicate = edge_tts.Communicate(text, EDGE_VOICE, rate=EDGE_RATE)
            with tempfile.NamedTemporaryFile(suffix=".mp3", delete=False) as f:
                tmp_path = f.name
            await communicate.save(tmp_path)
            _play_audio_file(tmp_path)
            # 延迟删除临时文件
            def _cleanup():
                time.sleep(2)
                try:
                    os.unlink(tmp_path)
                except OSError:
                    pass
            threading.Thread(target=_cleanup, daemon=True).start()
        except Exception as e:
            logger.debug("Edge TTS async failed: %s", e)

    def stop(self):
        pass  # Edge TTS 同步播放，无法中断


# ── PyTTSX3 回退引擎 ──────────────────────────────

class Pyttsx3Engine:
    """回退方案：离线 TTS。"""

    def __init__(self):
        self.engine = None
        self.voices = []
        self.current_voice_id = None
        self.muted = False
        if HAS_PYTTSX3:
            self._init()

    def _init(self):
        try:
            self.engine = pyttsx3.init()
            self.voices = self.engine.getProperty("voices")
            for v in self.voices:
                lang = getattr(v, "languages", [])
                lang_str = ""
                if lang:
                    lang_str = (
                        lang[0].decode()
                        if isinstance(lang[0], bytes)
                        else lang[0]
                    )
                if "zh" in lang_str:
                    self.current_voice_id = v.id
                    break
            if self.current_voice_id:
                self.engine.setProperty("voice", self.current_voice_id)
            self.engine.setProperty("rate", 160)
            self.engine.setProperty("volume", 0.9)
        except Exception as e:
            logger.warning("pyttsx3 init failed: %s", e)
            self.engine = None

    def speak(self, text: str):
        if self.muted or not self.engine:
            return
        try:
            self.engine.say(text)
            self.engine.runAndWait()
        except Exception:
            pass

    def stop(self):
        if self.engine:
            try:
                self.engine.stop()
            except Exception:
                pass


# ── TTS 门面 ──────────────────────────────────────

class TTSEngine:
    """TTS 门面：Edge TTS → pyttsx3 → 静音。"""

    def __init__(self):
        self.muted = False
        if HAS_EDGE_TTS:
            self._engine = EdgeTTSEngine()
            self._name = "edge-tts"
            logger.info("TTS: using Edge TTS (%s)", EDGE_VOICE)
        elif HAS_PYTTSX3:
            self._engine = Pyttsx3Engine()
            self._name = "pyttsx3"
            logger.info("TTS: using pyttsx3 (fallback)")
        else:
            self._engine = None
            self._name = "silent"
            logger.warning("TTS: no engine available — install edge-tts or pyttsx3")

    def speak(self, text: str):
        if self.muted or self._engine is None:
            return
        self._engine.speak(text)

    def stop(self):
        if self._engine:
            self._engine.stop()

    @property
    def engine_name(self) -> str:
        return self._name
