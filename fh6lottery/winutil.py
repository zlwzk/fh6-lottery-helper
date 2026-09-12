"""Win32 底层能力：窗口枚举、客户区取景、模拟输入、全局热键、窗口样式。

这里的所有操作都是"外部"的：只枚举窗口、读窗口矩形、往系统输入队列塞按键事件，
不打开游戏进程内存、不加载任何模块到游戏里。
"""

from __future__ import annotations

import ctypes
import ctypes.wintypes as wt
import os
import subprocess
import time
from dataclasses import dataclass

user32 = ctypes.WinDLL("user32", use_last_error=True)
kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
try:
    shcore = ctypes.WinDLL("shcore", use_last_error=True)
except OSError:  # pragma: no cover - 老系统
    shcore = None

IS_64BIT = ctypes.sizeof(ctypes.c_void_p) == 8


# --------------------------------------------------------------------------- #
# DPI
# --------------------------------------------------------------------------- #
def enable_dpi_awareness() -> None:
    """必须在创建 Qt 应用前调用，保证坐标与截图物理像素一致。"""
    try:
        if shcore is not None:
            shcore.SetProcessDpiAwareness(2)  # PROCESS_PER_MONITOR_DPI_AWARE
            return
    except Exception:
        pass
    try:
        user32.SetProcessDPIAware()
    except Exception:
        pass


# --------------------------------------------------------------------------- #
# 窗口枚举
# --------------------------------------------------------------------------- #
WNDENUMPROC = ctypes.WINFUNCTYPE(wt.BOOL, wt.HWND, wt.LPARAM)

user32.EnumWindows.argtypes = [WNDENUMPROC, wt.LPARAM]
user32.EnumWindows.restype = wt.BOOL
user32.GetWindowTextLengthW.argtypes = [wt.HWND]
user32.GetWindowTextW.argtypes = [wt.HWND, wt.LPWSTR, ctypes.c_int]
user32.IsWindowVisible.argtypes = [wt.HWND]
user32.IsWindow.argtypes = [wt.HWND]
user32.GetWindowThreadProcessId.argtypes = [wt.HWND, ctypes.POINTER(wt.DWORD)]
user32.GetClientRect.argtypes = [wt.HWND, ctypes.POINTER(wt.RECT)]
user32.ClientToScreen.argtypes = [wt.HWND, ctypes.POINTER(wt.POINT)]
user32.GetWindowRect.argtypes = [wt.HWND, ctypes.POINTER(wt.RECT)]
user32.IsIconic.argtypes = [wt.HWND]
user32.PostMessageW.argtypes = [wt.HWND, wt.UINT, wt.WPARAM, wt.LPARAM]
user32.SetForegroundWindow.argtypes = [wt.HWND]
user32.GetForegroundWindow.restype = wt.HWND
user32.GetCursorPos.argtypes = [ctypes.POINTER(wt.POINT)]
user32.GetWindow.argtypes = [wt.HWND, wt.UINT]
user32.GetWindow.restype = wt.HWND
user32.GetForegroundWindow.argtypes = []
user32.WindowFromPoint.argtypes = [wt.POINT]
user32.WindowFromPoint.restype = wt.HWND
user32.GetAncestor.argtypes = [wt.HWND, wt.UINT]
user32.GetAncestor.restype = wt.HWND
user32.RegisterHotKey.argtypes = [wt.HWND, ctypes.c_int, wt.UINT, wt.UINT]
user32.UnregisterHotKey.argtypes = [wt.HWND, ctypes.c_int]
user32.GetSystemMetrics.argtypes = [ctypes.c_int]

kernel32.OpenProcess.argtypes = [wt.DWORD, wt.BOOL, wt.DWORD]
kernel32.OpenProcess.restype = wt.HANDLE
kernel32.QueryFullProcessImageNameW.argtypes = [wt.HANDLE, wt.DWORD, wt.LPWSTR, ctypes.POINTER(wt.DWORD)]
kernel32.CloseHandle.argtypes = [wt.HANDLE]

