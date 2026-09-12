"""完成后的操作、设置、日志。"""

from __future__ import annotations

import json
import os
import subprocess

from PySide6.QtCore import Qt, QUrl
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import (QApplication, QComboBox, QDoubleSpinBox, QFileDialog,
                               QLabel, QLineEdit, QPlainTextEdit, QPushButton,
                               QSlider, QSpinBox, QTextEdit, QVBoxLayout, QWidget)

from .. import paths, winutil
from ..hotkeys import ACTION_LABELS
from . import theme
from .page_base import PageBase
from .widgets import Card, KeyCaptureEdit, hint, hline, muted, row, toggle_row


class PostPage(PageBase):
    title = "完成后的操作"
    description = "抽完设定次数后自动帮你收尾。默认全部关闭，按需打开。"

    def __init__(self, ctx, parent: QWidget | None = None):
        super().__init__(ctx, parent=parent)
        card = Card("收尾动作")
        self.add(card)
        holder, self.enabled_switch = toggle_row(
            "启用收尾动作", bool(ctx.config.get("post.enabled")),
            lambda value: self.ctx.set("post.enabled", bool(value)))
        card.add(holder)
        holder2, self.only_success = toggle_row(
            "只在正常跑完时执行（手动按 F8 停止不执行）",
            bool(ctx.config.get("post.only_on_success", True)),
            lambda value: self.ctx.set("post.only_on_success", bool(value)))
        card.add(holder2)
        card.add(hline())

        # 关机
        shutdown_row, self.shutdown_switch = toggle_row(
            "关机", bool(ctx.config.get("post.shutdown")),
            lambda value: self.ctx.set("post.shutdown", bool(value)))
        self.shutdown_delay = QSpinBox()
        self.shutdown_delay.setRange(0, 3600)
        self.shutdown_delay.setSuffix(" 秒后关机")
        self.shutdown_delay.setValue(int(ctx.config.get("post.shutdown_delay_s", 60)))
        self.shutdown_delay.setFixedWidth(140)
        self.shutdown_delay.valueChanged.connect(
            lambda value: self.ctx.set("post.shutdown_delay_s", int(value)))
        abort = QPushButton("取消关机")
        abort.setProperty("variant", "ghost")
        abort.clicked.connect(self._abort_shutdown)
        card.add(row(shutdown_row, self.shutdown_delay, abort, QLabel(""), None))
        card.add(muted("留 60 秒缓冲，反悔时点「取消关机」或运行 shutdown /a。"))

        # 关游戏
        close_row, self.close_switch = toggle_row(
            "关闭游戏", bool(ctx.config.get("post.close_game")),
            lambda value: self.ctx.set("post.close_game", bool(value)))
        force_row, self.force_switch = toggle_row(
            "关不掉时强制结束进程", bool(ctx.config.get("post.close_game_force")),
            lambda value: self.ctx.set("post.close_game_force", bool(value)))
        card.add(close_row)
        card.add(force_row)

        # 启动别的游戏
        launch_row, self.launch_switch = toggle_row(
            "打开另一个游戏 / 程序", bool(ctx.config.get("post.launch_enabled")),
            lambda value: self.ctx.set("post.launch_enabled", bool(value)))
        card.add(launch_row)
        launch_line = row()
        self.launch_edit = QLineEdit(str(ctx.config.get("post.launch_target", "")))
        self.launch_edit.setPlaceholderText("steam://rungameid/123456 或 exe 完整路径")
        self.launch_edit.editingFinished.connect(
            lambda: self.ctx.set("post.launch_target", self.launch_edit.text().strip()))
        pick = QPushButton("选择程序")
        pick.clicked.connect(self._pick_target)
        test = QPushButton("试一下")
        test.clicked.connect(self._test_launch)
        launch_line.addWidget(self.launch_edit, 1)
        launch_line.addWidget(pick)
        launch_line.addWidget(test)
        card.add(launch_line)

        # 回暂停界面 / 提示音
        esc_row, self.esc_switch = toggle_row(
            "返回暂停界面（发送 Esc）", bool(ctx.config.get("post.pause_esc")),
            lambda value: self.ctx.set("post.pause_esc", bool(value)))
        self.esc_count = QSpinBox()
        self.esc_count.setRange(1, 5)
        self.esc_count.setValue(int(ctx.config.get("post.pause_esc_count", 1)))
        self.esc_count.setSuffix(" 次")
        self.esc_count.setFixedWidth(90)
        self.esc_count.valueChanged.connect(
            lambda value: self.ctx.set("post.pause_esc_count", int(value)))
        card.add(row(esc_row, self.esc_count, None))
        sound_row, self.sound_switch = toggle_row(
            "结束时播放提示音", bool(ctx.config.get("post.sound")),
            lambda value: self.ctx.set("post.sound", bool(value)))
        card.add(sound_row)

    def _abort_shutdown(self) -> None:
        winutil.abort_shutdown()
        self.ctx.log("warn", "已发送取消关机命令")

    def _pick_target(self) -> None:
        path, _filter = QFileDialog.getOpenFileName(
            self, "选择要启动的程序", "", "程序 (*.exe *.bat *.cmd);;所有文件 (*.*)")
        if path:
            self.launch_edit.setText(path)
            self.ctx.set("post.launch_target", path)

    def _test_launch(self) -> None:
        target = self.launch_edit.text().strip()
        if not target:
            self.ctx.log("warn", "先填一个启动目标")
            return
        try:
            from ..actions import _launch
            _launch(target)
            self.ctx.log("info", f"已尝试启动：{target}")
        except Exception as exc:
            self.ctx.log("error", f"启动失败：{exc}")


