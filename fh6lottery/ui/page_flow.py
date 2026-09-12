"""流程页：智能识别的处理规则 + 节奏宏编排。"""

from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (QComboBox, QDialog, QDialogButtonBox, QFrame,
                               QHBoxLayout, QLabel, QLineEdit, QListWidget,
                               QListWidgetItem, QPushButton, QRadioButton,
                               QSpinBox, QVBoxLayout, QWidget)

from .. import winutil
from ..templates import ROLE_ORDER, role_label
from . import theme
from .page_base import PageBase
from .widgets import Card, hint, hline, muted, row


def _spin(value: int, minimum: int = 0, maximum: int = 600000, step: int = 50,
          suffix: str = " ms") -> QSpinBox:
    box = QSpinBox()
    box.setRange(minimum, maximum)
    box.setSingleStep(step)
    box.setValue(int(value))
    if suffix:
        box.setSuffix(suffix)
    box.setFixedWidth(120)
    return box


class TemplatePickDialog(QDialog):
    """从模板库里挑若干条绑定到指定角色。"""

    def __init__(self, ctx, role: str, parent=None):
        super().__init__(parent)
        self.ctx = ctx
        self.role = role
        self.setWindowTitle(f"为「{role_label(role)}」选择模板")
        self.setMinimumSize(520, 420)
        layout = QVBoxLayout(self)
        layout.setSpacing(10)
        layout.addWidget(muted("勾选 = 该模板用于识别这个界面；取消勾选会把模板移到「未使用」。"))

        self.list = QListWidget()
        for item in ctx.store.items:
            entry = QListWidgetItem(f"{item.name}　·　{role_label(item.role)}"
                                    f"　·　{item.width}×{item.height}")
            entry.setData(Qt.ItemDataRole.UserRole, item.id)
            entry.setFlags(entry.flags() | Qt.ItemFlag.ItemIsUserCheckable)
            entry.setCheckState(Qt.CheckState.Checked if item.role == role
                                else Qt.CheckState.Unchecked)
            self.list.addItem(entry)
        layout.addWidget(self.list, 1)

        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok
                                   | QDialogButtonBox.StandardButton.Cancel)
        buttons.button(QDialogButtonBox.StandardButton.Ok).setText("确定")
        buttons.button(QDialogButtonBox.StandardButton.Cancel).setText("取消")
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    def apply(self) -> None:
        for index in range(self.list.count()):
            entry = self.list.item(index)
            template_id = entry.data(Qt.ItemDataRole.UserRole)
            item = self.ctx.store.get(template_id)
            if item is None:
                continue
            checked = entry.checkState() == Qt.CheckState.Checked
            if checked and item.role != self.role:
                self.ctx.store.update(template_id, role=self.role)
            elif not checked and item.role == self.role:
                self.ctx.store.update(template_id, role="unused")
        self.ctx.templatesChanged.emit()


