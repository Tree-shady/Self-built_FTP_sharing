"""站点管理：新增/编辑/删除站点配置的对话框。"""
from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFormLayout,
    QGroupBox,
    QHBoxLayout,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMessageBox,
    QPushButton,
    QSpinBox,
    QVBoxLayout,
)

from core.models import ENCODINGS, SiteProfile
from core.storage import load_sites, save_sites

_PROTO_ITEMS = (("FTPS (FTP over TLS)", "ftps"), ("SFTP (SSH)", "sftp"))

_ENC_LABELS = {
    "auto": "自动(按 UTF-8)",
    "utf-8": "UTF-8",
    "gbk": "GBK",
    "gb18030": "GB18030",
    "latin-1": "Latin-1",
}


class SiteEditDialog(QDialog):
    """新增或编辑一个站点。"""

    def __init__(self, parent=None, site: SiteProfile | None = None):
        super().__init__(parent)
        self._site = site
        self.setWindowTitle("编辑站点" if site else "新建站点")
        self.setMinimumWidth(430)

        # ---------- 基本信息 ----------
        self.name_edit = QLineEdit(site.name if site else "")
        self.name_edit.setPlaceholderText("例如: 家里服务器")

        self.proto_combo = QComboBox()
        for label, value in _PROTO_ITEMS:
            self.proto_combo.addItem(label, value)
        proto = (site.protocol if site else "ftps")
        idx = self.proto_combo.findData(proto)
        self.proto_combo.setCurrentIndex(max(0, idx))

        self.host_edit = QLineEdit(site.host if site else "")
        self.host_edit.setPlaceholderText("IP 或域名(可用 Tailscale 组网后的地址)")

        default_port = site.port if site else (22 if proto == "sftp" else 21)
        self.port_spin = QSpinBox()
        self.port_spin.setRange(1, 65535)
        self.port_spin.setValue(default_port)

        self.user_edit = QLineEdit(site.username if site else "")
        self.user_edit.setPlaceholderText("留空表示匿名(仅部分服务器允许)")

        self.pass_edit = QLineEdit(site.password if site else "")
        self.pass_edit.setEchoMode(QLineEdit.EchoMode.Password)

        self.save_pass_check = QCheckBox(
            "保存密码(明文存放在本机配置文件, 建议装 keyring 后改为系统钥匙串)")
        self.save_pass_check.setChecked(site.save_password if site else True)
        self.save_pass_check.toggled.connect(self.pass_edit.setEnabled)

        basic = QFormLayout()
        basic.addRow("站点名称:", self.name_edit)
        basic.addRow("协议:", self.proto_combo)
        basic.addRow("主机:", self.host_edit)
        basic.addRow("端口:", self.port_spin)
        basic.addRow("用户名:", self.user_edit)
        basic.addRow("密码:", self.pass_edit)
        basic.addRow("", self.save_pass_check)

        # ---------- FTPS 高级 ----------
        self.enc_combo = QComboBox()
        for code in ENCODINGS:
            self.enc_combo.addItem(_ENC_LABELS.get(code, code), code)
        enc = site.encoding if site else "auto"
        self.enc_combo.setCurrentIndex(max(0, self.enc_combo.findData(enc)))

        self.passive_check = QCheckBox("使用被动模式(PASV, 默认推荐)")
        self.passive_check.setChecked(site.passive if site else True)

        self.insecure_check = QCheckBox("不校验服务器证书(仅自签名/本地测试用)")
        self.insecure_check.setChecked(site.insecure_tls if site else False)

        self.ftps_box = QGroupBox("FTPS 选项")
        fl = QFormLayout(self.ftps_box)
        fl.addRow("文件名编码:", self.enc_combo)
        fl.addRow("", self.passive_check)
        fl.addRow("", self.insecure_check)

        # ---------- SFTP 高级 ----------
        self.auto_key_check = QCheckBox("首次连接自动信任主机密钥(TOFU)")
        self.auto_key_check.setChecked(site.auto_accept_key if site else True)

        self.sftp_box = QGroupBox("SFTP 选项")
        sfl = QFormLayout(self.sftp_box)
        sfl.addRow("", self.auto_key_check)

        # ---------- 通用高级 ----------
        self.timeout_spin = QSpinBox()
        self.timeout_spin.setRange(5, 600)
        self.timeout_spin.setSuffix(" 秒")
        self.timeout_spin.setValue(site.timeout if site else 30)

        self.root_edit = QLineEdit(site.remote_root if site else "/")
        self.root_edit.setPlaceholderText("/")

        self.common_box = QGroupBox("通用")
        cl = QFormLayout(self.common_box)
        cl.addRow("连接超时:", self.timeout_spin)
        cl.addRow("起始目录:", self.root_edit)

        # ---------- 按钮 ----------
        self.button_box = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        self.button_box.accepted.connect(self._on_accept)
        self.button_box.rejected.connect(self.reject)

        lay = QVBoxLayout(self)
        lay.addLayout(basic)
        lay.addWidget(self.ftps_box)
        lay.addWidget(self.sftp_box)
        lay.addWidget(self.common_box)
        lay.addWidget(self.button_box)

        self.proto_combo.currentIndexChanged.connect(self._sync_protocol)
        self._sync_protocol()
        self.pass_edit.setEnabled(self.save_pass_check.isChecked())

    def _sync_protocol(self) -> None:
        proto = self.proto_combo.currentData()
        is_ftps = proto == "ftps"
        self.ftps_box.setVisible(is_ftps)
        self.sftp_box.setVisible(not is_ftps)
        # 切换协议时自动换默认端口(仅当端口还是另一个协议的默认值)
        cur = self.port_spin.value()
        if proto == "sftp" and cur == 21:
            self.port_spin.setValue(22)
        elif proto == "ftps" and cur == 22:
            self.port_spin.setValue(21)

    def _on_accept(self) -> None:
        if not self.name_edit.text().strip():
            QMessageBox.warning(self, "提示", "请填写站点名称。")
            return
        if not self.host_edit.text().strip():
            QMessageBox.warning(self, "提示", "请填写服务器主机地址。")
            return
        self.accept()

    def site(self) -> SiteProfile:
        data = dict(
            name=self.name_edit.text().strip() or "未命名",
            protocol=self.proto_combo.currentData(),
            host=self.host_edit.text().strip(),
            port=self.port_spin.value(),
            username=self.user_edit.text().strip(),
            encoding=self.enc_combo.currentData(),
            passive=self.passive_check.isChecked(),
            insecure_tls=self.insecure_check.isChecked(),
            auto_accept_key=self.auto_key_check.isChecked(),
            save_password=self.save_pass_check.isChecked(),
            timeout=self.timeout_spin.value(),
            remote_root=self.root_edit.text().strip() or "/",
        )
        if self.save_pass_check.isChecked():
            data["password"] = self.pass_edit.text()
        else:
            data["password"] = ""
        if self._site is not None:
            data["id"] = self._site.id
        return SiteProfile(**data)


