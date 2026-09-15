"""识别页：识别方式开关、OCR 关键词规则、实时调试画面。"""

from __future__ import annotations

import time

import numpy as np
from PySide6.QtCore import Qt
from PySide6.QtGui import QColor, QFont, QImage, QPainter, QPen, QPixmap
from PySide6.QtWidgets import (QComboBox, QDoubleSpinBox, QFrame, QHBoxLayout,
                               QLabel, QLineEdit, QListWidget, QPushButton,
                               QSpinBox, QTextEdit, QVBoxLayout, QWidget)

from .. import capture, matcher, ocr as ocr_mod, winutil
from ..templates import ROLE_ORDER, role_label
from . import theme
from .page_base import PageBase
from .widgets import Card, ImageView, hint, hline, muted, numpy_to_qimage, row, with_unit


def _spin(value: int, minimum: int, maximum: int, step: int) -> QSpinBox:
    """数值输入框。单位由调用方用 ``with_unit()`` 加在框外。"""
    box = QSpinBox()
    box.setRange(minimum, maximum)
    box.setSingleStep(step)
    box.setValue(int(value))
    box.setFixedWidth(120)
    return box


class DebugView(QLabel):
    """显示调试画面，并标出模板命中的位置。"""

    def __init__(self, parent: QWidget | None = None):
        super().__init__(parent)
        self._image = QImage()
        self._box = None
        self.setMinimumHeight(240)
        self.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.setStyleSheet(f"background:{theme.BG_DEEP}; border:1px solid {theme.BORDER};"
                           "border-radius:8px;")
        self.setText("点击「立即截图并识别」看效果")

    def set_debug(self, image: QImage, box=None) -> None:
        self._image = image
        self._box = box
        if image.isNull():
            self.clear()
            self.setText("截图失败")
        self._render()

    def _render(self) -> None:
        if self._image.isNull():
            return
        area = self.contentsRect().adjusted(4, 4, -4, -4)
        scaled = self._image.scaled(area.size(), Qt.AspectRatioMode.KeepAspectRatio,
                                    Qt.TransformationMode.SmoothTransformation)
        pixmap = QPixmap.fromImage(scaled)
        if self._box:
            x, y, w, h = self._box
            ratio = scaled.width() / max(1, self._image.width())
            painter = QPainter(pixmap)
            painter.setPen(QPen(QColor(theme.ACCENT), 2))
            painter.drawRect(int(x * ratio), int(y * ratio), int(w * ratio), int(h * ratio))
            painter.end()
        self.setPixmap(pixmap)

    def resizeEvent(self, event):  # noqa: N802
        super().resizeEvent(event)
        self._render()