class SettingsPage(PageBase):
    title = "设置"
    description = "窗口、按键、安全与热键。改完即时生效并自动保存。"

    def __init__(self, ctx, parent: QWidget | None = None):
        super().__init__(ctx, parent=parent)
        self._build_window()
        self._build_input()
        self._build_safety()
        self._build_hotkeys()
        self._build_overlay()
        self._build_data()
        ctx.configChanged.connect(self.refresh)

    # ------------------------------------------------------------------ #
    def _build_window(self) -> None:
        card = Card("游戏窗口")
        self.add(card)
        self.keyword_edit = QLineEdit(str(self.ctx.config.get("window.keyword", "forza")))
        self.keyword_edit.setPlaceholderText("窗口标题或进程名里的关键字，如 forza")
        self.keyword_edit.editingFinished.connect(self._save_keyword)
        detect = QPushButton("自动识别")
        detect.clicked.connect(self._auto_detect)
        card.add(row(QLabel("关键字"), self.keyword_edit, detect, None))
        self.window_info = hint("")
        card.add(self.window_info)
        card.add(muted("「自动识别」按关键字找最大的那个窗口，找到了就记住它，之后启动会直接用它。"))

    def _build_input(self) -> None:
        card = Card("按键发送")
        self.add(card)
        self.start_key = QComboBox()
        for name in winutil.KEY_CHOICES:
            self.start_key.addItem(name, name)
        current = str(self.ctx.config.get("input.start_key", "enter"))
        if self.start_key.findData(current) < 0:
            self.start_key.addItem(current, current)
        self.start_key.setCurrentIndex(max(0, self.start_key.findData(current)))
        self.start_key.currentIndexChanged.connect(
            lambda _i: self.ctx.set("input.start_key", self.start_key.currentData()))
        card.add(row(QLabel("在「抽奖主界面」按下的键"), self.start_key, None))

        self.input_mode = QComboBox()
        self.input_mode.addItem("全局键盘事件（推荐，游戏需在前台）", "global")
        self.input_mode.addItem("直接投递给游戏窗口（后台模式）", "message")
        self.input_mode.setCurrentIndex(
            max(0, self.input_mode.findData(str(self.ctx.config.get("input.mode", "global")))))
        self.input_mode.currentIndexChanged.connect(
            lambda _i: self.ctx.set("input.mode", self.input_mode.currentData()))
        card.add(row(QLabel("发送方式"), self.input_mode, None))
        card.add(muted("全局模式用的是系统级 SendInput，跟真人按键几乎一样；"
                       "后台模式用窗口消息，部分游戏收不到，但切出去也能继续跑。"))

        self.hold_spin = QSpinBox()
        self.hold_spin.setRange(10, 2000)
        self.hold_spin.setSuffix(" ms 按住")
        self.hold_spin.setValue(int(self.ctx.config.get("input.hold_ms", 45)))
        self.hold_spin.setFixedWidth(130)
        self.hold_spin.valueChanged.connect(
            lambda value: self.ctx.set("input.hold_ms", int(value)))
        self.delay_spin = QSpinBox()
        self.delay_spin.setRange(0, 2000)
        self.delay_spin.setSuffix(" ms 间隔")
        self.delay_spin.setValue(int(self.ctx.config.get("input.key_delay_ms", 90)))
        self.delay_spin.setFixedWidth(130)
        self.delay_spin.valueChanged.connect(
            lambda value: self.ctx.set("input.key_delay_ms", int(value)))
        self.gap_spin = QSpinBox()
        self.gap_spin.setRange(0, 5000)
        self.gap_spin.setSuffix(" ms 动作间隔")
        self.gap_spin.setValue(int(self.ctx.config.get("safety.min_action_interval_ms", 260)))
        self.gap_spin.setFixedWidth(150)
        self.gap_spin.valueChanged.connect(
            lambda value: self.ctx.set("safety.min_action_interval_ms", int(value)))
        card.add(row(QLabel("按键手感"), self.hold_spin, self.delay_spin, self.gap_spin, None))

    def _build_safety(self) -> None:
        card = Card("安全保护")
        self.add(card)
        holder, self.failsafe_switch = toggle_row(
            "鼠标移到屏幕左上角立刻停止", bool(self.ctx.config.get("safety.corner_failsafe", True)),
            lambda value: self.ctx.set("safety.corner_failsafe", bool(value)))
        card.add(holder)
        holder2, self.fg_switch = toggle_row(
            "游戏不在前台时不发按键（防止误按到别的程序）",
            bool(self.ctx.config.get("safety.require_foreground", True)),
            lambda value: self.ctx.set("safety.require_foreground", bool(value)))
        card.add(holder2)
        self.unknown_spin = QSpinBox()
        self.unknown_spin.setRange(3, 9999)
        self.unknown_spin.setSuffix(" 次后停止")
        self.unknown_spin.setValue(int(self.ctx.config.get("safety.max_unknown_before_stop", 30)))
        self.unknown_spin.setFixedWidth(150)
        self.unknown_spin.valueChanged.connect(
            lambda value: self.ctx.set("safety.max_unknown_before_stop", int(value)))
        card.add(row(QLabel("连续识别失败"), self.unknown_spin, None))
        holder3, self.failframe_switch = toggle_row(
            "识别失败停机时保存现场截图，便于照着补模板",
            bool(self.ctx.config.get("safety.save_fail_frames", True)),
            lambda value: self.ctx.set("safety.save_fail_frames", bool(value)))
        card.add(holder3)
        card.add(muted("本工具只做「截屏识别 + 模拟按键」，不读写游戏内存、不注入模块、不碰网络包。"))

    def _build_hotkeys(self) -> None:
        card = Card("全局热键", "点输入框后直接按键盘就能改绑定；按 Esc 清空则停用该热键。")
        self.add(card)
        self.hotkey_edits: dict[str, KeyCaptureEdit] = {}
        hotkeys = self.ctx.config.get("hotkeys", {}) or {}
        for action, label in ACTION_LABELS.items():
            edit = KeyCaptureEdit(str(hotkeys.get(action, "")))
            self.hotkey_edits[action] = edit
            card.add(row(QLabel(label), edit, None))
        apply_button = QPushButton("立即应用热键")
        apply_button.setProperty("variant", "primary")
        apply_button.clicked.connect(self._apply_hotkeys)
        self.hotkey_status = hint("")
        card.add(row(apply_button, self.hotkey_status, None))

    def _build_overlay(self) -> None:
        card = Card("悬浮窗")
        self.add(card)
        holder, self.overlay_switch = toggle_row(
            "显示悬浮窗", bool(self.ctx.config.get("overlay.enabled", True)),
            lambda value: self.ctx.show_overlay(bool(value)))
        card.add(holder)
        holder2, self.preview_switch = toggle_row(
            "悬浮窗里显示画面预览", bool(self.ctx.config.get("overlay.show_preview", True)),
            self._set_preview)
        card.add(holder2)

        self.opacity_slider = QSlider(Qt.Orientation.Horizontal)
        self.opacity_slider.setRange(30, 100)
        self.opacity_slider.setValue(int(float(self.ctx.config.get("overlay.opacity", 0.94)) * 100))
        self.opacity_slider.valueChanged.connect(self._set_opacity)
        card.add(row(QLabel("不透明度"), self.opacity_slider, None))

        self.scale_spin = QDoubleSpinBox()
        self.scale_spin.setRange(0.7, 1.8)
        self.scale_spin.setSingleStep(0.05)
        self.scale_spin.setValue(float(self.ctx.config.get("overlay.scale", 1.0)))
        self.scale_spin.setFixedWidth(90)
        self.scale_spin.valueChanged.connect(self._set_scale)
        reset = QPushButton("复位到左上角")
        reset.setProperty("variant", "ghost")
        reset.clicked.connect(self._reset_overlay_pos)
        card.add(row(QLabel("缩放"), self.scale_spin, reset, None))
        card.add(muted("悬浮窗默认「鼠标穿透」，点不到按钮是正常的：用 F10 切换锁定状态，"
                       "或在这里关掉再显示。"))

    def _build_data(self) -> None:
        card = Card("数据与配置")
        self.add(card)
        self.data_label = hint(f"数据目录：{paths.display_path(paths.DATA_DIR)}")
        card.add(self.data_label)
        open_button = QPushButton("打开目录")
        open_button.clicked.connect(lambda: self._open_dir(paths.DATA_DIR))
        export_button = QPushButton("导出配置")
        export_button.clicked.connect(self._export)
        import_button = QPushButton("导入配置")
        import_button.clicked.connect(self._import)
        reset_button = QPushButton("恢复默认")
        reset_button.setProperty("variant", "danger")
        reset_button.clicked.connect(self._reset)
        card.add(row(open_button, export_button, import_button, reset_button, None))
        frames_button = QPushButton("打开失败截图目录")
        frames_button.clicked.connect(lambda: self._open_dir(paths.FROZEN_DIR))
        self.update_button = QPushButton("检查更新")
        self.update_button.clicked.connect(self._check_update)
        card.add(row(frames_button, self.update_button, None))
        self.update_status = hint("")
        card.add(self.update_status)
        card.add(muted("「检查更新」只在你点它的时候联网读一次 GitHub 发布页，平时不联网。"))

    # ------------------------------------------------------------------ #
    def refresh(self) -> None:
        info = winutil.window_info(int(self.ctx.config.get("window.hwnd", 0) or 0))
        if info is None:
            self.window_info.setText("当前还没锁定游戏窗口。")
        else:
            self.window_info.setText(f"当前窗口：{info.title}　{info.client[2]}×{info.client[3]}　"
                                     f"进程 {info.process}")
        self.hotkey_status.setText("热键：" + (self.ctx.hotkeys.last_error or "全部正常"))

    def _save_keyword(self) -> None:
        self.ctx.set("window.keyword", self.keyword_edit.text().strip())

    def _auto_detect(self) -> None:
        if self.ctx.sync_window():
            self.ctx.logged.emit("info", "已自动识别并锁定游戏窗口")
        else:
            self.ctx.log("warn", "没找到匹配的窗口，请确认游戏已经在运行")

    def _apply_hotkeys(self) -> None:
        mapping = {action: edit.value() for action, edit in self.hotkey_edits.items()}
        self.ctx.config.set("hotkeys", mapping)
        self.ctx.save()
        _ok, failed = self.ctx.apply_hotkeys()
        self.hotkey_status.setText("热键：" + ("；".join(failed) if failed else "全部正常"))

    def _set_preview(self, value: bool) -> None:
        self.ctx.set("overlay.show_preview", bool(value))
        if self.ctx.overlay is not None:
            self.ctx.overlay.apply_config(self.ctx.config)

    def _set_opacity(self, value: int) -> None:
        self.ctx.set("overlay.opacity", max(0.3, value / 100.0))
        if self.ctx.overlay is not None:
            self.ctx.overlay.apply_config(self.ctx.config)

    def _set_scale(self, value: float) -> None:
        self.ctx.set("overlay.scale", float(value))
        if self.ctx.overlay is not None:
            self.ctx.overlay.apply_config(self.ctx.config)

    def _reset_overlay_pos(self) -> None:
        self.ctx.set("overlay.x", 48)
        self.ctx.set("overlay.y", 48)
        if self.ctx.overlay is not None:
            self.ctx.overlay.apply_config(self.ctx.config)

    @staticmethod
    def _open_dir(path) -> None:
        try:
            os.startfile(str(path))  # noqa: S606
        except OSError:
            pass

    def _export(self) -> None:
        path, _f = QFileDialog.getSaveFileName(self, "导出配置", "fh6-lottery-config.json",
                                               "JSON (*.json)")
        if not path:
            return
        try:
            with open(path, "w", encoding="utf-8") as fh:
                json.dump(self.ctx.config.to_dict(), fh, ensure_ascii=False, indent=2)
            self.ctx.log("info", "配置已导出")
        except OSError as exc:
            self.ctx.log("error", f"导出失败：{exc}")

    def _import(self) -> None:
        path, _f = QFileDialog.getOpenFileName(self, "导入配置", "", "JSON (*.json)")
        if not path:
            return
        try:
            with open(path, encoding="utf-8") as fh:
                data = json.load(fh)
        except (OSError, ValueError) as exc:
            self.ctx.log("error", f"读取配置失败：{exc}")
            return
        if not isinstance(data, dict):
            self.ctx.log("error", "配置格式不对")
            return
        self.ctx.config.merge(data)
        self.ctx.save()
        self.ctx.apply_hotkeys()
        self.ctx.configChanged.emit()
        self.ctx.log("info", "配置已导入")

    def _reset(self) -> None:
        self.ctx.config.reset()
        self.ctx.save()
        self.ctx.configChanged.emit()
        self.ctx.log("warn", "已恢复默认设置")

    def _check_update(self) -> None:
        from .. import __version__
        from ..update import check_latest
        self.update_status.setText("正在检查…（最多 6 秒）")
        QApplication.processEvents()
        tag, url, error = check_latest()
        if error:
            message = f"检查更新失败：{error}"
        elif tag and tag.lstrip("vV") != __version__:
            QDesktopServices.openUrl(QUrl(url))
            message = f"发现新版本 {tag}（当前 {__version__}），已在浏览器打开发布页"
        else:
            message = f"已是最新版本（{__version__}）"
        self.update_status.setText(message)
        self.ctx.log("info", message)


