"""FTP 共享客户端入口。

用法:  .venv/Scripts/python.exe main.py
"""
from __future__ import annotations

import sys


def main() -> int:
    from PySide6.QtWidgets import QApplication
    from ui.main_window import MainWindow

    app = QApplication(sys.argv)
    app.setApplicationName("FTP Sharing")
    app.setOrganizationName("FTPSharing")

    win = MainWindow()
    win.show()
    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())
