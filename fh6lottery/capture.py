"""屏幕捕获（mss，按线程隔离实例）。只截取游戏窗口的客户区。"""

from __future__ import annotations

import threading
from dataclasses import dataclass

import numpy as np

try:
    import mss
except ImportError as exc:  # pragma: no cover
    raise RuntimeError("缺少依赖 mss，请先执行 pip install -r requirements.txt") from exc

from . import winutil


@dataclass
class Frame:
    image: np.ndarray          # BGR
    rect: tuple[int, int, int, int]  # 屏幕物理像素 x, y, w, h
    origin: tuple[int, int]    # 客户区左上角屏幕坐标

    @property
    def size(self) -> tuple[int, int]:
        return self.image.shape[1], self.image.shape[0]

    def to_screen(self, x: int, y: int) -> tuple[int, int]:
        return self.origin[0] + x, self.origin[1] + y


_local = threading.local()


def _sct():
    inst = getattr(_local, "sct", None)
    if inst is None:
        inst = mss.mss()
        _local.sct = inst
    return inst


def grab_rect(x: int, y: int, w: int, h: int) -> np.ndarray | None:
    if w <= 0 or h <= 0:
        return None
    try:
        raw = _sct().grab({"left": int(x), "top": int(y), "width": int(w), "height": int(h)})
    except Exception:
        return None
    arr = np.asarray(raw, dtype=np.uint8)
    if arr.ndim != 3 or arr.shape[2] < 3:
        return None
    return np.ascontiguousarray(arr[:, :, :3])


def grab_window(hwnd: int) -> Frame | None:
    """截取指定窗口的客户区（不含标题栏与边框）。"""
    info = winutil.window_info(hwnd)
    if info is None or info.minimized:
        return None
    cx, cy, cw, ch = info.client
    img = grab_rect(cx, cy, cw, ch)
    if img is None:
        return None
    return Frame(image=img, rect=(cx, cy, cw, ch), origin=(cx, cy))


def grab_window_rect(hwnd: int) -> Frame | None:
    """截取整个窗口矩形（包含标题栏），在客户区不可用时兜底。"""
    info = winutil.window_info(hwnd)
    if info is None:
        return None
    x, y, w, h = info.rect
    img = grab_rect(x, y, w, h)
    if img is None:
        return None
    return Frame(image=img, rect=(x, y, w, h), origin=(x, y))


def grab_region(hwnd: int, region: tuple[int, int, int, int] | None) -> np.ndarray | None:
    """在游戏客户区内按下区域坐标取图。region 用「客户区局部坐标」，None 表示整张。"""
    frame = grab_window(hwnd)
    if frame is None:
        return None
    if not region:
        return frame.image
    x, y, w, h = (int(v) for v in region)
    fh, fw = frame.image.shape[:2]
    x0 = max(0, min(fw - 1, x))
    y0 = max(0, min(fh - 1, y))
    x1 = max(x0 + 1, min(fw, x + max(1, w)))
    y1 = max(y0 + 1, min(fh, y + max(1, h)))
    return np.ascontiguousarray(frame.image[y0:y1, x0:x1])


def grab_virtual_desktop() -> tuple[np.ndarray | None, tuple[int, int, int, int]]:
    """整块虚拟桌面（多屏时用于「冻结画面框选」）。"""
    x, y, w, h = winutil.virtual_screen_rect()
    return grab_rect(x, y, w, h), (x, y, w, h)


def close_all() -> None:
    inst = getattr(_local, "sct", None)
    if inst is not None:
        try:
            inst.close()
        except Exception:
            pass
        _local.sct = None