GW_OWNER = 4
GA_ROOT = 2
PROCESS_QUERY_LIMITED_INFORMATION = 0x1000


@dataclass
class WindowInfo:
    hwnd: int
    title: str
    pid: int
    process: str
    rect: tuple[int, int, int, int]  # 窗口矩形 x, y, w, h
    client: tuple[int, int, int, int]  # 客户区（屏幕坐标）x, y, w, h
    minimized: bool

    @property
    def size(self) -> tuple[int, int]:
        return self.client[2], self.client[3]

    def label(self) -> str:
        proc = self.process or "?"
        return f"{self.title or '(无标题)'}  ·  {proc}  ·  {self.client[2]}×{self.client[3]}"


def process_name_from_pid(pid: int) -> str:
    if not pid:
        return ""
    handle = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if not handle:
        return ""
    try:
        buf = ctypes.create_unicode_buffer(1024)
        size = wt.DWORD(len(buf))
        if kernel32.QueryFullProcessImageNameW(handle, 0, buf, ctypes.byref(size)):
            return os.path.basename(buf.value)
    except Exception:
        return ""
    finally:
        kernel32.CloseHandle(handle)
    return ""


def _window_text(hwnd: int) -> str:
    length = user32.GetWindowTextLengthW(hwnd)
    if length <= 0:
        return ""
    buf = ctypes.create_unicode_buffer(length + 1)
    user32.GetWindowTextW(hwnd, buf, length + 1)
    return buf.value


def _rect(hwnd: int) -> tuple[int, int, int, int]:
    r = wt.RECT()
    if not user32.GetWindowRect(hwnd, ctypes.byref(r)):
        return (0, 0, 0, 0)
    return (r.left, r.top, r.right - r.left, r.bottom - r.top)


def _client_rect_screen(hwnd: int) -> tuple[int, int, int, int]:
    r = wt.RECT()
    if not user32.GetClientRect(hwnd, ctypes.byref(r)):
        return (0, 0, 0, 0)
    pt = wt.POINT(r.left, r.top)
    if not user32.ClientToScreen(hwnd, ctypes.byref(pt)):
        return (0, 0, 0, 0)
    return (pt.x, pt.y, r.right - r.left, r.bottom - r.top)


def window_info(hwnd: int) -> WindowInfo | None:
    if not hwnd or not user32.IsWindow(hwnd):
        return None
    pid = wt.DWORD()
    user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
    x, y, w, h = _rect(hwnd)
    cx, cy, cw, ch = _client_rect_screen(hwnd)
    return WindowInfo(
        hwnd=int(hwnd),
        title=_window_text(hwnd),
        pid=int(pid.value),
        process=process_name_from_pid(int(pid.value)),
        rect=(x, y, w, h),
        client=(cx, cy, cw, ch),
        minimized=bool(user32.IsIconic(hwnd)),
    )


def list_windows(min_client: tuple[int, int] = (160, 120)) -> list[WindowInfo]:
    """列出可见的顶层窗口（过滤掉无标题的小工具窗口）。"""
    found: list[WindowInfo] = []

    def _cb(hwnd, _lparam):
        if not user32.IsWindowVisible(hwnd):
            return True
        if user32.GetWindow(hwnd, GW_OWNER):
            return True
        info = window_info(hwnd)
        if info is None or not info.title:
            return True
        if info.client[2] < min_client[0] or info.client[3] < min_client[1]:
            return True
        found.append(info)
        return True

    user32.EnumWindows(WNDENUMPROC(_cb), 0)
    return found


