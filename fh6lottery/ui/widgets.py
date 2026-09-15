"""通用控件与绘图小工具。"""

from __future__ import annotations

from typing import Callable

import numpy as np
from PySide6.QtCore import Property, QEasingCurve, QPropertyAnimation, QRectF, Qt, Signal
from PySide6.QtGui import QColor, QImage, QPainter, QPen
from PySide6.QtWidgets import (QAbstractButton, QFrame, QHBoxLayout, QLabel,
                               QLineEdit, QSizePolicy, QVBoxLayout, QWidget)

from . import theme


# --------------------------------------------------------------------------- #
# 图像转换
# --------------------------------------------------------------------------- #
def numpy_to_qimage(arr: np.ndarray | None) -> QImage:
    if arr is None or getattr(arr, "size", 0) == 0:
        return QImage()
    data = np.ascontiguousarray(arr)
    h, w = data.shape[:2]
    if data.ndim == 2:
        img = QImage(data.data, w, h, w, QImage.Format.Format_Grayscale8)
    elif data.shape[2] == 4:
        img = QImage(data.data, w, h, w * 4, QImage.Format.Format_ARGB32)
    else:
        img = QImage(data.data, w, h, w * 3, QImage.Format.Format_BGR888)
    return img.copy()


class ImageView(QLabel):
    """按比例自适应显示 numpy 图像。"""

    def __init__(self, placeholder: str = "暂无画面", parent: QWidget | None = None):
        super().__init__(parent)
        self._image = QImage()
        self._placeholder = placeholder
        self.setMinimumSize(320, 180)
        self.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        self.setStyleSheet(
            f"background:{theme.BG_DEEP}; border:1px solid {theme.BORDER};"
            f"border-radius:8px; color:{theme.TEXT_FAINT};"
        )
        self.setText(placeholder)

    def set_numpy(self, arr: np.ndarray | None) -> None:
        self._image = numpy_to_qimage(arr)
        if self._image.isNull():
            self.setText(self._placeholder)
        else:
            self.setText("")
        self.update()

    def clear_image(self) -> None:
        self._image = QImage()
        self.setText(self._placeholder)
        self.update()

    def paintEvent(self, event):  # noqa: N802
        super().paintEvent(event)
        if self._image.isNull():
            return
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)
        area = self.contentsRect().adjusted(4, 4, -4, -4)
        scaled = self._image.scaled(area.size(), Qt.AspectRatioMode.KeepAspectRatio,
                                    Qt.TransformationMode.SmoothTransformation)
        x = area.x() + (area.width() - scaled.width()) // 2
        y = area.y() + (area.height() - scaled.height()) // 2
        painter.drawImage(x, y, scaled)
        painter.end()


# --------------------------------------------------------------------------- #
# 卡片与文本
# --------------------------------------------------------------------------- #
class Card(QFrame):
    def __init__(self, title: str = "", subtitle: str = "",
                 parent: QWidget | None = None):
        super().__init__(parent)
        self.setObjectName("Card")
        self._layout = QVBoxLayout(self)
        self._layout.setContentsMargins(16, 14, 16, 14)
        self._layout.setSpacing(10)
        if title:
            row = QHBoxLayout()
            row.setSpacing(8)
            label = QLabel(title)
            label.setObjectName("CardTitle")
            row.addWidget(label)
            row.addStretch(1)
            self.header = row
            self._layout.addLayout(row)
        else:
            self.header = None
        if subtitle:
            sub = QLabel(subtitle)
            sub.setObjectName("Hint")
            sub.setWordWrap(True)
            self._layout.addWidget(sub)
        self.body = self._layout

    def add(self, widget_or_layout):
        if isinstance(widget_or_layout, QWidget):
            self.body.addWidget(widget_or_layout)
        else:
            self.body.addLayout(widget_or_layout)
        return widget_or_layout


class SubCard(QFrame):
    def __init__(self, parent: QWidget | None = None):
        super().__init__(parent)
        self.setObjectName("SubCard")
        self.body = QVBoxLayout(self)
        self.body.setContentsMargins(12, 10, 12, 10)
        self.body.setSpacing(8)


