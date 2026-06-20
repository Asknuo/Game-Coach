"""
桌面小玩偶（Desktop Pet Companion）
- 小巧的桌面窗口，始终置顶
- 通过 WebSocket 接收教练建议
- 用 Edge TTS 朗读（中文自然度极高），pyttsx3 作为回退方案
- 可爱的卡通角色 + 气泡文字

启动方式: python companion.py
依赖: 
  - 首选: pip install edge-tts      （免费、中文自然、微软 Edge 引擎）
  - 回退: pip install pyttsx3       （离线、机械化）
  - 必需: pip install websocket-client
"""

import asyncio
import json
import logging
import os
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

try:
    import tkinter as tk
    from tkinter import messagebox
    HAS_TK = True
except ImportError:
    HAS_TK = False

# ── TTS 引擎优先级：Edge TTS > pyttsx3 > silent ──
HAS_EDGE_TTS = False
HAS_PYTTSX3 = False

try:
    import edge_tts
    HAS_EDGE_TTS = True
except ImportError:
    pass

try:
    import pyttsx3
    HAS_PYTTSX3 = True
except ImportError:
    pass

try:
    import websocket
    HAS_WS = True
except ImportError:
    HAS_WS = False

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("companion")

# ── 配置 ──────────────────────────────────────────────
AGENT_HOST = os.getenv("AGENT_HOST", "localhost")
AGENT_PORT = os.getenv("AGENT_PORT", "8000")
WS_URL = f"ws://{AGENT_HOST}:{AGENT_PORT}/ws/overlay"
# 首选 Edge TTS 中文女声
EDGE_VOICE = os.getenv("EDGE_VOICE", "zh-CN-XiaoxiaoNeural")
EDGE_RATE = os.getenv("EDGE_RATE", "+15%")  # 稍快更清晰


# ═══════════════════════════════════════════════════════
# Edge TTS 引擎（首选：微软免费中文引擎，自然度高）
# ═══════════════════════════════════════════════════════
class EdgeTTSEngine:
    """使用微软 Edge TTS 免费 API，中文语音效果极佳."""

    def __init__(self):
        self.muted = False
        self._loop = None

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
            # 写入临时文件 → 用系统默认播放器播放
            with tempfile.NamedTemporaryFile(suffix=".mp3", delete=False) as f:
                tmp_path = f.name
            await communicate.save(tmp_path)
            self._play_audio(tmp_path)
            # 延迟删除临时文件
            threading.Thread(target=lambda: (time.sleep(2), os.unlink(tmp_path)), daemon=True).start()
        except Exception as e:
            logger.debug("Edge TTS async failed: %s", e)

    @staticmethod
    def _play_audio(filepath: str):
        """跨平台播放音频文件."""
        try:
            if sys.platform == "win32":
                subprocess.run(
                    ["powershell", "-c", f'(New-Object Media.SoundPlayer "{filepath}").PlaySync()'],
                    capture_output=True, timeout=15,
                )
            elif sys.platform == "darwin":
                subprocess.run(["afplay", filepath], capture_output=True, timeout=15)
            else:
                subprocess.run(["ffplay", "-nodisp", "-autoexit", filepath], capture_output=True, timeout=15)
        except Exception:
            pass

    def stop(self):
        pass  # Edge TTS 是同步阻塞播放，stop 不支持


# ═══════════════════════════════════════════════════════
# PyTTSX3 引擎（回退：离线 TTS，中文效果一般）
# ═══════════════════════════════════════════════════════
class Pyttsx3Engine:
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
                lang_str = lang[0].decode() if isinstance(lang[0], bytes) else lang[0] if lang else ""
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

    def set_voice(self, voice_id: str):
        if self.engine:
            self.engine.setProperty("voice", voice_id)
            self.current_voice_id = voice_id

    def get_voice_names(self) -> list:
        return [v.name for v in self.voices]


