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
    pausedChanged = Signal(bool)
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
        # 日志常常被截图贴到公开的 issue 里，进内存前就把本机路径 / 用户名折掉
        message = paths.sanitize_text(str(message))
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
                self.pause(False)
                return True
            return False
        if not self.sync_window():
            self.log("error", "找不到游戏窗口：请到「设置」页手动选择窗口")
            return False
        ok = self.engine.start()
        if ok:
            self.pausedChanged.emit(False)
        if ok and self.overlay is not None and not self.overlay.isVisible():
            self.show_overlay(True)
        return ok

    def stop(self) -> None:
        self.engine.stop()

    def pause(self, value: bool) -> None:
        self.engine.pause(value)
        self.pausedChanged.emit(bool(value))

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
        elif action == "pause":
            if self.engine.running:
                self.pause(not self.engine.paused)
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
    def client_size(self) -> list[int] | None:
        """当前游戏客户区尺寸，随模板一起记下来，用于跨分辨率自适应。"""
        info = winutil.window_info(int(self.config.get("window.hwnd", 0) or 0))
        if info is None:
            return None
        cw, ch = int(info.client[2]), int(info.client[3])
        if cw <= 0 or ch <= 0:
            return None
        return [cw, ch]

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
                              float(self.config.get("smart.match_threshold", 0.86)),
                              ref_size=self.client_size())
        self.log("info", f"已保存模板「{item.name}」（{item.width}×{item.height}，"
                         f"角色：{role_label(role)}）")
        self.templatesChanged.emit()
        return item

    def recapture_template(self, template_id: str) -> bool:
        """重新框选一个已有模板的图样，并刷新它记录的窗口尺寸。"""
        item = self.store.get(template_id)
        if item is None:
            return False
        overlay_visible = bool(self.overlay and self.overlay.isVisible())
        window_visible = bool(self.window and self.window.isVisible())
        if self.overlay:
            self.overlay.hide()
        if self.window:
            self.window.hide()
        QApplication.processEvents()
        time.sleep(0.35)
        try:
            region, shot = pick_region_interactive(
                prompt=f"重新框选「{item.name}」的样子，Esc 取消")
        finally:
            if window_visible and self.window:
                self.window.show()
                self.window.activateWindow()
            if overlay_visible and self.overlay:
                self.overlay.show()
        if not region or shot is None:
            self.log("info", "已取消重录")
            return False
        crop = shot.crop_global(region)
        if crop is None or crop.size == 0 or crop.shape[0] < 6 or crop.shape[1] < 6:
            self.log("warn", "重录失败：选中的区域无效或太小")
            return False
        ok = self.store.replace_image(template_id, crop, ref_size=self.client_size())
        if ok:
            self.log("info", f"已更新模板「{item.name}」的图样")
            self.templatesChanged.emit()
        return ok

    def capture_template_region(self, prompt: str = "框选这个模板的匹配范围（Esc 取消）"):
        """框选一个区域并换算成「客户区内相对坐标」。

        模板限定在这个区域里匹配，能大幅减少画面中长得像的图案导致的误判。
        """
        region, _shot = self.capture_region(prompt)
        if not region:
            return None
        info = winutil.window_info(int(self.config.get("window.hwnd", 0) or 0))
        if info is None:
            self.log("warn", "还没锁定游戏窗口，无法换算区域坐标")
            return None
        cx, cy, cw, ch = info.client
        if cw <= 0 or ch <= 0:
            return None
        x, y, w, h = (int(v) for v in region)
        rx, ry = x - cx, y - cy
        x0 = max(0, min(cw - 1, rx))
        y0 = max(0, min(ch - 1, ry))
        x1 = max(x0 + 1, min(cw, rx + w))
        y1 = max(y0 + 1, min(ch, ry + h))
        return [x0, y0, x1 - x0, y1 - y0]

    def set_template_region(self, template_id: str, region) -> None:
        cleaned = self._clean_region(region)
        self.store.update(template_id, region=cleaned)
        self.templatesChanged.emit()
        self.log("info", "已设置模板的匹配区域" if cleaned else "已清除模板的匹配区域")

    @staticmethod
    def _clean_region(region) -> list[int] | None:
        if not region:
            return None
        try:
            values = [int(v) for v in region]
        except (TypeError, ValueError):
            return None
        if len(values) != 4 or any(v < 0 for v in values):
            return None
        return values

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