class LogPage(PageBase):
    title = "日志"
    description = "运行过程中的每一步都在这里，出问题时截图这一段就行（已经不含本机用户名与路径细节）。"

    def __init__(self, ctx, parent: QWidget | None = None):
        super().__init__(ctx, parent=parent)
        card = Card("运行日志")
        self.add(card)
        self.view = QPlainTextEdit()
        self.view.setReadOnly(True)
        self.view.setMinimumHeight(380)
        self.view.setStyleSheet(f"background:{theme.BG_DEEP};")
        card.add(self.view)

        self.autoscroll = QPushButton("自动滚动：开")
        self.autoscroll.setCheckable(True)
        self.autoscroll.setChecked(True)
        self.autoscroll.clicked.connect(
            lambda: self.autoscroll.setText(
                "自动滚动：" + ("开" if self.autoscroll.isChecked() else "关")))
        clear = QPushButton("清空")
        clear.setProperty("variant", "ghost")
        clear.clicked.connect(self._clear)
        save = QPushButton("另存为")
        save.clicked.connect(self._save)
        folder = QPushButton("打开日志目录")
        folder.clicked.connect(lambda: SettingsPage._open_dir(paths.LOG_DIR))
        card.add(row(self.autoscroll, clear, save, folder, None))

        ctx.logged.connect(self._append)
        self._replay()

    def refresh(self) -> None:
        self._replay()

    def _replay(self) -> None:
        self.view.clear()
        for stamp, _level, message in self.ctx.logs:
            self.view.appendPlainText(f"[{stamp}] {message}")

    def _append(self, level: str, message: str) -> None:
        import datetime as _dt
        stamp = _dt.datetime.now().strftime("%H:%M:%S")
        prefix = {"info": "·", "warn": "!", "error": "×"}.get(level, "·")
        self.view.appendPlainText(f"[{stamp}] {prefix} {message}")
        if self.autoscroll.isChecked():
            self.view.verticalScrollBar().setValue(self.view.verticalScrollBar().maximum())

    def _clear(self) -> None:
        self.view.clear()
        self.ctx.logs.clear()

    def _save(self) -> None:
        path, _f = QFileDialog.getSaveFileName(self, "保存日志", "fh6-lottery-log.txt",
                                               "文本 (*.txt)")
        if not path:
            return
        try:
            with open(path, "w", encoding="utf-8") as fh:
                fh.write(self.view.toPlainText())
            self.ctx.log("info", "日志已保存")
        except OSError as exc:
            self.ctx.log("error", f"保存失败：{exc}")