# ═══════════════════════════════════════════════════════
# TTS 出口（自动选择最佳引擎）
# ═══════════════════════════════════════════════════════
class TTSEngine:
    """TTS 门面：Edge TTS（首选）→ pyttsx3（回退）→ 静音."""

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

    def set_voice(self, voice_id: str):
        if self._engine and hasattr(self._engine, "set_voice"):
            self._engine.set_voice(voice_id)

    @property
    def engine_name(self) -> str:
        return self._name


# ═══════════════════════════════════════════════════════
# 桌面玩偶窗口
# ═══════════════════════════════════════════════════════
class DesktopPet:
    if not HAS_TK:
        raise RuntimeError("tkinter not available")

    WIDTH = 140
    HEIGHT = 180
    SPEECH_DURATION = 8  # 气泡显示秒数

    def __init__(self, tts: TTSEngine, ws_thread: threading.Thread):
        self.tts = tts
        self.ws_thread = ws_thread
        self.speech_hide_job = None

        self.root = tk.Tk()
        self.root.title("LOL 教练")
        self.root.overrideredirect(True)  # 无边框
        self.root.wm_attributes("-topmost", True)
        self.root.wm_attributes("-transparentcolor", "#000001")
        self.root.configure(bg="#000001")

        # 定位右下角
        screen_w = self.root.winfo_screenwidth()
        screen_h = self.root.winfo_screenheight()
        x = screen_w - self.WIDTH - 20
        y = screen_h - self.HEIGHT - 60
        self.root.geometry(f"{self.WIDTH}x{self.HEIGHT}+{x}+{y}")

        # ── 画布 ──
        self.canvas = tk.Canvas(
            self.root, width=self.WIDTH, height=self.HEIGHT,
            bg="#000001", highlightthickness=0, bd=0,
        )
        self.canvas.pack()

        # ── 拖拽 ──
        self._drag_x = 0
        self._drag_y = 0
        self.canvas.bind("<Button-1>", self._on_drag_start)
        self.canvas.bind("<B1-Motion>", self._on_drag_move)

        # ── 右键菜单 ──
        self.context_menu = tk.Menu(self.root, tearoff=0)
        self.context_menu.add_command(label="静音 / 取消静音", command=self._toggle_mute)
        self.context_menu.add_command(label="测试语音", command=self._test_voice)
        self.context_menu.add_separator()
        self.context_menu.add_command(label="退出", command=self._quit)
        self.canvas.bind("<Button-3>", self._show_menu)

        # ── 绘制角色 ──
        self._draw_character()

        # ── 气泡标签 ──
        self.bubble_text = ""
        self.bubble_id = None
        self.bubble_bg_id = None
        self._create_speech_bubble("")

        # ── 平滑退出 ──
        self.root.protocol("WM_DELETE_WINDOW", self._quit)

    def _draw_character(self):
        """绘制可爱的小教练角色"""
        c = self.canvas

        # 身体（圆球）
        body_cx = self.WIDTH // 2
        body_cy = 95
        body_r = 38
        # 阴影
        c.create_oval(
            body_cx - body_r + 2, body_cy - body_r + 2,
            body_cx + body_r + 2, body_cy + body_r + 2,
            fill="#1a1a2e", outline=""
        )
        # 主体
        c.create_oval(
            body_cx - body_r, body_cy - body_r,
            body_cx + body_r, body_cy + body_r,
            fill="#3b82f6", outline="#1d4ed8", width=2
        )

        # 眼睛
        eye_y = body_cy - 12
        c.create_oval(body_cx - 16, eye_y - 8, body_cx - 5, eye_y + 2, fill="white", outline="")
        c.create_oval(body_cx + 5, eye_y - 8, body_cx + 16, eye_y + 2, fill="white", outline="")
        # 瞳孔
        c.create_oval(body_cx - 10, eye_y - 4, body_cx - 8, eye_y - 2, fill="#0f172a", outline="")
        c.create_oval(body_cx + 8, eye_y - 4, body_cx + 10, eye_y - 2, fill="#0f172a", outline="")

        # 腮红
        c.create_oval(body_cx - 26, eye_y + 8, body_cx - 16, eye_y + 14, fill="#f472b6", outline="")
        c.create_oval(body_cx + 16, eye_y + 8, body_cx + 26, eye_y + 14, fill="#f472b6", outline="")

        # 嘴巴（微笑弧线）
        mouth_y = body_cy + 8
        c.create_arc(
            body_cx - 12, mouth_y - 4, body_cx + 12, mouth_y + 10,
            start=0, extent=-180, style=tk.ARC, outline="#1e40af", width=2
        )

        # 耳机（教练标志）
        band_top = body_cy - body_r - 14
        c.create_arc(
            body_cx - 22, band_top - 6, body_cx + 22, band_top + 24,
            start=160, extent=220, style=tk.ARC, outline="#f59e0b", width=3
        )
        # 耳罩
        c.create_rectangle(body_cx - 42, band_top + 18, body_cx - 33, band_top + 32, fill="#f59e0b", outline="#d97706")
        c.create_rectangle(body_cx + 33, band_top + 18, body_cx + 42, band_top + 32, fill="#f59e0b", outline="#d97706")

        # 手臂（两只小短手）
        c.create_line(body_cx - body_r, body_cy + 5, body_cx - body_r - 14, body_cy + 16,
                      fill="#3b82f6", width=5, capstyle=tk.ROUND)
        c.create_line(body_cx + body_r, body_cy + 5, body_cx + body_r + 14, body_cy + 16,
                      fill="#3b82f6", width=5, capstyle=tk.ROUND)

        # 脚
        c.create_oval(body_cx - 14, body_cy + body_r - 6, body_cx - 4, body_cy + body_r + 12,
                      fill="#2563eb", outline="#1d4ed8")
        c.create_oval(body_cx + 4, body_cy + body_r - 6, body_cx + 14, body_cy + body_r + 12,
                      fill="#2563eb", outline="#1d4ed8")

    def _create_speech_bubble(self, text: str):
        """在角色上方创建/更新气泡"""
        c = self.canvas
        # 清除旧气泡
        if self.bubble_bg_id:
            c.delete(self.bubble_bg_id)
        if self.bubble_id:
            c.delete(self.bubble_id)

        if not text:
            self.bubble_bg_id = None
            self.bubble_id = None
            return

        # 气泡背景（圆角矩形）
        bubble_x = 10
        bubble_y = 8
        bubble_w = self.WIDTH - 20
        bubble_h = 50

        self.bubble_bg_id = self._create_round_rect(
            bubble_x, bubble_y, bubble_x + bubble_w, bubble_y + bubble_h,
            radius=12, fill="#1e293b", outline="#334155", width=1
        )

        # 气泡尖角
        tri_cx = self.WIDTH // 2
        tri_y = bubble_y + bubble_h
        c.create_polygon(
            tri_cx - 8, tri_y, tri_cx + 8, tri_y, tri_cx, tri_y + 12,
            fill="#1e293b", outline="#334155"
        )

        # 文字（限制宽度自动换行）
        self.bubble_id = c.create_text(
            self.WIDTH // 2, bubble_y + bubble_h // 2,
            text=text, fill="#e2e8f0", font=("Microsoft YaHei", 9, "bold"),
            width=bubble_w - 16, anchor="center", justify="center"
        )

    def _create_round_rect(self, x1, y1, x2, y2, radius=10, **kwargs):
        """绘制圆角矩形"""
        c = self.canvas
        points = [
            x1 + radius, y1,
            x2 - radius, y1,
            x2, y1,
            x2, y1 + radius,
            x2, y2 - radius,
            x2, y2,
            x2 - radius, y2,
            x1 + radius, y2,
            x1, y2,
            x1, y2 - radius,
            x1, y1 + radius,
            x1, y1,
        ]
        return c.create_polygon(points, smooth=True, **kwargs)

    def show_tip(self, skill: str, message: str):
        """显示教练建议：气泡 + 语音"""
        # 截断过长消息
        display_text = message[:80] + "…" if len(message) > 80 else message

        # 显示气泡
        self._create_speech_bubble(display_text)
        if self.speech_hide_job:
            self.root.after_cancel(self.speech_hide_job)
        self.speech_hide_job = self.root.after(
            self.SPEECH_DURATION * 1000,
            lambda: self._create_speech_bubble("")
        )

        # TTS 朗读
        def speak_text():
            self.tts.speak(message)
        t = threading.Thread(target=speak_text, daemon=True)
        t.start()

    def _toggle_mute(self):
        self.tts.muted = not self.tts.muted
        if self.tts.muted:
            self.tts.stop()

    def _test_voice(self):
        def speak():
            self.tts.speak("你好，我是你的专属教练！")
        threading.Thread(target=speak, daemon=True).start()

    def _on_drag_start(self, event):
        self._drag_x = event.x
        self._drag_y = event.y

    def _on_drag_move(self, event):
        x = self.root.winfo_x() + event.x - self._drag_x
        y = self.root.winfo_y() + event.y - self._drag_y
        self.root.geometry(f"+{x}+{y}")

    def _show_menu(self, event):
        try:
            self.context_menu.tk_popup(event.x_root, event.y_root)
        finally:
            self.context_menu.grab_release()

    def _quit(self):
        self.tts.stop()
        self.root.destroy()

    def run(self):
        self.root.mainloop()


