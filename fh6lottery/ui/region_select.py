"""冻结画面 + 鼠标框选区域。

做法：先截一张整块虚拟桌面的静态图，再把图贴到每个屏幕的全屏无边框窗口上。
这样即使游戏在全屏独占模式下被"抢焦点"，也不会影响用户框选的位置精度。
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np
from PySide6.QtCore import QPoint, QRect, Qt
from PySide6.QtGui import QColor, QFont, QGuiApplication, QPainter, QPen
from PySide6.QtWidgets import QWidget

from .. import capture, winutil
from . import theme
from .widgets import numpy_to_qimage


@dataclass
class FrozenShot:
    image: np.ndarray
    rect: tuple[int, int, int, int]  # 虚拟桌面在屏幕坐标里的 x, y, w, h

    def crop_global(self, region: tuple[int, int, int, int]) -> np.ndarray | None:
        x, y, w, h = (int(v) for v in region)
        ox, oy = self.rect[0], self.rect[1]
        x0, y0 = x - ox, y - oy
        fh, fw = self.image.shape[:2]
        x0 = max(0, min(fw - 1, x0))
        y0 = max(0, min(fh - 1, y0))
        x1 = max(x0 + 1, min(fw, x0 + max(1, w)))
        y1 = max(y0 + 1, min(fh, y0 + max(1, h)))
        return np.ascontiguousarray(self.image[y0:y1, x0:x1])


def freeze_desktop() -> FrozenShot | None:
    image, rect = capture.grab_virtual_desktop()
    if image is None:
        return None
    return FrozenShot(image=image, rect=rect)


class _PickerWindow(QWidget):
    def __init__(self, shot: FrozenShot, phys: QRect, dpr: float,
                 prompt: str, state: "_PickerState"):
        super().__init__()
        self.state = state
        self.dpr = max(0.5, float(dpr))
        self.phys = phys
        self.prompt = prompt
        self.origin = QPoint()
        self.current = QPoint()
        self.dragging = False

        self.setWindowFlags(Qt.WindowType.FramelessWindowHint
                            | Qt.WindowType.WindowStaysOnTopHint
                            | Qt.WindowType.Tool)
        self.setCursor(Qt.CursorShape.CrossCursor)
        self.setMouseTracking(True)

        ox, oy = shot.rect[0], shot.rect[1]
        x0 = max(0, phys.x() - ox)
        y0 = max(0, phys.y() - oy)
        x1 = min(shot.image.shape[1], x0 + max(1, int(phys.width())))
        y1 = min(shot.image.shape[0], y0 + max(1, int(phys.height())))
        self._slice = numpy_to_qimage(shot.image[y0:y1, x0:x1])
        self._phys_origin = QPoint(phys.x(), phys.y())

    # ---------------- 交互 ----------------
    def mousePressEvent(self, event):  # noqa: N802
        if event.button() != Qt.MouseButton.LeftButton:
            self.state.cancel()
            return
        self.dragging = True
        self.origin = event.position().toPoint()
        self.current = self.origin
        self.update()

    def mouseMoveEvent(self, event):  # noqa: N802
        if self.dragging:
            self.current = event.position().toPoint()
            self.update()

    def mouseReleaseEvent(self, event):  # noqa: N802
        if event.button() != Qt.MouseButton.LeftButton or not self.dragging:
            return
        self.dragging = False
        selection = QRect(self.origin, self.current).normalized()
        if selection.width() < 6 or selection.height() < 6:
            self.state.cancel()
            return
        self.state.finish(self._to_physical(selection))

    def keyPressEvent(self, event):  # noqa: N802
        if event.key() == Qt.Key.Key_Escape:
            self.state.cancel()

    def _to_physical(self, local: QRect) -> tuple[int, int, int, int]:
        d = self.dpr
        return (
            int(self._phys_origin.x() + local.x() * d),
            int(self._phys_origin.y() + local.y() * d),
            int(local.width() * d),
            int(local.height() * d),
        )

    # ---------------- 绘制 ----------------
    def paintEvent(self, event):  # noqa: N802
        painter = QPainter(self)
        if not self._slice.isNull():
            painter.drawImage(self.rect(), self._slice)

        painter.fillRect(self.rect(), QColor(0, 0, 0, 110))
        selection = QRect(self.origin, self.current).normalized() if self.origin else None
        if selection and selection.width() > 0 and selection.height() > 0:
            src = QRect(int(selection.x() * self.dpr), int(selection.y() * self.dpr),
                        int(selection.width() * self.dpr), int(selection.height() * self.dpr))
            src = src.intersected(QRect(0, 0, self._slice.width(), self._slice.height()))
            if src.width() > 0 and src.height() > 0:
                painter.drawImage(selection, self._slice, src)
            pen = QPen(QColor(theme.ACCENT), 2)
            painter.setPen(pen)
            painter.setBrush(Qt.BrushStyle.NoBrush)
            painter.drawRect(selection)
            d = self.dpr
            label = f"{int(selection.width() * d)} × {int(selection.height() * d)}"
            painter.setFont(QFont("Microsoft YaHei UI", 10, QFont.Weight.Bold))
            painter.setPen(QColor(theme.TEXT))
            text_rect = QRect(selection.x(), max(0, selection.y() - 24), 200, 20)
            painter.drawText(text_rect, Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter,
                             " " + label)

        painter.setFont(QFont("Microsoft YaHei UI", 12, QFont.Weight.Bold))
        painter.setPen(QColor(theme.TEXT))
        banner = QRect(0, 40, self.width(), 34)
        painter.fillRect(banner, QColor(26, 26, 46, 210))
        painter.drawText(banner, Qt.AlignmentFlag.AlignCenter, self.prompt)
        painter.end()


class _PickerState:
    def __init__(self):
        self.result: tuple[int, int, int, int] | None = None
        self.windows: list[_PickerWindow] = []

    def finish(self, rect: tuple[int, int, int, int]) -> None:
        if self.result is None:
            x, y, w, h = rect
            vx, vy, vw, vh = winutil.virtual_screen_rect()
            x = max(vx, min(vx + vw - 1, x))
            y = max(vy, min(vy + vh - 1, y))
            w = max(2, min(vw - (x - vx), w))
            h = max(2, min(vh - (y - vy), h))
            self.result = (x, y, w, h)
        self.close_all()

    def cancel(self) -> None:
        self.result = None
        self.close_all()

    def close_all(self) -> None:
        for window in self.windows:
            window.close()
        self.windows.clear()


def pick_region(shot: FrozenShot,
                prompt: str = "拖动鼠标框选区域（Esc 取消）") -> tuple[int, int, int, int] | None:
    """在冻结画面上框选，返回屏幕物理坐标 (x, y, w, h)。"""
    state = _PickerState()
    for screen in QGuiApplication.screens():
        geom = screen.geometry()
        dpr = screen.devicePixelRatio() or 1.0
        phys = QRect(int(geom.x() * dpr), int(geom.y() * dpr),
                     int(geom.width() * dpr), int(geom.height() * dpr))
        window = _PickerWindow(shot, phys, dpr, prompt, state)
        window.setGeometry(geom)
        state.windows.append(window)

    for window in state.windows:
        window.show()
        window.raise_()
    if state.windows:
        state.windows[0].activateWindow()
        state.windows[0].setFocus()

    while state.windows:
        QGuiApplication.processEvents()
        still_open = [w for w in state.windows if w.isVisible()]
        if not still_open:
            break
        import time
        time.sleep(0.01)
    return state.result


def pick_region_interactive(parent=None,
                            prompt: str = "拖动鼠标框选区域（Esc 取消）"
                            ) -> tuple[tuple[int, int, int, int] | None, FrozenShot | None]:
    """截屏 + 框选一步到位，返回 (区域, 冻结画面)。"""
    shot = freeze_desktop()
    if shot is None:
        return None, None
    region = pick_region(shot, prompt)
    return region, shot
