"""目录级递归传输(纯逻辑, 与协议无关)。

只用 Session 的最小原语(listdir / mkdir / download / upload)实现"整棵目录树"
的递归下载与上传, GUI 线程与自测都能复用; 不依赖第三方库。

进度约定:
  * byte_progress  单文件字节进度, 原样透传给 Session.download/upload;
  * on_file        每个文件开始传输前回调, 参数为相对路径(供界面显示"正在传 x");
  * cancel         每处理一个条目时检查一次, Session 传输内也自行检查。

下载时本地名字经过 sanitize_windows_name 清洗, 同一目录内若清洗后重名会追加
(1)、(2) 防覆盖; 空目录也会被创建。上传保持本地原名。
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Optional

from .base import CancelCheck, ProgressCallback, Session
from .errors import RemoteError
from .models import RemoteEntry
from .paths import rjoin, rnorm, sanitize_windows_name


@dataclass
class TreeStats:
    """一次目录级传输的结果统计。"""

    dirs: int = 0
    files: int = 0


def _free_name(name: str, taken: set[str]) -> str:
    """同一目录内清洗后重名时追加 (1)、(2)…。"""
    cand = name
    i = 1
    while cand.casefold() in taken:
        stem, dot, ext = cand.rpartition(".")
        base = stem if dot else cand
        suffix = ("." + ext) if dot else ""
        cand = f"{base} ({i}){suffix}"
        i += 1
    taken.add(cand.casefold())
    return cand


def _sorted(entries: list[RemoteEntry]) -> list[RemoteEntry]:
    return sorted(entries, key=lambda e: (0 if e.is_dir else 1, e.name.casefold()))


# ---------------------------------------------------------------------------
# 下载整棵目录树: remote_dir 的子项会逐一镜像到 local_dir 里
# ---------------------------------------------------------------------------
def download_tree(session: Session, remote_dir: str, local_dir: str | Path, *,
                  cancel: CancelCheck | None = None,
                  byte_progress: ProgressCallback | None = None,
                  on_file: Callable[[str], None] | None = None) -> TreeStats:
    """把远端目录 remote_dir 递归下载到本地目录 local_dir(自动创建)。

    空目录也会被创建; 返回统计。
    """
    local = Path(local_dir)
    local.mkdir(parents=True, exist_ok=True)
    stats = TreeStats(dirs=1)

    def walk(rp: str, lp: Path) -> None:
        if cancel and cancel():
            return
        taken: set[str] = set()
        for e in _sorted(session.listdir(rp)):
            if cancel and cancel():
                return
            name = _free_name(sanitize_windows_name(e.name), taken)
            child_r = rjoin(rp, e.name)
            child_l = lp / name
            if e.is_dir:
                child_l.mkdir(parents=True, exist_ok=True)
                stats.dirs += 1
                walk(child_r, child_l)
            else:
                if on_file:
                    on_file(str(child_l.relative_to(local)))
                session.download(child_r, str(child_l),
                                 progress=byte_progress, cancel=cancel)
                stats.files += 1

    walk(rnorm(remote_dir), local)
    return stats


# ---------------------------------------------------------------------------
# 上传本地目录树: local_dir 的子项镜像到 remote_dir 里
# ---------------------------------------------------------------------------
def upload_tree(session: Session, local_dir: str | Path, remote_dir: str, *,
                cancel: CancelCheck | None = None,
                byte_progress: ProgressCallback | None = None,
                on_file: Callable[[str], None] | None = None) -> TreeStats:
    """把本地目录 local_dir 递归上传到远端 remote_dir(自动创建)。

    同名远端文件会被覆盖(与单文件上传语义一致); 返回统计。
    """
    local = Path(local_dir)
    target = rnorm(remote_dir)
    stats = TreeStats()

    def ensure_dir(rd: str) -> None:
        existing = session.stat(rd)
        if existing is None:
            session.mkdir(rd)
        elif not existing.is_dir:
            raise RemoteError(f"远端存在同名文件, 无法作为目录: {rd}")
        stats.dirs += 1

    ensure_dir(target)

    def walk(lp: Path, rd: str) -> None:
        for name in sorted(os.listdir(lp)):
            if cancel and cancel():
                return
            if name.startswith("."):
                continue
            src = lp / name
            if src.is_dir():
                child_r = rjoin(rd, name)
                ensure_dir(child_r)
                walk(src, child_r)
            elif src.is_file():
                child_r = rjoin(rd, name)
                if on_file:
                    on_file(str(src.relative_to(local)))
                session.upload(str(src), child_r,
                               progress=byte_progress, cancel=cancel)
                stats.files += 1

    walk(local, target)
    return stats