class StatTile(QFrame):
    def __init__(self, label: str, unit: str = "", parent: QWidget | None = None):
        super().__init__(parent)
        self.setObjectName("SubCard")
        layout = QVBoxLayout(self)
        layout.setContentsMargins(12, 10, 12, 10)
        layout.setSpacing(2)
        self.value_label = QLabel("0")
        self.value_label.setObjectName("StatValue")
        self.label = QLabel(label + (f"（{unit}）" if unit else ""))
        self.label.setObjectName("StatLabel")
        layout.addWidget(self.value_label)
        layout.addWidget(self.label)
        self.setMinimumWidth(120)

    def set_value(self, value) -> None:
        self.value_label.setText(str(value))


def hint(text: str, wrap: bool = True) -> QLabel:
    label = QLabel(text)
    label.setObjectName("Hint")
    label.setWordWrap(wrap)
    return label


def muted(text: str, wrap: bool = True) -> QLabel:
    label = QLabel(text)
    label.setObjectName("Muted")
    label.setWordWrap(wrap)
    return label


def hline() -> QFrame:
    line = QFrame()
    line.setObjectName("HLine")
    line.setFrameShape(QFrame.Shape.HLine)
    return line


def with_unit(spin: QWidget, unit: str) -> QWidget:
    """数值控件 + 单位标签。

    单位（ms / 秒 / 次 / px …）放在控件**外部**作为独立标签，
    输入框内部只留半角数字 —— 见「UI 数值输入强制规则」第 3 条。
    传入空字符串时原样返回控件本身。
    """
    unit = (unit or "").strip()
    if not unit:
        return spin
    holder = QWidget()
    layout = QHBoxLayout(holder)
    layout.setContentsMargins(0, 0, 0, 0)
    layout.setSpacing(5)
    layout.addWidget(spin)
    layout.addWidget(muted(unit, wrap=False))
    return holder


def row(*widgets, spacing: int = 8, stretch_at_end: bool = False) -> QHBoxLayout:
    layout = QHBoxLayout()
    layout.setSpacing(spacing)
    for widget in widgets:
        if widget is None:
            layout.addStretch(1)
        elif isinstance(widget, QWidget):
            layout.addWidget(widget)
        else:
            layout.addLayout(widget)
    if stretch_at_end:
        layout.addStretch(1)
    return layout


# --------------------------------------------------------------------------- #
# 开关
# --------------------------------------------------------------------------- #
class ToggleSwitch(QAbstractButton):
    """带动画的圆角开关。"""

    def __init__(self, parent: QWidget | None = None):
        super().__init__(parent)
        self.setCheckable(True)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setFixedSize(44, 24)
        self._offset = 0.0
        self._anim = QPropertyAnimation(self, b"offset", self)
        self._anim.setDuration(140)
        self._anim.setEasingCurve(QEasingCurve.Type.OutCubic)
        self.toggled.connect(self._animate)

    def _animate(self, checked: bool) -> None:
        self._anim.stop()
        self._anim.setStartValue(self._offset)
        self._anim.setEndValue(1.0 if checked else 0.0)
        self._anim.start()

    def get_offset(self) -> float:
        return self._offset

    def set_offset(self, value: float) -> None:
        self._offset = float(value)
        self.update()

    offset = Property(float, get_offset, set_offset)

    def paintEvent(self, event):  # noqa: N802
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        track = QRectF(0, 1, self.width(), self.height() - 2)
        off_color = QColor(theme.BORDER_HI)
        on_color = QColor(theme.ACCENT)
        color = QColor(
            int(off_color.red() + (on_color.red() - off_color.red()) * self._offset),
            int(off_color.green() + (on_color.green() - off_color.green()) * self._offset),
            int(off_color.blue() + (on_color.blue() - off_color.blue()) * self._offset),
        )
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(color)
        painter.drawRoundedRect(track, track.height() / 2, track.height() / 2)
        knob_d = self.height() - 6
        x = 3 + (self.width() - knob_d - 6) * self._offset
        painter.setBrush(QColor("#FFFFFF"))
        painter.drawEllipse(QRectF(x, 3, knob_d, knob_d))
        painter.end()


