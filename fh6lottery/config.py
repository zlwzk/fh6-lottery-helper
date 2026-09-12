"""配置读写（JSON，原子写入，缺失字段自动补默认值）。"""

from __future__ import annotations

import copy
import json
import os
import tempfile
from typing import Any

from . import paths

DEFAULTS: dict[str, Any] = {
    "version": 1,
    # 目标窗口
    "window": {
        "title": "",
        "hwnd": 0,
        "process": "",
        "keyword": "forza",  # 自动探测时匹配的标题/进程名关键字（不区分大小写）
    },
    "mode": "smart",  # smart = 识别驱动；rhythm = 固定节奏宏
    "hotkeys": {
        "start": "F7",
        "stop": "F8",
        "pause": "F11",
        "capture": "F9",
        "toggle_overlay": "F10",
    },
    "overlay": {
        "enabled": True,
        "locked": True,
        "x": 48,
        "y": 48,
        "opacity": 0.94,
        "scale": 1.0,
        "show_preview": True,
    },
    "input": {
        "mode": "global",  # global = SendInput 到系统（游戏需前台）；message = PostMessage 到目标窗口
        "hold_ms": 45,
        "key_delay_ms": 90,
        "start_key": "enter",
    },
    "safety": {
        "corner_failsafe": True,        # 鼠标移到屏幕左上角急停
        "require_foreground": True,     # 仅当游戏在前台时才发按键（全局模式）
        "max_unknown_before_stop": 30,  # 连续识别失败上限
        "min_action_interval_ms": 260,  # 两次按键之间的最小间隔
        "save_fail_frames": True,       # 识别失败停机时把画面存到 frames\ 便于补模板
    },
    "smart": {
        "poll_interval_ms": 220,
        "ocr_interval_ms": 900,
        "match_threshold": 0.86,
        "multi_scale": False,
        "auto_scale": True,             # 按窗口尺寸自动缩放模板（换分辨率不用重录）
        "stuck_timeout_s": 20,          # 同一个静态画面停留多久算「卡住」
        "use_templates": True,
        "use_ocr": True,
        "roles": {
            "ready": [],
            "result_owned": [],
            "result_new": [],
            "sell": [],
            "confirm": [],
            "end": [],
            "blocked": [],
        },
        "keyword_rules": [
            {"id": "owned", "role": "result_owned", "enabled": True,
             "keywords": ["已拥有", "已有"], "region": None, "lang": "zh-Hans-CN"},
            {"id": "sell", "role": "sell", "enabled": False,
             "keywords": ["出售价格", "出售"], "region": None, "lang": "zh-Hans-CN"},
            {"id": "ready", "role": "ready", "enabled": False,
             "keywords": ["抽奖", "Wheelspin"], "region": None, "lang": "zh-Hans-CN"},
        ],
        "waits": {
            "after_start_ms": 5200,    # 从抽奖主界面按下开始后，转盘动画时长
            "after_result_ms": 650,    # 结果界面操作后
            "after_option_ms": 950,    # 已拥有选项（下键）操作后
            "after_confirm_ms": 700,
        },
        "owned_policy": "garage",  # garage | gift | sell
        "owned_keys": {
            "garage": ["enter"],
            "gift": ["down", "enter"],
            "sell": ["down", "down", "enter"],
        },
        "sell_confirm_key": "enter",
        "target_spins": 0,  # 0 = 不限
        "spin_count_rule": {"enabled": False, "region": None, "regex": "(\\d+)",
                            "lang": "zh-Hans-CN", "scale": 2.0},
        "sell_price_rule": {"enabled": False, "region": None, "regex": "([\\d][\\d,\\.]*)",
                            "lang": "en-US", "scale": 2.0,
                            "abort_on_fail": False},  # 读不到价格就不确认出售（避免误卖）
        "auto_stop_on_end_template": True,
        "unknown_action": "wait",  # wait | enter | esc
    },
    "rhythm": {
        "steps": [
            {"type": "key", "key": "enter", "hold_ms": 45, "delay_ms": 1200, "x": 0, "y": 0},
        ],
        "loop_delay_ms": 0,
        "max_loops": 0,
        "click_relative": True,  # 点击坐标相对游戏客户区（窗口挪了位置也不点偏）
    },
    "post": {
        "enabled": False,
        "only_on_success": True,
        "shutdown": False,
        "shutdown_delay_s": 60,
        "close_game": False,
        "close_game_force": False,
        "launch_enabled": False,
        "launch_target": "",          # steam://rungameid/1234 或 exe/url 路径
        "pause_esc": False,
        "pause_esc_count": 1,
        "sound": True,
    },
}


def _deep_merge(base: dict, override: dict) -> dict:
    out = copy.deepcopy(base)
    for key, value in (override or {}).items():
        if key in out and isinstance(out[key], dict) and isinstance(value, dict):
            out[key] = _deep_merge(out[key], value)
        else:
            out[key] = copy.deepcopy(value)
    return out


class Config:
    def __init__(self, data: dict[str, Any] | None = None):
        self.data: dict[str, Any] = _deep_merge(DEFAULTS, data or {})

    # ---------- 读写 ----------
    @classmethod
    def load(cls) -> "Config":
        if paths.CONFIG_FILE.exists():
            try:
                raw = json.loads(paths.CONFIG_FILE.read_text(encoding="utf-8"))
                if isinstance(raw, dict):
                    return cls(raw)
            except (OSError, ValueError):
                pass
        return cls()

    def save(self) -> None:
        paths.DATA_DIR.mkdir(parents=True, exist_ok=True)
        payload = json.dumps(self.data, ensure_ascii=False, indent=2)
        fd, tmp = tempfile.mkstemp(dir=str(paths.DATA_DIR), suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                fh.write(payload)
            os.replace(tmp, paths.CONFIG_FILE)
        except Exception:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise

    # ---------- 访问 ----------
    def get(self, path: str, default: Any = None) -> Any:
        node: Any = self.data
        for part in path.split("."):
            if not isinstance(node, dict) or part not in node:
                return default
            node = node[part]
        return node

    def set(self, path: str, value: Any) -> None:
        parts = path.split(".")
        node = self.data
        for part in parts[:-1]:
            nxt = node.get(part)
            if not isinstance(nxt, dict):
                nxt = {}
                node[part] = nxt
            node = nxt
        node[parts[-1]] = value

    def merge(self, patch: dict) -> None:
        self.data = _deep_merge(self.data, patch)

    def clone(self) -> "Config":
        return Config(copy.deepcopy(self.data))

    def to_dict(self) -> dict:
        return copy.deepcopy(self.data)

    def reset(self) -> None:
        self.data = copy.deepcopy(DEFAULTS)
