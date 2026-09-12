"""模板库：管理「界面截图小图」及其对应的语义角色。"""

from __future__ import annotations

import datetime as _dt
import json
import threading
import uuid
from dataclasses import dataclass, field
from typing import Any

import numpy as np

from . import paths

ROLE_LABELS: dict[str, str] = {
    "ready": "抽奖主界面（可开始抽奖）",
    "result_owned": "结果界面 · 已拥有（出现选项）",
    "result_new": "结果界面 · 新车",
    "sell": "出售价格界面",
    "confirm": "确认弹窗",
    "end": "抽奖结束 / 次数用尽",
    "blocked": "需要处理的阻挡界面",
    "unused": "未使用（暂不参与识别）",
}
ROLE_ORDER = list(ROLE_LABELS.keys())


def role_label(role: str) -> str:
    return ROLE_LABELS.get(role, role or "未指定")


def _ints_or_none(value: Any, length: int) -> list[int] | None:
    """把配置里的坐标序列规整成固定长度的非负整数列表，非法时返回 None。"""
    if value is None or isinstance(value, (str, bytes)):
        return None
    try:
        items = [int(v) for v in value]
    except (TypeError, ValueError):
        return None
    if len(items) != length or any(v < 0 for v in items):
        return None
    return items


@dataclass
class TemplateItem:
    id: str = field(default_factory=lambda: uuid.uuid4().hex[:12])
    name: str = ""
    role: str = "ready"
    file: str = ""
    threshold: float = 0.86
    created: str = ""
    width: int = 0
    height: int = 0
    region: list[int] | None = None      # 限定匹配区域（客户区相对坐标 x, y, w, h）
    ref_size: list[int] | None = None    # 采集时的客户区尺寸 [w, h]，用于跨分辨率自适应

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id, "name": self.name, "role": self.role, "file": self.file,
            "threshold": self.threshold, "created": self.created,
            "width": self.width, "height": self.height,
            "region": list(self.region) if self.region else None,
            "ref_size": list(self.ref_size) if self.ref_size else None,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "TemplateItem":
        return cls(
            id=str(data.get("id") or uuid.uuid4().hex[:12]),
            name=str(data.get("name") or "未命名"),
            role=str(data.get("role") or "ready"),
            file=str(data.get("file") or ""),
            threshold=float(data.get("threshold") or 0.86),
            created=str(data.get("created") or ""),
            width=int(data.get("width") or 0),
            height=int(data.get("height") or 0),
            region=_ints_or_none(data.get("region"), 4),
            ref_size=_ints_or_none(data.get("ref_size"), 2),
        )


