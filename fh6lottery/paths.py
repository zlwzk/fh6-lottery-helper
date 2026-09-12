"""统一的路径管理。

用户数据一律放在 %APPDATA% 下，绝不写进游戏目录，也绝不在界面里暴露 Windows 用户名。
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

APP_NAME = "FH6LotteryHelper"
APP_DISPLAY = "FH6 抽奖助手"


def _appdata_root() -> Path:
    base = os.environ.get("APPDATA")
    if not base:
        base = str(Path.home() / "AppData" / "Roaming")
    return Path(base) / APP_NAME


DATA_DIR = _appdata_root()
TEMPLATE_DIR = DATA_DIR / "templates"
TEMPLATE_INDEX = DATA_DIR / "templates.json"
LOG_DIR = DATA_DIR / "logs"
TEMP_DIR = DATA_DIR / "temp"
CONFIG_FILE = DATA_DIR / "config.json"
FROZEN_DIR = DATA_DIR / "frames"

for _d in (DATA_DIR, TEMPLATE_DIR, LOG_DIR, TEMP_DIR, FROZEN_DIR):
    try:
        _d.mkdir(parents=True, exist_ok=True)
    except OSError:
        pass


def resource_path(relative: str) -> Path:
    """取打包后（PyInstaller）或源码运行时都有效的资源路径。"""
    base = getattr(sys, "_MEIPASS", None)
    if base:
        return Path(base) / relative
    return Path(__file__).resolve().parent.parent / relative


def display_path(p: Path | str) -> str:
    """把绝对路径折叠成占位符形式，避免在界面上暴露本机用户名。"""
    s = str(p)
    appdata = os.environ.get("APPDATA", "")
    if appdata and s.lower().startswith(appdata.lower()):
        return "%APPDATA%" + s[len(appdata):]
    home = str(Path.home())
    if home and s.lower().startswith(home.lower()):
        return "%USERPROFILE%" + s[len(home):]
    return s


def sanitize_text(text: str) -> str:
    """把一段文本里的本机路径 / 用户名 / 机器名换成占位符。

    日志和崩溃信息经常被贴到公开的 issue 里，落盘前统一过一遍，
    规则与界面上的 display_path 保持一致。
    """
    if not text:
        return text
    import re

    out = str(text)
    pairs = [
        (os.environ.get("LOCALAPPDATA", ""), "%LOCALAPPDATA%"),
        (os.environ.get("APPDATA", ""), "%APPDATA%"),
        (os.environ.get("TEMP", ""), "%TEMP%"),
        (os.environ.get("TMP", ""), "%TEMP%"),
        (str(Path.home()), "%USERPROFILE%"),
    ]
    # 先替换更长的路径（LOCALAPPDATA 在 APPDATA 之上），避免被短前缀截胡
    for value, placeholder in sorted(pairs, key=lambda item: len(item[0]), reverse=True):
        if value and len(value) > 3:
            out = re.sub(re.escape(value), placeholder, out, flags=re.IGNORECASE)
    # 兜底：任何盘符下的 \Users\<名字>\
    out = re.sub(r"([A-Za-z]:\\Users\\)[^\\\s\"']+", r"\1<用户名>", out, flags=re.IGNORECASE)
    computer = os.environ.get("COMPUTERNAME", "")
    if computer and len(computer) > 2:
        out = re.sub(re.escape(computer), "<机器名>", out, flags=re.IGNORECASE)
    return out
