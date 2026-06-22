"""
Pet Widget — 白蛋 + 眼睛（极简桌宠）。
"""

import math

from PyQt6.QtCore import Qt, QTimer, QRectF, QPointF
from PyQt6.QtGui import QPainter, QPen, QColor, QRadialGradient
from PyQt6.QtWidgets import QWidget, QLabel


class PetWidget(QWidget):
    """极简桌宠：白蛋 + 眼睛。"""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)

        self._frame = 0
        self._bob = 0.0
        self._blink = 0.0
        self._blink_cd = 90
        self._dragging = False
        self._speaking = False

        self._bubble_label = QLabel(self)
        self._bubble_label.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
        self._bubble_label.setStyleSheet("""
            QLabel {
                background: rgba(255, 255, 255, 0.95);
                border: 1px solid rgba(203, 213, 225, 0.9);
                border-radius: 10px;
                padding: 6px 10px;
                color: #334155;
                font-size: 11px;
                font-family: "Microsoft YaHei";
            }
        """)
        self._bubble_label.setWordWrap(True)
        self._bubble_label.setAttribute(
            Qt.WidgetAttribute.WA_TransparentForMouseEvents, True
        )
        self._bubble_label.hide()

        self._bubble_timer = QTimer(self)
        self._bubble_timer.timeout.connect(self._on_bubble_hide)
        self._bubble_timer.setSingleShot(True)

        self._speak_timer = QTimer(self)
        self._speak_timer.setSingleShot(True)
        self._speak_timer.timeout.connect(self._on_speak_end)

        self._anim_timer = QTimer(self)
        self._anim_timer.timeout.connect(self._tick)
        self._anim_timer.start(40)

    def set_pose(self, pose: str) -> None:
        self._dragging = pose == "drag"
        self.update()

    def say(self, text: str, duration_ms: int = 5000):
        display = text[:80] + "…" if len(text) > 80 else text
        self._bubble_label.setText(display)
        self._bubble_label.adjustSize()
        bw = min(self._bubble_label.width(), self.width() - 12)
        self._bubble_label.setFixedWidth(bw)
        self._bubble_label.move((self.width() - bw) // 2, 4)
        self._bubble_label.show()
        self._bubble_label.raise_()
        self._bubble_timer.start(duration_ms)
        self._speaking = True
        self._speak_timer.start(min(duration_ms, 5000))

    def _on_bubble_hide(self):
        self._bubble_label.hide()

    def _on_speak_end(self):
        self._speaking = False

    def stop(self):
        self._anim_timer.stop()

    def _tick(self):
        self._frame += 1
        self._bob = math.sin(self._frame * 0.07) * 2

        if self._blink > 0:
            self._blink = max(0.0, self._blink - 0.22)
        else:
            self._blink_cd -= 1
            if self._blink_cd <= 0:
                self._blink = 1.0
                self._blink_cd = 85 + (self._frame % 50)

        self.update()

    def _gaze_offset(self, s: float) -> tuple[float, float]:
        if self._dragging:
            return 3.5 * s, 1.2 * s
        if self._speaking:
            pulse = math.sin(self._frame * 0.25) * 0.6 * s
            return pulse, -1.5 * s
        lx = math.sin(self._frame * 0.04) * 2.2 * s
        ly = math.sin(self._frame * 0.031 + 1.2) * 1.6 * s
        return lx, ly

    def paintEvent(self, _event):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)

        w, h = self.width(), self.height()
        s = min(w, h) / 140.0
        cx = w / 2
        cy = h * 0.55 + self._bob
        rx, ry = 46 * s, 58 * s

        if self._dragging:
            p.save()
            p.translate(cx, cy)
            p.rotate(-6)
            p.translate(-cx, -cy)

        self._draw_shadow(p, cx, cy + ry * 0.85, rx)
        self._draw_egg(p, cx, cy, rx, ry)
        self._draw_eyes(p, cx, cy, rx, ry, s)

        if self._dragging:
            self._draw_wobble_lines(p, cx, cy, s)
            p.restore()

        p.end()

    @staticmethod
    def _oval(p: QPainter, cx: float, cy: float, rx: float, ry: float):
        p.drawEllipse(QRectF(cx - rx, cy - ry, rx * 2, ry * 2))

    def _draw_shadow(self, p: QPainter, cx: float, cy: float, rx: float):
        g = QRadialGradient(cx, cy, rx * 0.9)
        g.setColorAt(0, QColor(0, 0, 0, 45))
        g.setColorAt(1, QColor(0, 0, 0, 0))
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(g)
        self._oval(p, cx, cy, rx * 0.75, rx * 0.18)

    def _draw_egg(self, p: QPainter, cx: float, cy: float, rx: float, ry: float):
        grad = QRadialGradient(cx - rx * 0.25, cy - ry * 0.35, rx * 1.4)
        grad.setColorAt(0, QColor(255, 255, 255))
        grad.setColorAt(0.7, QColor(248, 250, 252))
        grad.setColorAt(1, QColor(226, 232, 240))

        p.setPen(QPen(QColor(203, 213, 225), max(1.2, rx * 0.04)))
        p.setBrush(grad)
        self._oval(p, cx, cy, rx, ry)

        hi = QRadialGradient(cx - rx * 0.35, cy - ry * 0.45, rx * 0.45)
        hi.setColorAt(0, QColor(255, 255, 255, 180))
        hi.setColorAt(1, QColor(255, 255, 255, 0))
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(hi)
        self._oval(p, cx - rx * 0.15, cy - ry * 0.25, rx * 0.35, ry * 0.22)

    def _draw_eyes(self, p: QPainter, cx: float, cy: float, rx: float, ry: float, s: float):
        eye_y = cy - ry * 0.06
        eye_dx = rx * 0.30
        scale = 1.12 if self._speaking else 1.0
        eye_rx = rx * 0.15 * scale
        eye_ry = rx * 0.17 * scale
        gaze_x, gaze_y = self._gaze_offset(s)

        for side in (-1, 1):
            ex = cx + side * eye_dx
            self._draw_one_eye(p, ex, eye_y, eye_rx, eye_ry, gaze_x, gaze_y, s)

    def _draw_one_eye(
        self,
        p: QPainter,
        ex: float,
        ey: float,
        eye_rx: float,
        eye_ry: float,
        gaze_x: float,
        gaze_y: float,
        s: float,
    ):
        open_amt = 1.0 - min(self._blink, 1.0)

        if open_amt < 0.12:
            p.setPen(QPen(QColor(71, 85, 105), max(1.8, 2 * s), Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap))
            p.drawLine(QPointF(ex - eye_rx * 0.7, ey), QPointF(ex + eye_rx * 0.7, ey))
            return

        p.save()
        p.translate(ex, ey)
        p.scale(1.0, open_amt)
        p.translate(-ex, -ey)

        p.setPen(QPen(QColor(148, 163, 184), max(1.0, 1.2 * s)))
        p.setBrush(Qt.GlobalColor.white)
        self._oval(p, ex, ey, eye_rx, eye_ry)

        iris_rx = eye_rx * 0.62
        iris_ry = eye_ry * 0.68
        ix = ex + gaze_x * 0.35
        iy = ey + gaze_y * 0.35 + eye_ry * 0.06

        iris_grad = QRadialGradient(ix - iris_rx * 0.15, iy - iris_ry * 0.2, iris_rx)
        iris_grad.setColorAt(0, QColor(147, 197, 253))
        iris_grad.setColorAt(0.55, QColor(96, 165, 250))
        iris_grad.setColorAt(1, QColor(59, 130, 246))

        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(iris_grad)
        self._oval(p, ix, iy, iris_rx, iris_ry)

        pupil_r = iris_rx * (0.42 if self._speaking else 0.36)
        px = ix + gaze_x * 0.45
        py = iy + gaze_y * 0.45
        p.setBrush(QColor(15, 23, 42))
        self._oval(p, px, py, pupil_r, pupil_r * 1.05)

        p.setBrush(Qt.GlobalColor.white)
        self._oval(p, px - pupil_r * 0.55, py - pupil_r * 0.65, pupil_r * 0.28, pupil_r * 0.28)
        self._oval(p, px + pupil_r * 0.35, py + pupil_r * 0.25, pupil_r * 0.14, pupil_r * 0.14)

        p.restore()

        if self._speaking and open_amt > 0.5:
            p.setPen(QPen(QColor(250, 204, 21, 160), max(1.0, s)))
            p.setBrush(Qt.BrushStyle.NoBrush)
            for angle in (30, 150, 270):
                rad = math.radians(angle)
                sx = ex + math.cos(rad) * eye_rx * 1.25
                sy = ey + math.sin(rad) * eye_ry * 1.1
                p.drawLine(QPointF(sx, sy), QPointF(sx + math.cos(rad) * 3 * s, sy + math.sin(rad) * 3 * s))

    def _draw_wobble_lines(self, p: QPainter, cx: float, cy: float, s: float):
        p.setPen(QPen(QColor(148, 163, 184, 100), max(1.2, 1.5 * s), Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap))
        for i, dx in enumerate((-32, -42)):
            y = cy + (4 + i * 5) * s
            p.drawLine(QPointF(cx + dx * s, y), QPointF(cx + (dx - 8) * s, y))