class RecogPage(PageBase):
    title = "识别与调试"
    description = ("识别不灵的时候先来这里：点「立即截图并识别」看助手看到了什么，"
                   "再用「测试 OCR」确认文字识别有没有工作。")

    def __init__(self, ctx, parent: QWidget | None = None):
        super().__init__(ctx, parent=parent)
        self._build_switches()
        self._build_rules()
        self._build_debug()
        ctx.recognitionUpdated.connect(self._on_recognition)
        ctx.configChanged.connect(self.refresh)

    # ------------------------------------------------------------------ #
    def _build_switches(self) -> None:
        card = Card("识别方式")
        self.add(card)
        self.template_switch = QPushButton("模板匹配：开")
        self.template_switch.setCheckable(True)
        self.template_switch.clicked.connect(self._toggle_templates)
        self.ocr_switch = QPushButton("OCR 关键词：开")
        self.ocr_switch.setCheckable(True)
        self.ocr_switch.clicked.connect(self._toggle_ocr)
        self.scale_switch = QPushButton("多尺度匹配：关")
        self.scale_switch.setCheckable(True)
        self.scale_switch.clicked.connect(self._toggle_scale)
        card.add(row(self.template_switch, self.ocr_switch, self.scale_switch, None))

        self.poll_spin = _spin(int(self.ctx.config.get("smart.poll_interval_ms", 220)),
                               60, 2000, 20)
        self.poll_spin.valueChanged.connect(
            lambda value: self.ctx.set("smart.poll_interval_ms", int(value)))
        self.ocr_interval_spin = _spin(int(self.ctx.config.get("smart.ocr_interval_ms", 900)),
                                       200, 5000, 100)
        self.ocr_interval_spin.valueChanged.connect(
            lambda value: self.ctx.set("smart.ocr_interval_ms", int(value)))
        card.add(row(QLabel("截图间隔"), with_unit(self.poll_spin, "ms"),
                     QLabel("OCR 间隔"), with_unit(self.ocr_interval_spin, "ms"), None))
        card.add(muted("截图间隔越小反应越快、越吃 CPU；OCR 比较慢，间隔别低于 0.6 秒。"))
        self.ocr_status = hint("")
        card.add(self.ocr_status)

    def _build_rules(self) -> None:
        card = Card("OCR 关键词规则",
                    "不想录模板时用这个：在画面上框一小块区域，识别出来的文字里出现关键词，"
                    "就认为当前是这个界面。")
        self.add(card)
        self.rules_holder = QWidget()
        self.rules_layout = QVBoxLayout(self.rules_holder)
        self.rules_layout.setContentsMargins(0, 0, 0, 0)
        self.rules_layout.setSpacing(6)
        card.add(self.rules_holder)
        add_button = QPushButton("+ 添加规则")
        add_button.clicked.connect(self._add_rule)
        card.add(add_button)

    def _build_debug(self) -> None:
        card = Card("实时调试")
        self.add(card)
        self.preview = DebugView()
        card.add(self.preview)
        buttons = QHBoxLayout()
        buttons.setSpacing(8)
        snap = QPushButton("立即截图并识别")
        snap.setProperty("variant", "primary")
        snap.clicked.connect(self._snapshot)
        ocr_test = QPushButton("测试 OCR（全屏）")
        ocr_test.clicked.connect(self._test_ocr)
        peek = QPushButton("只看截图")
        peek.clicked.connect(self._just_capture)
        buttons.addWidget(snap)
        buttons.addWidget(ocr_test)
        buttons.addWidget(peek)
        buttons.addStretch(1)
        card.add(buttons)

        self.info_label = QLabel("等待识别…")
        self.info_label.setWordWrap(True)
        card.add(self.info_label)

        card.add(hline())
        card.add(muted("识别到的文字（最近一次）："))
        self.text_box = QTextEdit()
        self.text_box.setReadOnly(True)
        self.text_box.setFixedHeight(90)
        card.add(self.text_box)

        self.hits_box = QListWidget()
        self.hits_box.setFixedHeight(110)
        card.add(self.hits_box)

    # ------------------------------------------------------------------ #
    def refresh(self) -> None:
        for button, path, on_text, off_text in (
                (self.template_switch, "smart.use_templates", "模板匹配：开", "模板匹配：关"),
                (self.ocr_switch, "smart.use_ocr", "OCR 关键词：开", "OCR 关键词：关"),
                (self.scale_switch, "smart.multi_scale", "多尺度匹配：开", "多尺度匹配：关")):
            button.blockSignals(True)
            button.setChecked(bool(self.ctx.config.get(path, True)))
            button.blockSignals(False)
            button.setText(on_text if button.isChecked() else off_text)
        self.scale_switch.setText("多尺度匹配：开" if self.ctx.config.get("smart.multi_scale")
                                 else "多尺度匹配：关")
        self.ocr_status.setText("OCR 状态：" + self.ctx.ocr_status())
        self._rebuild_rules()

    def _rebuild_rules(self) -> None:
        while self.rules_layout.count():
            item = self.rules_layout.takeAt(0)
            widget = item.widget()
            if widget is not None:
                widget.deleteLater()
        for index, rule in enumerate(self.ctx.config.get("smart.keyword_rules", []) or []):
            self.rules_layout.addWidget(self._rule_row(index, rule))

    def _rule_row(self, index: int, rule: dict) -> QWidget:
        holder = QFrame()
        holder.setObjectName("SubCard")
        layout = QVBoxLayout(holder)
        layout.setContentsMargins(10, 8, 10, 8)
        layout.setSpacing(6)

        top = QHBoxLayout()
        top.setSpacing(7)
        enable = QPushButton("启用" if rule.get("enabled") else "停用")
        enable.setCheckable(True)
        enable.setChecked(bool(rule.get("enabled")))
        enable.setFixedWidth(64)
        enable.clicked.connect(lambda _checked=False, i=index: self._toggle_rule(i))
        top.addWidget(enable)
        role = QComboBox()
        for key in ROLE_ORDER:
            role.addItem(role_label(key), key)
        role.setCurrentIndex(max(0, role.findData(str(rule.get("role", "blocked")))))
        role.currentIndexChanged.connect(
            lambda _i, idx=index, combo=role: self._set_rule(idx, role=combo.currentData()))
        top.addWidget(role)
        top.addWidget(QLabel("关键词"))
        keywords = QLineEdit("、".join(rule.get("keywords") or []))
        keywords.setPlaceholderText("用「、」或逗号分隔，如：已拥有、重复")
        keywords.editingFinished.connect(
            lambda idx=index, w=keywords: self._set_keywords(idx, w.text()))
        top.addWidget(keywords, 1)
        layout.addLayout(top)

        bottom = QHBoxLayout()
        bottom.setSpacing(7)
        lang = QComboBox()
        for code in ("zh-Hans-CN", "en-US"):
            lang.addItem("中文界面" if code.startswith("zh") else "英文界面", code)
        current_lang = str(rule.get("lang") or "zh-Hans-CN")
        if lang.findData(current_lang) < 0:
            lang.addItem(current_lang, current_lang)
        lang.setCurrentIndex(max(0, lang.findData(current_lang)))
        lang.currentIndexChanged.connect(
            lambda _i, idx=index, combo=lang: self._set_rule(idx, lang=combo.currentData()))
        bottom.addWidget(QLabel("语言"))
        bottom.addWidget(lang)

        scale = QDoubleSpinBox()
        scale.setRange(0.5, 3.0)
        scale.setSingleStep(0.1)
        scale.setValue(float(rule.get("scale") or 1.5))
        scale.setFixedWidth(84)
        scale.valueChanged.connect(lambda value, idx=index: self._set_rule(idx, scale=float(value)))
        bottom.addWidget(QLabel("放大"))
        bottom.addWidget(scale)

        region_text = QLabel(self._region_text(rule.get("region")))
        region_text.setObjectName("Hint")
        pick = QPushButton("框选区域")
        pick.clicked.connect(lambda _checked=False, idx=index: self._pick_region(idx))
        clear = QPushButton("整屏")
        clear.setProperty("variant", "ghost")
        clear.clicked.connect(lambda _checked=False, idx=index: self._set_rule(idx, region=None))
        test = QPushButton("测试这条")
        test.clicked.connect(lambda _checked=False, idx=index: self._test_rule(idx))
        remove = QPushButton("删除")
        remove.setProperty("variant", "danger")
        remove.clicked.connect(lambda _checked=False, idx=index: self._remove_rule(idx))
        bottom.addWidget(pick)
        bottom.addWidget(clear)
        bottom.addWidget(region_text, 1)
        bottom.addWidget(test)
        bottom.addWidget(remove)
        layout.addLayout(bottom)
        return holder

    @staticmethod
    def _region_text(region) -> str:
        if not region:
            return "范围：整张画面"
        try:
            x, y, w, h = (int(v) for v in region)
            return f"范围：({x}, {y}) {w}×{h}"
        except (TypeError, ValueError):
            return "范围：整张画面"

    # ------------------------------------------------------------------ #
    def _rules(self) -> list:
        return list(self.ctx.config.get("smart.keyword_rules", []) or [])

    def _save_rules(self, rules: list) -> None:
        self.ctx.set("smart.keyword_rules", rules)
        self._rebuild_rules()

    def _patch_rule(self, index: int, **fields) -> None:
        rules = self._rules()
        if not (0 <= index < len(rules)):
            return
        rules[index] = {**rules[index], **fields}
        self._save_rules(rules)

    def _set_rule(self, index: int, **fields) -> None:
        self._patch_rule(index, **fields)

    def _set_keywords(self, index: int, text: str) -> None:
        parts = [p.strip() for p in text.replace("，", "、").replace(",", "、").split("、")]
        self._patch_rule(index, keywords=[p for p in parts if p])

    def _toggle_rule(self, index: int) -> None:
        rules = self._rules()
        if not (0 <= index < len(rules)):
            return
        self._patch_rule(index, enabled=not rules[index].get("enabled"))

    def _add_rule(self) -> None:
        rules = self._rules()
        rules.append({"id": f"rule{len(rules) + 1}", "role": "blocked", "enabled": True,
                      "keywords": ["确定"], "region": None, "lang": "zh-Hans-CN", "scale": 1.5})
        self._save_rules(rules)

    def _remove_rule(self, index: int) -> None:
        rules = self._rules()
        if 0 <= index < len(rules):
            del rules[index]
        self._save_rules(rules)

    def _pick_region(self, index: int) -> None:
        region, _shot = self.ctx.capture_region("框选要识别的文字区域")
        if region:
            self._patch_rule(index, region=list(region))

    def _test_rule(self, index: int) -> None:
        rules = self._rules()
        if not (0 <= index < len(rules)):
            return
        rule = rules[index]
        hwnd = self._ensure_window()
        if not hwnd:
            return
        frame = capture.grab_window(hwnd)
        if frame is None:
            self.ctx.log("warn", "截图失败")
            return
        crop = self._crop(frame.image, rule.get("region"))
        started = time.perf_counter()
        text = ocr_mod.get_ocr().recognize(crop, str(rule.get("lang") or "zh-Hans-CN"),
                                           float(rule.get("scale") or 1.5))
        cost = (time.perf_counter() - started) * 1000
        hit = ocr_mod.contains_any(text, rule.get("keywords") or [])
        self.text_box.setPlainText((text or "（没有识别到文字）").strip())
        level = "info" if hit else "warn"
        self.ctx.log(level, f"规则测试：{'命中 ' + hit if hit else '未命中'}，耗时 {cost:.0f} ms，"
                            f"文字：{(text or '').strip()[:60]!r}")

    # ------------------------------------------------------------------ #
    def _ensure_window(self) -> int:
        hwnd = int(self.ctx.config.get("window.hwnd", 0) or 0)
        if hwnd and winutil.is_window_alive(hwnd):
            return hwnd
        if self.ctx.sync_window():
            return int(self.ctx.config.get("window.hwnd", 0) or 0)
        self.ctx.log("warn", "还没选游戏窗口，请到「概览」或「设置」页选一个")
        return 0

    @staticmethod
    def _crop(image: np.ndarray, region) -> np.ndarray:
        if not region:
            return image
        try:
            x, y, w, h = (int(v) for v in region)
        except (TypeError, ValueError):
            return image
        fh, fw = image.shape[:2]
        x0, y0 = max(0, min(fw - 1, x)), max(0, min(fh - 1, y))
        x1, y1 = max(x0 + 1, min(fw, x + max(1, w))), max(y0 + 1, min(fh, y + max(1, h)))
        return np.ascontiguousarray(image[y0:y1, x0:x1])

    def _snapshot(self) -> None:
        """就地做一次完整识别（模板 + 关键词），把过程显示出来。"""
        hwnd = self._ensure_window()
        if not hwnd:
            return
        self.hide()
        try:
            frame = capture.grab_window(hwnd)
        finally:
            self.show()
        if frame is None:
            self.ctx.log("warn", "截图失败：窗口可能被最小化")
            return
        hits = []
        entries = []
        for role in ROLE_ORDER:
            for item in self.ctx.store.by_role(role):
                gray = self.ctx.store.gray(item.id)
                if gray is not None:
                    entry = type("E", (), {})()
                    entry.id, entry.name, entry.role = item.id, item.name, item.role
                    entry.threshold, entry.gray = item.threshold, gray
                    entries.append(entry)
        best = None
        if entries and self.ctx.config.get("smart.use_templates", True):
            scales = (0.95, 1.0, 1.05) if self.ctx.config.get("smart.multi_scale") else (1.0,)
            all_hits = matcher.match_all(frame.image, entries, scales)
            hits = all_hits
            if all_hits:
                best = all_hits[0]
        box = best.box if best else None
        self.preview.set_debug(numpy_to_qimage(frame.image), box)

        self.hits_box.clear()
        for hit in hits:
            self.hits_box.addItem(
                f"{hit.name}　[{role_label(hit.role)}]　相似度 {hit.score:.3f}　位置 {hit.box}")
        text = ""
        if self.ctx.config.get("smart.use_ocr", True):
            for rule in self.ctx.config.get("smart.keyword_rules", []) or []:
                if not rule.get("enabled"):
                    continue
                crop = self._crop(frame.image, rule.get("region"))
                got = ocr_mod.get_ocr().recognize(crop, str(rule.get("lang") or "zh-Hans-CN"),
                                                  float(rule.get("scale") or 1.5))
                if got:
                    text += f"[{rule.get('id')}] {got.strip()}\n"
        self.text_box.setPlainText(text or "（关键词规则没有开启，或没有识别到文字）")

        if best:
            self.info_label.setText(
                f"判定：{role_label(best.role)}　·　来源模板「{best.name}」　·　"
                f"相似度 {best.score:.3f}　·　画面 {frame.size[0]}×{frame.size[1]}")
        else:
            self.info_label.setText("没有命中任何模板。可以调低阈值、重录模板，"
                                    "或改用 OCR 关键词规则。")

    def _just_capture(self) -> None:
        hwnd = self._ensure_window()
        if not hwnd:
            return
        self.hide()
        try:
            frame = capture.grab_window(hwnd)
        finally:
            self.show()
        if frame is None:
            self.ctx.log("warn", "截图失败")
            return
        self.preview.set_debug(numpy_to_qimage(frame.image), None)
        self.info_label.setText(f"画面 {frame.size[0]}×{frame.size[1]}，客户区原点 {frame.origin}")

    def _test_ocr(self) -> None:
        hwnd = self._ensure_window()
        if not hwnd:
            return
        self.hide()
        try:
            frame = capture.grab_window(hwnd)
        finally:
            self.show()
        if frame is None:
            self.ctx.log("warn", "截图失败")
            return
        client = ocr_mod.get_ocr()
        if not client.ready and not client.start(wait=True):
            self.ctx.log("error", f"OCR 启动失败：{client.error}")
            return
        started = time.perf_counter()
        text = client.recognize(frame.image, "zh-Hans-CN", 1.0)
        cost = (time.perf_counter() - started) * 1000
        self.text_box.setPlainText((text or "（没有识别到文字）").strip())
        self.ctx.log("info", f"全屏 OCR 完成，耗时 {cost:.0f} ms，"
                             f"识别到 {len((text or '').strip())} 个字符")

    # ------------------------------------------------------------------ #
    def _toggle_templates(self) -> None:
        self.ctx.set("smart.use_templates", bool(self.template_switch.isChecked()))
        self.refresh()

    def _toggle_ocr(self) -> None:
        self.ctx.set("smart.use_ocr", bool(self.ocr_switch.isChecked()))
        self.refresh()

    def _toggle_scale(self) -> None:
        self.ctx.set("smart.multi_scale", bool(self.scale_switch.isChecked()))
        self.refresh()

    def _on_recognition(self, info) -> None:
        role = role_label(info.role) if info.role in ROLE_ORDER else info.role
        score_text = f"　相似度 {info.score:.3f}" if info.score else ""
        self.info_label.setText(
            f"当前判定：{role}　·　{info.source or '未命中'}{score_text}　·　"
            f"画面 {info.frame_size[0]}×{info.frame_size[1]}")
        if info.frame is not None:
            box = None
            if info.box and info.frame_size[0]:
                ratio = info.frame.shape[1] / float(info.frame_size[0])
                box = tuple(int(v * ratio) for v in info.box)
            self.preview.set_debug(numpy_to_qimage(info.frame), box)
        if info.texts:
            self.text_box.setPlainText(
                "\n".join(f"[{k}] {v}" for k, v in info.texts.items()))
        if info.hits:
            self.hits_box.clear()
            for name, role_key, score, box in info.hits[:8]:
                self.hits_box.addItem(
                    f"{name}　[{role_label(role_key)}]　相似度 {score:.3f}　位置 {box}")
