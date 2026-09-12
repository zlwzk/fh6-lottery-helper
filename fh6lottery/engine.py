"""抽奖引擎：两种模式 + 安全保护 + 统计。

- smart  识别驱动：截屏 → 模板/OCR 判定当前界面角色 → 收发按键
- rhythm 节奏宏：按用户编排的按键序列循环，不依赖识别（识别失败时的保底方案）

线程模型：一个工作线程跑循环，通过 Qt 信号把状态推给界面。
"""

from __future__ import annotations

import copy
import datetime as _dt
import threading
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Any

import numpy as np
from PySide6.QtCore import QObject, Signal

from . import capture, matcher, ocr as ocr_mod, paths, winutil
from .templates import TemplateStore, get_store

PREVIEW_MAX_WIDTH = 460
ROLE_PRIORITY = ["end", "sell", "result_owned", "result_new", "confirm", "blocked", "ready"]
# 「已处理过的静态画面」：这些角色的界面按一次键就会切走
STATE_ROLES = ("result_owned", "result_new", "sell", "confirm", "end")
START_GUARD_S = 0.6          # 按下「开始抽奖」后的起步保护时间
FAIL_FRAME_KEEP = 30         # 失败现场截图最多保留张数


class EngineState(str, Enum):
    IDLE = "idle"
    RUNNING = "running"
    FINISHED = "finished"
    ERROR = "error"


@dataclass
class Stats:
    spins_started: int = 0
    results: int = 0
    owned: int = 0
    new_cars: int = 0
    sells: int = 0
    credits: int = 0
    loops: int = 0
    actions: int = 0
    prices: list[int] = field(default_factory=list)
    remaining: int | None = None
    started_at: float = 0.0
    ended_at: float = 0.0
    last_action: str = ""
    last_state: str = "待机"
    last_source: str = ""
    last_score: float = 0.0
    unknown_streak: int = 0

    def elapsed(self) -> float:
        if not self.started_at:
            return 0.0
        end = self.ended_at or time.time()
        return max(0.0, end - self.started_at)

    def rate_per_min(self) -> float:
        elapsed = self.elapsed()
        if elapsed < 1.0:
            return 0.0
        done = self.spins_started if self.spins_started else self.loops
        return done / elapsed * 60.0

    def avg_price(self) -> int:
        if not self.prices:
            return 0
        return int(sum(self.prices) / len(self.prices))

    def clone(self) -> "Stats":
        return copy.deepcopy(self)


@dataclass
class RecogInfo:
    role: str = "unknown"
    source: str = ""
    score: float = 0.0
    box: tuple[int, int, int, int] | None = None
    hits: list[tuple[str, str, float, tuple[int, int, int, int]]] = field(default_factory=list)
    texts: dict[str, str] = field(default_factory=dict)
    frame: np.ndarray | None = None
    frame_size: tuple[int, int] = (0, 0)
    timestamp: float = 0.0
    note: str = ""


class EngineSignals(QObject):
    stateChanged = Signal(str)
    statsChanged = Signal(object)
    logMessage = Signal(str, str)
    recognized = Signal(object)
    finished = Signal(str, object)


class EngineError(RuntimeError):
    pass


def _preview(image: np.ndarray) -> np.ndarray | None:
    if image is None:
        return None
    import cv2
    h, w = image.shape[:2]
    if w <= PREVIEW_MAX_WIDTH:
        return image
    scale = PREVIEW_MAX_WIDTH / float(w)
    return cv2.resize(image, (PREVIEW_MAX_WIDTH, max(1, int(h * scale))),
                      interpolation=cv2.INTER_AREA)


class _TplEntry:
    __slots__ = ("id", "name", "role", "threshold", "gray", "region", "scales")

    def __init__(self, item, gray, scales=None):
        self.id = item.id
        self.name = item.name
        self.role = item.role
        self.threshold = item.threshold
        self.gray = gray
        self.region = list(item.region) if getattr(item, "region", None) else None
        self.scales = tuple(scales) if scales else None


