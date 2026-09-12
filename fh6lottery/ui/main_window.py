"""主窗口：左侧导航 + 右侧页面 + 悬浮窗/托盘/状态栏。"""

from __future__ import annotations

from PySide6.QtCore import QTimer, Qt
from PySide6.QtGui import QAction, QColor, QFont, QIcon, QPainter, QPixmap
from PySide6.QtWidgets import (QApplication, QButtonGroup, QFrame, QHBoxLayout,
                               QLabel, QMainWindow, QMenu, QPushButton,
                               QStackedWidget, QSystemTrayIcon, QVBoxLayout,
                               QWidget)

from .. import __app_name__, __version__, paths
from . import theme
from .overlay import OverlayWindow
from .page_dashboard import DashboardPage
from .page_finish import LogPage, PostPage, SettingsPage
from .page_flow import RhythmPage, SmartPage
from .page_templates import TemplatesPage
from .page_vision import RecogPage

NAV_ITEMS = [
    ("dashboard", "概览"),
    ("flow", "流程（智能识别）"),
    ("templates", "模板库"),
    ("rhythm", "节奏宏"),
    ("vision", "识别与调试"),
    ("post", "完成后的操作"),
    ("settings", "设置"),
    ("log", "日志"),
]


def app_icon() -> QIcon:
    """运行时画一个图标，不依赖外部资源文件。"""
    pixmap = QPixmap(128, 128)
    pixmap.fill(Qt.GlobalColor.transparent)
    painter = QPainter(pixmap)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)
    painter.setPen(Qt.PenStyle.NoPen)
    painter.setBrush(QColor(theme.BG))
    painter.drawRoundedRect(4, 4, 120, 120, 26, 26)
    painter.setBrush(QColor(theme.ACCENT))
    painter.drawEllipse(20, 20, 88, 88)
    painter.setBrush(QColor("#FFFFFF"))
    painter.drawPie(34, 34, 60, 60, 90 * 16, 90 * 16)
    painter.setBrush(QColor(theme.BG))
    painter.drawEllipse(56, 56, 16, 16)
    painter.end()
    return QIcon(pixmap)