class SmartPage(PageBase):
    title = "流程（智能识别）"
    description = "告诉助手：识别到哪个界面该做什么。左半边是「已拥有车辆」的处理策略，右半边是各界面绑定的模板。"

    def __init__(self, ctx, parent: QWidget | None = None):
        super().__init__(ctx, parent=parent)
        self._policy_radios: dict[str, QRadioButton] = {}
        self._key_edits: dict[str, QLineEdit] = {}
        self._role_rows: dict[str, QWidget] = {}
        self._build_policy()
        self._build_waits()
        self._build_stop()
        self._build_roles()
        ctx.templatesChanged.connect(self.refresh)

    # ------------------------------------------------------------------ #
    def _build_policy(self) -> None:
        card = Card("已拥有车辆怎么处理",
                    "抽到重复车时，《地平线》会弹出「加入车库 / 送礼 / 出售」选项。"
                    "识别到「已拥有」界面后，助手会按下面的按键序列选择。")
        self.add(card)
        policies = [("garage", "加入车库", "按一次 Enter（默认选项）"),
                    ("gift", "送礼", "按 ↓ 一次 + Enter"),
                    ("sell", "出售赚 CR", "按 ↓↓ 两次 + Enter，并尝试识别出售价格")]
        for key, label, tip in policies:
            line = QHBoxLayout()
            radio = QRadioButton(label)
            radio.toggled.connect(lambda checked, k=key: self._on_policy(k, checked))
            self._policy_radios[key] = radio
            edit = QLineEdit()
            edit.setPlaceholderText("按键序列，逗号分隔，如 down,down,enter")
            edit.setFixedWidth(220)
            edit.editingFinished.connect(lambda k=key: self._on_keys(k))
            self._key_edits[key] = edit
            line.addWidget(radio)
            line.addWidget(edit)
            line.addWidget(hint(tip, wrap=False))
            line.addStretch(1)
            card.add(line)
        card.add(hline())
        self.price_holder = QWidget()
        price_layout = QHBoxLayout(self.price_holder)
        price_layout.setContentsMargins(0, 0, 0, 0)
        self.price_enabled = QPushButton("识别出售价格：未开启")
        self.price_enabled.setCheckable(True)
        self.price_enabled.clicked.connect(self._toggle_price)
        self.price_region_label = hint("", wrap=False)
        pick = QPushButton("框选价格区域")
        pick.clicked.connect(self._pick_price_region)
        clear = QPushButton("清除")
        clear.setProperty("variant", "ghost")
        clear.clicked.connect(self._clear_price_region)
        price_layout.addWidget(self.price_enabled)
        price_layout.addWidget(pick)
        price_layout.addWidget(clear)
        price_layout.addWidget(self.price_region_label, 1)
        card.add(self.price_holder)

    def _build_waits(self) -> None:
        card = Card("等待时间（按自己电脑的节奏微调）",
                    "如果助手按键太早（界面还没出现）就调大；太慢就调小。")
        self.add(card)
        items = [("after_start_ms", "按下开始后等转盘动画", 5200),
                 ("after_result_ms", "结果界面操作后", 650),
                 ("after_option_ms", "已拥有选项操作后", 950),
                 ("after_confirm_ms", "确认后", 700)]
        self._wait_spins: dict[str, QSpinBox] = {}
        for key, label, default in items:
            box = _spin(default)
            box.valueChanged.connect(lambda value, k=key: self.ctx.set(f"smart.waits.{k}", int(value)))
            self._wait_spins[key] = box
            card.add(row(QLabel(label), box, None))
        return card

    def _build_stop(self) -> None:
        card = Card("停止条件与兜底")
        self.add(card)
        self.target_spin = _spin(0, 0, 1000000, 1, " 次")
        self.target_spin.valueChanged.connect(
            lambda value: self.ctx.set("smart.target_spins", int(value)))
        card.add(row(QLabel("抽够多少次后自动停止（0 = 不限）"), self.target_spin, None))

        self.end_switch = QPushButton("识别到「抽奖结束」界面时自动停止：开")
        self.end_switch.setCheckable(True)
        self.end_switch.clicked.connect(self._toggle_end)
        card.add(self.end_switch)

        self.unknown_combo = QComboBox()
        for value, label in (("wait", "什么都不做（推荐）"), ("enter", "按 Enter 试试"),
                             ("esc", "按 Esc 退出当前界面")):
            self.unknown_combo.addItem(label, value)
        self.unknown_combo.currentIndexChanged.connect(
            lambda index: self.ctx.set("smart.unknown_action",
                                       self.unknown_combo.itemData(index)))
        card.add(row(QLabel("看到「需要处理的阻挡界面」时"), self.unknown_combo, None))
        card.add(muted("连续识别不出任何界面超过「设置」里的上限，助手会自动停下来并提示你补模板。"))

        self.count_enabled = QPushButton("读取游戏内剩余抽奖次数：未开启")
        self.count_enabled.setCheckable(True)
        self.count_enabled.clicked.connect(self._toggle_count)
        self.count_label = hint("", wrap=False)
        pick = QPushButton("框选次数区域")
        pick.clicked.connect(self._pick_count_region)
        card.add(self.count_enabled)
        card.add(row(pick, self.count_label, None))

    def _build_roles(self) -> None:
        card = Card("各界面绑定的模板", "点「捕获」会临时隐藏本窗口、冻结游戏画面，让你直接框选。")
        self.add(card)
        for role in [r for r in ROLE_ORDER if r != "unused"]:
            holder = QWidget()
            layout = QHBoxLayout(holder)
            layout.setContentsMargins(0, 0, 0, 0)
            layout.setSpacing(8)
            label = QLabel(role_label(role))
            label.setMinimumWidth(190)
            names = QLabel("")
            names.setObjectName("Hint")
            names.setWordWrap(True)
            capture = QPushButton("捕获")
            capture.clicked.connect(lambda _checked=False, r=role: self._capture(r))
            pick = QPushButton("从库里挑")
            pick.setProperty("variant", "ghost")
            pick.clicked.connect(lambda _checked=False, r=role: self._pick(r))
            layout.addWidget(label)
            layout.addWidget(names, 1)
            layout.addWidget(capture)
            layout.addWidget(pick)
            holder.names_label = names  # type: ignore[attr-defined]
            self._role_rows[role] = holder
            card.add(holder)
        card.add(hline())
        card.add(muted("小技巧：《地平线》抽奖时，先录「抽奖主界面」，再录「结果界面 · 已拥有」。"
                       "其它角色可以先不录。"))

    # ------------------------------------------------------------------ #
    def refresh(self) -> None:
        smart = self.ctx.config.get("smart", {}) or {}
        policy = str(smart.get("owned_policy", "garage"))
        for key, radio in self._policy_radios.items():
            radio.blockSignals(True)
            radio.setChecked(key == policy)
            radio.blockSignals(False)
        keys = smart.get("owned_keys", {}) or {}
        for key, edit in self._key_edits.items():
            text = ", ".join(keys.get(key) or [])
            if edit.text() != text:
                edit.setText(text)

        waits = smart.get("waits", {}) or {}
        for key, box in self._wait_spins.items():
            box.blockSignals(True)
            box.setValue(int(waits.get(key, box.value())))
            box.blockSignals(False)

        self.target_spin.blockSignals(True)
        self.target_spin.setValue(int(smart.get("target_spins", 0) or 0))
        self.target_spin.blockSignals(False)

        self.end_switch.setChecked(bool(smart.get("auto_stop_on_end_template", True)))
        self.end_switch.setText("识别到「抽奖结束」界面时自动停止："
                                + ("开" if self.end_switch.isChecked() else "关"))

        unknown = str(smart.get("unknown_action", "wait"))
        index = max(0, self.unknown_combo.findData(unknown))
        self.unknown_combo.blockSignals(True)
        self.unknown_combo.setCurrentIndex(index)
        self.unknown_combo.blockSignals(False)

        price_rule = smart.get("sell_price_rule", {}) or {}
        self.price_enabled.blockSignals(True)
        self.price_enabled.setChecked(bool(price_rule.get("enabled")))
        self.price_enabled.blockSignals(False)
        self.price_enabled.setText("识别出售价格：" + ("已开启" if price_rule.get("enabled") else "未开启"))
        region = price_rule.get("region")
        self.price_region_label.setText(f"区域：{self._region_text(region)}")
        self.price_holder.setVisible(policy == "sell")

        count_rule = smart.get("spin_count_rule", {}) or {}
        self.count_enabled.blockSignals(True)
        self.count_enabled.setChecked(bool(count_rule.get("enabled")))
        self.count_enabled.blockSignals(False)
        self.count_enabled.setText("读取游戏内剩余抽奖次数："
                                   + ("已开启" if count_rule.get("enabled") else "未开启"))
        self.count_label.setText(f"区域：{self._region_text(count_rule.get('region'))}")

        for role, holder in self._role_rows.items():
            bound = [i.name for i in self.ctx.store.by_role(role)]
            holder.names_label.setText("、".join(bound) if bound else "（未绑定）")

    @staticmethod
    def _region_text(region) -> str:
        if not region:
            return "整张画面"
        try:
            x, y, w, h = (int(v) for v in region)
            return f"({x}, {y}) {w}×{h}"
        except (TypeError, ValueError):
            return "整张画面"

    # ------------------------------------------------------------------ #
    def _on_policy(self, key: str, checked: bool) -> None:
        if not checked:
            return
        self.ctx.set("smart.owned_policy", key)
        self.refresh()

    def _on_keys(self, key: str) -> None:
        text = self._key_edits[key].text().strip()
        keys = [k.strip() for k in text.replace("，", ",").split(",") if k.strip()]
        bad = [k for k in keys if k.lower() not in winutil.KEYMAP]
        if bad:
            self.ctx.log("warn", f"这些按键名不认识：{', '.join(bad)}（可用：enter / down / up / esc …）")
            keys = [k for k in keys if k.lower() in winutil.KEYMAP]
        self.ctx.set(f"smart.owned_keys.{key}", keys)
        self._key_edits[key].setText(", ".join(keys))

    def _toggle_price(self) -> None:
        self.ctx.set("smart.sell_price_rule.enabled", bool(self.price_enabled.isChecked()))
        self.refresh()

    def _toggle_count(self) -> None:
        self.ctx.set("smart.spin_count_rule.enabled", bool(self.count_enabled.isChecked()))
        self.refresh()

    def _toggle_end(self) -> None:
        self.ctx.set("smart.auto_stop_on_end_template", bool(self.end_switch.isChecked()))
        self.refresh()

    def _pick_price_region(self) -> None:
        region, _shot = self.ctx.capture_region("框选游戏里显示「出售价格」的那块数字区域")
        if region:
            self.ctx.set("smart.sell_price_rule.region", list(region))
            self.ctx.log("info", f"出售价格区域已设置为 {region}")
        self.refresh()

    def _clear_price_region(self) -> None:
        self.ctx.set("smart.sell_price_rule.region", None)
        self.refresh()

    def _pick_count_region(self) -> None:
        region, _shot = self.ctx.capture_region("框选游戏里显示「剩余抽奖次数」的数字区域")
        if region:
            self.ctx.set("smart.spin_count_rule.region", list(region))
            self.ctx.log("info", f"剩余次数区域已设置为 {region}")
        self.refresh()

    def _capture(self, role: str) -> None:
        self.ctx.capture_template(role, interactive=True)
        self.refresh()

    def _pick(self, role: str) -> None:
        if not self.ctx.store.items:
            self.ctx.log("warn", "模板库还是空的，先点「捕获」录一个吧")
            return
        dialog = TemplatePickDialog(self.ctx, role, self)
        if dialog.exec() == QDialog.DialogCode.Accepted:
            dialog.apply()
            self.refresh()


