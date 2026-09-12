"""自检：验证底层能力是否在你的机器上正常工作。

用法：python scripts/selftest.py
不会发送任何真实按键，安全。
"""

from __future__ import annotations

import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import numpy as np  # noqa: E402

RESULTS: list[tuple[str, bool, str]] = []


def check(name: str, fn) -> None:
    started = time.perf_counter()
    try:
        detail = fn() or ""
        RESULTS.append((name, True, f"{detail}（{time.perf_counter() - started:.2f}s）"))
    except Exception as exc:  # noqa: BLE001
        RESULTS.append((name, False, f"{type(exc).__name__}: {exc}"))


def test_paths() -> str:
    from fh6lottery import paths
    assert paths.DATA_DIR.exists(), "数据目录创建失败"
    assert "%" in paths.display_path(paths.DATA_DIR), "路径折叠没有生效"
    return paths.display_path(paths.DATA_DIR)


def test_config() -> str:
    from fh6lottery.config import Config
    cfg = Config()
    assert cfg.get("smart.owned_policy") == "garage"
    cfg.set("smart.owned_policy", "sell")
    assert cfg.get("smart.owned_policy") == "sell"
    cloned = cfg.clone()
    cloned.set("smart.owned_policy", "gift")
    assert cfg.get("smart.owned_policy") == "sell", "clone 不是深拷贝"
    return "配置读写与深拷贝正常"


def test_winutil() -> str:
    from fh6lottery import winutil
    vk, scan, ext = winutil.normalize_key("enter")
    assert (vk, scan) == (0x0D, 0x1C)
    assert winutil.hotkey_spec("F7") is not None
    assert winutil.hotkey_spec("Ctrl+F8") is not None
    assert winutil.hotkey_spec("nonsense") is None
    windows = winutil.list_windows()
    x, y, w, h = winutil.virtual_screen_rect()
    assert w > 0 and h > 0
    return f"按键映射正常；检测到 {len(windows)} 个可见窗口；桌面 {w}×{h}"


def test_capture() -> str:
    from fh6lottery import capture
    image, rect = capture.grab_virtual_desktop()
    assert image is not None, "截图失败"
    assert image.ndim == 3 and image.shape[2] == 3
    return f"截屏 {image.shape[1]}×{image.shape[0]}（BGR）"


def test_matcher() -> str:
    import cv2
    from fh6lottery import matcher
    frame = np.zeros((240, 320, 3), np.uint8)
    frame[80:120, 100:200] = (30, 200, 90)
    cv2.rectangle(frame, (100, 80), (199, 119), (255, 255, 255), 2)
    template = frame[80:120, 100:200].copy()
    gray = matcher._gray(frame)
    found = matcher.match_one(gray, matcher._gray(template), (1.0,))
    assert found is not None
    score, box = found
    assert score > 0.98, f"相似度偏低：{score}"
    assert abs(box[0] - 100) <= 2 and abs(box[1] - 80) <= 2, f"命中位置不准：{box}"
    assert matcher.signature(frame)
    return f"合成图相似度 {score:.3f}，位置 {box}"


def test_templates() -> str:
    from fh6lottery.templates import get_store
    import cv2
    store = get_store()
    before = len(store.items)
    image = np.zeros((40, 120, 3), np.uint8)
    cv2.putText(image, "TEST", (6, 30), cv2.FONT_HERSHEY_SIMPLEX, 1.0, (255, 255, 255), 2)
    item = store.add("自检临时模板", "unused", image, 0.9)
    try:
        assert store.gray(item.id) is not None, "灰度缓存读取失败"
        assert store.thumbnail(item.id) is not None, "缩略图生成失败"
        store.update(item.id, name="自检改名")
        assert store.get(item.id).name == "自检改名"
    finally:
        store.remove(item.id)
    assert len(store.items) == before, "删除模板后数量不对"
    return f"模板增删改查正常（当前 {len(store.items)} 个）"


def test_ocr() -> str:
    import cv2
    from fh6lottery import ocr as ocr_mod
    client = ocr_mod.get_ocr()
    if not client.start(wait=True):
        raise RuntimeError(f"OCR 启动失败：{client.error}")

    canvas = np.full((120, 520, 3), 255, np.uint8)
    cv2.putText(canvas, "12,345", (20, 80), cv2.FONT_HERSHEY_SIMPLEX, 2.2, (0, 0, 0), 4)
    text = client.recognize(canvas, "en-US", 1.5)

    t0 = time.perf_counter()
    canvas2 = np.full((140, 620, 3), 255, np.uint8)
    cv2.putText(canvas2, "TEST 88", (20, 90), cv2.FONT_HERSHEY_SIMPLEX, 2.0, (0, 0, 0), 4)
    text2 = client.recognize(canvas2, "en-US", 1.5)
    cost = (time.perf_counter() - t0) * 1000

    assert text is not None, "OCR 返回空（引擎可能未就绪）"
    digits = ocr_mod.first_int(text, r"[\d][\d,\.]*")
    assert digits == 12345, f"数字识别不对：{text!r} → {digits}"
    assert ocr_mod.first_int("剩余 7 次", r"\d+") == 7
    assert ocr_mod.contains_any(text2, ["TEST"]) is not None, f"关键词识别失败：{text2!r}"
    return (f"语言 {client.languages}；识别 {text!r} / {text2!r}；"
            f"单次约 {cost:.0f} ms")


def test_engine() -> str:
    from fh6lottery.config import Config
    from fh6lottery.engine import LotteryEngine
    from fh6lottery.templates import get_store
    engine = LotteryEngine(Config(), get_store())
    summary = engine.summary()
    assert "抽奖次数" in summary
    assert not engine.running
    return "引擎可正常构造，统计字段齐全"


def test_hotkeys() -> str:
    from fh6lottery import winutil
    parsed = winutil.hotkey_spec("F7")
    assert parsed is not None
    mods, vk = parsed
    assert vk == 0x76, f"F7 虚拟键不对：{vk:#x}"
    return f"F7 → mods={mods:#x}, vk={vk:#x}"


def main() -> int:
    check("路径与隐私占位符", test_paths)
    check("配置读写", test_config)
    check("Win32 窗口与按键映射", test_winutil)
    check("屏幕捕获", test_capture)
    check("模板匹配", test_matcher)
    check("模板库读写", test_templates)
    check("全局热键解析", test_hotkeys)
    check("Windows OCR", test_ocr)
    check("引擎构造", test_engine)

    print("=" * 72)
    failed = 0
    for name, ok, detail in RESULTS:
        flag = "PASS" if ok else "FAIL"
        if not ok:
            failed += 1
        print(f"[{flag}] {name}\n        {detail}")
    print("=" * 72)
    print(f"共 {len(RESULTS)} 项，失败 {failed} 项")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