class SiteManagerDialog(QDialog):
    """站点列表的增删改管理。"""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("站点管理")
        self.resize(480, 320)
        self._sites: list[SiteProfile] = load_sites()

        self.list_widget = QListWidget()
        self.list_widget.itemDoubleClicked.connect(lambda _i: self._edit())

        btn_new = QPushButton("新建…")
        btn_edit = QPushButton("编辑…")
        btn_del = QPushButton("删除")
        btn_close = QPushButton("完成")
        btn_new.clicked.connect(self._add)
        btn_edit.clicked.connect(self._edit)
        btn_del.clicked.connect(self._delete)
        btn_close.clicked.connect(self.accept)

        btn_col = QVBoxLayout()
        for b in (btn_new, btn_edit, btn_del):
            btn_col.addWidget(b)
        btn_col.addStretch(1)
        btn_col.addWidget(btn_close)

        lay = QHBoxLayout(self)
        lay.addWidget(self.list_widget, 1)
        lay.addLayout(btn_col)
        self._refresh()

    # ---------- 内部 ----------
    def _refresh(self) -> None:
        self.list_widget.clear()
        for s in self._sites:
            item = QListWidgetItem(s.display())
            item.setData(256, s.id)
            self.list_widget.addItem(item)
        if not self._sites:
            item = QListWidgetItem('(还没有站点, 点击"新建…"添加)')
            item.setFlags(Qt.ItemFlag.ItemIsEnabled)  # 灰显占位, 不可选
            self.list_widget.addItem(item)

    def _current_index(self) -> int | None:
        row = self.list_widget.currentRow()
        if 0 <= row < len(self._sites):
            return row
        return None

    def _add(self) -> None:
        dlg = SiteEditDialog(self)
        if dlg.exec() == QDialog.DialogCode.Accepted:
            self._sites.append(dlg.site())
            self._persist()
            self._refresh()
            self.list_widget.setCurrentRow(len(self._sites) - 1)

    def _edit(self) -> None:
        idx = self._current_index()
        if idx is None:
            return
        dlg = SiteEditDialog(self, self._sites[idx])
        if dlg.exec() == QDialog.DialogCode.Accepted:
            self._sites[idx] = dlg.site()
            self._persist()
            self._refresh()
            self.list_widget.setCurrentRow(idx)

    def _delete(self) -> None:
        idx = self._current_index()
        if idx is None:
            return
        s = self._sites[idx]
        if QMessageBox.question(
                self, "删除站点", f'确定删除站点 "{s.name}"?',
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.No) != QMessageBox.StandardButton.Yes:
            return
        del self._sites[idx]
        self._persist()
        self._refresh()

    def _persist(self) -> None:
        save_sites(self._sites)