class TemplateStore:
    def __init__(self) -> None:
        self.items: list[TemplateItem] = []
        self._gray_cache: dict[str, np.ndarray] = {}
        self._thumb_cache: dict[str, np.ndarray] = {}
        self._lock = threading.RLock()
        self.load()

    # ---------------- 持久化 ----------------
    def load(self) -> None:
        if not paths.TEMPLATE_INDEX.exists():
            self.items = []
            return
        try:
            raw = json.loads(paths.TEMPLATE_INDEX.read_text(encoding="utf-8"))
            items = raw.get("items") if isinstance(raw, dict) else raw
            self.items = [TemplateItem.from_dict(d) for d in (items or []) if isinstance(d, dict)]
        except (OSError, ValueError):
            self.items = []
        self._gray_cache.clear()
        self._thumb_cache.clear()

    def save(self) -> None:
        paths.DATA_DIR.mkdir(parents=True, exist_ok=True)
        payload = json.dumps({"version": 1, "items": [i.to_dict() for i in self.items]},
                             ensure_ascii=False, indent=2)
        tmp = paths.TEMPLATE_INDEX.with_suffix(".json.tmp")
        tmp.write_text(payload, encoding="utf-8")
        tmp.replace(paths.TEMPLATE_INDEX)

    # ---------------- 查询 ----------------
    def get(self, template_id: str) -> TemplateItem | None:
        for item in self.items:
            if item.id == template_id:
                return item
        return None

    def path_of(self, item: TemplateItem):
        return paths.TEMPLATE_DIR / (item.file or f"{item.id}.png")

    def by_role(self, role: str) -> list[TemplateItem]:
        return [i for i in self.items if i.role == role]

    def image(self, template_id: str) -> np.ndarray | None:
        item = self.get(template_id)
        if item is None:
            return None
        with self._lock:
            if template_id in self._gray_cache:
                return self._gray_cache[template_id]
        path = self.path_of(item)
        if not path.exists():
            return None
        try:
            import cv2
            img = cv2.imread(str(path), cv2.IMREAD_COLOR)
        except Exception:
            img = None
        if img is None:
            return None
        with self._lock:
            self._gray_cache[template_id] = img
        return img

    def gray(self, template_id: str) -> np.ndarray | None:
        """返回缓存的灰度图，供匹配使用。"""
        key = f"gray:{template_id}"
        with self._lock:
            if key in self._thumb_cache:
                return self._thumb_cache[key]
        img = self.image(template_id)
        if img is None:
            return None
        try:
            import cv2
            gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
        except Exception:
            return None
        with self._lock:
            self._thumb_cache[key] = gray
        return gray

    def thumbnail(self, template_id: str, max_side: int = 220) -> np.ndarray | None:
        key = f"thumb:{template_id}:{max_side}"
        with self._lock:
            if key in self._thumb_cache:
                return self._thumb_cache[key]
        img = self.image(template_id)
        if img is None:
            return None
        try:
            import cv2
            h, w = img.shape[:2]
            scale = min(1.0, max_side / max(1, max(h, w)))
            if scale < 1.0:
                img = cv2.resize(img, (max(1, int(w * scale)), max(1, int(h * scale))),
                                 interpolation=cv2.INTER_AREA)
        except Exception:
            return None
        with self._lock:
            self._thumb_cache[key] = img
        return img

    # ---------------- 增删改 ----------------
    def add(self, name: str, role: str, image: np.ndarray,
            threshold: float = 0.86, region=None, ref_size=None) -> TemplateItem:
        import cv2
        paths.TEMPLATE_DIR.mkdir(parents=True, exist_ok=True)
        item = TemplateItem(
            name=name or _dt.datetime.now().strftime("模板 %H%M%S"),
            role=role if role in ROLE_LABELS else "ready",
            threshold=float(threshold),
            created=_dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            height=int(image.shape[0]), width=int(image.shape[1]),
            region=_ints_or_none(region, 4),
            ref_size=_ints_or_none(ref_size, 2),
        )
        item.file = f"{item.id}.png"
        cv2.imwrite(str(paths.TEMPLATE_DIR / item.file), image)
        self.items.append(item)
        self.save()
        return item

    def add_from_file(self, path: str, name: str, role: str, threshold: float = 0.86,
                      region=None, ref_size=None) -> TemplateItem:
        import cv2
        img = cv2.imread(str(path), cv2.IMREAD_COLOR)
        if img is None:
            raise ValueError("无法读取该图片文件")
        return self.add(name, role, img, threshold, region=region, ref_size=ref_size)

    def replace_image(self, template_id: str, image: np.ndarray, ref_size=None) -> bool:
        import cv2
        item = self.get(template_id)
        if item is None or image is None or getattr(image, "size", 0) == 0:
            return False
        paths.TEMPLATE_DIR.mkdir(parents=True, exist_ok=True)
        if not item.file:
            item.file = f"{item.id}.png"
        cv2.imwrite(str(paths.TEMPLATE_DIR / item.file), image)
        item.width, item.height = int(image.shape[1]), int(image.shape[0])
        size = _ints_or_none(ref_size, 2)
        if size:
            item.ref_size = size
        with self._lock:
            self._gray_cache.pop(template_id, None)
            self._thumb_cache.clear()
        self.save()
        return True

    def update(self, template_id: str, **fields: Any) -> None:
        item = self.get(template_id)
        if item is None:
            return
        for key, value in fields.items():
            if hasattr(item, key):
                setattr(item, key, value)
        with self._lock:
            self._gray_cache.pop(template_id, None)
            self._thumb_cache.clear()
        self.save()

    def remove(self, template_id: str) -> None:
        item = self.get(template_id)
        if item is None:
            return
        self.items = [i for i in self.items if i.id != template_id]
        self._gray_cache.pop(template_id, None)
        try:
            self.path_of(item).unlink(missing_ok=True)
        except OSError:
            pass
        self.save()

    def prune_missing(self) -> int:
        """清理索引里图片已丢失的条目。"""
        removed = [i for i in self.items if not self.path_of(i).exists()]
        if removed:
            for item in removed:
                self._gray_cache.pop(item.id, None)
            self.items = [i for i in self.items if self.path_of(i).exists()]
            self.save()
        return len(removed)


_store: TemplateStore | None = None
_store_lock = threading.Lock()


def get_store() -> TemplateStore:
    global _store
    with _store_lock:
        if _store is None:
            _store = TemplateStore()
        return _store
