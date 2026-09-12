"""悬浮窗：置顶显示进度，默认鼠标穿透，不抢游戏焦点。"""

from __future__ import annotations

from PySide6.QtCore import QPoint, Qt, Signal
from PySide6.QtGui import QColor, QFont, QPainter, QPainterPath, QPen
from PySide6.QtWidgets import (QFrame, QGridLayout, QHBoxLayout, QLabel,
                               QPushButton, QVBoxLayout, QWidget)

from .. import winutil
from ..engine import RecogInfo, Stats
from . import theme
from .widgets import ImageView, StatusDot

ROLE_TEXT = {
    "unknown": "未识别",
    "ready": "抽奖界面",
    "result_owned": "结果·已拥有",
    "result_new": "结果·新车",
    "sell": "出售价格",
    "confirm": "确认弹窗",
    "end": "抽奖结束",
    "blocked": "需要处理",
    "rhythm": "节奏模式",
}


class OverlayWindow(QWidget):
    startRequested = Signal()
    stopRequested = Signal()
    pauseRequested = Signal(bool)
    lockChanged = Signal(bool)
    hideRequested = Signal()
    geometryChanged = Signal(int, int)

    def __init__(self, cfg, parent: QWidget | None = None):
        super().__init__(parent)
        self.cfg = cfg
        self._locked = bool(cfg.get("overlay.locked", True))
        self._drag_offset: QPoint | None = None
        self._paused = False

        self.setWindowFlags(Qt.WindowType.FramelessWindowHint
                            | Qt.WindowType.WindowStaysOnTopHint
                            | Qt.WindowType.Tool)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
        self.setWindowTitle("FH6 抽奖助手")

        self._build()
        self.apply_config(cfg)

    # ---------------- 构建 ----------------
    def _build(self) -> None:
        outer = QVBoxLayout(self)
        outer.setContentsMargins(10, 10, 10, 10)
        self.card = QFrame()
        self.card.setObjectName("OverlayCard")
        outer.addWidget(self.card)

        layout = QVBoxLayout(self.card)
        layout.setContentsMargins(14, 12, 14, 12)
        layout.setSpacing(8)

        # 标题行
        head = QHBoxLayout()
        head.setSpacing(7)
        self.dot = StatusDot(theme.TEXT_FAINT)
        self.title = QLabel("FH6 抽奖助手")
        self.title.setFont(QFont("Microsoft YaHei UI", 10, QFont.Weight.Bold))
        self.state_label = QLabel("待机")
        self.state_label.setObjectName("Hint")
        self.lock_button = QPushButton("锁")
        self.lock_button.setFixedSize(26, 22)
        self.lock_button.setToolTip("切换鼠标穿透（穿透后只能靠快捷键操作）")
        self.lock_button.clicked.connect(lambda: self.set_locked(not self._locked))
        self.hide_button = QPushButton("×")
        self.hide_button.setFixedSize(26, 22)
        self.hide_button.setToolTip("隐藏悬浮窗（F10 可切换）")
        self.hide_button.clicked.connect(self.hideRequested.emit)
        head.addWidget(self.dot)
        head.addWidget(self.title)
        head.addStretch(1)
        head.addWidget(self.state_label)
        head.addWidget(self.lock_button)
        head.addWidget(self.hide_button)
        layout.addLayout(head)

        # 统计
        grid = QGridLayout()
        grid.setHorizontalSpacing(10)
        grid.setVerticalSpacing(2)
        self.stat_labels: dict[str, QLabel] = {}
        items = [("spins", "抽奖"), ("cars", "抽到车"), ("owned", "已拥有"),
                 ("cash", "抽奖所得"), ("credits", "出售所得"), ("total", "合计 CR"),
                 ("remaining", "剩余次数"), ("rate", "速度"), ("time", "耗时")]
        for index, (key, text) in enumerate(items):
            caption = QLabel(text)
            caption.setObjectName("StatLabel")
            value = QLabel("-")
            value.setFont(QFont("Microsoft YaHei UI", 11, QFont.Weight.Bold))
            value.setStyleSheet(f"color:{theme.ACCENT};")
            row, col = divmod(index, 3)
            grid.addWidget(caption, row * 2, col)
            grid.addWidget(value, row * 2 + 1, col)
            self.stat_labels[key] = value
        layout.addLayout(grid)

        # 当前动作 + 识别信息
        self.action_label = QLabel("等待开始")
        self.action_label.setWordWrap(True)
        self.action_label.setStyleSheet(f"color:{theme.TEXT}; font-size:12px;")
        layout.addWidget(self.action_label)
        self.recog_label = QLabel("识别：-")
        self.recog_label.setObjectName("Hint")
        self.recog_label.setWordWrap(True)
        layout.addWidget(self.recog_label)

        self.preview = ImageView("预览关闭")
        self.preview.setMinimumSize(280, 150)
        self.preview.setMaximumHeight(190)
        layout.addWidget(self.preview)

        # 按钮
        buttons = QHBoxLayout()
        buttons.setSpacing(6)
        self.start_button = QPushButton("开始 (F7)")
        self.start_button.setProperty("variant", "primary")
        self.start_button.clicked.connect(self.startRequested.emit)
        self.pause_button = QPushButton("暂停")
        self.pause_button.clicked.connect(self._toggle_pause)
        self.stop_button = QPushButton("停止 (F8)")
        self.stop_button.setProperty("variant", "danger")
        self.stop_button.clicked.connect(self.stopRequested.emit)
        buttons.addWidget(self.start_button)
        buttons.addWidget(self.pause_button)
        buttons.addWidget(self.stop_button)
        layout.addLayout(buttons)

    # ---------------- 配置与外观 ----------------
    def apply_config(self, cfg) -> None:
        self.cfg = cfg
        scale = float(cfg.get("overlay.scale", 1.0) or 1.0)
        self.setFixedWidth(int(330 * scale))
        self.card.setStyleSheet(
            f"QFrame#OverlayCard {{ background: rgba(26, 26, 46, {int(0.96 * 255)});"
            f" border: 1px solid {theme.BORDER_HI}; border-radius: 14px; }}"
        )
        self.setWindowOpacity(float(cfg.get("overlay.opacity", 0.94) or 0.94))
        show_preview = bool(cfg.get("overlay.show_preview", True))
        self.preview.setVisible(show_preview)
        self.move(int(cfg.get("overlay.x", 48) or 0), int(cfg.get("overlay.y", 48) or 0))
        self.set_locked(bool(cfg.get("overlay.locked", True)))
        self.adjustSize()

    def set_locked(self, locked: bool) -> None:
        self._locked = bool(locked)
        self.lock_button.setText("穿透" if self._locked else "锁")
        self.lock_button.setToolTip("当前：鼠标穿透（F10 切换）" if self._locked
                                    else "当前：可拖动/可点击（F10 切换）")
        self.start_button.setEnabled(not self._locked)
        self.pause_button.setEnabled(not self._locked)
        self.stop_button.setEnabled(not self._locked)
        self.hide_button.setEnabled(not self._locked)
        self.lock_button.setEnabled(not self._locked)
        winutil.set_click_through(int(self.winId()), self._locked)
        self.lockChanged.emit(self._locked)

    @property
    def locked(self) -> bool:
        return self._locked

    def showEvent(self, event):  # noqa: N802
        super().showEvent(event)
        self.adjustSize()
        winutil.set_click_through(int(self.winId()), self._locked)

    # ---------------- 数据刷新 ----------------
    def set_state(self, state: str) -> None:
        self.dot.set_color(theme.STATE_COLORS.get(state, theme.TEXT_FAINT))
        text = {"idle": "待机", "running": "运行中", "finished": "已结束", "error": "出错了"}
        self.state_label.setText(text.get(state, state))
        running = state == "running"
        self.start_button.setText("运行中" if running else "开始 (F7)")
        self.start_button.setEnabled(not running and not self._locked)
        self.stop_button.setEnabled(running and not self._locked)
        self.pause_button.setEnabled(running and not self._locked)

    def set_stats(self, stats: Stats) -> None:
        self.stat_labels["spins"].setText(str(stats.spins_started))
        self.stat_labels["cars"].setText(str(stats.car_wins))
        self.stat_labels["owned"].setText(str(stats.owned))
        self.stat_labels["cash"].setText(f"{stats.cash_credits:,}")
        self.stat_labels["credits"].setText(f"{stats.credits:,}")
        self.stat_labels["total"].setText(f"{stats.total_credits():,}")
        self.stat_labels["remaining"].setText(
            "-" if stats.remaining is None else str(stats.remaining))
        self.stat_labels["rate"].setText(f"{stats.rate_per_min():.0f}/分")
        self.stat_labels["time"].setText(stats.elapsed_text())
        if stats.last_action:
            self.action_label.setText(f"动作：{stats.last_action}")
        if stats.unknown_streak > 3:
            self.recog_label.setText(f"识别：连续未识别 {stats.unknown_streak} 次，"
                                     "可能界面变了或阈值太高")
        self._fit()

    def set_recog(self, info: RecogInfo) -> None:
        role = ROLE_TEXT.get(info.role, info.role)
        if info.source:
            self.recog_label.setText(f"识别：{role} · {info.source}"
                                     + (f"（{info.score:.2f}）" if info.score and info.score < 1 else ""))
        else:
            self.recog_label.setText(f"识别：{role}（未命中模板）")
        if self.preview.isVisible() and info.frame is not None:
            self.preview.set_numpy(info.frame)

    def _fit(self) -> None:
        self.adjustSize()

    def _toggle_pause(self) -> None:
        self._paused = not self._paused
        self.pause_button.setText("继续" if self._paused else "暂停")
        self.pauseRequested.emit(self._paused)

    # ---------------- 拖动 ----------------
    def mousePressEvent(self, event):  # noqa: N802
        if self._locked or event.button() != Qt.MouseButton.LeftButton:
            return
        self._drag_offset = event.globalPosition().toPoint() - self.frameGeometry().topLeft()

    def mouseMoveEvent(self, event):  # noqa: N802
        if self._locked or self._drag_offset is None:
            return
        self.move(event.globalPosition().toPoint() - self._drag_offset)

    def mouseReleaseEvent(self, event):  # noqa: N802
        if self._drag_offset is not None:
            self._drag_offset = None
            self.geometryChanged.emit(self.x(), self.y())

    def paintEvent(self, event):  # noqa: N802
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        path = QPainterPath()
        rect = self.rect().adjusted(1, 1, -1, -1)
        path.addRoundedRect(float(rect.x()), float(rect.y()),
                            float(rect.width()), float(rect.height()), 14, 14)
        painter.fillPath(path, QColor(26, 26, 46, int(0.96 * 255)))
        pen = QPen(QColor(theme.BORDER_HI))
        pen.setWidth(1)
        painter.setPen(pen)
        painter.drawPath(path)
        if self._locked:
            painter.setPen(QColor(theme.TEXT_FAINT))
            painter.setFont(QFont("Microsoft YaHei UI", 8))
            painter.drawText(rect.adjusted(0, 0, -10, -4),
                             Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignBottom,
                             "鼠标穿透中 · F10 切换")
        painter.end()
