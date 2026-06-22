"""
无边框桌面窗口 — 参考 PyQt-Frameless-Window 设计模式
- 无边框 + 半透明背景 + 始终置顶
- 鼠标拖拽移动
- 右键上下文菜单
"""

import ctypes
from ctypes import wintypes

from PyQt6.QtCore import Qt, QPoint, QTimer, QEvent, QObject
from PyQt6.QtGui import QAction, QMouseEvent
from PyQt6.QtWidgets import QWidget, QMenu, QApplication

_GWL_EXSTYLE = -20
_WS_EX_TOOLWINDOW = 0x00000080

try:
    _user32 = ctypes.windll.user32
    HAS_WIN32 = True
except (AttributeError, OSError):
    _user32 = None
    HAS_WIN32 = False


def _hide_from_taskbar(hwnd: int) -> None:
    """隐藏任务栏图标（仅 Windows；其他平台无操作）。"""
    if not HAS_WIN32 or _user32 is None:
        return
    ex_style = _user32.GetWindowLongW(wintypes.HWND(hwnd), _GWL_EXSTYLE)
    _user32.SetWindowLongW(
        wintypes.HWND(hwnd),
        _GWL_EXSTYLE,
        wintypes.DWORD(ex_style | _WS_EX_TOOLWINDOW),
    )


class FramelessPetWindow(QWidget):
    """无边框桌面宠物窗口。"""

    def __init__(self, parent=None):
        super().__init__(parent)

        self.setWindowFlags(
            Qt.WindowType.FramelessWindowHint
            | Qt.WindowType.WindowStaysOnTopHint
        )
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)

        self._context_menu: QMenu | None = None
        self._drag_offset: QPoint | None = None
        self._drag_targets: set[QWidget] = set()
        self._drag_start_cb = None
        self._drag_end_cb = None

        self.installEventFilter(self)
        self.resize(140, 170)

    def register_drag_target(self, widget: QWidget) -> None:
        """注册可拖拽的子控件（左键拖动移动整个窗口）。"""
        widget.installEventFilter(self)
        self._drag_targets.add(widget)

    def showEvent(self, event):
        super().showEvent(event)
        if HAS_WIN32:
            QTimer.singleShot(100, self._apply_win32_effects)

    def _apply_win32_effects(self):
        try:
            _hide_from_taskbar(int(self.winId()))
        except Exception:
            pass

    def move_to_bottom_right(self, offset_x: int = 20, offset_y: int = 60):
        screen = QApplication.primaryScreen()
        if screen:
            geom = screen.availableGeometry()
            x = geom.right() - self.width() - offset_x
            y = geom.bottom() - self.height() - offset_y
            self.move(x, y)

    def center_on_screen(self):
        screen = QApplication.primaryScreen()
        if screen:
            geom = screen.availableGeometry()
            x = (geom.width() - self.width()) // 2
            y = (geom.height() - self.height()) // 2
            self.move(x, y)

    def set_context_menu(self, menu: QMenu):
        self._context_menu = menu

    def contextMenuEvent(self, event):
        if self._context_menu:
            self._context_menu.exec(event.globalPos())
        event.accept()

    def keyPressEvent(self, event):
        if event.key() == Qt.Key.Key_Escape:
            QApplication.quit()
        super().keyPressEvent(event)

    # ── 拖拽 ────────────────────────────────────

    def _is_drag_source(self, widget: QWidget) -> bool:
        return widget is self or widget in self._drag_targets

    def _global_point(self, event: QMouseEvent) -> QPoint:
        return event.globalPosition().toPoint()

    def set_drag_callbacks(self, on_start=None, on_end=None):
        self._drag_start_cb = on_start
        self._drag_end_cb = on_end

    def start_drag(self, global_pos: QPoint) -> None:
        self._drag_offset = global_pos - self.frameGeometry().topLeft()
        self.grabMouse()
        if self._drag_start_cb:
            self._drag_start_cb()

    def drag_to(self, global_pos: QPoint) -> None:
        if self._drag_offset is not None:
            self.move(global_pos - self._drag_offset)

    def end_drag(self) -> None:
        self._drag_offset = None
        if self.mouseGrabber() is self:
            self.releaseMouse()
        if self._drag_end_cb:
            self._drag_end_cb()

    def eventFilter(self, watched: QObject, event: QEvent) -> bool:
        if not isinstance(watched, QWidget) or not self._is_drag_source(watched):
            return super().eventFilter(watched, event)

        if event.type() == QEvent.Type.MouseButtonPress:
            me = event  # type: ignore[assignment]
            if isinstance(me, QMouseEvent) and me.button() == Qt.MouseButton.LeftButton:
                self.start_drag(self._global_point(me))
                return True

        if event.type() == QEvent.Type.MouseMove:
            me = event  # type: ignore[assignment]
            if (
                isinstance(me, QMouseEvent)
                and self._drag_offset is not None
                and me.buttons() & Qt.MouseButton.LeftButton
            ):
                self.drag_to(self._global_point(me))
                return True

        if event.type() == QEvent.Type.MouseButtonRelease:
            me = event  # type: ignore[assignment]
            if isinstance(me, QMouseEvent) and me.button() == Qt.MouseButton.LeftButton:
                self.end_drag()
                return True

        return super().eventFilter(watched, event)

    @staticmethod
    def create_default_menu(
        mute_callback=None,
        test_voice_callback=None,
        quit_callback=None,
    ) -> QMenu:
        menu = QMenu()

        if mute_callback:
            action = QAction("静音 / 取消静音", menu)
            action.triggered.connect(mute_callback)
            menu.addAction(action)

        if test_voice_callback:
            action = QAction("测试语音", menu)
            action.triggered.connect(test_voice_callback)
            menu.addAction(action)

        menu.addSeparator()

        if quit_callback:
            action = QAction("退出", menu)
            action.triggered.connect(quit_callback)
            menu.addAction(action)

        return menu
