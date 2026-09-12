"""全局热键：用 RegisterHotKey 注册到一个消息专用窗口，独占按键（不会漏给游戏）。

在独立线程里跑消息循环，命中后通过 Qt 信号（跨线程队列）抛给界面。
"""

from __future__ import annotations

import ctypes
import ctypes.wintypes as wt
import threading

from PySide6.QtCore import QObject, Signal

from . import winutil

HWND_MESSAGE = -3
WM_CLOSE = 0x0010
WM_DESTROY = 0x0002
WM_APP_REAPPLY = 0x8001

user32 = winutil.user32
kernel32 = winutil.kernel32

LRESULT = ctypes.c_ssize_t
WNDPROC = ctypes.WINFUNCTYPE(LRESULT, wt.HWND, wt.UINT, wt.WPARAM, wt.LPARAM)

user32.CreateWindowExW.restype = wt.HWND
user32.CreateWindowExW.argtypes = [wt.DWORD, wt.LPCWSTR, wt.LPCWSTR, wt.DWORD,
                                   ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int,
                                   wt.HWND, wt.HMENU, wt.HINSTANCE, wt.LPVOID]
user32.DefWindowProcW.restype = LRESULT
user32.DefWindowProcW.argtypes = [wt.HWND, wt.UINT, wt.WPARAM, wt.LPARAM]
user32.RegisterClassExW.restype = wt.ATOM
kernel32.GetModuleHandleW.restype = wt.HMODULE

ACTIONS = ["start", "stop", "pause", "capture", "toggle_overlay"]
ACTION_LABELS = {
    "start": "开始抽奖",
    "stop": "停止抽奖",
    "pause": "暂停 / 继续",
    "capture": "捕获模板（冻结画面框选）",
    "toggle_overlay": "显示/隐藏悬浮窗",
}


class _WNDCLASSEXW(ctypes.Structure):
    _fields_ = [
        ("cbSize", wt.UINT), ("style", wt.UINT), ("lpfnWndProc", WNDPROC),
        ("cbClsExtra", ctypes.c_int), ("cbWndExtra", ctypes.c_int),
        ("hInstance", wt.HINSTANCE), ("hIcon", wt.HICON), ("hCursor", wt.HANDLE),
        ("hbrBackground", wt.HBRUSH), ("lpszMenuName", wt.LPCWSTR),
        ("lpszClassName", wt.LPCWSTR), ("hIconSm", wt.HICON),
    ]


class HotkeyService(QObject):
    triggered = Signal(str)

    def __init__(self, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._thread: threading.Thread | None = None
        self._hwnd = 0
        self._ready = threading.Event()
        self._registered: dict[int, str] = {}
        self._pending: dict[str, str] = {}
        self._ok_actions: list[str] = []
        self._failed: list[str] = []
        self._class_atom = 0
        self._proc_ref = None
        self._last_error = ""
        self._ack = threading.Event()

    # ---------------- 生命周期 ----------------
    @property
    def last_error(self) -> str:
        return self._last_error

    def start(self) -> bool:
        if self._thread and self._thread.is_alive():
            return True
        self._ready.clear()
        self._thread = threading.Thread(target=self._message_loop, daemon=True,
                                        name="hotkey-service")
        self._thread.start()
        return self._ready.wait(5.0)

    def stop(self) -> None:
        hwnd = self._hwnd
        self._thread = None
        self._hwnd = 0
        if hwnd:
            try:
                user32.PostMessageW(wt.HWND(hwnd), WM_CLOSE, 0, 0)
            except Exception:
                pass

    # ---------------- 注册 ----------------
    def apply(self, mapping: dict[str, str]) -> tuple[list[str], list[str]]:
        """mapping: {"start": "F7", ...}；返回 (成功列表, 失败描述列表)。"""
        self._pending = {k: v for k, v in mapping.items() if k in ACTIONS}
        if not self.start():
            return [], ["热键服务启动失败"]
        # 在消息循环线程里注册（RegisterHotKey 必须与窗口同线程更稳）
        self._ack.clear()
        try:
            user32.PostMessageW(wt.HWND(self._hwnd), WM_APP_REAPPLY, 0, 0)
        except Exception as exc:
            return [], [f"热键注册请求失败：{exc}"]
        self._ack.wait(2.0)
        best = {action: spec for action, spec in self._pending.items()
                if action in self._ok_actions}
        self._last_error = "；".join(self._failed)
        return list(best.keys()), list(self._failed)

    def current(self) -> dict[str, str]:
        return {action: self._pending.get(action, "") for action in ACTIONS}

    # ---------------- 内部 ----------------
    def _message_loop(self) -> None:
        try:
            hinstance = kernel32.GetModuleHandleW(None)
        except Exception:
            hinstance = 0
        class_name = "FH6LotteryHotkeySink"

        def _proc(hwnd, msg, wparam, lparam):
            try:
                if msg == winutil.WM_HOTKEY:
                    action = self._registered.get(int(wparam))
                    if action:
                        self.triggered.emit(action)
                    return 0
                if msg == WM_APP_REAPPLY:
                    self._do_register()
                    self._ack.set()
                    return 0
                if msg == WM_CLOSE:
                    for ident in list(self._registered):
                        try:
                            user32.UnregisterHotKey(wt.HWND(hwnd), ident)
                        except Exception:
                            pass
                    self._registered.clear()
                    user32.DestroyWindow(hwnd)
                    return 0
                if msg == WM_DESTROY:
                    user32.PostQuitMessage(0)
                    return 0
            except Exception:
                pass
            return user32.DefWindowProcW(hwnd, msg, wparam, lparam)

        self._proc_ref = WNDPROC(_proc)
        wc = _WNDCLASSEXW()
        wc.cbSize = ctypes.sizeof(_WNDCLASSEXW)
        wc.style = 0
        wc.lpfnWndProc = self._proc_ref
        wc.hInstance = hinstance
        wc.lpszClassName = class_name
        atom = user32.RegisterClassExW(ctypes.byref(wc))
        self._class_atom = atom or 0
        hwnd = user32.CreateWindowExW(0, class_name, "fh6-lottery-hotkeys", 0,
                                      0, 0, 0, 0, wt.HWND(HWND_MESSAGE), None,
                                      hinstance, None)
        if not hwnd:
            self._last_error = "无法创建热键消息窗口"
            self._ready.set()
            return
        self._hwnd = int(hwnd)
        self._do_register()
        self._ready.set()

        msg = wt.MSG()
        while user32.GetMessageW(ctypes.byref(msg), None, 0, 0) > 0:
            user32.TranslateMessage(ctypes.byref(msg))
            user32.DispatchMessageW(ctypes.byref(msg))
        self._hwnd = 0

    def _do_register(self) -> None:
        hwnd = self._hwnd
        if not hwnd:
            return
        ok: list[str] = []
        failed: list[str] = []
        for index, action in enumerate(ACTIONS, start=1):
            spec = str(self._pending.get(action, "") or "")
            try:
                user32.UnregisterHotKey(wt.HWND(hwnd), index)
            except Exception:
                pass
            self._registered.pop(index, None)
            if not spec:
                continue
            if winutil.register_hotkey(hwnd, index, spec):
                self._registered[index] = action
                ok.append(action)
            else:
                failed.append(f"{spec}（{ACTION_LABELS.get(action, action)}）可能被别的程序占用")
        self._ok_actions = ok
        self._failed = failed

    def reapply(self) -> tuple[list[str], list[str]]:
        return self.apply(dict(self._pending))
