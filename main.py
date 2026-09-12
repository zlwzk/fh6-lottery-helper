"""FH6 抽奖助手 —— 启动入口。

运行：python main.py
打包：powershell -ExecutionPolicy Bypass -File scripts/build.ps1
"""

from __future__ import annotations

import ctypes
import sys
import traceback

APP_USER_MODEL_ID = "zlwzk.fh6lottery"
ERROR_ALREADY_EXISTS = 183


def _single_instance() -> bool:
    """返回 True 表示这是第一个实例。"""
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.CreateMutexW.restype = ctypes.c_void_p
    kernel32.CreateMutexW.argtypes = [ctypes.c_void_p, ctypes.c_bool, ctypes.c_wchar_p]
    handle = kernel32.CreateMutexW(None, False, "Global\\FH6LotteryHelperSingleInstance")
    if not handle:
        return True
    return ctypes.get_last_error() != ERROR_ALREADY_EXISTS


def main() -> int:
    from fh6lottery import paths, winutil

    winutil.enable_dpi_awareness()
    try:
        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(APP_USER_MODEL_ID)
    except Exception:
        pass

    duplicated = not _single_instance()

    from PySide6.QtWidgets import QApplication, QMessageBox

    app = QApplication(sys.argv)
    app.setApplicationName(paths.APP_DISPLAY)
    app.setApplicationDisplayName(paths.APP_DISPLAY)
    app.setOrganizationName("FH6LotteryHelper")

    from fh6lottery.context import AppContext
    from fh6lottery.ui import theme
    from fh6lottery.ui.main_window import MainWindow, app_icon

    theme.apply(app)
    app.setWindowIcon(app_icon())

    ctx = AppContext()

    if duplicated:
        QMessageBox.information(None, paths.APP_DISPLAY,
                                "助手已经在运行了：请在任务栏或托盘里找到它的窗口。")
        return 0

    def _hook(exc_type, exc_value, exc_tb):
        detail = "".join(traceback.format_exception(exc_type, exc_value, exc_tb))
        try:
            # 崩溃日志经常被贴进公开的 issue，落盘前先把本机路径和用户名折掉
            (paths.LOG_DIR / "crash.log").write_text(paths.sanitize_text(detail),
                                                     encoding="utf-8")
        except OSError:
            pass
        try:
            QMessageBox.critical(None, f"{paths.APP_DISPLAY} 出错了",
                                 f"发生了未处理的错误：\n{exc_value}\n\n"
                                 f"详情已写入日志目录（{paths.display_path(paths.LOG_DIR)}）")
        except Exception:
            pass
        sys.__excepthook__(exc_type, exc_value, exc_tb)

    sys.excepthook = _hook

    window = MainWindow(ctx)
    window.show()
    return app.exec()


if __name__ == "__main__":
    sys.exit(main())