def toggle_row(label: str, checked: bool, on_change: Callable[[bool], None],
               tip: str = "") -> tuple[QWidget, ToggleSwitch]:
    holder = QWidget()
    layout = QHBoxLayout(holder)
    layout.setContentsMargins(0, 0, 0, 0)
    layout.setSpacing(8)
    text = QLabel(label)
    if tip:
        text.setToolTip(tip)
    switch = ToggleSwitch()
    switch.setChecked(bool(checked))
    switch.toggled.connect(on_change)
    layout.addWidget(text)
    layout.addStretch(1)
    layout.addWidget(switch)
    return holder, switch


# --------------------------------------------------------------------------- #
# 热键输入框
# --------------------------------------------------------------------------- #
_QT_KEY_NAMES = {
    Qt.Key.Key_Return: "enter", Qt.Key.Key_Enter: "enter",
    Qt.Key.Key_Escape: "esc", Qt.Key.Key_Space: "space",
    Qt.Key.Key_Tab: "tab", Qt.Key.Key_Backspace: "backspace",
    Qt.Key.Key_Up: "up", Qt.Key.Key_Down: "down",
    Qt.Key.Key_Left: "left", Qt.Key.Key_Right: "right",
    Qt.Key.Key_Home: "home", Qt.Key.Key_End: "end",
    Qt.Key.Key_PageUp: "pageup", Qt.Key.Key_PageDown: "pagedown",
    Qt.Key.Key_Insert: "insert", Qt.Key.Key_Delete: "delete",
}
for _i in range(1, 13):
    _QT_KEY_NAMES[getattr(Qt.Key, f"Key_F{_i}")] = f"F{_i}"


class KeyCaptureEdit(QLineEdit):
    """点击后按下任意键即完成绑定，Esc 清空。"""

    captured = Signal(str)

    def __init__(self, value: str = "", parent: QWidget | None = None):
        super().__init__(parent)
        self.setReadOnly(True)
        self.setPlaceholderText("点击后按下按键")
        self.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._value = value or ""
        self.setText(self._value)
        self.setCursor(Qt.CursorShape.PointingHandCursor)

    def value(self) -> str:
        return self._value

    def set_value(self, value: str) -> None:
        self._value = value or ""
        self.setText(self._value)

    def keyPressEvent(self, event):  # noqa: N802
        key = event.key()
        if key in (Qt.Key.Key_Control, Qt.Key.Key_Shift, Qt.Key.Key_Alt,
                   Qt.Key.Key_Meta, Qt.Key.Key_unknown):
            return
        if key == Qt.Key.Key_Escape:
            self.set_value("")
            self.captured.emit("")
            return
        name = _QT_KEY_NAMES.get(key)
        if name is None:
            text = event.text()
            if text and text.isprintable() and len(text) == 1 and text.isalnum():
                name = text.lower()
        if name is None:
            return
        mods = event.modifiers()
        prefix = ""
        if mods & Qt.KeyboardModifier.ControlModifier:
            prefix += "Ctrl+"
        if mods & Qt.KeyboardModifier.AltModifier:
            prefix += "Alt+"
        if mods & Qt.KeyboardModifier.ShiftModifier:
            prefix += "Shift+"
        spec = prefix + name
        self.set_value(spec)
        self.captured.emit(spec)


# --------------------------------------------------------------------------- #
# 状态点
# --------------------------------------------------------------------------- #
class StatusDot(QWidget):
    def __init__(self, color: str = theme.TEXT_FAINT, parent: QWidget | None = None):
        super().__init__(parent)
        self._color = QColor(color)
        self.setFixedSize(12, 12)

    def set_color(self, color: str) -> None:
        self._color = QColor(color)
        self.update()

    def paintEvent(self, event):  # noqa: N802
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.setPen(QPen(self._color.lighter(130), 1))
        painter.setBrush(self._color)
        painter.drawEllipse(0.5, 0.5, 10, 10)
        painter.end()
