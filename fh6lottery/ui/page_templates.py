"""模板库页：采集、导入、改名、分配角色、调阈值、测试匹配。"""

from __future__ import annotations

import numpy as np
from PySide6.QtCore import Qt
from PySide6.QtGui import QGuiApplication, QImage, QPixmap
from PySide6.QtWidgets import (QComboBox, QDoubleSpinBox, QFileDialog, QFrame,
                               QHBoxLayout, QLabel, QLineEdit, QPushButton,
                               QVBoxLayout, QWidget)

from .. import capture, matcher, winutil
from ..templates import ROLE_ORDER, role_label
from . import theme
from .page_base import PageBase
from .widgets import Card, hint, hline, muted, numpy_to_qimage, row


def qimage_to_numpy(qimage: QImage) -> np.ndarray | None:
    if qimage.isNull():
        return None
    import cv2
    img = qimage.convertToFormat(QImage.Format.Format_RGB888)
    width, height = img.width(), img.height()
    ptr = img.constBits()
    arr = np.frombuffer(ptr, dtype=np.uint8).reshape(height, img.bytesPerLine())
    arr = arr[:, : width * 3].reshape(height, width, 3)
    return cv2.cvtColor(np.ascontiguousarray(arr), cv2.COLOR_RGB2BGR)


