"""网页分享对话框: 选一个本地文件夹, 一键在本机起 Web 分享服务。

把 scripts.lan_share 的服务端(纯标准库)嵌进桌面客户端: 开始分享后
自动列出局域网 / Tailscale 访问地址, 可一键复制发给对方; 对方用浏览器
即可下载(文件夹可整包 ZIP), 也支持反向上传(可关闭)。
"""
from __future__ import annotations

import threading
from http.server import ThreadingHTTPServer
from pathlib import Path

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QApplication,
    QCheckBox,
    QDialog,
    QFileDialog,
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPlainTextEdit,
    QPushButton,
    QSpinBox,
    QVBoxLayout,
)

from scripts.lan_share import lan_ips, make_handler, tailscale_ips


class ShareDialog(QDialog):
    """“分享本机文件夹”对话框(内嵌 lan_share Web 服务)。"""

    def __init__(self, parent=None, default_dir: str | None = None):
        super().__init__(parent)
        self.setWindowTitle("网页分享文件夹")
        self.setMinimumWidth(560)
        self._httpd: ThreadingHTTPServer | None = None
        self._thread: threading.Thread | None = None
        self._running = False

        folder = default_dir if default_dir else str(Path.home())
        if not Path(folder).is_dir():
            folder = str(Path.home())

        intro = QLabel(
            "把某个本地文件夹临时变成网页分享: 别人浏览器打开下面的网址就能"
            "下载里面的文件(文件夹可整包 ZIP 下载), 也可以反向传文件给你。")
        intro.setWordWrap(True)

        self.ed_folder = QLineEdit(folder)
        self.ed_folder.setReadOnly(True)
        btn_pick = QPushButton("选择文件夹…")
        btn_pick.clicked.connect(self._pick_folder)

        self.sp_port = QSpinBox()
        self.sp_port.setRange(0, 65535)
        self.sp_port.setValue(8000)
        self.sp_port.setSpecialValueText("自动(随机端口)")
        self.sp_port.setToolTip("0 表示随机选一个空闲端口")

        self.ed_pwd = QLineEdit()
        self.ed_pwd.setPlaceholderText("留空 = 不设密码(同一网络内任何人都能访问)")
        self.ed_pwd.setEchoMode(QLineEdit.EchoMode.Password)

        self.chk_upload = QCheckBox("允许别人上传文件到该文件夹")
        self.chk_upload.setChecked(True)

        form = QFormLayout()
        form.addRow("分享文件夹:", self._row(self.ed_folder, btn_pick))
        form.addRow("端口:", self.sp_port)
        form.addRow("访问密码:", self.ed_pwd)
        form.addRow("", self.chk_upload)

        self.btn_start = QPushButton("开始分享")
        self.btn_start.clicked.connect(self._start)
        self.btn_stop = QPushButton("停止分享")
        self.btn_stop.clicked.connect(self._stop)
        self.btn_stop.setEnabled(False)
        btn_row = QHBoxLayout()
        btn_row.addWidget(self.btn_start)
        btn_row.addWidget(self.btn_stop)
        btn_row.addStretch(1)

        self.te_links = QPlainTextEdit()
        self.te_links.setReadOnly(True)
        self.te_links.setPlaceholderText(
            "点“开始分享”后, 这里会出现可以发给对方的网址…")
        self.btn_copy = QPushButton("复制链接")
        self.btn_copy.setEnabled(False)
        self.btn_copy.clicked.connect(self._copy_links)

        self.status = QLabel("未分享")
        self.status.setStyleSheet("color:#666")

        lay = QVBoxLayout(self)
        lay.addWidget(intro)
        lay.addLayout(form)
        lay.addLayout(btn_row)
        lay.addWidget(self.te_links, 1)
        copy_row = QHBoxLayout()
        copy_row.addWidget(self.btn_copy)
        copy_row.addWidget(self.status)
        copy_row.addStretch(1)
        lay.addLayout(copy_row)

    # ---------- 工具 ----------
    @staticmethod
    def _row(*widgets):
        row = QHBoxLayout()
        for w in widgets:
            row.addWidget(w)
        return row

    def _pick_folder(self) -> None:
        d = QFileDialog.getExistingDirectory(
            self, "选择要分享的本地文件夹", self.ed_folder.text())
        if d:
            self.ed_folder.setText(d)

    # ---------- 开始/停止 ----------
    def _set_running(self, running: bool) -> None:
        self._running = running
        self.btn_start.setEnabled(not running)
        self.btn_stop.setEnabled(running)
        self.ed_pwd.setEnabled(not running)
        self.sp_port.setEnabled(not running)
        self.chk_upload.setEnabled(not running)
        self.btn_copy.setEnabled(running)
        self.status.setText("分享运行中" if running else "未分享")

    def _start(self) -> None:
        if self._running:
            return
        folder = Path(self.ed_folder.text().strip())
        if not folder.is_dir():
            QMessageBox.warning(self, "网页分享", "请先选择一个存在的文件夹。")
            self._pick_folder()
            return
        if self.chk_upload.isChecked() and not self.ed_pwd.text():
            box = QMessageBox(self)
            box.setWindowTitle("未设密码")
            box.setText(
                "你允许别人上传文件, 但没有设置访问密码。\n"
                "同一网络里的任何人都能读写这个文件夹, 确定继续?")
            b_yes = box.addButton("无密码继续",
                                  QMessageBox.ButtonRole.AcceptRole)
            box.addButton("去设密码", QMessageBox.ButtonRole.RejectRole)
            box.exec()
            if box.clickedButton() != b_yes:
                return
        port = self.sp_port.value()
        pwd = self.ed_pwd.text().strip() or None
        try:
            handler = make_handler(folder, pwd,
                                   allow_upload=self.chk_upload.isChecked())
            httpd = ThreadingHTTPServer(("0.0.0.0", port), handler)
        except OSError as e:
            QMessageBox.warning(self, "网页分享", f"启动失败: {e}")
            return
        self._httpd = httpd
        self._thread = threading.Thread(target=httpd.serve_forever,
                                        name="lan-share", daemon=True)
        self._thread.start()

        lines = []
        port = httpd.server_address[1]
        for ip in lan_ips():
            lines.append(f"http://{ip}:{port}/     (局域网)")
        if not lines:
            lines.append(f"http://127.0.0.1:{port}/     (本机自测)")
        for ip in tailscale_ips():
            lines.append(f"http://{ip}:{port}/     (Tailscale 组网)")
        if not pwd:
            lines.append("")
            lines.append("注意: 未设密码, 任何人都能访问")
        if not self.chk_upload.isChecked():
            lines.append("只读分享(已关闭上传)")
        self.te_links.setPlainText("\n".join(lines))
        self.status.setText(f"分享中: {folder}  →  端口 {port}")
        self._set_running(True)

    def _stop(self) -> None:
        httpd, self._httpd = self._httpd, None
        if httpd is not None:
            try:
                httpd.shutdown()
                httpd.server_close()
            except OSError:
                pass
        if self._thread is not None:
            self._thread.join(timeout=2)
            self._thread = None
        self._set_running(False)
        self.status.setText("已停止分享")

    def _copy_links(self) -> None:
        text = self.te_links.toPlainText().strip()
        if not text:
            return
        QApplication.clipboard().setText(text)
        self.status.setText("链接已复制到剪贴板")

    def closeEvent(self, event) -> None:
        if self._running:
            ans = QMessageBox.question(
                self, "网页分享", "分享服务仍在运行。关闭窗口会停止分享, 确定?",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.No)
            if ans != QMessageBox.StandardButton.Yes:
                event.ignore()
                return
            self._stop()
        super().closeEvent(event)
