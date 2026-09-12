"""概览页：选窗口、选模式、开始/停止、看统计。"""

from __future__ import annotations

from PySide6.QtWidgets import (QComboBox, QGridLayout, QHBoxLayout, QLabel,
                               QPushButton, QRadioButton, QVBoxLayout, QWidget)

from .. import winutil
from ..engine import Stats
from . import theme
from .page_base import PageBase
from .widgets import Card, StatTile, StatusDot, SubCard, hint, hline, muted, row, toggle_row


class DashboardPage(PageBase):
    title = "概览"
    description = "选定游戏窗口 → 选择运行方式 → 开始抽奖。整个过程只用到「截图 + 模拟按键」，不碰游戏内存。"

    def __init__(self, ctx, parent: QWidget | None = None):
        super().__init__(ctx, parent=parent)
        self._tiles: dict[str, StatTile] = {}
        self._build_control()
        self._build_stats()
        self._build_guide()

        ctx.stateChanged.connect(self._on_state)
        ctx.statsUpdated.connect(self._on_stats)
        ctx.configChanged.connect(self.refresh)
        ctx.pausedChanged.connect(self._on_paused)
        self._on_state("idle")
        self.refresh()

    # ------------------------------------------------------------------ #
    def _build_control(self) -> None:
        card = Card("运行控制", "提示：游戏里请把界面语言保持固定，识别效果最稳。")
        self.add(card)

        # 窗口
        win_row = QHBoxLayout()
        win_row.setSpacing(8)
        self.window_combo = QComboBox()
        self.window_combo.setMinimumWidth(420)
        self.window_combo.currentIndexChanged.connect(self._on_window_changed)
        refresh_button = QPushButton("刷新列表")
        refresh_button.setProperty("variant", "ghost")
        refresh_button.clicked.connect(self.refresh_windows)
        auto_button = QPushButton("自动识别游戏")
        auto_button.clicked.connect(self._auto_detect)
        win_row.addWidget(QLabel("游戏窗口"))
        win_row.addWidget(self.window_combo, 1)
        win_row.addWidget(refresh_button)
        win_row.addWidget(auto_button)
        card.add(win_row)

        self.window_hint = hint("")
        card.add(self.window_hint)
        card.add(hline())

        # 模式
        mode_row = QHBoxLayout()
        mode_row.setSpacing(10)
        self.mode_smart = QRadioButton("智能识别模式（推荐）")
        self.mode_rhythm = QRadioButton("节奏宏模式（保底）")
        self.mode_smart.toggled.connect(self._on_mode_changed)
        mode_row.addWidget(self.mode_smart)
        mode_row.addWidget(self.mode_rhythm)
        mode_row.addStretch(1)
        card.add(mode_row)
        card.add(muted("智能识别：每 0.2 秒截一次游戏画面，用模板 / OCR 判断当前是哪个界面，"
                       "再决定按什么键。需要先在「模板库」录几个模板（或用 OCR 关键词）。"))
        card.add(muted("节奏宏：按你编排的按键序列循环执行，不依赖识别，界面识别不准时用它兜底。"))
        card.add(hline())

        # 按钮
        buttons = QHBoxLayout()
        buttons.setSpacing(8)
        self.start_button = QPushButton("开始抽奖（F7）")
        self.start_button.setProperty("variant", "primary")
        self.start_button.setMinimumHeight(38)
        self.start_button.clicked.connect(self._start)
        self.pause_button = QPushButton("暂停（F11）")
        self.pause_button.setToolTip("全局热键可在「设置 → 全局热键」里改")
        self.pause_button.clicked.connect(self._pause)
        self.stop_button = QPushButton("停止（F8）")
        self.stop_button.setProperty("variant", "danger")
        self.stop_button.clicked.connect(self.ctx.stop)
        self.start_button.setMinimumWidth(160)
        buttons.addWidget(self.start_button)
        buttons.addWidget(self.pause_button)
        buttons.addWidget(self.stop_button)
        buttons.addStretch(1)
        card.add(buttons)

        status_row = QHBoxLayout()
        status_row.setSpacing(7)
        self.status_dot = StatusDot()
        self.status_label = QLabel("待机")
        status_row.addWidget(self.status_dot)
        status_row.addWidget(self.status_label)
        status_row.addStretch(1)
        card.add(status_row)

        self.overlay_holder, self.overlay_switch = toggle_row(
            "显示悬浮窗（游戏内随时看进度，鼠标穿透不抢焦点）",
            bool(self.ctx.config.get("overlay.enabled", True)),
            lambda value: self.ctx.show_overlay(value))
        card.add(self.overlay_holder)

    def _build_stats(self) -> None:
        card = Card("本次统计", "抽奖次数＝引擎按下开始键的次数；出售收益来自识别到的出售价格累计。")
        self.add(card)
        grid = QGridLayout()
        grid.setSpacing(10)
        items = [("spins", "抽奖次数", ""), ("owned", "已拥有车辆", ""),
                 ("new", "抽到新车", ""), ("sells", "出售次数", ""),
                 ("credits", "出售收益", "CR"), ("avg", "平均单价", "CR"),
                 ("remaining", "游戏内剩余次数", ""), ("rate", "速度", "次/分")]
        for index, (key, label, unit) in enumerate(items):
            tile = StatTile(label, unit)
            self._tiles[key] = tile
            grid.addWidget(tile, index // 4, index % 4)
        card.add(grid)
        self.elapsed_label = muted("运行时长：0 秒")
        card.add(self.elapsed_label)

    def _build_guide(self) -> None:
        card = Card("三分钟上手")
        self.add(card)
        steps = [
            "1. 进游戏 → 打开抽奖界面 → 按 F9（或点「模板库 → 捕获模板」），"
            "把「抽奖主界面」的按钮区域框下来，角色选「抽奖主界面（可开始抽奖）」。",
            "2. 手动抽一次，抽到已拥有车辆时按 F9，把结果界面里「已拥有 / 出售 / 送礼」"
            "那几行字框下来，角色选「结果界面 · 已拥有」。",
            "3. 回到这里选「智能识别模式」，在「流程」页确认已拥有车辆的处理方式，点开始。",
            "4. 万一识别不准：去「识别」页看实时画面命中了什么，调低阈值或重录模板；"
            "实在不行切「节奏宏模式」，手动编排按键序列。",
        ]
        for text in steps:
            card.add(muted(text))
        card.add(hline())
        card.add(muted("安全兜底：鼠标移到屏幕左上角立刻停止；游戏不在前台时不发按键；"
                       "任何时候 F8 停止、F11 暂停 / 继续。"))

    # ------------------------------------------------------------------ #
    def refresh(self) -> None:
        self.refresh_windows()
        mode = str(self.ctx.config.get("mode", "smart"))
        self.mode_smart.blockSignals(True)
        self.mode_rhythm.blockSignals(True)
        self.mode_smart.setChecked(mode == "smart")
        self.mode_rhythm.setChecked(mode != "smart")
        self.mode_smart.blockSignals(False)
        self.mode_rhythm.blockSignals(False)
        self.overlay_switch.blockSignals(True)
        self.overlay_switch.setChecked(bool(self.ctx.config.get("overlay.enabled", True)))
        self.overlay_switch.blockSignals(False)

    def refresh_windows(self) -> None:
        current = int(self.ctx.config.get("window.hwnd", 0) or 0)
        self.window_combo.blockSignals(True)
        self.window_combo.clear()
        windows = winutil.list_windows()
        windows.sort(key=lambda w: (w.process.lower(), w.title.lower()))
        index_to_select = -1
        for index, info in enumerate(windows):
            self.window_combo.addItem(info.label(), info.hwnd)
            if info.hwnd == current:
                index_to_select = index
        if not windows:
            self.window_combo.addItem("（没有找到可用窗口）", 0)
        elif index_to_select < 0:
            keyword = str(self.ctx.config.get("window.keyword", "") or "").lower()
            for index in range(self.window_combo.count()):
                text = self.window_combo.itemText(index).lower()
                if keyword and keyword in text:
                    index_to_select = index
                    break
        self.window_combo.setCurrentIndex(max(0, index_to_select))
        self.window_combo.blockSignals(False)
        self._update_window_hint()

    def _update_window_hint(self) -> None:
        info = winutil.window_info(int(self.ctx.config.get("window.hwnd", 0) or 0))
        if info is None:
            self.window_hint.setText("还没选窗口。建议先启动游戏，再点「自动识别游戏」。")
            return
        self.window_hint.setText(
            f"已锁定：{info.title}　客户区 {info.client[2]}×{info.client[3]}　"
            f"进程 {info.process}")

    # ------------------------------------------------------------------ #
    def _on_window_changed(self, index: int) -> None:
        hwnd = self.window_combo.itemData(index) or 0
        if not hwnd:
            return
        info = winutil.window_info(int(hwnd))
        self.ctx.config.set("window.hwnd", int(hwnd))
        if info:
            self.ctx.config.set("window.title", info.title)
            self.ctx.config.set("window.process", info.process)
            if info.process:
                self.ctx.config.set("window.keyword", info.process)
        self.ctx.save()
        self._update_window_hint()
        self.ctx.log("info", f"已选择游戏窗口：{info.title if info else hwnd}")

    def _auto_detect(self) -> None:
        if self.ctx.sync_window():
            self.refresh_windows()
        else:
            self.ctx.log("warn", "没找到像《极限竞速：地平线》的窗口，请手动从列表里选")

    def _on_mode_changed(self, checked: bool) -> None:
        mode = "smart" if checked else "rhythm"
        if not checked and not self.mode_rhythm.isChecked():
            return
        self.ctx.config.set("mode", mode)
        self.ctx.save()

    def _start(self) -> None:
        self.ctx.start()

    def _pause(self) -> None:
        running = self.ctx.engine.running
        if not running:
            return
        self.ctx.pause(not self.ctx.engine.paused)
        self.pause_button.setText("继续" if self.ctx.engine.paused else "暂停")

    def _on_state(self, state: str) -> None:
        self.status_dot.set_color(theme.STATE_COLORS.get(state, theme.TEXT_FAINT))
        text = {"idle": "待机", "running": "运行中", "finished": "已结束", "error": "出错了"}
        self.status_label.setText(text.get(state, state))
        running = state == "running"
        self.start_button.setEnabled(not running)
        self.stop_button.setEnabled(running)
        self.pause_button.setEnabled(running)
        if not running:
            self.pause_button.setText("暂停（F11）")

    def _on_paused(self, paused: bool) -> None:
        self.pause_button.setText("继续（F11）" if paused else "暂停（F11）")

    def _on_stats(self, stats: Stats) -> None:
        self._tiles["spins"].set_value(stats.spins_started)
        self._tiles["owned"].set_value(stats.owned)
        self._tiles["new"].set_value(stats.new_cars)
        self._tiles["sells"].set_value(stats.sells)
        self._tiles["credits"].set_value(f"{stats.credits:,}")
        self._tiles["avg"].set_value(f"{stats.avg_price():,}")
        self._tiles["remaining"].set_value("-" if stats.remaining is None else stats.remaining)
        self._tiles["rate"].set_value(f"{stats.rate_per_min():.1f}")
        self.elapsed_label.setText(f"运行时长：{stats.elapsed():.0f} 秒"
                                   + (f"　最近动作：{stats.last_action}" if stats.last_action else ""))
