"""
Live2D 渲染组件 — 通过 QWebEngineView 加载 Live2D HTML 页面。
支持：
  A) 完整 Live2D 模式 (需 Cubism SDK for Web + model 文件)
  B) Canvas 回退模式 (内置可爱角色，开箱即用)

通过 QWebChannel 实现 Python ↔ JavaScript 双向通信。
"""

import logging
import os
from pathlib import Path

from PyQt6.QtCore import QObject, pyqtSlot, pyqtSignal, QUrl, Qt
from PyQt6.QtWebChannel import QWebChannel
from PyQt6.QtWebEngineWidgets import QWebEngineView
from PyQt6.QtWebEngineCore import QWebEngineSettings

logger = logging.getLogger("desktop_pet.live2d")

_HERE = Path(__file__).parent
_LIVE2D_HTML_DIR = _HERE / "live2d_html"


class Live2DBridge(QObject):
    """QWebChannel 桥接对象，暴露给 JavaScript 调用。"""

    ready = pyqtSignal()
    expression_changed = pyqtSignal(str)

    def __init__(self, parent=None):
        super().__init__(parent)
        self._page_loaded = False

    @pyqtSlot()
    def on_ready(self):
        """JavaScript 通知页面已就绪。"""
        self._page_loaded = True
        self.ready.emit()
        logger.info("Live2D page ready")

    @pyqtSlot(str)
    def on_expression(self, name: str):
        """JavaScript 通知表情变化。"""
        self.expression_changed.emit(name)


class Live2DWidget(QWebEngineView):
    """Live2D 渲染 Widget。

    用法:
        widget = Live2DWidget(parent_window)
        widget.show_speech_bubble("你好！")
    """

    def __init__(self, parent=None):
        super().__init__(parent)

        # 透明背景
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)

        page = self.page()
        if page:
            page.setBackgroundColor(Qt.GlobalColor.transparent)
            settings = page.settings()
            settings.setAttribute(
                QWebEngineSettings.WebAttribute.LocalContentCanAccessFileUrls, True
            )
            settings.setAttribute(
                QWebEngineSettings.WebAttribute.LocalContentCanAccessRemoteUrls, True
            )
            settings.setAttribute(
                QWebEngineSettings.WebAttribute.JavascriptEnabled, True
            )
            settings.setAttribute(
                QWebEngineSettings.WebAttribute.Accelerated2dCanvasEnabled, True
            )

        # QWebChannel 桥接
        self._channel = QWebChannel(self)
        self._bridge = Live2DBridge(self)
        self._channel.registerObject("bridge", self._bridge)
        self.page().setWebChannel(self._channel)

        # 加载 HTML
        self._html_path = _LIVE2D_HTML_DIR / "index.html"
        self._load_page()

    def _load_page(self):
        """加载 Live2D HTML 页面。"""
        if self._html_path.exists():
            url = QUrl.fromLocalFile(str(self._html_path.resolve()))
            self.load(url)
            logger.info("Live2D page loaded: %s", self._html_path)
        else:
            logger.warning("Live2D HTML not found: %s", self._html_path)
            self.setHtml(
                '<html><body style="background:transparent;display:flex;'
                'align-items:center;justify-content:center;font-family:sans-serif;'
                'color:rgba(255,255,255,0.5);font-size:14px;">'
                'Live2D 页面未找到</body></html>'
            )

    # ── Python → JavaScript API ──────────────────

    def show_speech_bubble(self, text: str):
        """显示说话气泡。"""
        js = f'showSpeechBubble({_to_js_string(text)});'
        self.page().runJavaScript(js)

    def hide_speech_bubble(self):
        """隐藏说话气泡。"""
        self.page().runJavaScript('hideSpeechBubble();')

    def talk_animation(self, enable: bool = True):
        """切换说话动画。"""
        mode = 'talk' if enable else 'idle'
        self.page().runJavaScript(
            f"if(typeof setAnimationMode === 'function') setAnimationMode('{mode}');"
        )

    def set_idle_animation(self):
        """设置为待机动画。"""
        self.page().runJavaScript(
            "if(typeof setAnimationMode === 'function') setAnimationMode('idle');"
        )

    def set_expression(self, name: str):
        """切换 Live2D 表情。"""
        self.page().runJavaScript(
            f"if(typeof setExpression === 'function') setExpression('{name}');"
        )

    @property
    def bridge(self) -> Live2DBridge:
        return self._bridge


def _to_js_string(text: str) -> str:
    """将 Python 字符串转为安全的 JS 字符串字面量。"""
    escaped = (
        text.replace("\\", "\\\\")
        .replace("'", "\\'")
        .replace("\n", "\\n")
        .replace("\r", "")
    )
    return f"'{escaped}'"
