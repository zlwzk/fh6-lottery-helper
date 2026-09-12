"""抽奖完成后的收尾动作：关机 / 关游戏 / 打开另一个游戏 / 回暂停界面。"""

from __future__ import annotations

import os
import subprocess
import time
from typing import Callable

from . import winutil

CREATE_NO_WINDOW = 0x08000000


def _launch(target: str) -> None:
    target = target.strip()
    if not target:
        raise ValueError("启动目标为空")
    if target.lower().startswith(("steam://", "http://", "https://", "uplay://", "com.epicgames")):
        os.startfile(target)  # noqa: S606 - 由用户显式填写
        return
    path = os.path.expandvars(os.path.expanduser(target))
    if not os.path.exists(path):
        raise FileNotFoundError(f"找不到目标：{path}")
    if path.lower().endswith((".bat", ".cmd")):
        subprocess.Popen(["cmd", "/c", path], creationflags=CREATE_NO_WINDOW)
    else:
        os.startfile(path)  # noqa: S606


def run_post_actions(cfg, stats, success: bool,
                     log: Callable[[str, str], None]) -> None:
    """按配置执行收尾动作；任何一步失败都只记日志，不中断其他步骤。"""
    post = cfg.get("post", {}) or {}
    if not post.get("enabled"):
        return
    if post.get("only_on_success", True) and not success:
        log("info", "非正常结束，跳过收尾动作")
        return

    if post.get("pause_esc"):
        count = max(1, int(post.get("pause_esc_count", 1) or 1))
        try:
            hwnd = int(cfg.get("window.hwnd", 0) or 0)
            for _ in range(count):
                winutil.press_key("esc", 45, cfg.get("input.mode", "global"), hwnd)
                time.sleep(0.25)
            log("info", f"已发送 ESC ×{count}，返回暂停界面")
        except Exception as exc:
            log("warn", f"返回暂停界面失败：{exc}")

    if post.get("close_game"):
        _close_game(cfg, post, log)

    if post.get("launch_enabled") and post.get("launch_target"):
        try:
            _launch(str(post["launch_target"]))
            log("info", f"已尝试启动：{post['launch_target']}")
        except Exception as exc:
            log("warn", f"启动目标失败：{exc}")

    if post.get("sound"):
        try:
            winutil.beep_success()
        except Exception:
            pass

    if post.get("shutdown"):
        delay = max(0, int(post.get("shutdown_delay_s", 60) or 0))
        try:
            winutil.shutdown_computer(delay)
            log("warn", f"已计划关机：{delay} 秒后执行（可用「取消关机」按钮或 shutdown /a 撤销）")
        except Exception as exc:
            log("error", f"关机命令执行失败：{exc}")


def _close_game(cfg, post, log: Callable[[str, str], None]) -> None:
    hwnd = int(cfg.get("window.hwnd", 0) or 0)
    process = str(cfg.get("window.process", "") or "")
    info = winutil.window_info(hwnd) if hwnd else None
    if info is None and process:
        info = winutil.find_window_by_process(process)
    if info is None:
        log("warn", "关闭游戏失败：找不到目标窗口")
        return
    pid = info.pid
    winutil.close_window(info.hwnd)
    log("info", "已发送关闭请求（WM_CLOSE）")
    for _ in range(20):
        time.sleep(0.25)
        if not winutil.is_window_alive(info.hwnd):
            log("info", "游戏窗口已关闭")
            return
    if post.get("close_game_force"):
        if winutil.terminate_process(pid):
            log("warn", "游戏未响应关闭请求，已强制结束进程")
        else:
            log("warn", "强制结束进程失败（可能需要管理员权限）")
    else:
        log("warn", "游戏仍在运行；如需强制结束请在设置里开启「强制结束进程」")
