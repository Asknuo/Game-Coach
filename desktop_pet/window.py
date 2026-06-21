"""
无边框桌面窗口 — 参考 PyQt-Frameless-Window 设计模式
- 无边框 + 半透明背景 + 始终置顶
- 鼠标拖拽移动
- 右键上下文菜单
- 可选的 DWM 阴影 (Windows 10+)
"""

import ctypes
import sys
from ctypes import wintypes

from PyQt6.QtCore import Qt, QPoint, QTimer
from PyQt6.QtGui import QAction, QMouseEvent
from PyQt6.QtWidgets import QWidget, QMenu, QApplication

# ── Win32 DWM API 常量 ──────────────────────────────
_DWMWA_USE_IMMERSIVE_DARK_MODE = 20
_DWMWA_WINDOW_CORNER_PREFERENCE = 33
_DWM_WINDOW_CORNER_ROUNDED = 2
_GWL_EXSTYLE = -20
_WS_EX_LAYERED = 0x00080000
_WS_EX_TOOLWINDOW = 0x00000080

try:
    _dwmapi = ctypes.windll.dwmapi
    _user32 = ctypes.windll.user32

    def _set_dwm_attribute(hwnd: int, attr: int, value: int) -> None:
        _dwmapi.DwmSetWindowAttribute(
            wintypes.HWND(hwnd),
            wintypes.DWORD(attr),
            ctypes.byref(wintypes.DWORD(value)),
            ctypes.sizeof(wintypes.DWORD),
        )

    def _add_dwm_shadow(hwnd: int) -> None:
        """通过扩展客户区到边框来启用 DWM 原生阴影。"""
        margins = ctypes.create_string_buffer(b"\x01\x00\x00\x00" * 4)  # MARGINS {1,1,1,1}
        _dwmapi.DwmExtendFrameIntoClientArea(wintypes.HWND(hwnd), margins)

    def _hide_from_taskbar(hwnd: int) -> None:
        """将窗口从任务栏隐藏（使用 WS_EX_TOOLWINDOW）。"""
        ex_style = _user32.GetWindowLongW(wintypes.HWND(hwnd), _GWL_EXSTYLE)
        _user32.SetWindowLongW(
            wintypes.HWND(hwnd),
            _GWL_EXSTYLE,
            wintypes.DWORD(ex_style | _WS_EX_TOOLWINDOW | _WS_EX_LAYERED),
        )

    HAS_DWM = True
except (AttributeError, OSError):
    HAS_DWM = False

    def _set_dwm_attribute(hwnd, attr, value):
        pass

    def _add_dwm_shadow(hwnd):
        pass

    def _hide_from_taskbar(hwnd):
        pass


class FramelessPetWindow(QWidget):
    """无边框桌面宠物窗口。

    参考 PyQt-Frameless-Window 的设计：
    - 使用 FramelessWindowHint 移除原生边框
    - 使用 WA_TranslucentBackground 支持透明/异形窗口
    - 手动实现拖拽移动
    - 始终置顶
    """

    def __init__(self, parent=None):
        super().__init__(parent)

        # 无边框 + 透明背景
        self.setWindowFlags(
            Qt.WindowType.FramelessWindowHint
            | Qt.WindowType.WindowStaysOnTopHint
            | Qt.WindowType.Tool
        )
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
        self.setAttribute(Qt.WidgetAttribute.WA_NoSystemBackground, True)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)

        # 鼠标拖拽位置
        self._drag_pos: QPoint | None = None

        # 右键菜单
        self._context_menu: QMenu | None = None

        # 默认尺寸
        self.resize(320, 400)

    # ── 窗口显示事件 ──────────────────────────────

    def showEvent(self, event):
        super().showEvent(event)
        # 延迟应用 DWM 属性 (确保窗口句柄已创建)
        QTimer.singleShot(100, self._apply_dwm_effects)

    def _apply_dwm_effects(self):
        """应用 Windows DWM 特效（阴影、圆角等）。"""
        if not HAS_DWM:
            return
        hwnd = int(self.winId())
        try:
            _add_dwm_shadow(hwnd)
            _hide_from_taskbar(hwnd)
        except Exception:
            pass

    # ── 窗口定位 ────────────────────────────────

    def move_to_bottom_right(self, offset_x: int = 20, offset_y: int = 60):
        """将窗口移动到屏幕右下角。"""
        screen = QApplication.primaryScreen()
        if screen:
            geom = screen.availableGeometry()
            x = geom.right() - self.width() - offset_x
            y = geom.bottom() - self.height() - offset_y
            self.move(x, y)

    def center_on_screen(self):
        """居中窗口。"""
        screen = QApplication.primaryScreen()
        if screen:
            geom = screen.availableGeometry()
            x = (geom.width() - self.width()) // 2
            y = (geom.height() - self.height()) // 2
            self.move(x, y)

    # ── 鼠标拖拽 ────────────────────────────────

    def mousePressEvent(self, event: QMouseEvent):
        if event.button() == Qt.MouseButton.LeftButton:
            self._drag_pos = event.globalPosition().toPoint() - self.frameGeometry().topLeft()
            event.accept()
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event: QMouseEvent):
        if event.buttons() & Qt.MouseButton.LeftButton and self._drag_pos is not None:
            new_pos = event.globalPosition().toPoint() - self._drag_pos
            self.move(new_pos)
            event.accept()
        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event: QMouseEvent):
        self._drag_pos = None
        super().mouseReleaseEvent(event)

    # ── 右键菜单 ────────────────────────────────

    def set_context_menu(self, menu: QMenu):
        """设置自定义右键上下文菜单。"""
        self._context_menu = menu

    def contextMenuEvent(self, event):
        if self._context_menu:
            self._context_menu.exec(event.globalPos())
        event.accept()

    # ── 便捷菜单创建 ──────────────────────────────

    @staticmethod
    def create_default_menu(
        mute_callback=None,
        test_voice_callback=None,
        quit_callback=None,
    ) -> QMenu:
        """创建默认右键菜单。"""
        menu = QMenu()

        if mute_callback:
            mute_action = QAction("静音 / 取消静音", menu)
            mute_action.triggered.connect(mute_callback)
            menu.addAction(mute_action)

        if test_voice_callback:
            test_action = QAction("测试语音", menu)
            test_action.triggered.connect(test_voice_callback)
            menu.addAction(test_action)

        menu.addSeparator()

        if quit_callback:
            quit_action = QAction("退出", menu)
            quit_action.triggered.connect(quit_callback)
            menu.addAction(quit_action)

        return menu