def find_window_by_keyword(keyword: str) -> WindowInfo | None:
    """按标题/进程名关键字（不区分大小写）挑一个最像游戏窗口的窗口。"""
    if not keyword:
        return None
    needle = keyword.strip().lower()
    best: WindowInfo | None = None
    for info in list_windows():
        hay = f"{info.title} {info.process}".lower()
        if needle not in hay:
            continue
        if best is None or info.client[2] * info.client[3] > best.client[2] * best.client[3]:
            best = info
    return best


def find_window_by_process(process: str) -> WindowInfo | None:
    if not process:
        return None
    needle = process.strip().lower()
    best: WindowInfo | None = None
    for info in list_windows():
        if info.process.lower() != needle:
            continue
        if best is None or info.client[2] * info.client[3] > best.client[2] * best.client[3]:
            best = info
    return best


def is_foreground(hwnd: int) -> bool:
    if not hwnd:
        return False
    return int(user32.GetForegroundWindow()) == int(hwnd)


def foreground_hwnd() -> int:
    return int(user32.GetForegroundWindow())


def focus_window(hwnd: int) -> bool:
    try:
        return bool(user32.SetForegroundWindow(wt.HWND(hwnd)))
    except Exception:
        return False


def is_window_alive(hwnd: int) -> bool:
    return bool(user32.IsWindow(wt.HWND(hwnd)))


def close_window(hwnd: int) -> None:
    user32.PostMessageW(wt.HWND(hwnd), 0x0010, 0, 0)  # WM_CLOSE


def terminate_process(pid: int) -> bool:
    PROCESS_TERMINATE = 0x0001
    handle = kernel32.OpenProcess(PROCESS_TERMINATE, False, pid)
    if not handle:
        return False
    try:
        return bool(kernel32.TerminateProcess(handle, 0))
    finally:
        kernel32.CloseHandle(handle)


# --------------------------------------------------------------------------- #
# 鼠标位置
# --------------------------------------------------------------------------- #
def cursor_pos() -> tuple[int, int]:
    pt = wt.POINT()
    user32.GetCursorPos(ctypes.byref(pt))
    return int(pt.x), int(pt.y)


def window_at_point(x: int, y: int) -> int:
    hwnd = user32.WindowFromPoint(wt.POINT(x, y))
    return int(user32.GetAncestor(hwnd, GA_ROOT)) if hwnd else 0


# --------------------------------------------------------------------------- #
# 按键映射与模拟输入
# --------------------------------------------------------------------------- #
def _letters() -> dict[str, tuple[int, int, bool]]:
    scans = {
        "a": 0x1E, "b": 0x30, "c": 0x2E, "d": 0x20, "e": 0x12, "f": 0x21,
        "g": 0x22, "h": 0x23, "i": 0x17, "j": 0x24, "k": 0x25, "l": 0x26,
        "m": 0x32, "n": 0x31, "o": 0x18, "p": 0x19, "q": 0x10, "r": 0x13,
        "s": 0x1F, "t": 0x14, "u": 0x16, "v": 0x2F, "w": 0x11, "x": 0x2D,
        "y": 0x15, "z": 0x2C,
    }
    return {k: (ord(k.upper()), v, False) for k, v in scans.items()}


def _digits() -> dict[str, tuple[int, int, bool]]:
    scans = {"1": 0x02, "2": 0x03, "3": 0x04, "4": 0x05, "5": 0x06,
             "6": 0x07, "7": 0x08, "8": 0x09, "9": 0x0A, "0": 0x0B}
    return {k: (0x30 + int(k), v, False) for k, v in scans.items()}


def _functions() -> dict[str, tuple[int, int, bool]]:
    scans = {1: 0x3B, 2: 0x3C, 3: 0x3D, 4: 0x3E, 5: 0x3F, 6: 0x40, 7: 0x41,
             8: 0x42, 9: 0x43, 10: 0x44, 11: 0x57, 12: 0x58}
    return {f"f{i}": (0x6F + i, s, False) for i, s in scans.items()}


