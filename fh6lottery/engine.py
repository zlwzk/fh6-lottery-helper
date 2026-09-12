"""抽奖引擎：两种模式 + 安全保护 + 统计。

- smart  识别驱动：截屏 → 模板/OCR 判定当前界面角色 → 收发按键
- rhythm 节奏宏：按用户编排的按键序列循环，不依赖识别（识别失败时的保底方案）

线程模型：一个工作线程跑循环，通过 Qt 信号把状态推给界面。
"""

from __future__ import annotations

import copy
import datetime as _dt
import re
import threading
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Any

import numpy as np
from PySide6.QtCore import QObject, Signal

from . import capture, matcher, ocr as ocr_mod, paths, winutil
from .templates import TemplateStore, get_store, role_label

PREVIEW_MAX_WIDTH = 460
ROLE_PRIORITY = ["end", "sell", "result_owned", "result_new", "confirm", "blocked", "ready"]
# 「已处理过的静态画面」：这些角色的界面按一次键就会切走
STATE_ROLES = ("result_owned", "result_new", "sell", "confirm", "end")
# 这些界面本该「按一次键就走人」。要是连续按了十来次画面还原封不动，
# 那就不是在抽奖，而是在空转 —— 停下來把线索说清楚，别一直按。
STREAK_ROLES = ("result_owned", "result_new", "sell", "confirm")
SAME_ROLE_LIMIT = 12         # 同一个界面连续处理这么多次还没变化就停机
START_GUARD_S = 0.6          # 按下「开始抽奖」后的起步保护时间
REPEAT_GAP_S = 2.5           # 同一静态画面被重复识别时的重试间隔
FAIL_FRAME_KEEP = 30         # 失败现场截图最多保留张数
HIGHLIGHT_MIN_AREA = 600     # 「本次抽中」的高亮格至少要有这么多像素才算数
HIGHLIGHT_MAX_RATIO = 0.45   # 占框选面积超过这个比例的连通块是背景，不是某一格
REWARD_TEXT_MAX = 48         # 奖励文本在界面与日志里的最大长度
REWARD_LIST_KEEP = 400       # 奖励流水最多留多少条
FULL_REGION_RATIO = 0.85     # 框选范围超过画面的这个比例就算「整屏」，读数会不准
SANE_PRICE_MIN = 100         # 小于这个数的「出售价格」一定是读错了


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
    credits: int = 0             # 出售车辆换来 CR
    cash_credits: int = 0        # 抽奖直接抽到的 CR
    cash_hits: int = 0           # 抽到 CR 的次数
    car_wins: int = 0            # 抽到车（新车 + 重复）的次数
    loops: int = 0
    actions: int = 0
    prices: list[int] = field(default_factory=list)
    rewards: list[str] = field(default_factory=list)   # 奖励流水（最近若干条）
    remaining: int | None = None
    started_at: float = 0.0
    ended_at: float = 0.0
    last_action: str = ""
    last_state: str = "待机"
    last_source: str = ""
    last_score: float = 0.0
    last_reward: str = ""
    last_reward_kind: str = ""   # cash | car | unknown
    unknown_streak: int = 0

    def elapsed(self) -> float:
        if not self.started_at:
            return 0.0
        end = self.ended_at or time.time()
        return max(0.0, end - self.started_at)

    def elapsed_text(self) -> str:
        """把耗时格式化成 时:分:秒 / 分:秒，给界面和结束摘要用。"""
        total = int(self.elapsed())
        hours, rest = divmod(total, 3600)
        minutes, seconds = divmod(rest, 60)
        if hours:
            return f"{hours}:{minutes:02d}:{seconds:02d}"
        return f"{minutes:02d}:{seconds:02d}"

    def total_credits(self) -> int:
        return int(self.credits) + int(self.cash_credits)

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
        self._last_count_at = 0.0
        self._handled_sig = ""
        self._handled_at = 0.0       # 上次对 _handled_sig 这个画面动作的时间（重复画面重试节流用）
        self._stuck_since = 0.0      # 当前静态画面开始「一直没变」的时间（卡住保护用）
        self._reward_read_for = -1   # 已经读过奖励的「抽奖序号」，同一次抽奖只记一次账
        self._price_read_for = -1    # 已经读过出售价的「抽奖序号」，避免同一次卖车被记两遍
        self._streak_role = ""       # 连着处理的是哪个界面
        self._streak_count = 0       # 这个界面连着处理了多少次（空转检测）
        self._acted_role = ""        # 上一次真正动手处理的是哪个界面
        self._role_fresh = True      # 当前界面是「刚出现」还是「还停在原地」
        self._logged_role = ""       # 上一次写进日志的界面，只在切换时记一条
        self._region_warned: set[str] = set()
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
        self._handled_sig = ""
        self._handled_at = 0.0
        self._stuck_since = 0.0
        self._reward_read_for = -1
        self._price_read_for = -1
        self._streak_role = ""
        self._streak_count = 0
        self._acted_role = ""
        self._role_fresh = True
        self._logged_role = ""
        self._region_warned = set()
        self._exit_reason = ""
        self._refresh_roles()
        self._set_state(EngineState.RUNNING)
        mode = self.cfg.get("mode", "smart")
        self._log("info", f"开始运行（模式：{'智能识别' if mode == 'smart' else '节奏宏'}）")
        if mode == "smart":
            self._self_check()
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
    # 开局体检
    # ------------------------------------------------------------------ #
    @staticmethod
    def _region_ratio(region, cur_w: int, cur_h: int) -> float:
        """框选区域占整块画面的比例（用来识别「其实是整屏」的框选）。"""
        try:
            _x, _y, w, h = (int(v) for v in region)
        except (TypeError, ValueError):
            return 0.0
        if cur_w <= 0 or cur_h <= 0:
            return 0.0
        return (w * h) / float(cur_w * cur_h)

    def _warn_wide_region(self, key: str, label: str, region) -> None:
        """框选范围几乎是整块画面时提醒一次。

        这种情况下读数会把标题、别的数字一起读进来，结果多半不准 ——
        与其让用户对着一个错误数字纳闷，不如直说。每项只提醒一次，不刷屏。
        """
        if not region or key in self._region_warned:
            return
        cur_w, cur_h = self._client_size()
        if self._region_ratio(region, cur_w, cur_h) < FULL_REGION_RATIO:
            return
        self._region_warned.add(key)
        self._log("warn", f"「{label}」框选的是整块画面，读数大概率不准"
                          "（标题、别的数字都会被当成结果读进来）："
                          "到「流程」页重新框到真正的那一行/那一格，会稳很多")

    def _self_check(self) -> None:
        """开局体检：把那些「会让功能静默失效」的设置直接说出来。

        这类毛病用户自己看不出来 —— 没框区域、模板太大、界面既没模板也没关键词、
        输入方式游戏根本收不到。表现都长一个样：点了开始没动静，或者跑一圈什么都没统计到。
        """
        smart = self.cfg.get("smart", {}) or {}
        cur_w, cur_h = self._client_size()
        client_area = float(cur_w * cur_h)
        problems: list[str] = []
        notes: list[str] = []

        # 1) 每个界面有没有「认得出来」的手段
        rules = [r for r in (smart.get("keyword_rules") or []) if r.get("enabled", True)]
        kw_roles = {str(r.get("role")) for r in rules}
        tpl_roles = {role for role, items in self._roles_cache.items() if items}
        tpl_desc = "、".join(f"{role_label(role)}×{len(items)}"
                            for role, items in self._roles_cache.items() if items) or "无"
        self._log("info", f"开局体检：模板 {tpl_desc}；启用的关键词规则 {len(rules)} 条")
        blind = [role for role in ROLE_PRIORITY if role not in tpl_roles and role not in kw_roles]
        if blind:
            names = "」「".join(role_label(role) for role in blind)
            if "ready" in blind:
                problems.append(
                    f"「{names}」这些界面认不出来 —— 「抽奖主界面」认不出来就永远按不下开始键。"
                    "请到「模板库」页框一张按钮/标题附近的小图给它，"
                    "或在「识别」页给这个角色加一条关键词规则")

        # 2) 整屏的模板：画面稍微一动就废了
        for role, items in self._roles_cache.items():
            for entry in items:
                h, w = entry.gray.shape[:2]
                if client_area and w * h >= client_area * FULL_REGION_RATIO:
                    problems.append(
                        f"模板「{entry.item.name}」几乎覆盖整块画面（{w}×{h}）："
                        "画面稍有变化就对不上，建议只框按钮或标题那一小块，重新录一次")

        # 3) 开了识别却没框区域 / 框的是整屏 —— 统计会静默失效或读出垃圾值
        for key, label in (("reward_rule", "抽奖结果"), ("sell_price_rule", "出售价格"),
                           ("spin_count_rule", "剩余次数")):
            rule = smart.get(key) or {}
            if not rule.get("enabled"):
                continue
            region = rule.get("region")
            if not region:
                problems.append(f"已开启「{label}」识别，但还没框选区域 —— 这一项不会生效")
            elif self._region_ratio(region, cur_w, cur_h) >= FULL_REGION_RATIO:
                problems.append(
                    f"「{label}」框选的是整块画面：标题、别的数字都会一起读进来，"
                    "结果基本不准，请重新框到真正的那一行/那一格")

        # 4) 输入方式：PostMessage 不少游戏是整帧忽略的
        if str(self.cfg.get("input.mode", "global")) == "message":
            problems.append(
                "按键用的是「后台消息（PostMessage）」：不少游戏（尤其用 Raw Input 的）会完全忽略，"
                "表现出来就是画面一动不动。建议到「设置 → 输入方式」改用「全局按键」")

        # 5) 选了出售却没开价格识别
        if str(smart.get("owned_policy", "garage")) == "sell" \
                and not (smart.get("sell_price_rule") or {}).get("enabled"):
            notes.append("已拥有车辆选了「出售」，但没开「识别出售价格」：卖车赚的 CR 不会统计")

        # 6) 关键词规则在整屏上匹配，很容易被画面里的同一句话误触发
        loose = [r for r in rules if not r.get("region")]
        if loose:
            notes.append(
                f"有 {len(loose)} 条关键词规则在整块画面上匹配（"
                + "、".join(f"「{kw}」" for r in loose[:3] for kw in (r.get("keywords") or [])[:1])
                + "…）：如果某个界面总被认错，多半是画面里别处也写了这几个字，"
                  "给这条规则单独框个小区域就稳了")

        for text in problems:
            self._log("warn", "⚠ " + text)
        for text in notes:
            self._log("info", "提示：" + text)
        if not problems:
            self._log("info", "开局体检没有发现明显问题")

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
        self._log("info", self.summary_line())
        self.signals.finished.emit(reason, self.stats.clone())
        capture.close_all()

    def summary_line(self) -> str:
        """收工小结：抽了多少次、花了多久、一共到手多少 CR。"""
        stats = self.stats
        parts = [f"共抽奖 {stats.spins_started} 次，总耗时 {stats.elapsed_text()}"]
        if stats.car_wins:
            parts.append(f"抽到车辆 {stats.car_wins} 辆")
        if stats.cash_credits:
            parts.append(f"抽到 CR {stats.cash_credits:,}（{stats.cash_hits} 次）")
        if stats.sells:
            parts.append(f"卖出 {stats.sells} 台共 {stats.credits:,} CR")
        parts.append(f"合计收益 {stats.total_credits():,} CR")
        return "；".join(parts)

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

        # 一整轮就四件事，顺序固定：
        #   1) 截屏          2) 识别现在停在哪个界面
        #   3) 顺手把画面上的数字读出来记账（抽到多少 CR / 还剩几次）
        #   4) 按识别到的界面决定按什么键
        # 不假设游戏的固定流程，每一步都由「这一刻画面长什么样」决定。
        while not self._stop_flag.is_set():
            self._wait_while_paused()
            if self._stop_flag.is_set():
                break

            if self.cfg.get("safety.corner_failsafe", True) and self._corner_hit():
                self._exit_reason = "鼠标急停触发（把鼠标移到屏幕左上角即停止）"
                return

            # ---- 1) 看屏幕 ----
            frame = capture.grab_window(hwnd)
            if frame is None:
                self._log("warn", "截图失败：游戏窗口可能已最小化或被关闭")
                if not winutil.is_window_alive(hwnd):
                    self._exit_reason = "游戏窗口已关闭"
                    return
                self._sleep_cancellable(0.5)
                continue

            # ---- 2) 认清当前界面 ----
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

            # 界面一换就记一条，并且写清「凭什么」认定它是这个界面 ——
            # 出问题时日志里这是唯一的线索（认错了还是没认出，一眼能看出来）。
            if info.role != self._logged_role:
                self._logged_role = info.role
                detail = f"　{info.source}" if info.source else ""
                if info.note:
                    detail += f"　读到：{info.note}"
                self._log("info" if info.role != "unknown" else "warn",
                          f"识别 → {role_label(info.role)}{detail}")

            # ---- 3) 顺手读数记账 ----
            self._harvest(info, frame, smart)

            # ---- 4) 动手 ----
            sig = matcher.signature(frame.image)
            acted = self._dispatch(info, frame, sig, smart, owned_policy, owned_keys,
                                   start_key, input_mode, hwnd)

            # 本该「按一次键就走人」的界面，连着处理十来次画面却纹丝不动 ——
            # 那不是抽奖，是空转。停下来把线索说清楚，比一直按下去强。
            if acted and info.role in STREAK_ROLES:
                if info.role == self._streak_role:
                    self._streak_count += 1
                else:
                    self._streak_role, self._streak_count = info.role, 1
                if self._streak_count >= SAME_ROLE_LIMIT:
                    self._save_fail_frames(frame, "同一界面反复处理")
                    self._exit_reason = (
                        f"「{role_label(info.role)}」这个界面连续处理了 {SAME_ROLE_LIMIT} 次都没有变化，"
                        "已自动停止。最可能是这三种情况：\n"
                        "① 按键游戏根本没收到 —— 到「设置 → 输入方式」把「后台消息」改成「全局按键」；\n"
                        "② 这个界面被认错了 —— 例如奖品卡片上也写着「已拥有」，"
                        "给这条关键词规则单独框个小区域，或改用模板；\n"
                        "③ 等待时间设得太短，游戏还没反应过来就又被按了一次。")
                    return
            elif info.role not in STREAK_ROLES:
                self._streak_role, self._streak_count = "", 0

            if self.stats.remaining == 0:
                self._exit_reason = "游戏内抽奖次数已为 0，收工"
                return

            if target and self.stats.spins_started >= target:
                self._exit_reason = f"已完成设定的 {target} 次抽奖"
                return

            # 按键之后画面一直没变 → 游戏卡住，或模板匹配到了错误的固定位置
            if sig and sig == self._handled_sig and info.role != "unknown":
                if not self._stuck_since:
                    self._stuck_since = now
                elif now - self._stuck_since >= stuck_timeout:
                    self._save_fail_frames(frame, "画面长时间无变化")
                    self._exit_reason = (f"画面在同一状态停留超过 {int(stuck_timeout)} 秒都没有变化，"
                                         "已自动停止。游戏可能卡住了，"
                                         "也可能是模板匹配到了错误的位置（可在日志里看识别来源）")
                    return
            else:
                self._stuck_since = 0.0

            if info.role == "unknown":
                self.stats.unknown_streak += 1
                if self.stats.unknown_streak >= max_unknown:
                    self._save_fail_frames(frame, "连续识别失败")
                    self._exit_reason = ("连续识别失败，已自动停止。"
                                         "建议到「识别」页看看实时画面，补录模板或调低阈值")
                    return
            else:
                self.stats.unknown_streak = 0

            self._emit_stats()
            if not acted:
                self._sleep_cancellable(poll)
        if not self._exit_reason:
            self._exit_reason = "手动停止"

    # ---------------- 读数记账 ----------------
    def _harvest(self, info: RecogInfo, frame: capture.Frame, smart: dict) -> None:
        """只做「读数和记账」，不按键 —— 按键统一交给 _dispatch。

        1) 本次抽到了什么：奖励面板上只有一格会高亮，读那一格的文字；
        2) 游戏内还剩几次：滚轮数字，低频读一次。
        """
        if not self._ocr.ready:
            return
        if info.role in ("result_new", "result_owned"):
            # 这两个界面一出现就说明结果已经揭晓了，先读一手
            self._read_reward(frame, smart)
        rule = smart.get("spin_count_rule") or {}
        if rule.get("enabled"):
            interval = max(1.0, int(smart.get("ocr_interval_ms", 900)) / 1000.0 * 2)
            if time.time() - self._last_count_at >= interval:
                self._last_count_at = time.time()
                self._read_spin_count(frame)

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
                # 同一块区域（尤其是「整块画面」这种）这一轮只 OCR 一次，多条规则共用结果。
                # 否则每条规则都把整屏截一遍、写一次临时图，又慢又占磁盘。
                cache: dict[tuple, str | None] = {}
                best: tuple[int, str, str, str] | None = None   # (命中词长度, 角色, 命中词, 原文)
                for rule in smart.get("keyword_rules") or []:
                    if not rule.get("enabled"):
                        continue
                    keywords = list(rule.get("keywords") or [])
                    if not keywords:
                        continue
                    region = rule.get("region")
                    lang = str(rule.get("lang") or "zh-Hans-CN")
                    scale = float(rule.get("scale") or 1.5)
                    key = (tuple(region) if region else (), lang, scale)
                    if key not in cache:
                        crop = self._crop(frame.image, region)
                        cache[key] = None if crop is None else self._ocr.recognize(crop, lang, scale)
                    text = cache[key]
                    if not text:
                        continue
                    info.texts[str(rule.get("id") or rule.get("role"))] = text.strip()
                    hit = ocr_mod.contains_any(text, keywords)
                    if not hit:
                        continue
                    # 多条规则同时命中时，取「关键词更长」的那个 —— 越长的词越具体。
                    # 抽奖结果界面底部写着「领取奖励并再次抽奖」，而奖品卡片上可能也写着
                    # 「已拥有」；谁更长谁更能说明「这个界面到底是干嘛的」，按长度取才不会认错。
                    if best is None or len(hit) > best[0]:
                        best = (len(hit), str(rule.get("role") or "blocked"), hit, text.strip())
                if best:
                    info.role = best[1]
                    info.source = f"OCR 命中「{best[2]}」"
                    info.score = 1.0
                    info.note = best[3].replace("\n", " / ")[:80]
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
        """读游戏里的「剩余抽奖机会」。"""
        smart = self.cfg.get("smart", {}) or {}
        rule = smart.get("spin_count_rule") or {}
        region = rule.get("region")
        self._warn_wide_region("spin_count_rule", "剩余次数", region)
        crop = self._crop(frame.image, region)
        if crop is None:
            return
        text = self._ocr.recognize(crop, str(rule.get("lang") or "zh-Hans-CN"),
                                   float(rule.get("scale") or 2.0))
        if not text:
            return
        numbers = [int(n) for n in re.findall(r"\d{1,7}", text)]
        if not numbers:
            return
        pick = str(rule.get("pick") or "max")
        if pick == "min":
            value = min(numbers)
        elif pick == "first":
            value = numbers[0]
        else:
            # 剩余次数是个滚动数字，一屏可能同时看到 997 / 998 / 999。
            # 取最大的那个最安全：顶多多抽一次，不会提前停下。
            value = max(numbers)
        if value != self.stats.remaining:
            self._log("info", f"游戏内剩余抽奖次数：{value}")
        self.stats.remaining = value

    def _read_reward(self, frame: capture.Frame, smart: dict) -> None:
        """读出「本次抽中的那一格」，把抽到的 CR 或车记进统计。

        抽奖结果画面上只有一格会被画成高亮（白底），其余格子都是彩色。
        所以先在高亮格上定位，再只对那一小块做 OCR —— 既准又快。
        """
        rule = smart.get("reward_rule") or {}
        if not rule.get("enabled"):
            return
        # 一次抽奖只记一次账：结果画面会停留好几秒，重复读会把同一份奖励算成好几份
        if self._reward_read_for == self.stats.spins_started:
            return
        region = rule.get("region")
        if not region:
            return
        self._warn_wide_region("reward_rule", "抽奖结果", region)
        # 转盘是一列一列停的，每一列各中一格（Super Wheelspin 一次给三份），
        # 所以要把所有高亮格都读出来再汇总，不能只看最大的那一块。
        crops = self._reward_crops(frame.image, region, str(rule.get("highlight") or "bright"))
        if not crops:
            return
        lang = str(rule.get("lang") or "en-US")
        scale = float(rule.get("scale") or 2.0)
        min_digits = int(rule.get("min_digits", 3) or 3)
        cash_total = 0
        cash_cells = 0
        car_names: list[str] = []
        for crop in crops:
            text = self._ocr.recognize(crop, lang, scale)
            if text is None:
                return      # 这次 OCR 抽风了，先不算数，下一轮重试
            kind, value, label = self._parse_reward(text, min_digits)
            if kind == "cash":
                cash_total += value
                cash_cells += 1
            else:
                # 转盘上的格子不是钱就是车：读不出内容的多半是纯车图那一格，也算车
                car_names.append(label)
        # 能读到画面本身就算「这一次读过了」，之后画面不动也不会重复计数
        self._reward_read_for = self.stats.spins_started
        index = self.stats.spins_started
        if cash_total:
            self.stats.cash_hits += 1
            self.stats.last_reward_kind = "cash"
            self.stats.last_reward = f"{cash_total:,} CR"
            if rule.get("count_cash", True):
                self.stats.cash_credits += cash_total
            self._push_reward(f"{cash_total:,} CR")
            self._log("info", f"第 {index} 次抽奖 → 这轮 {cash_cells} 份奖励共 {cash_total:,} CR"
                              f"（抽奖所得累计 {self.stats.cash_credits:,} CR）")
        car_count = len(crops) - cash_cells
        if car_count > 0:
            self._note_cars(index, car_names, car_count)

    def probe_reward(self) -> tuple[str, int, str]:
        """按当前设置试读一次「本次抽到的奖励」，只报结果不记账（给设置页的测试按钮用）。"""
        if not self._ocr.ready:
            self._log("warn", f"OCR 引擎还没就绪：{self._ocr.error or '未知原因'}")
            return "unknown", 0, ""
        smart = self.cfg.get("smart", {}) or {}
        rule = smart.get("reward_rule") or {}
        region = rule.get("region")
        if not region:
            self._log("warn", "还没框选「奖励面板」区域，没法测试")
            return "unknown", 0, ""
        hwnd = int(self.cfg.get("window.hwnd", 0) or 0)
        frame = capture.grab_window(hwnd)
        if frame is None:
            self._log("warn", "截图失败：先把游戏窗口打开、别最小化")
            return "unknown", 0, ""
        crops = self._reward_crops(frame.image, region, str(rule.get("highlight") or "bright"))
        if not crops:
            self._log("warn", "框选区域里没找到高亮格：确认游戏正停在抽奖结果界面（转盘已经停下），"
                              "或者把框选范围放大到整块奖励面板")
            return "unknown", 0, ""
        lang = str(rule.get("lang") or "en-US")
        scale = float(rule.get("scale") or 2.0)
        min_digits = int(rule.get("min_digits", 3) or 3)
        cash_total, cash_cells, names = 0, 0, []
        for order, crop in enumerate(crops, 1):
            text = self._ocr.recognize(crop, lang, scale)
            if text is None:
                self._log("warn", f"第 {order} 格 OCR 没返回结果，跳过")
                continue
            raw = text.strip()[:60]
            if not raw:
                self._log("info", f"第 {order} 格：没读到文字（应该是纯车图那一格）→ 算车")
                names.append("")
                continue
            kind, value, label = self._parse_reward(text, min_digits)
            if kind == "cash":
                cash_total += value
                cash_cells += 1
                self._log("info", f"第 {order} 格：{value:,} CR（原始文本：{raw!r}）")
            else:
                names.append(label)
                self._log("info", f"第 {order} 格：车辆「{label}」（原始文本：{raw!r}）")
        if not cash_total and not names:
            self._log("warn", "高亮格都读不出内容：换个「高亮格的样子」选项再试，"
                              "或把框选范围调大一点")
            return "unknown", 0, ""
        parts = []
        if cash_cells:
            parts.append(f"{cash_cells} 份 CR 共 {cash_total:,}")
        if names:
            parts.append(f"{len(names)} 辆车")
        self._log("info", f"测试汇总：找到 {len(crops)} 个高亮格 → " + "，".join(parts))
        kind = "car" if names else "cash"
        return kind, cash_total, (names[0] if names else "")

    def _note_cars(self, index: int, names: list[str], count: int) -> None:
        named = [n for n in names if n]
        self.stats.car_wins += count
        self.stats.last_reward_kind = "car"
        self.stats.last_reward = named[0] if named else "车辆"
        self._push_reward("车：" + ("、".join(named) if named else "未知车辆"))
        shown = "、".join(named) if named else "名字没读清"
        self._log("info", f"第 {index} 次抽奖 → 这轮抽到 {count} 辆车（{shown}），"
                          "接着按「已拥有车辆」的策略处理")

    def _push_reward(self, text: str) -> None:
        self.stats.rewards.append(text)
        if len(self.stats.rewards) > REWARD_LIST_KEEP:
            del self.stats.rewards[:-REWARD_LIST_KEEP]

    def _reward_crops(self, image: np.ndarray, region, mode: str) -> list[np.ndarray]:
        """找出奖励面板上所有「本次抽中的高亮格」，从左到右返回切好的小图。

        转盘是一列一列停的，每一列各中一格（Super Wheelspin 一次给三份），
        所以这里返回的是一组格子而不是单个。
        判定办法：以整块面板的中位亮度作基准，明显更亮（或更暗）且面积够大的
        连通块就是中奖格；一个都找不到，说明这一刻结果还没揭晓，就什么都不做。
        """
        panel = self._crop(image, region)
        if panel is None or panel.size == 0:
            return []
        if mode == "none":
            return [panel]
        try:
            import cv2
        except Exception:
            return []
        gray = cv2.cvtColor(panel, cv2.COLOR_BGR2GRAY)
        base = float(np.median(gray))
        if mode == "dark":
            mask = np.where(gray < base - 40.0, 255, 0).astype(np.uint8)
        else:
            mask = np.where(gray > max(170.0, base + 40.0), 255, 0).astype(np.uint8)
        kernel = np.ones((7, 7), np.uint8)
        mask = cv2.morphologyEx(mask, cv2.MORPH_OPEN, kernel)
        mask = cv2.morphologyEx(mask, cv2.MORPH_CLOSE, kernel)
        contours, _ = cv2.findContours(mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
        panel_area = float(panel.shape[0] * panel.shape[1])
        boxes = []
        for contour in contours:
            area = float(cv2.contourArea(contour))
            if area < HIGHLIGHT_MIN_AREA:
                continue
            if area > panel_area * HIGHLIGHT_MAX_RATIO:
                continue      # 快占满整块了，那是背景，不是某一格
            boxes.append(cv2.boundingRect(contour))
        if not boxes:
            return []
        boxes.sort(key=lambda box: box[0])       # 从左到右，日志读起来和画面顺序一致
        crops = []
        pad = 4
        for x, y, w, h in boxes:
            x0, y0 = max(0, x - pad), max(0, y - pad)
            x1 = min(panel.shape[1], x + w + pad)
            y1 = min(panel.shape[0], y + h + pad)
            crops.append(np.ascontiguousarray(panel[y0:y1, x0:x1]))
        return crops

    @staticmethod
    def _parse_reward(text: str, min_digits: int = 3) -> tuple[str, int, str]:
        """把高亮格里的文字解析成 (类型, 金额, 显示名)。

        类型：cash 抽到钱 / car 抽到车 / unknown 认不出。
        格子内容要么是「CR 25,000」，要么是车名（「2021 迈凯伦 620R」），
        要么干脆只有一张车图没有文字。
        """
        flat = " ".join((text or "").replace("\u00a0", " ").split())
        if not flat:
            return "unknown", 0, ""
        value = 0
        found = re.search(r"\d[\d,\.]*", flat)
        if found:
            digits = re.sub(r"[^\d]", "", found.group(0))
            if len(digits) >= max(1, int(min_digits)):
                value = int(digits)
        # 把 CR / Credits 这类货币记号抹掉，剩下的如果还有字符，那就是车名（含中文/型号）
        noise = re.sub(r"(?i)cr(edits?)?", " ", flat)
        noise = re.sub(r"[\d,\.\s，、:：%\-—]", "", noise).strip()
        if noise:
            return "car", 0, flat[:REWARD_TEXT_MAX]
        if value:
            return "cash", value, f"{value:,} CR"
        return "unknown", 0, flat[:REWARD_TEXT_MAX]

    def _read_sell_price(self, frame: capture.Frame) -> bool:
        """读出售价并计入统计；返回是否读到了数字。

        价格就写在那行选项里（「出售价格：975,000」），所以这个界面一出现就该读，
        不用等某个单独的确认界面。同一次抽奖只记一次账，避免重复计数。
        """
        smart = self.cfg.get("smart", {}) or {}
        rule = smart.get("sell_price_rule") or {}
        if not rule.get("enabled"):
            return False
        if self._price_read_for == self.stats.spins_started:
            return True      # 这一次已经记过账了
        region = rule.get("region")
        self._warn_wide_region("sell_price_rule", "出售价格", region)
        crop = self._crop(frame.image, region)
        if crop is None:
            return False
        text = self._ocr.recognize(crop, str(rule.get("lang") or "en-US"),
                                   float(rule.get("scale") or 2.0))
        # 不能「取读到的第一个数字」：框选偏大时会把标题里的年份之类一起读进来，
        # 结果读出 6 CR 这种离谱值。优先看「价格 / 售价 / CR」附近，其次取最大的那个。
        value = ocr_mod.biggest_int(text, hints=ocr_mod.PRICE_HINTS, min_value=SANE_PRICE_MIN)
        if value is None or value < SANE_PRICE_MIN:
            self._log("warn", f"价格识别失败（读出 {value!r}，原始文本：{(text or '').strip()[:60]!r}）："
                              "把框选范围收到「出售价格：xxx」那一行会稳很多")
            return False
        self._price_read_for = self.stats.spins_started
        self._note_sale(value)
        return True

    def _note_sale(self, value: int) -> None:
        self.stats.sells += 1
        self.stats.prices.append(value)
        if len(self.stats.prices) > 500:
            del self.stats.prices[:-500]
        self.stats.credits += value
        self._log("info", f"出售车辆：{value:,} CR（出售所得累计 {self.stats.credits:,} CR）")

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
        """按「这一刻画面停在哪个界面」决定按什么键。

        顺序不是写死的流程，而是「认清界面 → 交给对应的处理函数」：
        每处理完一个界面，下一轮重新看画面再判断，所以中途多出一步确认、
        游戏卡一下、界面认错又恢复，都能自然接上。
        """
        now = time.time()
        role = info.role
        if role == "unknown":
            return False

        if role in STATE_ROLES and sig and sig == self._handled_sig:
            # 画面还没变，说明上一次按键游戏还没消化，先别重复按同一个界面。
            # 但如果是「按键后画面恰好又长得一模一样」（例如连续抽到同一辆车、
            # 结果界面反复出现），一直不动手就会干等到卡住保护停机；
            # 等够 REPEAT_GAP_S 就按一次重试，既不狂按也不空等。
            if now - self._handled_at < REPEAT_GAP_S:
                return False
            self._log("warn", f"画面已停留 {REPEAT_GAP_S:.1f} 秒没有变化，重试一次按键")
            self._handled_at = now

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

        # 这个界面是「刚出现」还是「还停在原地」——已拥有车辆、结果界面这类计数
        # 只在刚出现时算一次，否则同一个弹窗被按了十次就会被记成十台车。
        self._role_fresh = role != self._acted_role
        self._acted_role = role

        if role == "ready":
            return self._act_ready(frame, sig, now, smart, start_key, input_mode, hwnd, waits)
        if role == "result_new":
            return self._act_result_new(sig, now, input_mode, hwnd, waits)
        if role == "result_owned":
            return self._act_owned(frame, sig, now, smart, owned_policy, owned_keys,
                                   input_mode, hwnd, waits)
        if role == "sell":
            return self._act_sell(frame, sig, now, smart, input_mode, hwnd, waits)
        if role == "confirm":
            return self._act_confirm(sig, now, input_mode, hwnd, waits)
        if role == "end":
            return self._act_end(smart)
        if role == "blocked":
            return self._act_blocked(sig, now, smart, input_mode, hwnd, waits)
        return False

    # 下面每个函数只负责「这一个界面上该做的事」，互不干涉。
    # 抽奖界面（含结果页底部的「领取奖励并再次抽奖」）→ 继续下一次
    def _act_ready(self, frame, sig, now, smart, start_key, input_mode, hwnd, waits) -> bool:
        remaining = self.stats.remaining
        hint = f"，游戏内还剩 {remaining} 次" if isinstance(remaining, int) else ""
        # 正要按 Enter「领取奖励并再次抽奖」的这一瞬间，画面是停住的，
        # 正是读「这一次抽到了什么」最稳的时候；读完再按，顺序才不会错。
        # 第一次进来还没抽过，不用读。
        if self.stats.spins_started >= 1:
            self._read_reward(frame, smart)
        self._press([start_key], input_mode, hwnd,
                    f"开始抽奖（第 {self.stats.spins_started + 1} 次）{hint}")
        self.stats.spins_started += 1
        self.stats.last_action = f"第 {self.stats.spins_started} 次抽奖"
        # 不再死等固定的 after_start_ms：只做一小段起步保护，
        # 之后一识别到结果界面就立刻接手（低配机不会误判，高配机不用白等）
        self._cooldown_until = now + START_GUARD_S
        self._waiting_result_until = now + waits["after_start_ms"] / 1000.0
        self._handled_sig = sig
        self._handled_at = now
        return True

    def _act_result_new(self, sig, now, input_mode, hwnd, waits) -> bool:
        """抽到新车 → 收下继续。"""
        self._press(["enter"], input_mode, hwnd, "抽到新车 → 领取")
        if self._role_fresh:      # 同上：只在这一屏刚出现时记一次
            self.stats.results += 1
            self.stats.new_cars += 1
        self._cooldown_until = now + waits["after_result_ms"] / 1000.0
        self._handled_sig = sig
        self._handled_at = now
        return True

    def _act_owned(self, frame, sig, now, smart, owned_policy, owned_keys,
                   input_mode, hwnd, waits) -> bool:
        """抽到重复车：「添加至车库 / 送礼 / 出售」三选一。

        策略是提前设定好的，之后每台重复车都照这个来。
        选出售时，价格就写在选项那一行（「出售价格：975,000」），
        所以趁这个界面还在赶紧读出来记账，不用等后面的确认界面。
        """
        # 只在弹窗「刚出现」时计数：同一个弹窗反复按，不能记成好几台车
        if self._role_fresh:
            self.stats.results += 1
            self.stats.owned += 1
        if owned_policy == "sell":
            rule = smart.get("sell_price_rule") or {}
            priced = self._read_sell_price(frame)
            if rule.get("enabled") and rule.get("abort_on_fail") and not priced:
                # 界面认出来了但价格读不出来，多半是认错了，宁可停下来也别乱卖
                self._save_fail_frames(frame, "出售价格识别失败")
                self._exit_reason = "出售价格没识别出来，已停止（避免误判界面把车卖掉）"
                self._stop_flag.set()
                return True
        keys = list(owned_keys) or ["enter"]
        label = {"garage": "加入车库", "gift": "送礼", "sell": "出售"}.get(owned_policy, owned_policy)
        car = f"「{self.stats.last_reward}」" if self.stats.last_reward_kind == "car" else ""
        self._press(keys, input_mode, hwnd, f"已拥有车辆{car} → {label}")
        if owned_policy == "sell":
            self._cooldown_until = now + waits["after_option_ms"] / 1000.0
        else:
            self._cooldown_until = now + waits["after_result_ms"] / 1000.0
        self._handled_sig = sig
        self._handled_at = now
        return True

    def _act_sell(self, frame, sig, now, smart, input_mode, hwnd, waits) -> bool:
        """出售确认界面（有些流程在选完「出售」之后还会再确认一次）。"""
        rule = smart.get("sell_price_rule") or {}
        priced = self._read_sell_price(frame)
        if rule.get("enabled") and rule.get("abort_on_fail") and not priced:
            self._save_fail_frames(frame, "出售价格识别失败")
            self._exit_reason = "出售价格没识别出来，已停止（避免误判界面把车卖掉）"
            self._stop_flag.set()
            return True
        key = str(smart.get("sell_confirm_key") or "enter")
        self._press([key], input_mode, hwnd, "确认出售")
        self._cooldown_until = now + waits["after_confirm_ms"] / 1000.0
        self._handled_sig = sig
        self._handled_at = now
        return True

    def _act_confirm(self, sig, now, input_mode, hwnd, waits) -> bool:
        """通用确认弹窗。"""
        self._press(["enter"], input_mode, hwnd, "确认弹窗")
        self._cooldown_until = now + waits["after_confirm_ms"] / 1000.0
        self._handled_sig = sig
        self._handled_at = now
        return True

    def _act_end(self, smart) -> bool:
        """抽奖结束界面。"""
        if smart.get("auto_stop_on_end_template", True):
            self._exit_reason = "识别到「抽奖结束」界面，收工"
            self._stop_flag.set()
            return True
        # 用户明确关掉了「结束自动停」，这里就静观其变：
        # 返回 False 让主循环按轮询间隔歇一下，不然会满速空转吃 CPU
        return False

    def _act_blocked(self, sig, now, smart, input_mode, hwnd, waits) -> bool:
        """被别的界面挡住：按用户设定的兜底方式处理。"""
        action = str(smart.get("unknown_action") or "wait")
        if action == "enter":
            self._press(["enter"], input_mode, hwnd, "处理阻挡界面")
        elif action == "esc":
            self._press(["esc"], input_mode, hwnd, "处理阻挡界面")
        self._cooldown_until = now + max(0.6, waits["after_result_ms"] / 1000.0)
        self._handled_sig = sig
        self._handled_at = now
        # 「什么都不做」也要返回 False，交给主循环 sleep，避免满速空转
        return action in ("enter", "esc")

    def _press(self, keys: list[str], input_mode: str, hwnd: int, label: str) -> None:
        if not self._guard_foreground(hwnd, input_mode):
            # 游戏不在前台：这次不按键。稍微歇一下，别让主循环满速空转
            self._sleep_cancellable(0.2)
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
            "抽到车辆": stats.car_wins,
            "抽到 CR 次数": stats.cash_hits,
            "抽奖所得 CR": stats.cash_credits,
            "出售次数": stats.sells,
            "出售所得 CR": stats.credits,
            "平均单价": stats.avg_price(),
            "合计 CR": stats.total_credits(),
            "总耗时": stats.elapsed_text(),
            "运行时长": f"{stats.elapsed():.0f}s",
            "速度": f"{stats.rate_per_min():.1f} 次/分",
        }