# ═══════════════════════════════════════════════════════
# WebSocket 客户端（后台线程）
# ═══════════════════════════════════════════════════════
class TipClient:
    def __init__(self, pet: DesktopPet):
        self.pet = pet
        self.running = True
        self.ws = None

    def connect(self):
        if not HAS_WS:
            logger.error("websocket-client not installed. Run: pip install websocket-client")
            return

        while self.running:
            try:
                logger.info("Connecting to agent at %s...", WS_URL)
                self.ws = websocket.create_connection(WS_URL, timeout=10)
                logger.info("Connected to agent!")

                # 通知用户
                self.pet.root.after(0, lambda: self.pet.show_tip(
                    "教练已上线", "准备为你提供对局建议"
                ))

                while self.running:
                    raw = self.ws.recv()
                    try:
                        msg = json.loads(raw)
                        if msg.get("type") == "tip":
                            payload = msg.get("payload", {})
                            skill = payload.get("skill", "")
                            message = payload.get("message", "")
                            if message:
                                # 在主线程更新 UI
                                def update():
                                    self.pet.show_tip(skill, message)
                                self.pet.root.after(0, update)
                    except json.JSONDecodeError:
                        pass

            except websocket.WebSocketConnectionClosedException:
                logger.info("Connection closed, reconnecting in 3s...")
            except Exception as e:
                logger.warning("WS error: %s, reconnecting in 3s...", e)
            finally:
                if self.ws:
                    try:
                        self.ws.close()
                    except Exception:
                        pass
                    self.ws = None

            if self.running:
                time.sleep(3)

    def stop(self):
        self.running = False
        if self.ws:
            try:
                self.ws.close()
            except Exception:
                pass


def _start_ws(pet: DesktopPet):
    client = TipClient(pet)
    client.connect()


# ═══════════════════════════════════════════════════════
# 主入口
# ═══════════════════════════════════════════════════════
def main():
    if not HAS_TK:
        print("ERROR: tkinter is required (built-in with Python, reinstall Python with tk support)")
        sys.exit(1)

    if not HAS_WS:
        print("ERROR: websocket-client not installed. Run: pip install websocket-client")
        sys.exit(1)

    tts = TTSEngine()
    logger.info("TTS engine: %s", tts.engine_name)

    # 先创建 pet，再启动 WS 线程
    ws_thread = threading.Thread(target=_start_ws, args=(None,), daemon=True)
    pet = DesktopPet(tts, ws_thread)

    # 设置 WS 线程的 pet 引用
    ws_thread._target = _start_ws
    ws_thread._args = (pet,)

    ws_thread.start()
    pet.run()


if __name__ == "__main__":
    main()
