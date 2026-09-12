"""页面基类：统一的标题 + 可滚动内容区。"""

from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QLabel, QScrollArea, QVBoxLayout, QWidget


class PageBase(QWidget):
    title = "页面"
    description = ""

    def __init__(self, ctx, title: str | None = None, description: str | None = None,
                 parent: QWidget | None = None):
        super().__init__(parent)
        self.ctx = ctx
        self.page_title = title or self.title
        self.page_desc = description if description is not None else self.description

        outer = QVBoxLayout(self)
        outer.setContentsMargins(20, 18, 20, 18)
        outer.setSpacing(12)

        head = QVBoxLayout()
        head.setSpacing(3)
        label = QLabel(self.page_title)
        label.setObjectName("PageTitle")
        head.addWidget(label)
        if self.page_desc:
            desc = QLabel(self.page_desc)
            desc.setObjectName("PageDesc")
            desc.setWordWrap(True)
            head.addWidget(desc)
        outer.addLayout(head)

        self.scroll = QScrollArea()
        self.scroll.setWidgetResizable(True)
        self.scroll.setFrameShape(QScrollArea.Shape.NoFrame)
        self.scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        inner = QWidget()
        self.content = QVBoxLayout(inner)
        self.content.setContentsMargins(0, 2, 10, 2)
        self.content.setSpacing(12)
        self.content.addStretch(1)
        self.scroll.setWidget(inner)
        outer.addWidget(self.scroll, 1)

    def add(self, widget: QWidget) -> QWidget:
        self.content.insertWidget(self.content.count() - 1, widget)
        return widget

    def refresh(self) -> None:
        """页面被切换到前台时调用。"""
