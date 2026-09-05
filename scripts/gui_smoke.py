"""GUI 离屏冒烟测试: 验证主窗口能构造、站点对话框能打开、远端列表能刷新。

需要 QT_QPA_PLATFORM=offscreen。用法:
    .venv/Scripts/python.exe scripts/gui_smoke.py [--with-server]
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from PySide6.QtCore import QEventLoop, QTimer  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402


class Box:
    """可变的简单结果容器。"""


def main() -> int:
    app = QApplication(sys.argv)

    from ui.main_window import MainWindow  # noqa: E402
    from ui.site_dialogs import SiteEditDialog, SiteManagerDialog  # noqa: E402

    win = MainWindow()
    win.show()
    app.processEvents()
    print("OK 主窗口构造并显示:", win.windowTitle())

    dlg = SiteEditDialog(win)
    print("OK 站点编辑对话框构造")
    dlg.deleteLater()

    mgr = SiteManagerDialog(win)
    print("OK 站点管理对话框构造")
    mgr.deleteLater()

    if "--with-server" not in sys.argv:
        win.close()
        print("GUI 冒烟测试通过(未连服务器)")
        return 0

    from core.factory import create_session  # noqa: E402
    from core.models import SiteProfile  # noqa: E402

    box = Box()
    box.err = None
    box.done = False

    def capture_error(e: Exception) -> None:
        box.err = str(e)
        box.done = True

    win._show_err = capture_error  # 避免真实弹窗阻塞离屏事件循环

    site = SiteProfile(
        name="smoke", protocol="ftps", host="127.0.0.1",
        port=int(os.environ.get("FTP_PORT", "2121")),
        username="test", password="123456", insecure_tls=True)
    sess = create_session(site)
    sess.connect()
    win._session = sess
    win._set_connected(True)

    loop = QEventLoop()

    def poll():
        if box.err is not None or win.remote_model.rowCount() > 0:
            box.done = True
        if box.done:
            loop.quit()

    poll_timer = QTimer()
    poll_timer.timeout.connect(poll)
    poll_timer.start(50)

    timeout_timer = QTimer()
    timeout_timer.setSingleShot(True)
    timeout_timer.timeout.connect(lambda: setattr(box, "done", True))
    timeout_timer.start(8000)

    win._go_remote_path("/")
    loop.exec()

    rows = win.remote_model.rowCount()
    if box.err:
        print(f"远端列表出错: {box.err}")
    print(f"远端列表刷新, 行数={rows}, 状态栏={win.status_label.text()}")
    sess.close()
    win._session = None
    win.close()
    assert rows > 0 and box.err is None, "远端列表未刷新成功"
    print("GUI 冒烟测试通过")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