class MainWindow(QMainWindow):
    def __init__(self, ctx):
        super().__init__()
        self.ctx = ctx
        ctx.window = self
        self.setWindowTitle(f"{__app_name__} v{__version__}")
        self.setWindowIcon(app_icon())
        self.resize(1120, 760)
        self.setMinimumSize(940, 640)

        root = QWidget()
        root.setObjectName("Root")
        self.setCentralWidget(root)
        layout = QHBoxLayout(root)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        layout.addWidget(self._build_sidebar())

        self.stack = QStackedWidget()
        layout.addWidget(self.stack, 1)

        self.pages = {
            "dashboard": DashboardPage(ctx),
            "flow": SmartPage(ctx),
            "templates": TemplatesPage(ctx),
            "rhythm": RhythmPage(ctx),
            "vision": RecogPage(ctx),
            "post": PostPage(ctx),
            "settings": SettingsPage(ctx),
            "log": LogPage(ctx),
        }
        for key, _label in NAV_ITEMS:
            self.stack.addWidget(self.pages[key])

        self._build_overlay()
        self._build_statusbar()
        self._build_tray()
        self._wire()

        self.nav_buttons["dashboard"].setChecked(True)
        self.stack.setCurrentIndex(0)
        QTimer.singleShot(120, self._bootstrap)

    # ------------------------------------------------------------------ #
    def _build_sidebar(self) -> QWidget:
        sidebar = QWidget()
        sidebar.setObjectName("Sidebar")
        sidebar.setFixedWidth(204)
        layout = QVBoxLayout(sidebar)
        layout.setContentsMargins(12, 16, 12, 14)
        layout.setSpacing(6)

        title = QLabel(__app_name__)
        title.setObjectName("AppTitle")
        subtitle = QLabel(f"v{__version__} · 纯外部辅助")
        subtitle.setObjectName("AppSubtitle")
        layout.addWidget(title)
        layout.addWidget(subtitle)
        layout.addSpacing(10)

        self.nav_buttons: dict[str, QPushButton] = {}
        group = QButtonGroup(self)
        group.setExclusive(True)
        for index, (key, label) in enumerate(NAV_ITEMS):
            button = QPushButton(label)
            button.setObjectName("NavButton")
            button.setCheckable(True)
            button.setCursor(Qt.CursorShape.PointingHandCursor)
            button.clicked.connect(lambda _checked=False, i=index: self._go(i))
            group.addButton(button, index)
            layout.addWidget(button)
            self.nav_buttons[key] = button

        layout.addStretch(1)
        self.quick_button = QPushButton("开始抽奖（F7）")
        self.quick_button.setProperty("variant", "primary")
        self.quick_button.setMinimumHeight(36)
        self.quick_button.clicked.connect(self.ctx.start)
        layout.addWidget(self.quick_button)
        self.quick_stop = QPushButton("停止（F8）")
        self.quick_stop.setProperty("variant", "danger")
        self.quick_stop.clicked.connect(self.ctx.stop)
        layout.addWidget(self.quick_stop)
        tip = QLabel("鼠标移到屏幕左上角\n可以紧急停止")
        tip.setObjectName("Hint")
        tip.setAlignment(Qt.AlignmentFlag.AlignCenter)
        layout.addWidget(tip)
        return sidebar

    def _go(self, index: int) -> None:
        self.stack.setCurrentIndex(index)
        widget = self.stack.widget(index)
        refresh = getattr(widget, "refresh", None)
        if callable(refresh):
            refresh()

    # ------------------------------------------------------------------ #
    def _build_overlay(self) -> None:
        self.overlay = OverlayWindow(self.ctx.config)
        self.ctx.overlay = self.overlay
        self.overlay.startRequested.connect(self.ctx.start)
        self.overlay.stopRequested.connect(self.ctx.stop)
        self.overlay.pauseRequested.connect(self.ctx.pause)
        self.overlay.lockChanged.connect(self.ctx.overlay_locked)
        self.overlay.geometryChanged.connect(self.ctx.overlay_moved)
        self.overlay.hideRequested.connect(lambda: self.ctx.show_overlay(False))
        if self.ctx.config.get("overlay.enabled", True):
            self.overlay.show()

    def _build_statusbar(self) -> None:
        bar = QFrame()
        bar.setObjectName("SubCard")
        bar.setFixedHeight(30)
        layout = QHBoxLayout(bar)
        layout.setContentsMargins(12, 2, 12, 2)
        layout.setSpacing(14)
        self.status_text = QLabel("就绪")
        self.status_text.setObjectName("Hint")
        self.status_hotkey = QLabel("")
        self.status_hotkey.setObjectName("Hint")
        self.status_ocr = QLabel("")
        self.status_ocr.setObjectName("Hint")
        layout.addWidget(self.status_text)
        layout.addStretch(1)
        layout.addWidget(self.status_hotkey)
        layout.addWidget(self.status_ocr)
        self.statusBar().addPermanentWidget(bar, 1)

        timer = QTimer(self)
        timer.timeout.connect(self._refresh_status)
        timer.start(2000)
        self._refresh_status()

    def _build_tray(self) -> None:
        self.tray: QSystemTrayIcon | None = None
        if not QSystemTrayIcon.isSystemTrayAvailable():
            return
        try:
            self.tray = QSystemTrayIcon(app_icon(), self)
            self.tray.setToolTip(f"{__app_name__} v{__version__}")
            menu = QMenu(self)
            show_action = QAction("显示主窗口", self)
            show_action.triggered.connect(self._show_up)
            start_action = QAction("开始抽奖", self)
            start_action.triggered.connect(self.ctx.start)
            stop_action = QAction("停止抽奖", self)
            stop_action.triggered.connect(self.ctx.stop)
            overlay_action = QAction("显示/隐藏悬浮窗", self)
            overlay_action.triggered.connect(
                lambda: self.ctx.show_overlay(not self.overlay.isVisible()))
            quit_action = QAction("退出", self)
            quit_action.triggered.connect(self.close)
            menu.addAction(show_action)
            menu.addSeparator()
            menu.addAction(start_action)
            menu.addAction(stop_action)
            menu.addAction(overlay_action)
            menu.addSeparator()
            menu.addAction(quit_action)
            self.tray.setContextMenu(menu)
            self.tray.activated.connect(
                lambda reason: self._show_up()
                if reason == QSystemTrayIcon.ActivationReason.Trigger else None)
            self.tray.show()
        except Exception:
            self.tray = None

    def _show_up(self) -> None:
        self.showNormal()
        self.raise_()
        self.activateWindow()

    # ------------------------------------------------------------------ #
    def _wire(self) -> None:
        self.ctx.logged.connect(self._on_log)
        self.ctx.stateChanged.connect(self._on_state)
        self.ctx.statsUpdated.connect(self.overlay.set_stats)
        self.ctx.recognitionUpdated.connect(self.overlay.set_recog)
        self.ctx.templatesChanged.connect(self._on_templates)

    def _bootstrap(self) -> None:
        self.ctx.log("info", f"{__app_name__} v{__version__} 已启动，数据目录 "
                             f"{paths.display_path(paths.DATA_DIR)}")
        self.ctx.sync_window()
        self.ctx.apply_hotkeys()
        self.pages["dashboard"].refresh()
        self._refresh_status()

    def _on_log(self, level: str, message: str) -> None:
        self.status_text.setText(message[:110])
        self.status_text.setObjectName("Danger" if level == "error" else "Hint")
        self.status_text.style().unpolish(self.status_text)
        self.status_text.style().polish(self.status_text)

    def _on_state(self, state: str) -> None:
        running = state == "running"
        self.quick_button.setEnabled(not running)
        self.quick_stop.setEnabled(running)
        self.quick_button.setText("运行中…" if running else "开始抽奖（F7）")

    def _on_templates(self) -> None:
        self.pages["flow"].refresh()

    def _refresh_status(self) -> None:
        hotkeys = self.ctx.config.get("hotkeys", {}) or {}
        self.status_hotkey.setText(
            f"{hotkeys.get('start', 'F7')} 开始 · {hotkeys.get('stop', 'F8')} 停止 · "
            f"{hotkeys.get('capture', 'F9')} 捕获 · {hotkeys.get('toggle_overlay', 'F10')} 悬浮窗")
        self.status_ocr.setText(self.ctx.ocr_status())

    # ------------------------------------------------------------------ #
    def closeEvent(self, event):  # noqa: N802
        self.ctx.engine.stop()
        try:
            self.ctx.config.set("overlay.enabled", self.overlay.isVisible())
            self.ctx.save()
        except Exception:
            pass
        if self.tray is not None:
            self.tray.hide()
        self.overlay.close()
        self.ctx.hotkeys.stop()
        try:
            from .. import ocr as ocr_mod
            ocr_mod.get_ocr().stop()
        except Exception:
            pass
        super().closeEvent(event)
