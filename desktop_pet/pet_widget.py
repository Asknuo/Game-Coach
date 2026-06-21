"""
Pet Widget — 纯 QWidget + QPainter 手绘角色。

用 QPainter 代替 QWebEngineView 画角色：
- 无 Chromium 子窗口劫持鼠标 → 拖拽 100% 可用
- 轻量，无需 QWebEngine 依赖
"""

import math
import sys
from PyQt6.QtCore import Qt, QTimer, QPoint, QRectF
from PyQt6.QtGui import (
    QPainter, QPainterPath, QColor, QLinearGradient, QRadialGradient,
    QPen, QFont, QMouseEvent,
)
from PyQt6.QtWidgets import QWidget, QLabel, QVBoxLayout


class PetWidget(QWidget):
    """桌面宠物角色组件 — QPainter 手绘，简洁可爱。"""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
        self.setMouseTracking(True)

        # 动画状态
        self._frame = 0
        self._bob = 0.0
        self._blink_timer = 0
        self._blink_threshold = 200
        self._blinking = False

        # 说话气泡
        self._bubble_text = ""
        self._show_bubble = False
        self._bubble_label = QLabel(self)
        self._bubble_label.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
        self._bubble_label.setStyleSheet("""
            QLabel {
                background: rgba(24, 32, 52, 0.92);
                border: 1px solid rgba(120, 100, 180, 0.5);
                border-radius: 12px;
                padding: 8px 14px;
                color: #e8e0f0;
                font-size: 12px;
                font-family: "Microsoft YaHei";
            }
        """)
        self._bubble_label.setWordWrap(True)
        self._bubble_label.hide()
        self._bubble_timer = QTimer(self)
        self._bubble_timer.timeout.connect(self._hide_bubble)
        self._bubble_timer.setSingleShot(True)

        # 动画定时器
        self._anim_timer = QTimer(self)
        self._anim_timer.timeout.connect(self._tick)
        self._anim_timer.start(33)  # ~30fps

    # ──── 公开 API ────────────────

    def say(self, text: str, duration_ms: int = 5000):
        """显示对话气泡。"""
        display = text[:80] + "…" if len(text) > 80 else text
        self._bubble_label.setText(display)
        self._bubble_label.adjustSize()
        # place bubble above the character
        bw = self._bubble_label.width()
        self._bubble_label.move(
            (self.width() - bw) // 2,
            max(4, self.height() // 20),
        )
        self._bubble_label.show()
        self._bubble_label.raise_()
        self._bubble_timer.start(duration_ms)

    def _hide_bubble(self):
        self._bubble_label.hide()

    def stop(self):
        self._anim_timer.stop()

    # ──── 动画 ─────────────────────

    def _tick(self):
        self._frame = (self._frame + 1) % 100000
        self._bob = math.sin(self._frame * 0.025) * 3

        self._blink_timer += 1
        if self._blink_timer > self._blink_threshold:
            self._blinking = True
        if self._blink_timer > self._blink_threshold + 5:
            self._blink_timer = 0
            self._blinking = False
            self._blink_threshold = 170 + (self._frame % 130)

        self.update()

    # ──── 绘制 ─────────────────────

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)

        w, h = self.width(), self.height()
        s = min(w, h) / 300  # 比例因子

        cx = w / 2
        base = h * 0.65 + self._bob
        hair_sway = math.cos(self._frame * 0.02) * 2 * s
        arm_sway = math.sin(self._frame * 0.03) * 3 * s

        # ── 颜色定义 ──
        C = {
            "skin": QColor(255, 242, 235),
            "skin_d": QColor(245, 218, 200),
            "hair": QColor(70, 45, 100),
            "hair_l": QColor(115, 80, 155),
            "eye_w": QColor(255, 255, 255),
            "eye_i": QColor(80, 180, 240),
            "eye_p": QColor(10, 10, 30),
            "blush": QColor(255, 160, 150, 100),
            "dress": QColor(240, 235, 250),
            "dress_d": QColor(70, 55, 120),
            "ribbon": QColor(220, 60, 60),
            "shoe": QColor(60, 40, 20),
            "mouth": QColor(200, 80, 80),
        }

        def _r(x, y, rr):
            """椭圆半径从中心。"""
            return QRectF(x - rr[0], y - rr[1], rr[0] * 2, rr[1] * 2)

        def _draw_ellipse(ox, oy, rx, ry, color, angle=0):
            painter.save()
            painter.translate(ox, oy)
            if angle:
                painter.rotate(angle)
            painter.setBrush(color)
            painter.setPen(Qt.PenStyle.NoPen)
            painter.drawEllipse(_r(0, 0, (rx, ry)))
            painter.restore()

        # ═══════════════════════
        # 身体 (小裙子)
        # ═══════════════════════
        body_y = base + 20 * s

        # 腿
        _draw_ellipse(cx - 22 * s, body_y + 10 * s, 6 * s, 16 * s, C["skin"])
        _draw_ellipse(cx + 22 * s, body_y + 10 * s, 6 * s, 16 * s, C["skin"])

        # 鞋子
        _draw_ellipse(cx - 24 * s, body_y + 28 * s, 10 * s, 5 * s, C["shoe"])
        _draw_ellipse(cx + 24 * s, body_y + 28 * s, 10 * s, 5 * s, C["shoe"])

        # 裙子
        skirt_path = QPainterPath()
        skirt_path.moveTo(cx - 28 * s, body_y - 12 * s)
        skirt_path.quadTo(cx - 38 * s, body_y, cx - 42 * s, body_y + 14 * s)
        skirt_path.quadTo(cx, body_y + 20 * s, cx + 42 * s, body_y + 14 * s)
        skirt_path.quadTo(cx + 38 * s, body_y, cx + 28 * s, body_y - 12 * s)
        skirt_path.closeSubpath()
        painter.setBrush(C["dress_d"])
        painter.setPen(Qt.PenStyle.NoPen)
        painter.drawPath(skirt_path)

        # 上衣
        body_path = QPainterPath()
        body_path.moveTo(cx - 18 * s, body_y - 32 * s)
        body_path.quadTo(cx - 22 * s, body_y - 8 * s, cx - 26 * s, body_y - 10 * s)
        body_path.lineTo(cx + 26 * s, body_y - 10 * s)
        body_path.quadTo(cx + 22 * s, body_y - 8 * s, cx + 18 * s, body_y - 32 * s)
        body_path.quadTo(cx, body_y - 38 * s, cx - 18 * s, body_y - 32 * s)
        body_path.closeSubpath()
        painter.setBrush(C["dress"])
        painter.drawPath(body_path)

        # 手臂
        _draw_ellipse(cx - 34 * s + arm_sway, body_y - 22 * s, 5 * s, 14 * s,
                      C["skin"], -10)
        _draw_ellipse(cx + 34 * s - arm_sway, body_y - 22 * s, 5 * s, 14 * s,
                      C["skin"], 10)

        # ═══════════════════════
        # 头
        # ═══════════════════════
        head_y = body_y - 32 * s - 48 * s
        head_rx, head_ry = 48 * s, 48 * s

        # 头发 (后面部分)
        hair_path = QPainterPath()
        hair_path.moveTo(cx - 52 * s, head_y + 5 * s)
        hair_path.quadTo(cx - 58 * s, head_y + 15 * s, cx - 48 * s, head_y + 35 * s)
        hair_path.quadTo(cx - 20 * s, head_y + 55 * s, cx, head_y + 50 * s)
        hair_path.quadTo(cx + 20 * s, head_y + 55 * s, cx + 48 * s, head_y + 35 * s)
        hair_path.quadTo(cx + 58 * s, head_y + 15 * s, cx + 52 * s, head_y + 5 * s)
        hair_path.quadTo(cx + 48 * s, head_y - 48 * s, cx + 20 * s, head_y - 52 * s)
        hair_path.quadTo(cx, head_y - 56 * s, cx - 20 * s, head_y - 52 * s)
        hair_path.quadTo(cx - 48 * s, head_y - 48 * s, cx - 52 * s, head_y + 5 * s)
        hair_path.closeSubpath()
        painter.setBrush(C["hair"])
        painter.drawPath(hair_path)

        # 脸
        _draw_ellipse(cx, head_y, head_rx, head_ry, C["skin"])

        # 刘海
        bang_path = QPainterPath()
        bang_path.moveTo(cx - 48 * s, head_y - 10 * s)
        bang_path.quadTo(cx - 40 * s, head_y - 46 * s, cx - 12 * s, head_y - 52 * s)
        bang_path.quadTo(cx, head_y - 56 * s, cx + 12 * s, head_y - 52 * s)
        bang_path.quadTo(cx + 40 * s, head_y - 46 * s, cx + 48 * s, head_y - 10 * s)
        bang_path.quadTo(cx + 35 * s, head_y - 20 * s, cx + 10 * s, head_y - 8 * s)
        bang_path.quadTo(cx, head_y - 2 * s, cx - 10 * s, head_y - 8 * s)
        bang_path.quadTo(cx - 35 * s, head_y - 20 * s, cx - 48 * s, head_y - 10 * s)
        bang_path.closeSubpath()
        painter.setBrush(C["hair"])
        painter.drawPath(bang_path)

        # 侧面头发
        _draw_ellipse(cx - 46 * s, head_y + 12 * s, 8 * s, 16 * s, C["hair"])
        _draw_ellipse(cx + 46 * s, head_y + 12 * s, 8 * s, 16 * s, C["hair"])

        # ═══════════════════════
        # 五官
        # ═══════════════════════

        eye_y = head_y - 6 * s
        eye_sp = 18 * s
        eye_w, eye_h = 16 * s, (1.5 if self._blinking else 20) * s

        # 眼白
        _draw_ellipse(cx - eye_sp, eye_y, eye_w, eye_h * 0.6, C["eye_w"])
        _draw_ellipse(cx + eye_sp, eye_y, eye_w, eye_h * 0.6, C["eye_w"])

        if not self._blinking:
            # 虹膜 (渐变)
            for side in [-1, 1]:
                x = cx + side * eye_sp
                grad = QRadialGradient(x + 2 * s, eye_y + 4 * s, eye_w * 0.7)
                grad.setColorAt(0, QColor(100, 200, 255))
                grad.setColorAt(0.6, QColor(20, 100, 180))
                grad.setColorAt(1, QColor(5, 20, 60))
                painter.setBrush(grad)
                painter.setPen(Qt.PenStyle.NoPen)
                painter.drawEllipse(_r(x + 1 * s, eye_y + 4 * s, (eye_w * 0.45, eye_h * 0.35)))

                # 瞳孔
                painter.setBrush(C["eye_p"])
                painter.drawEllipse(_r(x + 2 * s, eye_y + 3 * s, (eye_w * 0.12, eye_w * 0.12)))

                # 高光
                painter.setBrush(C["eye_w"])
                painter.drawEllipse(_r(x - 3 * s, eye_y - 7 * s, (4.5 * s, 4.5 * s)))
                painter.drawEllipse(_r(x + 5 * s, eye_y + 1 * s, (2 * s, 2 * s)))

        # 眉毛
        pen = QPen(C["hair"], 1.8 * s)
        pen.setCapStyle(Qt.PenCapStyle.RoundCap)
        painter.setPen(pen)
        painter.setBrush(Qt.BrushStyle.NoBrush)
        for side in [-1, 1]:
            x = cx + side * eye_sp
            path = QPainterPath()
            path.moveTo(x - eye_w * 0.5, eye_y - eye_h * 0.5)
            path.quadTo(x, eye_y - eye_h * 0.65, x + eye_w * 0.35, eye_y - eye_h * 0.5)
            painter.drawPath(path)
        painter.setPen(Qt.PenStyle.NoPen)

        # 鼻子
        painter.setBrush(QColor(230, 190, 160))
        painter.drawEllipse(_r(cx, head_y + 8 * s, (1.8 * s, 1.8 * s)))

        # 嘴 (微笑)
        painter.setPen(QPen(C["mouth"], 2 * s, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap))
        painter.setBrush(Qt.BrushStyle.NoBrush)
        mouth_path = QPainterPath()
        mouth_path.moveTo(cx - 5 * s, head_y + 16 * s)
        mouth_path.quadTo(cx, head_y + 21 * s, cx + 5 * s, head_y + 16 * s)
        painter.drawPath(mouth_path)
        painter.setPen(Qt.PenStyle.NoPen)

        # 腮红
        for side in [-1, 1]:
            x = cx + side * (eye_sp + 5 * s)
            grad = QRadialGradient(x, head_y + 12 * s, 10 * s)
            grad.setColorAt(0, QColor(255, 150, 150, 90))
            grad.setColorAt(1, QColor(255, 150, 150, 0))
            painter.setBrush(grad)
            painter.drawEllipse(_r(x, head_y + 12 * s, (10 * s, 5 * s)))

        # ═══════════════════════
        # 蝴蝶结 (头顶)
        # ═══════════════════════
        bow_y = head_y - 46 * s
        painter.setBrush(C["ribbon"])
        painter.setPen(Qt.PenStyle.NoPen)
        for side in [-1, 1]:
            painter.drawEllipse(_r(cx + side * 9 * s, bow_y, (10 * s, 7 * s)))
        painter.setBrush(QColor(180, 40, 40))
        painter.drawEllipse(_r(cx, bow_y, (4 * s, 4 * s)))

        painter.end()

    # ──── 拖拽 (由 main.py 全局过滤器接管，此处仅作标记) ────

    def mousePressEvent(self, event: QMouseEvent):
        # 拖拽由 QApplication 全局事件过滤器处理 (见 main.py DragHelper)
        super().mousePressEvent(event)
