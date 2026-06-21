"""
Desktop Pet Companion — PyQt6 重构版入口

启动方式:
    python -m desktop_pet.main

依赖:
    pip install PyQt6 websocket-client edge-tts

架构:
    - PyQt6 无边框窗口 (参考 PyQt-Frameless-Window)
    - QPainter 手绘角色
    - Edge-TTS 语音合成
    - WebSocket 连接 FastAPI Agent 接收教练建议
"""

import ctypes
import logging
import os
import signal
import sys

from PyQt6.QtWidgets import QApplication, QWidget
from PyQt6.QtCore import Qt, QTimer, QObject, QEvent

from desktop_pet.pet_controller import PetController

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("desktop_pet")


# ════════════════════════════════════════════════════
# 全局拖拽过滤器 — 在 QApplication 层拦截事件
# ════════════════════════════════════════════════════

class DragHelper(QObject):
    """全局事件过滤器：在应用层拦截桌宠窗口的鼠标事件，触发系统拖拽。

    绕开所有子控件（QWidget / QWebEngineView / etc），直接向 Windows 发送
    WM_NCLBUTTONDOWN + HTCAPTION 消息，由操作系统接管窗口移动。
    """

    def __init__(self, pet_window: QWidget):
        super().__init__()
        self._pet = pet_window

    def eventFilter(self, obj, event):
        if event.type() != QEvent.Type.MouseButtonPress:
            return False

        # 只处理左键
        if event.button() != Qt.MouseButton.LeftButton:
            return False

        # 直接判断点击位置是否在桌宠窗口范围内
        # (不用 widgetAt — 透明窗口可能导致返回 None)
        pt = event.globalPosition().toPoint()
        if not self._pet.frameGeometry().contains(pt):
            return False

        # 触发 Windows 系统拖拽
        try:
            hwnd = int(self._pet.winId())
            ctypes.windll.user32.ReleaseCapture()
            ctypes.windll.user32.SendMessageW(hwnd, 0x00A1, 0x0002, 0)
        except Exception:
            pass

        return True  # 吃掉事件


def check_dependencies() -> list[str]:
    """检查关键依赖，返回缺失列表。"""
    missing = []

    try:
        from PyQt6.QtWidgets import QApplication  # noqa: F401
    except ImportError:
        missing.append("PyQt6")

    try:
        import websocket  # noqa: F401
    except ImportError:
        missing.append("websocket-client")

    return missing


def main():
    """主入口。"""
    missing = check_dependencies()
    if missing:
        print(f"缺少依赖: {', '.join(missing)}")
        print("请运行: pip install PyQt6 websocket-client edge-tts")
        sys.exit(1)

    # 高 DPI 支持
    QApplication.setHighDpiScaleFactorRoundingPolicy(
        Qt.HighDpiScaleFactorRoundingPolicy.PassThrough
    )

    app = QApplication(sys.argv)
    app.setApplicationName("Game Coach Desktop Pet")
    app.setQuitOnLastWindowClosed(True)

    # Ctrl+C 退出支持
    signal.signal(signal.SIGINT, lambda *_: app.quit())
    _timer = QTimer()
    _timer.timeout.connect(lambda: None)
    _timer.start(250)

    logger.info("=" * 50)
    logger.info("Desktop Pet Companion (PyQt6)")
    logger.info("=" * 50)

    controller = PetController()
    controller.start()

    # ── 全局拖拽 (应用层拦截，绕过所有子控件) ──
    pet_window = controller.window
    if pet_window:
        drag_helper = DragHelper(pet_window)
        app.installEventFilter(drag_helper)

    logger.info("Desktop Pet started. Right-click for menu.")
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
