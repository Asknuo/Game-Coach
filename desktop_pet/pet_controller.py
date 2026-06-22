"""
桌宠控制器 — 协调所有组件：
- FramelessPetWindow (无边框窗口)
- PetWidget (QPainter 手绘角色)
- TTSEngine (语音合成)
- TipClient (WebSocket 通信)
"""

import logging
import threading

from PyQt6.QtCore import Qt, QTimer, pyqtSignal, QObject
from PyQt6.QtWidgets import QVBoxLayout, QApplication, QSizePolicy

from desktop_pet.window import FramelessPetWindow
from desktop_pet.pet_widget import PetWidget
from desktop_pet.tts_engine import TTSEngine
from desktop_pet.ws_client import TipClient

logger = logging.getLogger("desktop_pet.controller")

# ── 配置 ─────────────────────────────────────────
WINDOW_WIDTH = 140
WINDOW_HEIGHT = 170
SPEECH_DURATION_MS = 8000  # 气泡显示时长


class PetController(QObject):
    """桌宠主控制器。

    用法:
        app = QApplication(sys.argv)
        controller = PetController()
        controller.start()
        sys.exit(app.exec())
    """

    # 信号
    status_changed = pyqtSignal(str)  # 状态文字

    def __init__(self):
        super().__init__()
        self._window: FramelessPetWindow | None = None
        self._pet: PetWidget | None = None
        self._tts: TTSEngine | None = None
        self._ws_client: TipClient | None = None

    # ── 初始化 ─────────────────────────────────

    def setup_ui(self):
        """创建并配置窗口和所有 UI 组件。"""
        self._window = FramelessPetWindow()
        self._window.setWindowTitle("LOL 教练")
        self._window.resize(WINDOW_WIDTH, WINDOW_HEIGHT)
        self._window.setMinimumSize(120, 150)

        # 布局
        layout = QVBoxLayout(self._window)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        # 手绘角色（铺满窗口，确保整个区域可接收鼠标事件）
        self._pet = PetWidget(self._window)
        self._pet.setSizePolicy(
            QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding
        )
        layout.addWidget(self._pet, 1)
        self._window.register_drag_target(self._pet)
        self._window.set_drag_callbacks(
            on_start=lambda: self._pet.set_pose("drag"),
            on_end=lambda: self._pet.set_pose("idle"),
        )
        logger.info("Pet widget added to window")

        # 右键菜单
        menu = FramelessPetWindow.create_default_menu(
            mute_callback=self._toggle_mute,
            test_voice_callback=self._test_voice,
            quit_callback=self._quit,
        )
        self._window.set_context_menu(menu)

        # 定位到右下角
        self._window.move_to_bottom_right()

    def setup_services(self):
        """初始化 TTS 和 WebSocket 连接。"""
        self._tts = TTSEngine()
        logger.info("TTS engine: %s", self._tts.engine_name)

        # WebSocket 客户端
        self._ws_client = TipClient()
        self._ws_client.tip_received.connect(self._on_tip_received)
        self._ws_client.connection_changed.connect(self._on_connection_changed)
        self._ws_client.error_occurred.connect(self._on_ws_error)

    # ── 启动 / 停止 ──────────────────────────────

    def start(self):
        """启动桌宠：显示窗口 + 连接 WebSocket。"""
        self.setup_ui()
        self.setup_services()

        if self._window:
            self._window.show()

        if self._ws_client:
            self._ws_client.start()
            logger.info("WebSocket client started")

    def stop(self):
        """停止所有服务并关闭窗口。"""
        if self._tts:
            self._tts.stop()
        if self._ws_client:
            self._ws_client.stop()
        if self._window:
            self._window.close()

    # ── Tip 处理 ────────────────────────────────

    def _on_tip_received(self, skill: str, message: str):
        """收到教练建议。"""
        logger.info("[%s] %s", skill, message[:80])

        # 显示气泡
        self._show_speech_bubble(message)

        # TTS 朗读（在独立线程中避免阻塞 UI）
        if self._tts:
            tts = self._tts  # 捕获本地引用
            threading.Thread(
                target=lambda: tts.speak(message), daemon=True
            ).start()

    def _show_speech_bubble(self, text: str):
        """显示说话气泡。"""
        if self._pet:
            self._pet.say(text, SPEECH_DURATION_MS)

    # ── 连接状态 ────────────────────────────────

    def _on_connection_changed(self, connected: bool):
        """WebSocket 连接状态变化。"""
        if connected:
            self.status_changed.emit("已连接")
            self._show_speech_bubble("教练已上线！准备为你提供对局建议")
            if self._tts:
                threading.Thread(
                    target=lambda: self._tts.speak("教练已上线！"),
                    daemon=True,
                ).start()
        else:
            self.status_changed.emit("未连接")

    def _on_ws_error(self, error_msg: str):
        """WebSocket 错误。"""
        logger.error("WebSocket error: %s", error_msg)
        self.status_changed.emit(f"错误: {error_msg}")

    # ── 右键菜单操作 ──────────────────────────────

    def _toggle_mute(self):
        if self._tts:
            self._tts.muted = not self._tts.muted
            if self._tts.muted:
                self._tts.stop()
            status = "已静音" if self._tts.muted else "已取消静音"
            self._show_speech_bubble(status)

    def _test_voice(self):
        if self._tts:
            threading.Thread(
                target=lambda: self._tts.speak("你好，我是你的专属教练！"),
                daemon=True,
            ).start()
            self._show_speech_bubble("测试语音：你好，我是你的专属教练！")

    def _quit(self):
        self.stop()
        QApplication.quit()

    # ── 属性 ──────────────────────────────────

    @property
    def window(self):
        return self._window
