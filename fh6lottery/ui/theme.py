"""PCL 风格深色主题：深底 + 橙色主色 + 圆角卡片。"""

from __future__ import annotations

from PySide6.QtGui import QColor, QFont, QPalette

# 调色板
BG = "#1A1A2E"
BG_DEEP = "#141424"
SURFACE = "#21213A"
SURFACE_HI = "#2A2A4A"
BORDER = "#33334F"
BORDER_HI = "#45456B"
TEXT = "#F3F3FA"
TEXT_DIM = "#9A9AB8"
TEXT_FAINT = "#6C6C8A"
ACCENT = "#FF6C34"
ACCENT_HOVER = "#FF8353"
ACCENT_PRESS = "#E4581F"
ACCENT_SOFT = "#3A2A28"
SUCCESS = "#3DD68C"
WARN = "#FFC53D"
DANGER = "#FF5F5F"
INFO = "#57A6FF"

FONT_FAMILY = "Microsoft YaHei UI, Segoe UI, sans-serif"


def qcolor(name: str, alpha: int = 255) -> QColor:
    color = QColor(name)
    color.setAlpha(alpha)
    return color


QSS = f"""
* {{
    font-family: {FONT_FAMILY};
    font-size: 13px;
    color: {TEXT};
    outline: none;
}}

QWidget#Root, QMainWindow, QDialog {{
    background: {BG};
}}

QWidget#Sidebar {{
    background: {BG_DEEP};
    border-right: 1px solid {BORDER};
}}

QLabel#AppTitle {{
    font-size: 17px;
    font-weight: 700;
    color: {TEXT};
}}
QLabel#AppSubtitle, QLabel#Muted, QLabel#Hint {{
    color: {TEXT_DIM};
    font-size: 12px;
}}
QLabel#Hint {{ color: {TEXT_FAINT}; }}
QLabel#PageTitle {{ font-size: 20px; font-weight: 700; }}
QLabel#PageDesc {{ color: {TEXT_DIM}; font-size: 12px; }}
QLabel#SectionTitle {{ font-size: 14px; font-weight: 600; color: {TEXT}; }}
QLabel#CardTitle {{ font-size: 14px; font-weight: 600; }}
QLabel#StatValue {{ font-size: 22px; font-weight: 700; color: {ACCENT}; }}
QLabel#StatLabel {{ color: {TEXT_DIM}; font-size: 12px; }}
QLabel#Accent {{ color: {ACCENT}; font-weight: 600; }}
QLabel#Ok {{ color: {SUCCESS}; }}
QLabel#Warn {{ color: {WARN}; }}
QLabel#Danger {{ color: {DANGER}; }}

QFrame#Card {{
    background: {SURFACE};
    border: 1px solid {BORDER};
    border-radius: 12px;
}}
QFrame#SubCard {{
    background: {SURFACE_HI};
    border: 1px solid {BORDER};
    border-radius: 10px;
}}
QFrame#HLine {{ background: {BORDER}; max-height: 1px; border: none; }}

QPushButton {{
    background: {SURFACE_HI};
    border: 1px solid {BORDER_HI};
    border-radius: 8px;
    padding: 7px 14px;
    color: {TEXT};
}}
QPushButton:hover {{ background: #333358; border-color: {ACCENT}; }}
QPushButton:pressed {{ background: #2A2A48; }}
QPushButton:disabled {{ color: {TEXT_FAINT}; border-color: {BORDER}; background: #23233A; }}

QPushButton[variant="primary"] {{
    background: {ACCENT};
    border: 1px solid {ACCENT};
    color: #1A1A2E;
    font-weight: 700;
}}
QPushButton[variant="primary"]:hover {{ background: {ACCENT_HOVER}; }}
QPushButton[variant="primary"]:pressed {{ background: {ACCENT_PRESS}; }}
QPushButton[variant="primary"]:disabled {{ background: #5A3A2A; color: #A88C80; }}

QPushButton[variant="danger"] {{
    background: transparent; border: 1px solid {DANGER}; color: {DANGER};
}}
QPushButton[variant="danger"]:hover {{ background: rgba(255, 95, 95, 40); }}

QPushButton[variant="ghost"] {{
    background: transparent; border: 1px solid {BORDER}; color: {TEXT_DIM};
}}
QPushButton[variant="ghost"]:hover {{ color: {TEXT}; border-color: {BORDER_HI}; }}

QPushButton#NavButton {{
    background: transparent;
    border: none;
    border-radius: 9px;
    padding: 10px 14px;
    text-align: left;
    color: {TEXT_DIM};
    font-size: 13px;
}}
QPushButton#NavButton:hover {{ background: {SURFACE}; color: {TEXT}; }}
QPushButton#NavButton:checked {{
    background: {ACCENT_SOFT};
    color: {ACCENT};
    font-weight: 700;
    border-left: 3px solid {ACCENT};
}}

QLineEdit, QSpinBox, QDoubleSpinBox, QComboBox, QPlainTextEdit, QTextEdit {{
    background: {BG_DEEP};
    border: 1px solid {BORDER};
    border-radius: 7px;
    padding: 5px 8px;
    selection-background-color: {ACCENT};
    selection-color: #1A1A2E;
}}
QLineEdit:focus, QSpinBox:focus, QDoubleSpinBox:focus, QComboBox:focus,
QPlainTextEdit:focus, QTextEdit:focus {{
    border-color: {ACCENT};
}}
QComboBox::drop-down {{ border: none; width: 18px; }}
QComboBox QAbstractItemView {{
    background: {SURFACE};
    border: 1px solid {BORDER_HI};
    selection-background-color: {ACCENT};
    selection-color: #1A1A2E;
}}

QCheckBox, QRadioButton {{ spacing: 7px; color: {TEXT}; }}
QCheckBox::indicator, QRadioButton::indicator {{ width: 15px; height: 15px; }}
QCheckBox::indicator {{ border: 1px solid {BORDER_HI}; border-radius: 4px; background: {BG_DEEP}; }}
QCheckBox::indicator:checked {{ background: {ACCENT}; border-color: {ACCENT}; }}
QRadioButton::indicator {{ border: 1px solid {BORDER_HI}; border-radius: 8px; background: {BG_DEEP}; }}
QRadioButton::indicator:checked {{ background: {ACCENT}; border: 4px solid {SURFACE}; }}

QListWidget, QTableWidget, QTreeWidget {{
    background: {BG_DEEP};
    border: 1px solid {BORDER};
    border-radius: 8px;
    padding: 4px;
}}
QListWidget::item {{ border-radius: 6px; padding: 4px; }}
QListWidget::item:selected {{ background: {ACCENT_SOFT}; color: {TEXT}; }}
QListWidget::item:hover {{ background: {SURFACE}; }}
QTableWidget::item:selected {{ background: {ACCENT_SOFT}; }}
QHeaderView::section {{
    background: {SURFACE};
    border: none;
    border-bottom: 1px solid {BORDER};
    padding: 6px;
    color: {TEXT_DIM};
}}

QScrollArea {{ border: none; background: transparent; }}
QScrollBar:vertical {{ background: transparent; width: 10px; margin: 2px; }}
QScrollBar::handle:vertical {{ background: {BORDER_HI}; border-radius: 5px; min-height: 30px; }}
QScrollBar::handle:vertical:hover {{ background: {ACCENT}; }}
QScrollBar:horizontal {{ background: transparent; height: 10px; margin: 2px; }}
QScrollBar::handle:horizontal {{ background: {BORDER_HI}; border-radius: 5px; min-width: 30px; }}
QScrollBar::add-line, QScrollBar::sub-line {{ height: 0; width: 0; }}
QScrollBar::add-page, QScrollBar::sub-page {{ background: transparent; }}

QTabWidget::pane {{ border: 1px solid {BORDER}; border-radius: 8px; }}
QTabBar::tab {{
    background: {SURFACE};
    border: 1px solid {BORDER};
    border-bottom: none;
    border-top-left-radius: 8px;
    border-top-right-radius: 8px;
    padding: 7px 14px;
    color: {TEXT_DIM};
    margin-right: 3px;
}}
QTabBar::tab:selected {{ background: {ACCENT_SOFT}; color: {ACCENT}; font-weight: 600; }}

QGroupBox {{
    border: 1px solid {BORDER};
    border-radius: 10px;
    margin-top: 14px;
    padding: 10px;
    color: {TEXT_DIM};
}}
QGroupBox::title {{ subcontrol-origin: margin; left: 12px; padding: 0 4px; }}

QToolTip {{
    background: {SURFACE_HI};
    color: {TEXT};
    border: 1px solid {BORDER_HI};
    border-radius: 6px;
    padding: 4px 8px;
}}

QProgressBar {{
    background: {BG_DEEP};
    border: 1px solid {BORDER};
    border-radius: 6px;
    text-align: center;
    height: 14px;
    color: {TEXT_DIM};
}}
QProgressBar::chunk {{ background: {ACCENT}; border-radius: 5px; }}

QMenu {{ background: {SURFACE}; border: 1px solid {BORDER_HI}; border-radius: 8px; padding: 4px; }}
QMenu::item {{ padding: 6px 18px; border-radius: 6px; }}
QMenu::item:selected {{ background: {ACCENT_SOFT}; color: {ACCENT}; }}

QStatusBar {{ background: {BG_DEEP}; color: {TEXT_DIM}; }}
QSplitter::handle {{ background: {BORDER}; }}
"""