KEYMAP: dict[str, tuple[int, int, bool]] = {
    "enter": (0x0D, 0x1C, False),
    "return": (0x0D, 0x1C, False),
    "esc": (0x1B, 0x01, False),
    "escape": (0x1B, 0x01, False),
    "space": (0x20, 0x39, False),
    "tab": (0x09, 0x0F, False),
    "backspace": (0x08, 0x0E, False),
    "delete": (0x2E, 0x53, True),
    "insert": (0x2D, 0x52, True),
    "home": (0x24, 0x47, True),
    "end": (0x23, 0x4F, True),
    "pageup": (0x21, 0x49, True),
    "pagedown": (0x22, 0x51, True),
    "up": (0x26, 0x48, True),
    "down": (0x28, 0x50, True),
    "left": (0x25, 0x4B, True),
    "right": (0x27, 0x4D, True),
    "shift": (0x10, 0x2A, False),
    "ctrl": (0x11, 0x1D, False),
    "control": (0x11, 0x1D, False),
    "alt": (0x12, 0x38, False),
    "plus": (0xBB, 0x0D, False),
    "minus": (0xBD, 0x0C, False),
}
KEYMAP.update(_letters())
KEYMAP.update(_digits())
KEYMAP.update(_functions())

KEY_CHOICES = [
    "enter", "space", "esc", "up", "down", "left", "right", "tab",
    "f1", "f2", "f3", "f4", "f5", "f6", "f7", "f8", "f9", "f10", "f11", "f12",
]

INPUT_KEYBOARD = 1
INPUT_MOUSE = 0
KEYEVENTF_EXTENDEDKEY = 0x0001
KEYEVENTF_KEYUP = 0x0002
KEYEVENTF_SCANCODE = 0x0008
MOUSEEVENTF_MOVE = 0x0001
MOUSEEVENTF_ABSOLUTE = 0x8000
MOUSEEVENTF_LEFTDOWN = 0x0002
MOUSEEVENTF_LEFTUP = 0x0004


class KEYBDINPUT(ctypes.Structure):
    _fields_ = [("wVk", wt.WORD), ("wScan", wt.WORD), ("dwFlags", wt.DWORD),
                ("time", wt.DWORD), ("dwExtraInfo", ctypes.POINTER(ctypes.c_ulong))]


class MOUSEINPUT(ctypes.Structure):
    _fields_ = [("dx", wt.LONG), ("dy", wt.LONG), ("mouseData", wt.DWORD),
                ("dwFlags", wt.DWORD), ("time", wt.DWORD),
                ("dwExtraInfo", ctypes.POINTER(ctypes.c_ulong))]


class HARDWAREINPUT(ctypes.Structure):
    _fields_ = [("uMsg", wt.DWORD), ("wParamL", wt.WORD), ("wParamH", wt.WORD)]


class _INPUTUNION(ctypes.Union):
    _fields_ = [("ki", KEYBDINPUT), ("mi", MOUSEINPUT), ("hi", HARDWAREINPUT)]


class INPUT(ctypes.Structure):
    _fields_ = [("type", wt.DWORD), ("u", _INPUTUNION)]


user32.SendInput.argtypes = [wt.UINT, ctypes.POINTER(INPUT), ctypes.c_int]
user32.SendInput.restype = wt.UINT

WM_KEYDOWN = 0x0100
WM_KEYUP = 0x0101
WM_SYSKEYDOWN = 0x0104
WM_SYSKEYUP = 0x0105


class InputError(RuntimeError):
    pass


def normalize_key(name: str) -> tuple[int, int, bool]:
    key = (name or "").strip().lower()
    if key not in KEYMAP:
        raise InputError(f"不支持的按键：{name}")
    return KEYMAP[key]


