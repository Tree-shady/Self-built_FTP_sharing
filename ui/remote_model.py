"""远端目录列表的 Qt 表格模型与展示格式工具。"""
from __future__ import annotations

from datetime import datetime

from PySide6.QtCore import QAbstractTableModel, QModelIndex, Qt

from core.models import RemoteEntry


def human_size(n: int | None) -> str:
    if n is None:
        return ""
    size = float(n)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if size < 1024 or unit == "TB":
            return f"{size:.1f} {unit}" if unit != "B" else f"{int(size)} B"
        size /= 1024
    return f"{int(size)} B"


def fmt_mtime(dt: datetime | None) -> str:
    if dt is None:
        return ""
    return dt.strftime("%Y-%m-%d %H:%M")


class RemoteTableModel(QAbstractTableModel):
    """远程文件表。第一行可能是合成的 '..' 返回上级。"""

    HEADERS = ("名称", "类型", "大小", "修改时间")

    def __init__(self, parent=None):
        super().__init__(parent)
        self._entries: list[RemoteEntry] = []

    # ---- 供界面使用 ----
    def set_entries(self, entries: list[RemoteEntry], show_parent: bool) -> None:
        self.beginResetModel()
        self._entries = list(entries)
        if show_parent:
            self._entries.insert(0, RemoteEntry(name="..", is_dir=True))
        self._entries.sort(key=lambda e: e.sort_key())
        self.endResetModel()

    def entry_at(self, row: int) -> RemoteEntry | None:
        if 0 <= row < len(self._entries):
            return self._entries[row]
        return None

    # ---- QAbstractTableModel ----
    def rowCount(self, parent=QModelIndex()) -> int:
        return 0 if parent.isValid() else len(self._entries)

    def columnCount(self, parent=QModelIndex()) -> int:
        return len(self.HEADERS)

    def data(self, index: QModelIndex, role: int = Qt.ItemDataRole.DisplayRole):
        if not index.isValid():
            return None
        e = self._entries[index.row()]
        col = index.column()
        if role == Qt.ItemDataRole.DisplayRole:
            if col == 0:
                return e.name
            if col == 1:
                return "文件夹" if e.is_dir else "文件"
            if col == 2:
                return human_size(e.size)
            if col == 3:
                return fmt_mtime(e.mtime)
        elif role == Qt.ItemDataRole.TextAlignmentRole and col == 2:
            return int(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        elif role == Qt.ItemDataRole.ToolTipRole and col == 0:
            return e.name
        return None

    def headerData(self, section: int, orientation: Qt.Orientation, role: int = Qt.ItemDataRole.DisplayRole):
        if role == Qt.ItemDataRole.DisplayRole and orientation == Qt.Orientation.Horizontal:
            return self.HEADERS[section]
        return None