class TemplatesPage(PageBase):
    title = "模板库"
    description = ("模板就是游戏界面上一小块截图。助手拿它跟游戏画面比对，相似度超过阈值就认为"
                   "「现在就是这个界面」。框选时只框最关键的一小块文字/按钮，越大越容易误判。")

    def __init__(self, ctx, parent: QWidget | None = None):
        super().__init__(ctx, parent=parent)
        self._build_toolbar()
        self.rows_holder = QWidget()
        self.rows_layout = QVBoxLayout(self.rows_holder)
        self.rows_layout.setContentsMargins(0, 0, 0, 0)
        self.rows_layout.setSpacing(8)
        self.add(self.rows_holder)
        ctx.templatesChanged.connect(self.refresh)

    # ------------------------------------------------------------------ #
    def _build_toolbar(self) -> None:
        card = Card("采集与导入")
        self.add(card)
        line = QHBoxLayout()
        line.setSpacing(8)
        line.addWidget(QLabel("捕获角色"))
        self.role_combo = QComboBox()
        for role in ROLE_ORDER:
            self.role_combo.addItem(role_label(role), role)
        line.addWidget(self.role_combo)
        capture_button = QPushButton("捕获模板（F9）")
        capture_button.setProperty("variant", "primary")
        capture_button.clicked.connect(self._capture)
        line.addWidget(capture_button)
        line.addStretch(1)
        paste_button = QPushButton("从剪贴板导入")
        paste_button.clicked.connect(self._from_clipboard)
        file_button = QPushButton("从文件导入")
        file_button.clicked.connect(self._from_file)
        line.addWidget(paste_button)
        line.addWidget(file_button)
        card.add(line)

        threshold_row = QHBoxLayout()
        self.threshold_spin = QDoubleSpinBox()
        self.threshold_spin.setRange(0.40, 0.999)
        self.threshold_spin.setSingleStep(0.01)
        self.threshold_spin.setDecimals(3)
        self.threshold_spin.setValue(float(self.ctx.config.get("smart.match_threshold", 0.86)))
        self.threshold_spin.setFixedWidth(110)
        self.threshold_spin.valueChanged.connect(
            lambda value: self.ctx.set("smart.match_threshold", float(value)))
        threshold_row.addWidget(QLabel("新模板默认阈值"))
        threshold_row.addWidget(self.threshold_spin)
        threshold_row.addWidget(hint("0.86 起步；识别不到就降到 0.80～0.84，误判就往上加。", wrap=False))
        threshold_row.addStretch(1)
        card.add(threshold_row)
        card.add(muted("提示：游戏分辨率或画质设置变了，模板要重新录。窗口化/无边框全屏比独占全屏稳。"))

    # ------------------------------------------------------------------ #
    def refresh(self) -> None:
        while self.rows_layout.count():
            item = self.rows_layout.takeAt(0)
            widget = item.widget()
            if widget is not None:
                widget.deleteLater()
        items = self.ctx.store.items
        if not items:
            empty = Card("还没有模板", "点上面的「捕获模板」，把游戏画面里对应的小块框下来即可。")
            self.rows_layout.addWidget(empty)
            return
        for item in items:
            self.rows_layout.addWidget(self._template_row(item))

    def _template_row(self, item) -> QWidget:
        holder = QFrame()
        holder.setObjectName("SubCard")
        layout = QHBoxLayout(holder)
        layout.setContentsMargins(12, 10, 12, 10)
        layout.setSpacing(10)

        thumb = QLabel()
        thumb.setFixedSize(132, 74)
        thumb.setAlignment(Qt.AlignmentFlag.AlignCenter)
        thumb.setStyleSheet(f"background:{theme.BG_DEEP}; border:1px solid {theme.BORDER};"
                            "border-radius:6px;")
        image = self.ctx.store.thumbnail(item.id, 128)
        if image is not None:
            qimage = numpy_to_qimage(image)
            thumb.setPixmap(QPixmap.fromImage(qimage).scaled(
                128, 70, Qt.AspectRatioMode.KeepAspectRatio,
                Qt.TransformationMode.SmoothTransformation))
        layout.addWidget(thumb)
        # 命中位置示意图放在缩略图上会看不清，这里用尺寸文字替代
        size_label = QLabel(f"{item.width}×{item.height}")
        size_label.setObjectName("Hint")

        info = QVBoxLayout()
        info.setSpacing(6)
        name = QLineEdit(item.name)
        name.setPlaceholderText("模板名字")
        name.editingFinished.connect(
            lambda i=item.id, w=name: self.ctx.store.update(i, name=w.text().strip() or "未命名"))
        info.addWidget(name)
        line = QHBoxLayout()
        line.setSpacing(6)
        role = QComboBox()
        for key in ROLE_ORDER:
            role.addItem(role_label(key), key)
        role.setCurrentIndex(max(0, role.findData(item.role)))
        role.currentIndexChanged.connect(
            lambda _index, i=item.id, combo=role: self._set_role(i, combo.currentData()))
        line.addWidget(role)
        threshold = QDoubleSpinBox()
        threshold.setRange(0.40, 0.999)
        threshold.setSingleStep(0.01)
        threshold.setDecimals(3)
        threshold.setValue(float(item.threshold))
        threshold.setFixedWidth(96)
        threshold.valueChanged.connect(
            lambda value, i=item.id: self.ctx.store.update(i, threshold=float(value)))
        line.addWidget(threshold)
        line.addWidget(size_label)
        line.addStretch(1)
        info.addLayout(line)
        layout.addLayout(info, 1)

        buttons = QVBoxLayout()
        buttons.setSpacing(5)
        test = QPushButton("测试匹配")
        test.clicked.connect(lambda _checked=False, i=item.id: self._test(i))
        recapture = QPushButton("重录")
        recapture.setProperty("variant", "ghost")
        recapture.clicked.connect(lambda _checked=False, i=item.id: self._recapture(i))
        remove = QPushButton("删除")
        remove.setProperty("variant", "danger")
        remove.clicked.connect(lambda _checked=False, i=item.id: self._remove(i))
        buttons.addWidget(test)
        buttons.addWidget(recapture)
        buttons.addWidget(remove)
        layout.addLayout(buttons)
        return holder

    # ------------------------------------------------------------------ #
    def _set_role(self, template_id: str, role: str) -> None:
        self.ctx.store.update(template_id, role=role)
        self.ctx.log("info", f"模板角色已改为「{role_label(role)}」")
        self.ctx.templatesChanged.emit()

    def _capture(self) -> None:
        role = self.role_combo.currentData()
        self.ctx.capture_template(role, interactive=True)

    def _from_clipboard(self) -> None:
        image = QGuiApplication.clipboard().image()
        array = qimage_to_numpy(image)
        if array is None:
            self.ctx.log("warn", "剪贴板里没有图片：先用 QQ / Win+Shift+S 截一小块界面再点这里")
            return
        if array.shape[0] < 6 or array.shape[1] < 6:
            self.ctx.log("warn", "剪贴板图片太小了")
            return
        item = self.ctx.store.add("剪贴板导入", self.role_combo.currentData(), array,
                                  float(self.ctx.config.get("smart.match_threshold", 0.86)))
        self.ctx.log("info", f"已从剪贴板导入模板「{item.name}」")
        self.ctx.templatesChanged.emit()

    def _from_file(self) -> None:
        path, _filter = QFileDialog.getOpenFileName(
            self, "选择模板图片", "", "图片 (*.png *.jpg *.jpeg *.bmp)")
        if not path:
            return
        try:
            item = self.ctx.store.add_from_file(
                path, "文件导入", self.role_combo.currentData(),
                float(self.ctx.config.get("smart.match_threshold", 0.86)))
        except ValueError as exc:
            self.ctx.log("warn", str(exc))
            return
        self.ctx.log("info", f"已导入模板「{item.name}」")
        self.ctx.templatesChanged.emit()

    def _recapture(self, template_id: str) -> None:
        item = self.ctx.store.get(template_id)
        if item is None:
            return
        shot_region, shot = self.ctx.capture_region(f"重新框选「{item.name}」")
        if not shot_region or shot is None:
            return
        crop = shot.crop_global(shot_region)
        if crop is None or crop.shape[0] < 6 or crop.shape[1] < 6:
            self.ctx.log("warn", "重录失败：区域无效或太小")
            return
        self.ctx.store.replace_image(template_id, crop)
        self.ctx.log("info", f"模板「{item.name}」已重录")
        self.ctx.templatesChanged.emit()

    def _remove(self, template_id: str) -> None:
        item = self.ctx.store.get(template_id)
        if item is None:
            return
        self.ctx.store.remove(template_id)
        self.ctx.log("info", f"已删除模板「{item.name}」")
        self.ctx.templatesChanged.emit()

    def _test(self, template_id: str) -> None:
        item = self.ctx.store.get(template_id)
        if item is None:
            return
        hwnd = int(self.ctx.config.get("window.hwnd", 0) or 0)
        if not hwnd or not winutil.is_window_alive(hwnd):
            if not self.ctx.sync_window():
                self.ctx.log("warn", "测试失败：还没选游戏窗口")
                return
            hwnd = int(self.ctx.config.get("window.hwnd", 0) or 0)
        self.hide()
        try:
            frame = capture.grab_window(hwnd)
        finally:
            self.show()
        if frame is None:
            self.ctx.log("warn", "测试失败：截不到游戏画面（窗口最小化了？）")
            return
        template = self.ctx.store.gray(template_id)
        if template is None:
            self.ctx.log("warn", "测试失败：模板图读取不了")
            return
        found = matcher.match_one(matcher._gray(frame.image), template, (0.95, 1.0, 1.05))
        if found is None:
            self.ctx.log("warn", f"「{item.name}」比画面还大，匹配不了")
            return
        score, box = found
        verdict = "命中" if score >= float(item.threshold) else "未命中"
        self.ctx.log("info" if verdict == "命中" else "warn",
                     f"「{item.name}」当前画面相似度 {score:.3f}（阈值 {item.threshold:.2f}）→ {verdict}"
                     f"，位置 {box}")