class LotteryEngine:
    def __init__(self, config, store: TemplateStore | None = None) -> None:
        self.cfg = config
        self.store = store or get_store()
        self.signals = EngineSignals()
        self.stats = Stats()
        self._thread: threading.Thread | None = None
        self._stop_flag = threading.Event()
        self._paused = threading.Event()
        self._state = EngineState.IDLE
        self._lock = threading.RLock()
        self._ocr = ocr_mod.get_ocr()
        self._roles_cache: dict[str, Any] = {}
        self._last_preview_emit = 0.0
        self._cooldown_until = 0.0
        self._waiting_result_until = 0.0
        self._last_ocr_at = 0.0
        self._last_sig = ""
        self._handled_sig = ""
        self._handled_at = 0.0
        self._foreground_warn_at = 0.0
        self._exit_reason = ""

    # ------------------------------------------------------------------ #
    # 对外接口
    # ------------------------------------------------------------------ #
    @property
    def state(self) -> EngineState:
        return self._state

    @property
    def running(self) -> bool:
        return self._state == EngineState.RUNNING

    @property
    def paused(self) -> bool:
        return self._paused.is_set()

    def start(self) -> bool:
        if self.running:
            return False
        ok, message = self.validate()
        if not ok:
            self._log("error", message)
            return False
        self.stats = Stats(started_at=time.time())
        self._stop_flag.clear()
        self._paused.clear()
        self._cooldown_until = 0.0
        self._waiting_result_until = 0.0
        self._last_sig = self._handled_sig = ""
        self._handled_at = 0.0
        self._exit_reason = ""
        self._refresh_roles()
        self._set_state(EngineState.RUNNING)
        mode = self.cfg.get("mode", "smart")
        self._log("info", f"开始运行（模式：{'智能识别' if mode == 'smart' else '节奏宏'}）")
        self._thread = threading.Thread(target=self._run, daemon=True, name="lottery-engine")
        self._thread.start()
        return True

    def pause(self, value: bool = True) -> None:
        if value:
            self._paused.set()
            self._log("warn", "已暂停（继续仍可用 F7 / 点开始）")
        else:
            self._paused.clear()
            self._log("info", "已继续")

    def stop(self, reason: str = "手动停止") -> None:
        if self._state != EngineState.RUNNING:
            return
        self._exit_reason = reason
        self._stop_flag.set()

    def validate(self) -> tuple[bool, str]:
        hwnd = int(self.cfg.get("window.hwnd", 0) or 0)
        if not hwnd or not winutil.is_window_alive(hwnd):
            info = winutil.find_window_by_keyword(str(self.cfg.get("window.keyword", "")))
            if info is None:
                return False, "还没选游戏窗口：请到「设置」页选择游戏窗口（或填一个标题关键字）"
            self.cfg.set("window.hwnd", info.hwnd)
            self.cfg.set("window.title", info.title)
            self.cfg.set("window.process", info.process)

        mode = self.cfg.get("mode", "smart")
        if mode == "smart":
            smart = self.cfg.get("smart", {}) or {}
            policy = smart.get("owned_policy", "garage")
            if not smart.get("use_templates", True) and not smart.get("use_ocr", True):
                return False, "识别方式至少要开启「模板匹配」或「OCR 关键词」其中之一"
            if policy != "garage" and not self.store.by_role("result_owned"):
                bound = any(r.get("enabled") and r.get("role") == "result_owned"
                            for r in (smart.get("keyword_rules") or []))
                if not bound:
                    return False, ("处理「已拥有车辆」选了送礼/出售，需要能识别出选项界面："
                                   "请给「结果界面 · 已拥有」绑定一个模板，"
                                   "或启用一条角色为它的 OCR 关键词规则")
        return True, ""

    # ------------------------------------------------------------------ #
    # 状态与日志
    # ------------------------------------------------------------------ #
    def _set_state(self, state: EngineState) -> None:
        self._state = state
        self.signals.stateChanged.emit(state.value)

    def _log(self, level: str, message: str) -> None:
        self.signals.logMessage.emit(level, message)

    def _emit_stats(self) -> None:
        self.signals.statsChanged.emit(self.stats.clone())

    def _refresh_roles(self) -> None:
        """把模板库里各角色的模板抓成可匹配的条目，并算好各自的缩放档位。"""
        smart = self.cfg.get("smart", {}) or {}
        base = (0.95, 1.0, 1.05) if smart.get("multi_scale") else (1.0,)
        auto = bool(smart.get("auto_scale", True))
        cur_w, cur_h = self._client_size()
        entries: dict[str, list[_TplEntry]] = {role: [] for role in ROLE_PRIORITY}
        for item in self.store.items:
            gray = self.store.gray(item.id)
            if gray is None:
                continue
            scales = self._scales_for(item, base, auto, cur_w, cur_h)
            entries.setdefault(item.role, []).append(_TplEntry(item, gray, scales))
        self._roles_cache = entries

    def _client_size(self) -> tuple[int, int]:
        info = winutil.window_info(int(self.cfg.get("window.hwnd", 0) or 0))
        if info is None:
            return 0, 0
        return int(info.client[2]), int(info.client[3])

    @staticmethod
    def _scales_for(item, base: tuple[float, ...], auto: bool,
                    cur_w: int, cur_h: int) -> tuple[float, ...]:
        """按「当前客户区宽 / 采集时客户区宽」推算缩放档位。

        换了分辨率或改了窗口大小后，模板不用重录也能对上。
        """
        if not auto:
            return base
        ref = getattr(item, "ref_size", None) or None
        if not ref or cur_w <= 0:
            return base
        try:
            ref_w = int(ref[0])
        except (TypeError, ValueError, IndexError):
            return base
        if ref_w <= 0:
            return base
        ratio = cur_w / float(ref_w)
        if abs(ratio - 1.0) < 0.01 or not (0.5 <= ratio <= 2.0):
            return base
        scales = {round(ratio, 4), round(ratio * 0.98, 4), round(ratio * 1.02, 4), 1.0}
        return tuple(sorted(s for s in scales if 0.4 <= s <= 2.5))

    # ------------------------------------------------------------------ #
    # 主循环
    # ------------------------------------------------------------------ #
    def _run(self) -> None:
        try:
            mode = self.cfg.get("mode", "smart")
            if mode == "rhythm":
                self._run_rhythm()
            else:
                self._run_smart()
        except Exception as exc:  # pragma: no cover - 兜底
            self._log("error", f"运行出错了：{exc}")
            self._set_state(EngineState.ERROR)
            self.signals.finished.emit(f"异常：{exc}", self.stats.clone())
            return
        reason = self._exit_reason or "已结束"
        self.stats.ended_at = time.time()
        self._emit_stats()
        self._set_state(EngineState.FINISHED)
        self._log("info", f"结束：{reason}")
        self.signals.finished.emit(reason, self.stats.clone())
        capture.close_all()

    # ---------------- 节奏宏 ----------------
    def _run_rhythm(self) -> None:
        rhythm = self.cfg.get("rhythm", {}) or {}
        steps = list(rhythm.get("steps") or [])
        if not steps:
            self._exit_reason = "节奏序列为空"
            return
        max_loops = int(rhythm.get("max_loops", 0) or 0)
        loop_delay = max(0, int(rhythm.get("loop_delay_ms", 0) or 0)) / 1000.0
        hwnd = int(self.cfg.get("window.hwnd", 0) or 0)
        input_mode = self.cfg.get("input.mode", "global")

        while not self._stop_flag.is_set():
            self.stats.loops += 1
            for step in steps:
                if self._stop_flag.is_set():
                    break
                self._wait_while_paused()
                if self._stop_flag.is_set():
                    break
                kind = str(step.get("type", "key"))
                if kind == "delay":
                    self._sleep_cancellable(max(0, int(step.get("delay_ms", 500))) / 1000.0)
                    continue
                if kind == "click":
                    if not self._guard_foreground(hwnd, input_mode):
                        continue
                    x, y = self._resolve_click(step, hwnd)
                    try:
                        winutil.click_at(x, y, input_mode, hwnd)
                        self.stats.actions += 1
                        self.stats.last_action = f"点击 ({step.get('x')}, {step.get('y')})"
                        self._log("info", self.stats.last_action)
                    except Exception as exc:
                        self._log("warn", f"点击失败：{exc}")
                else:
                    if not self._guard_foreground(hwnd, input_mode):
                        continue
                    key = str(step.get("key", "enter"))
                    hold = int(step.get("hold_ms", self.cfg.get("input.hold_ms", 45)))
                    try:
                        winutil.press_key(key, hold, input_mode, hwnd)
                        self.stats.actions += 1
                        self.stats.last_action = f"按键 {key}"
                        self._log("info", f"按键 {key}")
                    except Exception as exc:
                        self._log("warn", f"按键失败：{exc}")
                delay = max(0, int(step.get("delay_ms", 0) or 0)) / 1000.0
                if delay:
                    self._sleep_cancellable(delay)
                self._emit_stats()
            if loop_delay:
                self._sleep_cancellable(loop_delay)
            self._emit_stats()
            self._emit_rhythm_preview(hwnd)
            if max_loops and self.stats.loops >= max_loops:
                self._exit_reason = f"已完成 {max_loops} 轮"
                return
            if self.cfg.get("safety.corner_failsafe", True) and self._corner_hit():
                self._exit_reason = "鼠标急停触发（左上角）"
                return

    def _resolve_click(self, step: dict, hwnd: int) -> tuple[int, int]:
        """把节奏宏里的点击坐标解析成屏幕坐标。

        click_relative 开启时坐标以「客户区左上角」为原点，窗口挪了位置也不会点偏。
        """
        x = int(step.get("x", 0) or 0)
        y = int(step.get("y", 0) or 0)
        if not self.cfg.get("rhythm.click_relative", True):
            return x, y
        origin = winutil.client_origin(hwnd)
        if origin is None:
            return x, y
        return origin[0] + x, origin[1] + y

    def _emit_rhythm_preview(self, hwnd: int) -> None:
        now = time.time()
        if now - self._last_preview_emit < 0.6:
            return
        self._last_preview_emit = now
        frame = capture.grab_window(hwnd)
        if frame is None:
            return
        info = RecogInfo(role="rhythm", source="节奏模式", frame=_preview(frame.image),
                         frame_size=frame.size, timestamp=now,
                         note=f"第 {self.stats.loops} 轮")
        self.signals.recognized.emit(info)

    # ---------------- 智能识别 ----------------
    def _run_smart(self) -> None:
        smart = self.cfg.get("smart", {}) or {}
        poll = max(60, int(smart.get("poll_interval_ms", 220))) / 1000.0
        ocr_interval = max(200, int(smart.get("ocr_interval_ms", 900))) / 1000.0
        stuck_timeout = max(5.0, float(smart.get("stuck_timeout_s", 20) or 20))
        target = int(smart.get("target_spins", 0) or 0)
        max_unknown = max(3, int(self.cfg.get("safety.max_unknown_before_stop", 30)))
        hwnd = int(self.cfg.get("window.hwnd", 0) or 0)
        input_mode = self.cfg.get("input.mode", "global")
        owned_policy = smart.get("owned_policy", "garage")
        owned_keys = (smart.get("owned_keys") or {}).get(owned_policy) or ["enter"]
        start_key = str(self.cfg.get("input.start_key", "enter"))

        if not any(self._roles_cache.get(r) for r in ROLE_PRIORITY):
            self._log("warn", "模板库里还没有模板，将主要依赖 OCR 关键词判定界面")
        if not self._ocr.ready:
            self._log("warn", "OCR 引擎未就绪，关键词识别不可用（模板匹配仍可工作）")

        while not self._stop_flag.is_set():
            self._wait_while_paused()
            if self._stop_flag.is_set():
                break

            if self.cfg.get("safety.corner_failsafe", True) and self._corner_hit():
                self._exit_reason = "鼠标急停触发（把鼠标移到屏幕左上角即停止）"
                return

            frame = capture.grab_window(hwnd)
            if frame is None:
                self._log("warn", "截图失败：游戏窗口可能已最小化或被关闭")
                if not winutil.is_window_alive(hwnd):
                    self._exit_reason = "游戏窗口已关闭"
                    return
                self._sleep_cancellable(0.5)
                continue

            info = self._recognize(frame, ocr_interval)
            now = time.time()
            if now >= self._last_preview_emit + 0.15:
                self._last_preview_emit = now
                info.frame = _preview(frame.image)
                info.frame_size = frame.size
                self.signals.recognized.emit(info)

            self.stats.last_state = info.role
            self.stats.last_source = info.source
            self.stats.last_score = info.score

            sig = matcher.signature(frame.image)
            acted = self._dispatch(info, frame, sig, smart, owned_policy, owned_keys,
                                   start_key, input_mode, hwnd)

            if target and self.stats.spins_started >= target:
                self._exit_reason = f"已完成设定的 {target} 次抽奖"
                return

            # 画面长时间一动不动 → 游戏卡住，或模板匹配到了错误的固定位置
            if sig and sig == self._handled_sig and info.role != "unknown":
                if not self._handled_at:
                    self._handled_at = now
                elif now - self._handled_at >= stuck_timeout:
                    self._save_fail_frames(frame, "画面长时间无变化")
                    self._exit_reason = (f"画面在同一状态停留超过 {int(stuck_timeout)} 秒都没有变化，"
                                         "已自动停止。游戏可能卡住了，"
                                         "也可能是模板匹配到了错误的位置（可在日志里看识别来源）")
                    return
            else:
                self._handled_at = 0.0

            if info.role == "unknown":
                self.stats.unknown_streak += 1
                if self.stats.unknown_streak >= max_unknown:
                    self._save_fail_frames(frame, "连续识别失败")
                    self._exit_reason = ("连续识别失败，已自动停止。"
                                         "建议到「识别」页看看实时画面，补录模板或调低阈值")
                    return
            else:
                self.stats.unknown_streak = 0

            # 读取剩余的抽奖次数（低频）
            if smart.get("spin_count_rule", {}).get("enabled") and self._ocr.ready:
                if now - self._last_ocr_at >= max(1.0, ocr_interval * 2):
                    self._read_spin_count(frame)
                    if self.stats.remaining == 0:
                        self._exit_reason = "游戏内抽奖次数已为 0"
                        return

            self._emit_stats()
            if not acted:
                self._sleep_cancellable(poll)
        if not self._exit_reason:
            self._exit_reason = "手动停止"

    # ------------------------------------------------------------------ #
    # 识别
    # ------------------------------------------------------------------ #
    def _recognize(self, frame: capture.Frame, ocr_interval: float) -> RecogInfo:
        smart = self.cfg.get("smart", {}) or {}
        info = RecogInfo(timestamp=time.time(), frame_size=frame.size)

        if smart.get("use_templates", True) and self._roles_cache:
            entries: list[_TplEntry] = []
            for role in ROLE_PRIORITY:
                entries.extend(self._roles_cache.get(role) or [])
            if entries:
                scales = (0.95, 1.0, 1.05) if smart.get("multi_scale") else (1.0,)
                hits = matcher.match_all(frame.image, entries, scales)
                info.hits = [(h.name, h.role, h.score, h.box) for h in hits]
                if hits:
                    order = {role: idx for idx, role in enumerate(ROLE_PRIORITY)}
                    hits.sort(key=lambda h: (order.get(h.role, 99), -h.score))
                    top = hits[0]
                    info.role = top.role
                    info.source = f"模板「{top.name}」"
                    info.score = top.score
                    info.box = top.box
                    return info

        if smart.get("use_ocr", True) and self._ocr.ready:
            now = time.time()
            if now - self._last_ocr_at >= ocr_interval:
                self._last_ocr_at = now
                for rule in smart.get("keyword_rules") or []:
                    if not rule.get("enabled"):
                        continue
                    keywords = list(rule.get("keywords") or [])
                    if not keywords:
                        continue
                    region = rule.get("region")
                    crop = self._crop(frame.image, region)
                    if crop is None:
                        continue
                    text = self._ocr.recognize(crop, str(rule.get("lang") or "zh-Hans-CN"),
                                               float(rule.get("scale") or 1.5))
                    if text:
                        info.texts[str(rule.get("id") or rule.get("role"))] = text.strip()
                        hit = ocr_mod.contains_any(text, keywords)
                        if hit:
                            info.role = str(rule.get("role") or "blocked")
                            info.source = f"OCR 命中「{hit}」"
                            info.score = 1.0
                            info.note = text.strip().replace("\n", " / ")[:80]
                            return info
        return info

    @staticmethod
    def _crop(image: np.ndarray, region) -> np.ndarray | None:
        if image is None:
            return None
        h, w = image.shape[:2]
        if not region:
            return image
        try:
            x, y, rw, rh = (int(v) for v in region)
        except (TypeError, ValueError):
            return image
        x0, y0 = max(0, min(w - 1, x)), max(0, min(h - 1, y))
        x1, y1 = max(x0 + 1, min(w, x + max(1, rw))), max(y0 + 1, min(h, y + max(1, rh)))
        return np.ascontiguousarray(image[y0:y1, x0:x1])

    def _read_spin_count(self, frame: capture.Frame) -> None:
        smart = self.cfg.get("smart", {}) or {}
        rule = smart.get("spin_count_rule") or {}
        crop = self._crop(frame.image, rule.get("region"))
        if crop is None:
            return
        text = self._ocr.recognize(crop, str(rule.get("lang") or "zh-Hans-CN"),
                                   float(rule.get("scale") or 2.0))
        value = ocr_mod.first_int(text, str(rule.get("regex") or r"\d+"))
        if value is None:
            return
        if value != self.stats.remaining:
            self._log("info", f"游戏内剩余抽奖次数：{value}")
        self.stats.remaining = value

    def _read_sell_price(self, frame: capture.Frame) -> bool:
        """读出售价并计入统计；返回是否读到了数字。"""
        smart = self.cfg.get("smart", {}) or {}
        rule = smart.get("sell_price_rule") or {}
        if not rule.get("enabled"):
            return False
        crop = self._crop(frame.image, rule.get("region"))
        if crop is None:
            return False
        text = self._ocr.recognize(crop, str(rule.get("lang") or "en-US"),
                                   float(rule.get("scale") or 2.0))
        value = ocr_mod.first_int(text, str(rule.get("regex") or r"[\d][\d,\.]*"))
        if value is None:
            self._log("warn", f"价格识别失败（原始文本：{(text or '').strip()[:60]!r}）")
            return False
        self.stats.sells += 1
        self.stats.prices.append(value)
        if len(self.stats.prices) > 500:
            del self.stats.prices[:-500]
        self.stats.credits += value
        self._log("info", f"出售价格：{value:,}（累计 {self.stats.credits:,}）")
        return True

    # ------------------------------------------------------------------ #
    # 失败现场
    # ------------------------------------------------------------------ #
    def _save_fail_frames(self, frame: capture.Frame, reason: str) -> None:
        """停机时把当前画面存下来，方便用户照着补模板。"""
        if not self.cfg.get("safety.save_fail_frames", True):
            return
        image = getattr(frame, "image", None)
        if image is None:
            return
        try:
            import cv2
            paths.FROZEN_DIR.mkdir(parents=True, exist_ok=True)
            target = paths.FROZEN_DIR / f"fail_{_dt.datetime.now():%Y%m%d_%H%M%S}.png"
            if not cv2.imwrite(str(target), image):
                raise OSError("写入图片失败")
            self._prune_fail_frames()
            self._log("info", f"已保存失败现场截图：{paths.display_path(target)}（{reason}）")
        except Exception as exc:
            self._log("warn", f"保存失败现场截图出错：{exc}")

    @staticmethod
    def _prune_fail_frames(keep: int = FAIL_FRAME_KEEP) -> None:
        try:
            files = sorted(paths.FROZEN_DIR.glob("fail_*.png"))
        except OSError:
            return
        for old in files[:-keep]:
            try:
                old.unlink()
            except OSError:
                pass

    # ------------------------------------------------------------------ #
    # 动作派发
    # ------------------------------------------------------------------ #
    def _dispatch(self, info: RecogInfo, frame: capture.Frame, sig: str, smart: dict,
                  owned_policy: str, owned_keys: list, start_key: str,
                  input_mode: str, hwnd: int) -> bool:
        now = time.time()
        role = info.role
        if role == "unknown":
            return False

        if role in STATE_ROLES and sig and sig == self._handled_sig:
            # 画面还没变，说明上一次按键游戏还没消化，别重复按同一个界面
            return False

        if role == "ready" and now < self._waiting_result_until:
            # 已经开始抽奖了，正在等结果界面：转盘动画里会反复命中「开始」按钮
            return False

        if now < self._cooldown_until:
            return False

        waits = smart.get("waits", {}) or {}
        waits = {
            "after_start_ms": int(waits.get("after_start_ms", 5200)),
            "after_result_ms": int(waits.get("after_result_ms", 650)),
            "after_option_ms": int(waits.get("after_option_ms", 950)),
            "after_confirm_ms": int(waits.get("after_confirm_ms", 700)),
        }

        if role == "ready":
            self._press([start_key], input_mode, hwnd, "按下开始抽奖")
            self.stats.spins_started += 1
            self.stats.last_action = f"开始抽奖（第 {self.stats.spins_started} 次）"
            # 不再死等固定的 after_start_ms：只做一小段起步保护，
            # 之后一识别到结果界面就立刻接手（低配机不会误判，高配机不用白等）
            self._cooldown_until = now + START_GUARD_S
            self._waiting_result_until = now + waits["after_start_ms"] / 1000.0
            self._handled_sig = sig
            return True

        if role == "result_new":
            self._press(["enter"], input_mode, hwnd, "领取新车")
            self.stats.results += 1
            self.stats.new_cars += 1
            self._cooldown_until = now + waits["after_result_ms"] / 1000.0
            self._handled_sig = sig
            return True

        if role == "result_owned":
            self.stats.results += 1
            self.stats.owned += 1
            keys = list(owned_keys) or ["enter"]
            label = {"garage": "加入车库", "gift": "送礼", "sell": "出售"}.get(owned_policy, owned_policy)
            self._press(keys, input_mode, hwnd, f"处理已拥有车辆 → {label}")
            if owned_policy == "sell":
                self._cooldown_until = now + waits["after_option_ms"] / 1000.0
            else:
                self._cooldown_until = now + waits["after_result_ms"] / 1000.0
            self._handled_sig = sig
            return True

        if role == "sell":
            rule = smart.get("sell_price_rule") or {}
            priced = self._read_sell_price(frame)
            if rule.get("enabled") and rule.get("abort_on_fail") and not priced:
                # 界面认出来了但价格读不出来，多半是认错了，宁可停下来也别乱卖
                self._save_fail_frames(frame, "出售价格识别失败")
                self._exit_reason = "出售价格没识别出来，已停止（避免误判界面把车卖掉）"
                self._stop_flag.set()
                return True
            key = str(smart.get("sell_confirm_key") or "enter")
            self._press([key], input_mode, hwnd, "确认出售")
            self._cooldown_until = now + waits["after_confirm_ms"] / 1000.0
            self._handled_sig = sig
            return True

        if role == "confirm":
            self._press(["enter"], input_mode, hwnd, "确认弹窗")
            self._cooldown_until = now + waits["after_confirm_ms"] / 1000.0
            self._handled_sig = sig
            return True

        if role == "end":
            if smart.get("auto_stop_on_end_template", True):
                self._exit_reason = "识别到「抽奖结束」界面"
                self._stop_flag.set()
            return True

        if role == "blocked":
            action = str(smart.get("unknown_action") or "wait")
            if action == "enter":
                self._press(["enter"], input_mode, hwnd, "处理阻挡界面")
            elif action == "esc":
                self._press(["esc"], input_mode, hwnd, "处理阻挡界面")
            self._cooldown_until = now + max(0.6, waits["after_result_ms"] / 1000.0)
            self._handled_sig = sig
            return True

        return False

    def _press(self, keys: list[str], input_mode: str, hwnd: int, label: str) -> None:
        if not self._guard_foreground(hwnd, input_mode):
            return
        key_delay = max(0, int(self.cfg.get("input.key_delay_ms", 90))) / 1000.0
        hold = int(self.cfg.get("input.hold_ms", 45))
        min_gap = max(0, int(self.cfg.get("safety.min_action_interval_ms", 260))) / 1000.0
        for index, key in enumerate(keys):
            if self._stop_flag.is_set():
                return
            try:
                winutil.press_key(str(key), hold, input_mode, hwnd)
                self.stats.actions += 1
            except Exception as exc:
                self._log("warn", f"按键 {key} 失败：{exc}")
            if index < len(keys) - 1:
                self._sleep_cancellable(max(key_delay, 0.05))
        self.stats.last_action = label
        self._log("info", f"{label} → {' + '.join(str(k) for k in keys)}")
        if min_gap:
            self._sleep_cancellable(min_gap)

    def _guard_foreground(self, hwnd: int, input_mode: str) -> bool:
        if input_mode == "message":
            return True
        if not self.cfg.get("safety.require_foreground", True):
            return True
        if winutil.is_foreground(hwnd):
            return True
        now = time.time()
        if now - self._foreground_warn_at > 3.0:
            self._foreground_warn_at = now
            self._log("warn", "游戏窗口不在前台，已暂停发送按键（避免误按到别的程序）")
        return False

    # ------------------------------------------------------------------ #
    # 工具
    # ------------------------------------------------------------------ #
    def _corner_hit(self) -> bool:
        x, y = winutil.cursor_pos()
        return x <= 2 and y <= 2

    def _sleep_cancellable(self, seconds: float) -> None:
        self._stop_flag.wait(max(0.0, seconds))

    def _wait_while_paused(self) -> None:
        while self._paused.is_set() and not self._stop_flag.is_set():
            time.sleep(0.15)

    def summary(self) -> dict[str, Any]:
        stats = self.stats
        return {
            "抽奖次数": stats.spins_started,
            "结果界面次数": stats.results,
            "已拥有": stats.owned,
            "新车": stats.new_cars,
            "出售次数": stats.sells,
            "出售收益": stats.credits,
            "平均单价": stats.avg_price(),
            "运行时长": f"{stats.elapsed():.0f}s",
            "速度": f"{stats.rate_per_min():.1f} 次/分",
        }
