"""
Desktop Pet Companion — PyQt6 重构版入口

启动方式:
    python -m desktop_pet.main

依赖:
    pip install PyQt6 PyQt6-WebEngine websocket-client edge-tts

架构:
    - PyQt6 无边框窗口 (参考 PyQt-Frameless-Window)
    - Live2D / Canvas 角色渲染 (QWebEngineView + HTML5 Canvas)
    - Edge-TTS 语音合成
    - WebSocket 连接 FastAPI Agent 接收教练建议
"""

import logging
import os
import sys

from desktop_pet.pet_controller import PetController

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("desktop_pet")

# 减少 WebEngine 日志噪音
logging.getLogger("PyQt6.QtWebEngineCore").setLevel(logging.WARNING)


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

    # edge-tts 和 pyttsx3 是可选的（静默降级）
    return missing


def main():
    """主入口。"""
    # 检查依赖
    missing = check_dependencies()
    if missing:
        print(f"缺少依赖: {', '.join(missing)}")
        print("请运行: pip install PyQt6 PyQt6-WebEngine websocket-client edge-tts")
        sys.exit(1)

    # PyQt6 应用
    from PyQt6.QtWidgets import QApplication
    from PyQt6.QtCore import Qt

    # 高 DPI 支持
    QApplication.setHighDpiScaleFactorRoundingPolicy(
        Qt.HighDpiScaleFactorRoundingPolicy.PassThrough
    )

    app = QApplication(sys.argv)
    app.setApplicationName("Game Coach Desktop Pet")
    app.setQuitOnLastWindowClosed(True)

    logger.info("=" * 50)
    logger.info("Desktop Pet Companion (PyQt6)")
    logger.info("=" * 50)

    # 创建并启动控制器
    controller = PetController()
    controller.start()

    logger.info("Desktop Pet started. Right-click for menu.")
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
