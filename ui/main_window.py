"""主窗口：双栏文件浏览器 + 连接管理 + 单文件/整目录(递归)上传下载。

线程约定: 一切与远端会话的交互都通过 ui.worker.run_async 放到线程池,
避免阻塞界面；进度/文字经 Qt 信号回到界面线程。
"""
from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QAbstractItemView,
    QComboBox,
    QFileDialog,
    QFileSystemModel,
    QHBoxLayout,
    QHeaderView,
    QInputDialog,
    QLabel,
    QMainWindow,
    QMenu,
    QMessageBox,
    QProgressBar,
    QPushButton,
    QSplitter,
    QTableView,
    QToolButton,
    QTreeView,
    QVBoxLayout,
    QWidget,
)

from core.errors import RemoteError, TransferCancelled
from core.factory import create_session
from core.models import ENCODINGS, RemoteEntry, SiteProfile
from core.paths import rjoin, rname, rnorm, rparent, sanitize_windows_name
from core.storage import load_sites
from core.tree import download_tree, upload_tree
from ui.remote_model import RemoteTableModel
from ui.site_dialogs import SiteManagerDialog
from ui.worker import Task, run_async


class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("FTP 共享客户端")
        self.resize(1180, 680)

        self._sites: list[SiteProfile] = load_sites()
        self._session = None
        self._remote_path = "/"
        self._local_dir = Path.home()
        self._task: Task | None = None
        self._busy = False

        # ================= 顶部: 站点选择 =================
        self.site_combo = QComboBox()
        self.site_combo.setMinimumWidth(320)
        self.site_combo.currentIndexChanged.connect(self._on_site_changed)

        btn_manage = QPushButton("站点管理…")
        btn_manage.clicked.connect(self._open_site_manager)
        self.btn_connect = QPushButton("连接")
        self.btn_connect.clicked.connect(self._on_connect)
        self.btn_disconnect = QPushButton("断开")
        self.btn_disconnect.clicked.connect(self._on_disconnect)
        self.btn_disconnect.setEnabled(False)

        self.enc_btn = QToolButton()
        self.enc_btn.setText("编码 ▾")
        self.enc_btn.setPopupMode(QToolButton.ToolButtonPopupMode.InstantPopup)
        self.enc_menu = QMenu(self)
        for code in ENCODINGS:
            act = self.enc_menu.addAction(self._enc_label(code))
            act.setCheckable(True)
            act.triggered.connect(lambda _c=False, c=code: self._on_encoding(c))
        self.enc_btn.setMenu(self.enc_menu)

        btn_share = QPushButton("分享到网页…")
        btn_share.setToolTip("把本机某个文件夹一键变成网页分享, 别人用浏览器就能下载/上传")
        btn_share.clicked.connect(self._on_share_folder)

        top = QHBoxLayout()
        top.addWidget(QLabel("站点:"))
        top.addWidget(self.site_combo, 1)
        top.addWidget(btn_manage)
        top.addWidget(self.btn_connect)
        top.addWidget(self.btn_disconnect)
        top.addWidget(self.enc_btn)
        top.addWidget(btn_share)
        top.addStretch(1)

        # ================= 中部: 左右分栏 =================
        self._build_local_pane()
        self._build_remote_pane()

        splitter = QSplitter(Qt.Orientation.Horizontal)
        splitter.addWidget(self.local_pane)
        splitter.addWidget(self.remote_pane)
        splitter.setStretchFactor(0, 1)
        splitter.setStretchFactor(1, 2)
        splitter.setSizes([420, 720])

        # ================= 传输操作行 =================
        self.btn_upload = QPushButton("↑ 上传文件…")
        self.btn_upload.clicked.connect(self._on_upload)
        self.btn_upload_dir = QPushButton("↑ 上传文件夹…")
        self.btn_upload_dir.clicked.connect(self._on_upload_dir)
        self.btn_download = QPushButton("↓ 下载所选")
        self.btn_download.clicked.connect(self._on_download)
        self.btn_refresh = QPushButton("刷新")
        self.btn_refresh.clicked.connect(self._refresh_remote)
        self.btn_mkdir = QPushButton("新建目录")
        self.btn_mkdir.clicked.connect(self._on_mkdir)
        self.btn_delete = QPushButton("删除")
        self.btn_delete.clicked.connect(self._on_delete)

        actions = QHBoxLayout()
        actions.addWidget(self.btn_download)
        actions.addWidget(self.btn_upload)
        actions.addWidget(self.btn_upload_dir)
        actions.addStretch(1)
        actions.addWidget(self.btn_mkdir)
        actions.addWidget(self.btn_delete)
        actions.addWidget(self.btn_refresh)

        central = QWidget()
        lay = QVBoxLayout(central)
        lay.addLayout(top)
        lay.addWidget(splitter, 1)
        lay.addLayout(actions)
        self.setCentralWidget(central)

        # ================= 状态区 =================
        self.status_label = QLabel("就绪")
        self.progress = QProgressBar()
        self.progress.setRange(0, 1)
        self.progress.setValue(0)
        self.progress.setTextVisible(False)
        self.progress.setFixedWidth(260)
        self.btn_cancel = QPushButton("取消")
        self.btn_cancel.clicked.connect(self._on_cancel)
        self.btn_cancel.setEnabled(False)
        sb = self.statusBar()
        sb.addWidget(self.status_label, 1)
        sb.addPermanentWidget(self.progress)
        sb.addPermanentWidget(self.btn_cancel)

        self._reload_site_combo()
        self._set_local_dir(Path.home())
        self._set_busy(False)
        self._set_connected(False)

    # =====================================================
    #  UI 构件
    # =====================================================
    def _build_local_pane(self) -> None:
        pane = QWidget()
        self.local_path_label = QLabel()
        self.local_path_label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        btn_up = QPushButton("⇪")
        btn_up.setToolTip("上一级目录")
        btn_up.setFixedWidth(34)
        btn_up.clicked.connect(lambda: self._go_local_parent())
        btn_pick = QPushButton("选择文件夹…")
        btn_pick.clicked.connect(self._pick_local_dir)

        bar = QHBoxLayout()
        bar.addWidget(QLabel("本机:"))
        bar.addWidget(self.local_path_label, 1)
        bar.addWidget(btn_pick)

        self.fs_model = QFileSystemModel(self)
        self.fs_model.setRootPath(str(Path.home()))
        self.local_view = QTreeView()
        self.local_view.setModel(self.fs_model)
        self.local_view.setRootIsDecorated(True)
        self.local_view.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.local_view.doubleClicked.connect(self._on_local_double)

        lay = QVBoxLayout(pane)
        lay.setContentsMargins(6, 6, 6, 6)
        lay.addLayout(bar)
        lay.addWidget(self.local_view, 1)
        self.local_pane = pane

    def _build_remote_pane(self) -> None:
        pane = QWidget()
        self.remote_path_label = QLabel("/")
        self.remote_path_label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)

        self.remote_model = RemoteTableModel(self)
        self.remote_view = QTableView()
        self.remote_view.setModel(self.remote_model)
        self.remote_view.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.remote_view.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        self.remote_view.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.remote_view.verticalHeader().setVisible(False)
        self.remote_view.horizontalHeader().setSectionResizeMode(
            0, QHeaderView.ResizeMode.Stretch)
        self.remote_view.horizontalHeader().setSectionResizeMode(
            1, QHeaderView.ResizeMode.ResizeToContents)
        self.remote_view.horizontalHeader().setSectionResizeMode(
            2, QHeaderView.ResizeMode.ResizeToContents)
        self.remote_view.horizontalHeader().setSectionResizeMode(
            3, QHeaderView.ResizeMode.ResizeToContents)
        self.remote_view.doubleClicked.connect(self._on_remote_double)

        bar = QHBoxLayout()
        bar.addWidget(QLabel("远端:"))
        bar.addWidget(self.remote_path_label, 1)

        lay = QVBoxLayout(pane)
        lay.setContentsMargins(6, 6, 6, 6)
        lay.addLayout(bar)
        lay.addWidget(self.remote_view, 1)
        self.remote_pane = pane

    # =====================================================
    #  状态
    # =====================================================
    def _reload_site_combo(self) -> None:
        keep = self.site_combo.currentData()
        self.site_combo.blockSignals(True)
        self.site_combo.clear()
        self._sites = load_sites()
        idx = 0
        for i, s in enumerate(self._sites):
            self.site_combo.addItem(s.name, s.id)
            if s.id == keep:
                idx = i
        self.site_combo.blockSignals(False)
        if self._sites:
            self.site_combo.setCurrentIndex(idx)

    def _current_site(self) -> SiteProfile | None:
        sid = self.site_combo.currentData()
        for s in self._sites:
            if s.id == sid:
                return s
        return None

    def _on_site_changed(self) -> None:
        if not self._busy and not self._session:
            self._set_busy(False)  # 重新评估按钮状态

    def _set_busy(self, busy: bool, hint: str = "") -> None:
        self._busy = busy
        self.btn_connect.setEnabled(not busy and self._session is None and self._current_site() is not None)
        self.btn_disconnect.setEnabled(not busy and self._session is not None)
        self.btn_download.setEnabled(not busy and self._session is not None)
        self.btn_upload.setEnabled(not busy and self._session is not None)
        self.btn_upload_dir.setEnabled(not busy and self._session is not None)
        self.btn_refresh.setEnabled(not busy and self._session is not None)
        self.btn_mkdir.setEnabled(not busy and self._session is not None)
        self.btn_delete.setEnabled(not busy and self._session is not None)
        self.enc_btn.setEnabled(not busy and self._session is not None)
        if busy:
            self.btn_cancel.setEnabled(True)
        else:
            self.btn_cancel.setEnabled(False)
            self._task = None
        if hint:
            self.status_label.setText(hint)
        self.setWindowTitle(("忙碌中 - " if busy else "") + self._title_text())

    def _title_text(self) -> str:
        if self._session is not None:
            s = self._session.site
            return f"FTP 共享客户端 - {s.name} [{s.protocol.upper()}]"
        return "FTP 共享客户端"

    def _set_connected(self, connected: bool) -> None:
        self.btn_connect.setEnabled(not connected and not self._busy and self._current_site() is not None)
        self.btn_disconnect.setEnabled(connected and not self._busy)
        if not connected:
            self.remote_model.set_entries([], show_parent=False)
            self.remote_path_label.setText("未连接")

    def _show_err(self, e: Exception) -> None:
        from core.errors import TransferCancelled
        if isinstance(e, TransferCancelled):
            self.status_label.setText("已取消")
            return
        if isinstance(e, RemoteError):
            QMessageBox.warning(self, "操作失败", str(e))
        else:
            QMessageBox.critical(self, "错误", f"{type(e).__name__}: {e}")
        self.status_label.setText("出错了, 详见弹窗")

    # =====================================================
    #  站点
    # =====================================================
    def _open_site_manager(self) -> None:
        SiteManagerDialog(self).exec()
        self._reload_site_combo()

    def _on_connect(self) -> None:
        site = self._current_site()
        if site is None:
            QMessageBox.information(self, "提示", '请先在"站点管理"里添加站点。')
            self._open_site_manager()
            return
        self._set_busy(True, f"正在连接 {site.host}:{site.port} …")

        def job(ctx):
            sess = create_session(site)
            sess.connect()
            return sess

        def ok(sess):
            self._session = sess
            self._remote_path = rnorm(sess.site.remote_root)
            self._set_connected(True)
            self._set_busy(False, f"已连接 {site.display()}")
            self._refresh_remote()

        run_async(job, on_success=ok, on_error=self._on_connect_error)

    def _on_connect_error(self, e: Exception) -> None:
        self._set_busy(False, "连接失败")
        self._show_err(e)

    def _on_disconnect(self) -> None:
        if self._session is None:
            return
        sess, self._session = self._session, None
        self._set_connected(False)

        def job(ctx):
            sess.close()
            return None

        run_async(job, on_success=lambda _r: self.status_label.setText("已断开"),
                  on_error=lambda e: self.status_label.setText(f"断开时出错: {e}"))

    # =====================================================
    #  远端浏览
    # =====================================================
    def _refresh_remote(self, _idx=None) -> None:
        sess = self._session
        if sess is None or self._busy:
            return
        path = self._remote_path
        self._set_busy(True, f"正在读取 {path} …")

        def job(ctx):
            return sess.listdir(path)

        def ok(entries: list[RemoteEntry]):
            self._set_busy(False, f"{path}  共 {len(entries)} 项")
            self.remote_path_label.setText(self._display_remote_path(path))
            self.remote_model.set_entries(entries, show_parent=(path != "/"))
            self.remote_view.clearSelection()

        run_async(job, on_success=ok, on_error=self._on_browse_error)

    def _on_browse_error(self, e: Exception) -> None:
        self._set_busy(False, "读取目录失败")
        self._show_err(e)

    def _display_remote_path(self, path: str) -> str:
        s = self._session.site if self._session else None
        head = f"{s.protocol}://{s.host}" if s else ""
        return f"{head}{path}"

    def _go_remote_path(self, path: str) -> None:
        self._remote_path = rnorm(path)
        self._refresh_remote()

    def _on_remote_double(self, index) -> None:
        if self._busy or self._session is None:
            return
        entry = self.remote_model.entry_at(index.row())
        if entry is None:
            return
        if entry.name == "..":
            self._go_remote_path(rparent(self._remote_path))
        elif entry.is_dir:
            self._go_remote_path(rjoin(self._remote_path, entry.name))

    # =====================================================
    #  本机浏览
    # =====================================================
    def _set_local_dir(self, p: Path) -> None:
        try:
            p = p.resolve()
            if not p.is_dir():
                p = p.parent
        except OSError:
            return
        self._local_dir = p
        self.fs_model.setRootPath(str(p))
        self.local_view.setRootIndex(self.fs_model.index(str(p)))
        self.local_path_label.setText(str(p))

    def _go_local_parent(self) -> None:
        parent = self._local_dir.parent
        if parent != self._local_dir:
            self._set_local_dir(parent)

    def _pick_local_dir(self) -> None:
        d = QFileDialog.getExistingDirectory(
            self, "选择本地文件夹(下载目标)", str(self._local_dir))
        if d:
            self._set_local_dir(Path(d))

    def _on_local_double(self, index) -> None:
        p = Path(self.fs_model.filePath(index))
        if p.is_dir():
            self._set_local_dir(p)

    # =====================================================
    #  目录操作 / 编码
    # =====================================================
    def _on_mkdir(self) -> None:
        if self._session is None:
            return
        name, okp = QInputDialog.getText(self, "新建目录", "目录名:")
        if not okp or not name.strip():
            return
        target = rjoin(self._remote_path, name.strip())
        self._set_busy(True, f"新建目录 {target} …")

        def job(ctx):
            self._session.mkdir(target)
            return None

        def ok(_r):
            self._set_busy(False, "目录已创建")
            self._refresh_remote()

        run_async(job, on_success=ok, on_error=self._after_fail_refresh)

    def _on_delete(self) -> None:
        sess = self._session
        if sess is None:
            return
        entries = self._selected_remote_entries()
        entries = [e for e in entries if e.name != ".."]
        if not entries:
            QMessageBox.information(self, "提示", "请先在右侧选中要删除的文件或目录。")
            return
        names = ", ".join(e.name for e in entries[:5]) + ("…" if len(entries) > 5 else "")
        if QMessageBox.question(self, "删除", f"确定删除这 {len(entries)} 项?\n{names}",
                                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                                QMessageBox.StandardButton.No) != QMessageBox.StandardButton.Yes:
            return
        self._set_busy(True, "删除中 …")

        def job(ctx):
            for e in entries:
                if e.is_dir:
                    sess.rmdir(rjoin(self._remote_path, e.name))
                else:
                    sess.remove(rjoin(self._remote_path, e.name))
            return len(entries)

        def ok(n):
            self._set_busy(False, f"已删除 {n} 项")
            self._refresh_remote()

        run_async(job, on_success=ok, on_error=self._after_fail_refresh)

    def _after_fail_refresh(self, e: Exception) -> None:
        self._set_busy(False, "操作失败")
        self._show_err(e)
        self._refresh_remote()

    def _on_encoding(self, code: str) -> None:
        sess = self._session
        if sess is None or sess.protocol != "ftps":
            return
        self._refresh_encoding_menu(code)
        setter = getattr(sess, "set_encoding", None)
        if setter is not None:
            setter(code)
        self.status_label.setText(f"文件名编码: {self._enc_label(code)}")
        self._refresh_remote()

    def _refresh_encoding_menu(self, active: str | None) -> None:
        for act in self.enc_menu.actions():
            act.setChecked(act.text() == self._enc_label(active))

    def _enc_label(self, code: str) -> str:
        return {
            "auto": "自动(按 UTF-8)", "utf-8": "UTF-8", "gbk": "GBK",
            "gb18030": "GB18030", "latin-1": "Latin-1",
        }.get(code, code)

    # =====================================================
    #  传输
    # =====================================================
    def _selected_remote_entries(self) -> list[RemoteEntry]:
        out = []
        for idx in self.remote_view.selectionModel().selectedRows():
            e = self.remote_model.entry_at(idx.row())
            if e:
                out.append(e)
        return out

    def _on_download(self) -> None:
        sess = self._session
        if sess is None or self._busy:
            return
        entries = [e for e in self._selected_remote_entries() if e.name != ".."]
        if not entries:
            QMessageBox.information(self, "提示", "请先选中要下载的文件或目录。")
            return
        dirs = [e for e in entries if e.is_dir]
        if dirs:
            self._on_download_dirs(entries, dirs)
            return
        # ---- 只选了文件: 走单个文件对话框(可覆盖/断点续传) ----
        e = entries[0]
        safe = sanitize_windows_name(e.name)
        dest, okp = QFileDialog.getSaveFileName(
            self, "保存到", str(self._local_dir / safe))
        if not okp:
            return
        resume = False
        if Path(dest).exists():
            box = QMessageBox(self)
            box.setWindowTitle("目标已存在")
            box.setText(f"{Path(dest).name} 已存在, 如何处理?")
            b_ov = box.addButton("覆盖", QMessageBox.ButtonRole.AcceptRole)
            b_re = box.addButton("断点续传", QMessageBox.ButtonRole.ActionRole)
            box.addButton("取消", QMessageBox.ButtonRole.RejectRole)
            box.exec()
            clicked = box.clickedButton()
            if clicked == b_re:
                resume = True
            elif clicked != b_ov:
                return
        self._start_transfer(f"下载 {e.name}", Path(dest),
                             lambda ctx: sess.download(
                                 rjoin(self._remote_path, e.name), dest,
                                 progress=ctx.emit_progress, cancel=ctx.is_cancelled,
                                 resume=resume),
                             done_msg=f"下载完成: {e.name}")

    def _on_download_dirs(self, entries: list[RemoteEntry],
                          dirs: list[RemoteEntry]) -> None:
        """把选中的目录(整棵树, 含空目录)与文件一起下载到目标文件夹。"""
        sess = self._session
        if sess is None:
            return
        base = self._local_dir if self._local_dir.is_dir() else Path.home()
        dest = QFileDialog.getExistingDirectory(
            self, "选择下载位置(选中的文件夹会整个放进去)", str(base))
        if not dest:
            return
        dest_p = Path(dest)
        n_files = len(entries) - len(dirs)
        what = f"{len(dirs)} 个文件夹(含全部子目录)"
        if n_files:
            what += f" 和 {n_files} 个文件"
        if QMessageBox.question(
                self, "下载目录",
                f"将把 {what} 下载到:\n{dest_p}\n\n同名文件会被覆盖。是否继续?",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.No) != QMessageBox.StandardButton.Yes:
            return
        steps = []
        for e in entries:
            remote = rjoin(self._remote_path, e.name)
            local = dest_p / sanitize_windows_name(e.name)
            steps.append((remote, local, e.name, e.is_dir))
        total = len(steps)

        def job(ctx):
            for i, (remote, local, name, is_dir) in enumerate(steps):
                if ctx.is_cancelled():
                    raise TransferCancelled("已取消")
                if is_dir:
                    def _label(rel: str, i=i, name=name):
                        ctx.emit_label(f"[{i + 1}/{total}] 下载 {name} 中: {rel}")
                    download_tree(sess, remote, local,
                                  cancel=ctx.is_cancelled,
                                  byte_progress=ctx.emit_progress,
                                  on_file=_label)
                else:
                    ctx.emit_label(f"[{i + 1}/{total}] 下载 {name}")
                    sess.download(remote, str(local),
                                  progress=ctx.emit_progress,
                                  cancel=ctx.is_cancelled)
            return total

        self._start_batch(f"下载 {total} 项 → {dest_p.name}", job,
                          lambda n: f"下载完成: 共 {n} 项 → {dest_p}")

    def _on_upload(self) -> None:
        sess = self._session
        if sess is None or self._busy:
            return
        src, okp = QFileDialog.getOpenFileName(
            self, "选择要上传的文件", str(self._local_dir))
        if not okp:
            return
        src_path = Path(src)
        remote_target = rjoin(self._remote_path, src_path.name)
        # 存在性检查放在工作线程做, 避免界面卡顿; 简单起见先直接询问覆盖
        box = QMessageBox(self)
        box.setWindowTitle("上传")
        box.setText(f"上传到 {remote_target} ?")
        b_ok = box.addButton("上传", QMessageBox.ButtonRole.AcceptRole)
        box.addButton("取消", QMessageBox.ButtonRole.RejectRole)
        box.exec()
        if box.clickedButton() != b_ok:
            return
        self._start_transfer(f"上传 {src_path.name}", remote_target,
                             lambda ctx: sess.upload(
                                 src, remote_target,
                                 progress=ctx.emit_progress, cancel=ctx.is_cancelled),
                             done_msg=f"上传完成: {src_path.name}")

    def _on_upload_dir(self) -> None:
        """把本地文件夹整棵递归上传到远端当前目录下(同名文件会覆盖)。"""
        sess = self._session
        if sess is None or self._busy:
            return
        src_dir = QFileDialog.getExistingDirectory(
            self, "选择要上传的本地文件夹(整棵树递归上传)", str(self._local_dir))
        if not src_dir:
            return
        src = Path(src_dir)
        target = rjoin(self._remote_path, src.name)
        if QMessageBox.question(
                self, "上传文件夹",
                f"把整个文件夹\n{src}\n\n递归上传到\n{target} ?\n\n远端同名文件会被覆盖。",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.No) != QMessageBox.StandardButton.Yes:
            return

        def job(ctx):
            def _label(rel: str):
                ctx.emit_label(f"上传中: {src.name}/{rel}")
            return upload_tree(sess, src, target,
                               cancel=ctx.is_cancelled,
                               byte_progress=ctx.emit_progress,
                               on_file=_label)

        self._start_batch(
            f"上传文件夹 {src.name}", job,
            lambda st: f"上传完成: {src.name} ({st.files} 个文件) → {target}")

    def _start_transfer(self, title: str, target, job, done_msg: str) -> None:
        self._set_busy(True, f"{title} …")
        self.progress.setRange(0, 1)
        self.progress.setValue(0)

        def prog(done, total):
            if total is None:
                self.progress.setRange(0, 0)
            else:
                self.progress.setRange(0, max(int(total), 1))
                self.progress.setValue(int(done))
            self.status_label.setText(f"{title}  {done}/{total or '?'}")

        def ok(_r):
            self.progress.setRange(0, 1)
            self._set_busy(False, done_msg)

        def err(e: Exception):
            self.progress.setRange(0, 1)
            self._set_busy(False, "传输失败")
            self._show_err(e)

        task = run_async(job, on_success=ok, on_error=err, on_progress=prog)
        self._task = task
        self.btn_cancel.setEnabled(True)

    def _start_batch(self, title: str, job, done_msg) -> None:
        """多步/递归目录任务: 进度条显示单文件字节, 状态栏由 job 发 label 信号更新。"""
        self._set_busy(True, f"{title} …")
        self.progress.setRange(0, 1)
        self.progress.setValue(0)

        def prog(done, total):
            if total is None:
                self.progress.setRange(0, 0)
            else:
                self.progress.setRange(0, max(int(total), 1))
                self.progress.setValue(int(done))

        def ok(r):
            self.progress.setRange(0, 1)
            msg = done_msg(r) if callable(done_msg) else done_msg
            self._set_busy(False, msg)

        def err(e: Exception):
            self.progress.setRange(0, 1)
            self._set_busy(False, "操作失败")
            self._show_err(e)

        task = run_async(job, on_success=ok, on_error=err, on_progress=prog,
                         on_label=lambda t: self.status_label.setText(t))
        self._task = task
        self.btn_cancel.setEnabled(True)

    def _on_cancel(self) -> None:
        if self._task is not None:
            self._task.cancel()
            self.status_label.setText("正在取消…")

    # =====================================================
    #  一键网页分享本机文件夹
    # =====================================================
    def _on_share_folder(self) -> None:
        from ui.share_dialog import ShareDialog
        ShareDialog(self, default_dir=str(self._local_dir)).exec()

    # =====================================================
    #  退出
    # =====================================================
    def closeEvent(self, event) -> None:
        if self._session is not None and self._busy:
            if QMessageBox.question(self, "退出", "仍有任务在运行, 确定退出?")                     != QMessageBox.StandardButton.Yes:
                event.ignore()
                return
        if self._session is not None:
            try:
                self._session.close()
            except Exception:
                pass
            self._session = None
        event.accept()