class RhythmPage(PageBase):
    title = "节奏宏"
    description = "完全不做识别的保底方案：把「按键 + 等待」排成一个循环，助手照着循环按。适合界面识别不准时使用。"

    def __init__(self, ctx, parent: QWidget | None = None):
        super().__init__(ctx, parent=parent)
        card = Card("按键序列（按顺序执行，然后从头再来）")
        self.add(card)

        toolbar = QHBoxLayout()
        toolbar.setSpacing(6)
        for text, handler in (("+ 按键", self._add_key), ("+ 等待", self._add_delay),
                              ("+ 点击", self._add_click),
                              ("标准循环预设", self._preset_standard),
                              ("快速连按预设", self._preset_fast)):
            button = QPushButton(text)
            if text.startswith("+"):
                pass
            else:
                button.setProperty("variant", "ghost")
            button.clicked.connect(handler)
            toolbar.addWidget(button)
        toolbar.addStretch(1)
        card.add(toolbar)

        self.steps_holder = QWidget()
        self.steps_layout = QVBoxLayout(self.steps_holder)
        self.steps_layout.setContentsMargins(0, 0, 0, 0)
        self.steps_layout.setSpacing(6)
        card.add(self.steps_holder)

        card.add(hline())
        self.loop_delay = _spin(0)
        self.loop_delay.valueChanged.connect(
            lambda value: self.ctx.set("rhythm.loop_delay_ms", int(value)))
        self.max_loops = _spin(0, 0, 1000000, 1, " 轮")
        self.max_loops.valueChanged.connect(
            lambda value: self.ctx.set("rhythm.max_loops", int(value)))
        card.add(row(QLabel("每轮结束后等待"), self.loop_delay,
                     QLabel("最多循环"), self.max_loops, None))
        card.add(muted("节奏模式不计数、不识别，只按序列发按键；鼠标移到屏幕左上角依然可以急停。"))
        ctx.configChanged.connect(self.refresh)

    # ------------------------------------------------------------------ #
    def refresh(self) -> None:
        steps = list(self.ctx.config.get("rhythm.steps", []) or [])
        while self.steps_layout.count():
            item = self.steps_layout.takeAt(0)
            widget = item.widget()
            if widget is not None:
                widget.deleteLater()
        for index, step in enumerate(steps):
            self.steps_layout.addWidget(self._step_row(index, step))
        self.loop_delay.blockSignals(True)
        self.loop_delay.setValue(int(self.ctx.config.get("rhythm.loop_delay_ms", 0) or 0))
        self.loop_delay.blockSignals(False)
        self.max_loops.blockSignals(True)
        self.max_loops.setValue(int(self.ctx.config.get("rhythm.max_loops", 0) or 0))
        self.max_loops.blockSignals(False)

    def _step_row(self, index: int, step: dict) -> QWidget:
        holder = QFrame()
        holder.setObjectName("SubCard")
        layout = QHBoxLayout(holder)
        layout.setContentsMargins(10, 8, 10, 8)
        layout.setSpacing(8)
        layout.addWidget(QLabel(f"{index + 1}."))

        kind = QComboBox()
        for value, label in (("key", "按键"), ("delay", "等待"), ("click", "鼠标点击")):
            kind.addItem(label, value)
        kind.setCurrentIndex(max(0, kind.findData(str(step.get("type", "key")))))
        layout.addWidget(kind)

        key = QComboBox()
        for name in winutil.KEY_CHOICES:
            key.addItem(name, name)
        current_key = str(step.get("key", "enter"))
        if key.findData(current_key) < 0:
            key.addItem(current_key, current_key)
        key.setCurrentIndex(max(0, key.findData(current_key)))
        layout.addWidget(key)

        hold = _spin(int(step.get("hold_ms", 45)), 8, 3000, 5, " ms 按住")
        delay = _spin(int(step.get("delay_ms", 1000)), 0, 600000, 50, " ms 后继续")

        x_spin = _spin(int(step.get("x", 0)), 0, 20000, 10, " px")
        y_spin = _spin(int(step.get("y", 0)), 0, 20000, 10, " px")

        def _sync(*_args, i=index):
            steps = list(self.ctx.config.get("rhythm.steps", []) or [])
            if i >= len(steps):
                return
            steps[i] = {
                "type": kind.currentData(),
                "key": key.currentData(),
                "hold_ms": int(hold.value()),
                "delay_ms": int(delay.value()),
                "x": int(x_spin.value()),
                "y": int(y_spin.value()),
            }
            self.ctx.set("rhythm.steps", steps)

        kind.currentIndexChanged.connect(lambda *_: (self._sync_visibility(kind, key, hold, x_spin, y_spin), _sync()))
        key.currentIndexChanged.connect(_sync)
        hold.valueChanged.connect(_sync)
        delay.valueChanged.connect(_sync)
        x_spin.valueChanged.connect(_sync)
        y_spin.valueChanged.connect(_sync)

        layout.addWidget(key)
        layout.addWidget(hold)
        layout.addWidget(x_spin)
        layout.addWidget(y_spin)
        layout.addWidget(delay)
        layout.addStretch(1)

        up = QPushButton("↑")
        up.setFixedWidth(32)
        up.clicked.connect(lambda _checked=False, i=index: self._move(i, -1))
        down = QPushButton("↓")
        down.setFixedWidth(32)
        down.clicked.connect(lambda _checked=False, i=index: self._move(i, 1))
        remove = QPushButton("✕")
        remove.setFixedWidth(32)
        remove.setProperty("variant", "danger")
        remove.clicked.connect(lambda _checked=False, i=index: self._remove(i))
        layout.addWidget(up)
        layout.addWidget(down)
        layout.addWidget(remove)

        self._sync_visibility(kind, key, hold, x_spin, y_spin)
        holder.setProperty("row_index", index)
        return holder

    @staticmethod
    def _sync_visibility(kind: QComboBox, key: QComboBox, hold: QSpinBox,
                         x_spin: QSpinBox, y_spin: QSpinBox) -> None:
        mode = kind.currentData()
        key.setVisible(mode == "key")
        hold.setVisible(mode == "key")
        x_spin.setVisible(mode == "click")
        y_spin.setVisible(mode == "click")

    # ------------------------------------------------------------------ #
    def _steps(self) -> list:
        return list(self.ctx.config.get("rhythm.steps", []) or [])

    def _save(self, steps: list) -> None:
        self.ctx.set("rhythm.steps", steps)
        self.refresh()

    def _add_key(self) -> None:
        steps = self._steps()
        steps.append({"type": "key", "key": "enter", "hold_ms": 45, "delay_ms": 1000,
                      "x": 0, "y": 0})
        self._save(steps)

    def _add_delay(self) -> None:
        steps = self._steps()
        steps.append({"type": "delay", "key": "enter", "hold_ms": 45, "delay_ms": 1500,
                      "x": 0, "y": 0})
        self._save(steps)

    def _add_click(self) -> None:
        steps = self._steps()
        steps.append({"type": "click", "key": "enter", "hold_ms": 45, "delay_ms": 800,
                      "x": 960, "y": 540})
        self._save(steps)

    def _move(self, index: int, delta: int) -> None:
        steps = self._steps()
        target = index + delta
        if not (0 <= index < len(steps) and 0 <= target < len(steps)):
            return
        steps[index], steps[target] = steps[target], steps[index]
        self._save(steps)

    def _remove(self, index: int) -> None:
        steps = self._steps()
        if 0 <= index < len(steps):
            del steps[index]
        self._save(steps)

    def _preset_standard(self) -> None:
        self._save([
            {"type": "key", "key": "enter", "hold_ms": 45, "delay_ms": 1300, "x": 0, "y": 0},
            {"type": "delay", "key": "enter", "hold_ms": 45, "delay_ms": 2500, "x": 0, "y": 0},
            {"type": "key", "key": "enter", "hold_ms": 45, "delay_ms": 800, "x": 0, "y": 0},
        ])
        self.ctx.log("info", "已套用「标准循环」预设，可按自己的节奏微调")

    def _preset_fast(self) -> None:
        self._save([
            {"type": "key", "key": "enter", "hold_ms": 40, "delay_ms": 700, "x": 0, "y": 0},
        ])
        self.ctx.log("info", "已套用「快速连按」预设（只按 Enter，间隔 0.7 秒）")