def press_key(name: str, hold_ms: int = 45, mode: str = "global", hwnd: int = 0) -> None:
    """按一次键。global = SendInput（游戏需在前台）；message = PostMessage 到目标窗口。"""
    vk, scan, extended = normalize_key(name)
    hold_ms = max(8, int(hold_ms))

    if mode == "message":
        if not hwnd:
            raise InputError("消息模式需要先选择目标窗口")
        lparam_down = 1 | (scan << 16) | (1 << 24) | (0x20000000 if extended else 0)
        lparam_up = lparam_down | (1 << 30) | (1 << 31)
        down = WM_SYSKEYDOWN if (vk == 0x12 or vk == 0x10 or vk == 0x11) else WM_KEYDOWN
        up = WM_SYSKEYUP if down == WM_SYSKEYDOWN else WM_KEYUP
        user32.PostMessageW(wt.HWND(hwnd), down, vk, lparam_down)
        time.sleep(hold_ms / 1000.0)
        user32.PostMessageW(wt.HWND(hwnd), up, vk, lparam_up)
        return

    flags = KEYEVENTF_SCANCODE | (KEYEVENTF_EXTENDEDKEY if extended else 0)
    event = INPUT()
    event.type = INPUT_KEYBOARD
    event.u.ki = KEYBDINPUT(wVk=0, wScan=scan, dwFlags=flags, time=0, dwExtraInfo=None)
    if user32.SendInput(1, ctypes.byref(event), ctypes.sizeof(INPUT)) != 1:
        raise InputError(f"SendInput 失败（错误码 {ctypes.get_last_error()}）")
    time.sleep(hold_ms / 1000.0)
    event.u.ki.dwFlags = flags | KEYEVENTF_KEYUP
    user32.SendInput(1, ctypes.byref(event), ctypes.sizeof(INPUT))


def click_at(x: int, y: int, mode: str = "global", hwnd: int = 0) -> None:
    if mode == "message":
        if not hwnd:
            raise InputError("消息模式需要先选择目标窗口")
        lp = (y << 16) | (x & 0xFFFF)
        user32.PostMessageW(wt.HWND(hwnd), 0x0201, 1, lp)  # WM_LBUTTONDOWN
        time.sleep(0.03)
        user32.PostMessageW(wt.HWND(hwnd), 0x0202, 0, lp)  # WM_LBUTTONUP
        return

    screen_w = user32.GetSystemMetrics(0)
    screen_h = user32.GetSystemMetrics(1)
    nx = int(x * 65535 / max(1, screen_w - 1))
    ny = int(y * 65535 / max(1, screen_h - 1))
    events = (INPUT * 2)()
    events[0].type = INPUT_MOUSE
    events[0].u.mi = MOUSEINPUT(dx=nx, dy=ny, mouseData=0,
                                dwFlags=MOUSEEVENTF_MOVE | MOUSEEVENTF_ABSOLUTE,
                                time=0, dwExtraInfo=None)
    events[1].type = INPUT_MOUSE
    events[1].u.mi = MOUSEINPUT(dx=nx, dy=ny, mouseData=0,
                                dwFlags=MOUSEEVENTF_MOVE | MOUSEEVENTF_ABSOLUTE
                                | MOUSEEVENTF_LEFTDOWN | MOUSEEVENTF_LEFTUP,
                                time=0, dwExtraInfo=None)
    user32.SendInput(2, events, ctypes.sizeof(INPUT))


# --------------------------------------------------------------------------- #
# 窗口样式（悬浮窗用）
# --------------------------------------------------------------------------- #
GWL_EXSTYLE = -20
WS_EX_LAYERED = 0x00080000
WS_EX_TRANSPARENT = 0x00000020
WS_EX_TOOLWINDOW = 0x00000080
WS_EX_NOACTIVATE = 0x08000000

if IS_64BIT:
    _get_long = user32.GetWindowLongPtrW
    _set_long = user32.SetWindowLongPtrW
else:  # pragma: no cover
    _get_long = user32.GetWindowLongW
    _set_long = user32.SetWindowLongW
_set_long.restype = ctypes.c_long