def apply(app) -> None:
    """给 QApplication 套上主题。"""
    font = QFont("Microsoft YaHei UI", 9)
    font.setStyleStrategy(QFont.StyleStrategy.PreferAntialias)
    app.setFont(font)

    palette = QPalette()
    palette.setColor(QPalette.ColorRole.Window, QColor(BG))
    palette.setColor(QPalette.ColorRole.WindowText, QColor(TEXT))
    palette.setColor(QPalette.ColorRole.Base, QColor(BG_DEEP))
    palette.setColor(QPalette.ColorRole.Text, QColor(TEXT))
    palette.setColor(QPalette.ColorRole.Button, QColor(SURFACE_HI))
    palette.setColor(QPalette.ColorRole.ButtonText, QColor(TEXT))
    palette.setColor(QPalette.ColorRole.Highlight, QColor(ACCENT))
    palette.setColor(QPalette.ColorRole.HighlightedText, QColor("#1A1A2E"))
    palette.setColor(QPalette.ColorRole.ToolTipBase, QColor(SURFACE_HI))
    palette.setColor(QPalette.ColorRole.ToolTipText, QColor(TEXT))
    app.setPalette(palette)
    app.setStyle("Fusion")
    app.setStyleSheet(QSS)


STATE_COLORS = {
    "idle": TEXT_FAINT,
    "running": SUCCESS,
    "finished": INFO,
    "error": DANGER,
}
