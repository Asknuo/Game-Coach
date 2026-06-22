"""
Desktop Pet Companion — PyQt6 入口

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

import logging
import signal
import sys

from PyQt6.QtWidgets import QApplication
from PyQt6.QtCore import Qt, QTimer

from desktop_pet.pet_controller import PetController

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("desktop_pet")


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

    QApplication.setHighDpiScaleFactorRoundingPolicy(
        Qt.HighDpiScaleFactorRoundingPolicy.PassThrough
    )

    app = QApplication(sys.argv)
    app.setApplicationName("Game Coach Desktop Pet")
    app.setQuitOnLastWindowClosed(True)

    signal.signal(signal.SIGINT, lambda *_: app.quit())
    _timer = QTimer()
    _timer.timeout.connect(lambda: None)
    _timer.start(250)

    logger.info("=" * 50)
    logger.info("Desktop Pet Companion (PyQt6)")
    logger.info("=" * 50)

    controller = PetController()
    controller.start()

    logger.info("Desktop Pet started. Left-drag to move, right-click for menu.")
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