def set_click_through(hwnd: int, enabled: bool) -> None:
    """让悬浮窗鼠标穿透（不抢游戏焦点）。"""
    if not hwnd:
        return
    ex = _get_long(wt.HWND(hwnd), GWL_EXSTYLE)
    if enabled:
        ex |= WS_EX_LAYERED | WS_EX_TRANSPARENT | WS_EX_TOOLWINDOW | WS_EX_NOACTIVATE
    else:
        ex &= ~(WS_EX_TRANSPARENT | WS_EX_NOACTIVATE)
        ex |= WS_EX_LAYERED | WS_EX_TOOLWINDOW
    _set_long(wt.HWND(hwnd), GWL_EXSTYLE, ex)


def make_no_activate(hwnd: int) -> None:
    if not hwnd:
        return
    ex = _get_long(wt.HWND(hwnd), GWL_EXSTYLE)
    ex |= WS_EX_TOOLWINDOW | WS_EX_NOACTIVATE | WS_EX_LAYERED
    _set_long(wt.HWND(hwnd), GWL_EXSTYLE, ex)


# --------------------------------------------------------------------------- #
# 全局热键
# --------------------------------------------------------------------------- #
WM_HOTKEY = 0x0312
MOD_ALT = 0x0001
MOD_CONTROL = 0x0002
MOD_SHIFT = 0x0004
MOD_WIN = 0x0008
MOD_NOREPEAT = 0x4000


def hotkey_spec(name: str) -> tuple[int, int] | None:
    """把 "F7" / "Ctrl+F8" 解析成 (modifiers, vk)。"""
    if not name:
        return None
    text = name.strip().lower()
    mods = 0
    while "+" in text:
        head, text = text.split("+", 1)
        head = head.strip()
        if head in ("ctrl", "control"):
            mods |= MOD_CONTROL
        elif head == "alt":
            mods |= MOD_ALT
        elif head == "shift":
            mods |= MOD_SHIFT
        elif head in ("win", "super"):
            mods |= MOD_WIN
        else:
            return None
    try:
        vk, _scan, _ext = normalize_key(text)
    except InputError:
        return None
    return mods | MOD_NOREPEAT, vk


def register_hotkey(hwnd: int, ident: int, spec: str) -> bool:
    parsed = hotkey_spec(spec)
    if not parsed:
        return False
    mods, vk = parsed
    return bool(user32.RegisterHotKey(wt.HWND(hwnd), ident, mods, vk))


def unregister_hotkey(hwnd: int, ident: int) -> None:
    try:
        user32.UnregisterHotKey(wt.HWND(hwnd), ident)
    except Exception:
        pass


# --------------------------------------------------------------------------- #
# 系统级动作
# --------------------------------------------------------------------------- #
def shutdown_computer(delay_seconds: int = 60) -> None:
    subprocess.Popen(
        ["shutdown", "/s", "/t", str(max(0, int(delay_seconds)))],
        creationflags=0x08000000,  # CREATE_NO_WINDOW
    )


def abort_shutdown() -> None:
    subprocess.Popen(["shutdown", "/a"], creationflags=0x08000000)


def beep_success() -> None:
    try:
        import winsound
        for freq in (880, 1175):
            winsound.Beep(freq, 130)
    except Exception:
        pass


def virtual_screen_rect() -> tuple[int, int, int, int]:
    """整个虚拟桌面（物理像素）。"""
    x = user32.GetSystemMetrics(76)  # SM_XVIRTUALSCREEN
    y = user32.GetSystemMetrics(77)  # SM_YVIRTUALSCREEN
    w = user32.GetSystemMetrics(78)  # SM_CXVIRTUALSCREEN
    h = user32.GetSystemMetrics(79)  # SM_CYVIRTUALSCREEN
    if w <= 0 or h <= 0:
        w = user32.GetSystemMetrics(0)
        h = user32.GetSystemMetrics(1)
        x, y = 0, 0
    return x, y, w, h
