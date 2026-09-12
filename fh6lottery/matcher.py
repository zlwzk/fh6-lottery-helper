"""模板匹配（OpenCV 归一化互相关）。"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np


@dataclass
class MatchHit:
    template_id: str
    name: str
    role: str
    score: float
    box: tuple[int, int, int, int]  # 在帧内的 x, y, w, h


def _gray(image: np.ndarray) -> np.ndarray:
    import cv2
    if image.ndim == 2:
        return image
    if image.shape[2] == 4:
        image = cv2.cvtColor(image, cv2.COLOR_BGRA2BGR)
    return cv2.cvtColor(image, cv2.COLOR_BGR2GRAY)


def match_one(frame_gray: np.ndarray, template_gray: np.ndarray,
              scales: tuple[float, ...] = (1.0,)) -> tuple[float, tuple[int, int, int, int]] | None:
    """返回 (最高相似度, 命中框)。"""
    import cv2
    fh, fw = frame_gray.shape[:2]
    th0, tw0 = template_gray.shape[:2]
    if th0 < 6 or tw0 < 6 or th0 > fh or tw0 > fw:
        return None
    best_score = -1.0
    best_box = (0, 0, tw0, th0)
    for scale in scales:
        if abs(scale - 1.0) < 1e-3:
            tpl = template_gray
        else:
            tw, th = max(6, int(tw0 * scale)), max(6, int(th0 * scale))
            if tw > fw or th > fh:
                continue
            interp = cv2.INTER_AREA if scale < 1.0 else cv2.INTER_CUBIC
            tpl = cv2.resize(template_gray, (tw, th), interpolation=interp)
        try:
            result = cv2.matchTemplate(frame_gray, tpl, cv2.TM_CCOEFF_NORMED)
        except cv2.error:
            continue
        _min_v, max_v, _min_l, max_l = cv2.minMaxLoc(result)
        if max_v > best_score:
            best_score = float(max_v)
            best_box = (int(max_l[0]), int(max_l[1]), tpl.shape[1], tpl.shape[0])
    if best_score < 0:
        return None
    return best_score, best_box


def match_all(frame: np.ndarray, entries, scales: tuple[float, ...] = (1.0,)) -> list[MatchHit]:
    """entries 需提供 .id/.name/.role/.threshold/.gray 属性。"""
    frame_gray = _gray(frame)
    hits: list[MatchHit] = []
    for entry in entries:
        tpl = getattr(entry, "gray", None)
        if tpl is None or getattr(tpl, "size", 0) == 0:
            continue
        found = match_one(frame_gray, tpl, scales)
        if found is None:
            continue
        score, box = found
        if score < float(getattr(entry, "threshold", 0.86)):
            continue
        hits.append(MatchHit(
            template_id=str(getattr(entry, "id", "")),
            name=str(getattr(entry, "name", "")),
            role=str(getattr(entry, "role", "")),
            score=score, box=box,
        ))
    hits.sort(key=lambda h: h.score, reverse=True)
    return hits


def best_by_role(frame: np.ndarray, entries, roles_priority: list[str],
                 scales: tuple[float, ...] = (1.0,)) -> MatchHit | None:
    """roles_priority 靠前的角色优先；同角色取分最高。"""
    hits = match_all(frame, entries, scales)
    if not hits:
        return None
    order = {role: idx for idx, role in enumerate(roles_priority)}
    hits.sort(key=lambda h: (order.get(h.role, len(roles_priority)), -h.score))
    return hits[0]


def signature(frame: np.ndarray, size: int = 16) -> str:
    """给帧生成一个粗粒度指纹，用于判断"画面是否没变"。"""
    import cv2
    try:
        small = cv2.resize(_gray(frame), (size, size), interpolation=cv2.INTER_AREA)
    except cv2.error:
        return ""
    grid = (small // 16).astype(np.uint8)
    return grid.tobytes().hex()
