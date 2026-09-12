"""应用上下文：把配置、模板库、引擎、热键、悬浮窗串起来，供各页面调用。"""

from __future__ import annotations

import datetime as _dt
import threading
import time
from typing import Any

from PySide6.QtCore import QObject, Signal
from PySide6.QtWidgets import QApplication

from . import actions, ocr as ocr_mod, paths, winutil
from .config import Config
from .engine import LotteryEngine, RecogInfo, Stats
from .hotkeys import HotkeyService
from .templates import TemplateItem, TemplateStore, get_store, role_label
from .ui.region_select import pick_region_interactive

MAX_LOGS = 800


class AppContext(QObject):
    logged = Signal(str, str)              # level, message
    templatesChanged = Signal()
    stateChanged = Signal(str)
    configChanged = Signal()
    recognitionUpdated = Signal(object)
    statsUpdated = Signal(object)

    def __init__(self, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self.config: Config = Config.load()
        self.store: TemplateStore = get_store()
        self.store.prune_missing()
        self.engine = LotteryEngine(self.config, self.store)
        self.hotkeys = HotkeyService(self)
        self.overlay = None            # 由主窗口注入
        self.window = None             # 主窗口
        self.logs: list[tuple[str, str, str]] = []

        self.engine.signals.logMessage.connect(self.log)
        self.engine.signals.statsChanged.connect(self.statsUpdated.emit)
        self.engine.signals.recognized.connect(self.recognitionUpdated.emit)
        self.engine.signals.stateChanged.connect(self._on_state)
        self.engine.signals.finished.connect(self._on_finished)
        self.hotkeys.triggered.connect(self._on_hotkey)

        ocr_mod.warm_up()

    # ------------------------------------------------------------------ #
    # 日志
    # ------------------------------------------------------------------ #
    def log(self, level: str, message: str) -> None:
        stamp = _dt.datetime.now().strftime("%H:%M:%S")
        self.logs.append((stamp, level, message))
        if len(self.logs) > MAX_LOGS:
            del self.logs[: len(self.logs) - MAX_LOGS]
        self.logged.emit(level, message)
        try:
            with (paths.LOG_DIR / "app.log").open("a", encoding="utf-8") as fh:
                fh.write(f"{_dt.datetime.now():%Y-%m-%d %H:%M:%S} [{level}] {message}\n")
        except OSError:
            pass

    # ------------------------------------------------------------------ #
    # 配置
    # ------------------------------------------------------------------ #
    def save(self) -> None:
        try:
            self.config.save()
        except OSError as exc:
            self.log("warn", f"配置保存失败：{exc}")

    def set(self, path: str, value: Any) -> None:
        self.config.set(path, value)
        self.save()

    # ------------------------------------------------------------------ #
    # 引擎
    # ------------------------------------------------------------------ #
    def sync_window(self) -> bool:
        hwnd = int(self.config.get("window.hwnd", 0) or 0)
        if hwnd and winutil.is_window_alive(hwnd):
            return True
        keyword = str(self.config.get("window.keyword", "forza") or "forza")
        info = winutil.find_window_by_keyword(keyword)
        if info is None and keyword != "forza":
            info = winutil.find_window_by_keyword("forza")
        if info is None:
            return False
        self.config.set("window.hwnd", info.hwnd)
        self.config.set("window.title", info.title)
        self.config.set("window.process", info.process)
        self.save()
        self.log("info", f"已自动锁定游戏窗口：{info.title}（{info.process}）")
        self.configChanged.emit()
        return True

    def start(self) -> bool:
        if self.engine.running:
            if self.engine.paused:
                self.engine.pause(False)
                return True
            return False
        if not self.sync_window():
            self.log("error", "找不到游戏窗口：请到「设置」页手动选择窗口")
            return False
        ok = self.engine.start()
        if ok and self.overlay is not None and not self.overlay.isVisible():
            self.show_overlay(True)
        return ok

    def stop(self) -> None:
        self.engine.stop()

    def pause(self, value: bool) -> None:
        self.engine.pause(value)

    def _on_state(self, state: str) -> None:
        self.stateChanged.emit(state)
        if self.overlay is not None:
            self.overlay.set_state(state)

    def _on_finished(self, reason: str, stats: Stats) -> None:
        success = not any(word in reason for word in ("手动停止", "异常", "识别失败", "窗口已关闭"))
        post = self.config.get("post", {}) or {}
        if post.get("enabled"):
            threading.Thread(target=actions.run_post_actions,
                             args=(self.config, stats, success, self.log),
                             daemon=True, name="post-actions").start()
        self.statsUpdated.emit(stats)

    # ------------------------------------------------------------------ #
    # 热键
    # ------------------------------------------------------------------ #
    def apply_hotkeys(self) -> tuple[list[str], list[str]]:
        mapping = dict(self.config.get("hotkeys", {}) or {})
        ok, failed = self.hotkeys.apply(mapping)
        for item in failed:
            self.log("warn", f"热键注册失败：{item}")
        if ok:
            self.log("info", f"已注册热键：{', '.join(mapping.get(a, a) for a in ok)}")
        return ok, failed

    def _on_hotkey(self, action: str) -> None:
        if action == "start":
            self.start()
        elif action == "stop":
            self.stop()
        elif action == "capture":
            self.capture_template("ready", interactive=True)
        elif action == "toggle_overlay":
            if self.overlay is None:
                return
            if not self.overlay.isVisible():
                self.show_overlay(True)
                self.overlay.set_locked(True)
                self.log("info", "已显示悬浮窗（鼠标穿透中，F10 可切换）")
            else:
                self.overlay.set_locked(not self.overlay.locked)
                self.log("info", "悬浮窗已" + ("锁定（鼠标穿透）" if self.overlay.locked else "解锁"))

    # ------------------------------------------------------------------ #
    # 悬浮窗
    # ------------------------------------------------------------------ #
    def show_overlay(self, visible: bool) -> None:
        if self.overlay is None:
            return
        self.config.set("overlay.enabled", bool(visible))
        self.save()
        if visible:
            self.overlay.show()
            self.overlay.raise_()
        else:
            self.overlay.hide()
        self.configChanged.emit()

    # ------------------------------------------------------------------ #
    # 模板采集
    # ------------------------------------------------------------------ #
    def capture_template(self, role: str, name: str = "",
                         interactive: bool = False) -> TemplateItem | None:
        """隐藏自己的窗口 → 冻结画面 → 框选 → 存为模板。"""
        if not interactive and role is None:
            return None
        overlay_visible = bool(self.overlay and self.overlay.isVisible())
        window_visible = bool(self.window and self.window.isVisible())
        if self.overlay:
            self.overlay.hide()
        if self.window:
            self.window.hide()
        QApplication.processEvents()
        time.sleep(0.35)
        region = None
        shot = None
        try:
            region, shot = pick_region_interactive(
                prompt=f"框选「{role_label(role)}」在屏幕上的样子，Esc 取消")
        finally:
            if window_visible and self.window:
                self.window.show()
                self.window.activateWindow()
            if overlay_visible and self.overlay:
                self.overlay.show()
        if not region or shot is None:
            self.log("info", "已取消模板采集")
            return None
        crop = shot.crop_global(region)
        if crop is None or crop.size == 0:
            self.log("warn", "采集失败：选中的区域无效")
            return None
        if crop.shape[0] < 6 or crop.shape[1] < 6:
            self.log("warn", "采集失败：框选区域太小了，请把界面上的关键区域框大一点")
            return None
        item = self.store.add(name or f"{role_label(role)} "
                                      f"{_dt.datetime.now():%m-%d %H:%M}",
                              role, crop,
                              float(self.config.get("smart.match_threshold", 0.86)))
        self.log("info", f"已保存模板「{item.name}」（{item.width}×{item.height}，"
                         f"角色：{role_label(role)}）")
        self.templatesChanged.emit()
        return item

    def capture_region(self, prompt: str = "框选区域（Esc 取消）"):
        """隐藏自身窗口后框选一个区域，返回 (区域, 冻结画面)。"""
        overlay_visible = bool(self.overlay and self.overlay.isVisible())
        window_visible = bool(self.window and self.window.isVisible())
        if self.overlay:
            self.overlay.hide()
        if self.window:
            self.window.hide()
        QApplication.processEvents()
        time.sleep(0.3)
        try:
            return pick_region_interactive(prompt=prompt)
        finally:
            if window_visible and self.window:
                self.window.show()
            if overlay_visible and self.overlay:
                self.overlay.show()

    def refresh_templates(self) -> None:
        self.store.load()
        self.templatesChanged.emit()

    def overlay_locked(self, locked: bool) -> None:
        self.config.set("overlay.locked", bool(locked))
        self.save()

    def overlay_moved(self, x: int, y: int) -> None:
        self.config.set("overlay.x", int(x))
        self.config.set("overlay.y", int(y))
        self.save()

    # ------------------------------------------------------------------ #
    def ocr_status(self) -> str:
        client = ocr_mod.get_ocr()
        if not client.ready:
            return "OCR 未就绪" + (f"（{client.error}）" if client.error else "（正在启动…）")
        langs = "、".join(client.languages) or "无"
        return f"OCR 就绪 · 语言 {langs} · 平均 {client.avg_ms:.0f} ms"
