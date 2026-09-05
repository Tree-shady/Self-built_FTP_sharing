"""SFTP 会话：基于 paramiko。

SFTP 走 SSH(22), 天然加密, 是"Windows 自建共享"最省心的方案
（Windows 可选功能里可安装 OpenSSH 服务器）。
"""
from __future__ import annotations

import stat as statmod
import threading
from datetime import datetime
from pathlib import Path

from .base import Session
from .errors import (
    AuthError,
    ConnectError,
    RemoteError,
    RemoteNotFoundError,
    RemotePermissionError,
    RemoteProtocolError,
    RemoteTimeoutError,
    TransferCancelled,
    UnsupportedFeature,
)
from .models import RemoteEntry
from .paths import rname, rnorm
from .storage import known_hosts_file

_BLOCK = 64 * 1024


def _paramiko():
    try:
        import paramiko
    except ImportError as e:  # pragma: no cover
        raise UnsupportedFeature(
            "未安装 paramiko, 无法使用 SFTP。请执行: pip install paramiko") from e
    return paramiko


class SFTPSession(Session):
    protocol = "sftp"

    def __init__(self, site):
        super().__init__(site)
        self._client = None
        self._sftp = None
        self._lock = threading.Lock()

    # ---------------- 连接 ----------------
    def connect(self) -> None:
        paramiko = _paramiko()
        site = self.site
        kh_path = known_hosts_file()
        client = paramiko.SSHClient()
        client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        try:
            try:
                client.load_host_keys(str(kh_path))
            except OSError:
                pass
            if not site.auto_accept_key:
                client.set_missing_host_key_policy(paramiko.RejectPolicy())
            client.connect(
                hostname=site.host,
                port=site.port,
                username=site.username or None,
                password=site.password or None,
                timeout=site.timeout,
                banner_timeout=site.timeout,
                auth_timeout=site.timeout,
                look_for_keys=False,
                allow_agent=False,
            )
            client.set_keepalive(15)
            if site.auto_accept_key:
                try:
                    kh_path.parent.mkdir(parents=True, exist_ok=True)
                    client.get_host_keys().save(str(kh_path))
                except OSError:
                    pass
            self._client = client
            self._sftp = client.open_sftp()
        except paramiko.AuthenticationException as e:
            raise AuthError(f"认证失败: {e}") from e
        except paramiko.BadHostKeyException as e:
            raise ConnectError(
                f"主机密钥与已记录的不一致({e.hostname}), 可能存在中间人攻击") from e
        except paramiko.SSHException as e:
            raise ConnectError(f"SSH 握手失败: {e}") from e
        except TimeoutError as e:
            raise RemoteTimeoutError(f"连接超时: {e}") from e
        except OSError as e:
            raise ConnectError(f"网络错误: {e}") from e
        self._connected = True

    def close(self) -> None:
        with self._lock:
            for obj in (self._sftp, self._client):
                if obj is not None:
                    try:
                        obj.close()
                    except Exception:
                        pass
            self._sftp = None
            self._client = None
            self._connected = False

    # ---------------- 内部辅助 ----------------
    def _guard(self, what: str, fn):
        try:
            with self._lock:
                return fn()
        except (TransferCancelled, RemoteError):
            raise
        except FileNotFoundError as e:
            raise RemoteNotFoundError(f"{what}: 不存在") from e
        except PermissionError as e:
            raise RemotePermissionError(f"{what}: {e}") from e
        except TimeoutError as e:
            raise RemoteTimeoutError(f"{what}: 操作超时") from e
        except OSError as e:
            errno = getattr(e, "errno", None)
            if errno == 2:
                raise RemoteNotFoundError(f"{what}: 不存在") from e
            if errno in (13, 21):
                raise RemotePermissionError(f"{what}: {e}") from e
            raise ConnectError(f"{what}: {e}") from e

    def _ensure(self) -> None:
        if not self._connected or self._sftp is None:
            self.connect()

    def _attr_to_entry(self, name: str, a) -> RemoteEntry:
        mode = getattr(a, "st_mode", 0)
        mtime = None
        try:
            if getattr(a, "st_mtime", 0):
                mtime = datetime.fromtimestamp(a.st_mtime)
        except (OverflowError, OSError, ValueError):
            mtime = None
        return RemoteEntry(name=name, is_dir=statmod.S_ISDIR(mode), size=int(a.st_size or 0), mtime=mtime)

    # ---------------- 目录 ----------------
    def listdir(self, remote_dir: str) -> list[RemoteEntry]:
        p = rnorm(remote_dir)

        def _do() -> list[RemoteEntry]:
            self._ensure()
            attrs = self._sftp.listdir_attr(p)
            return [self._attr_to_entry(a.filename, a) for a in attrs
                    if a.filename not in (".", "..")]

        return self._guard(f"列目录 {p}", _do)

    def stat(self, remote_path: str) -> RemoteEntry | None:
        p = rnorm(remote_path)

        def _do():
            self._ensure()
            try:
                a = self._sftp.stat(p)
            except FileNotFoundError:
                return None
            return self._attr_to_entry(rname(p), a)

        return self._guard(f"查看 {p}", _do)

    # ---------------- 文件操作 ----------------
    def mkdir(self, remote_dir: str) -> None:
        p = rnorm(remote_dir)

        def _do() -> None:
            self._ensure()
            self._sftp.mkdir(p)

        self._guard(f"新建目录 {p}", _do)

    def remove(self, remote_path: str) -> None:
        p = rnorm(remote_path)

        def _do() -> None:
            self._ensure()
            self._sftp.remove(p)

        self._guard(f"删除 {p}", _do)

    def rmdir(self, remote_dir: str) -> None:
        p = rnorm(remote_dir)

        def _do() -> None:
            self._ensure()
            self._sftp.rmdir(p)

        self._guard(f"删除目录 {p}", _do)

    # ---------------- 传输 ----------------
    def download(self, remote_path, local_path, *, progress=None, cancel=None, resume=False) -> None:
        p = rnorm(remote_path)
        offset = 0
        mode = "wb"
        if resume:
            try:
                offset = Path(local_path).stat().st_size
                mode = "ab"
            except OSError:
                offset = 0
                mode = "wb"

        def _do() -> None:
            self._ensure()
            with self._sftp.open(p, "rb") as rf:
                total = None
                try:
                    total = rf.stat().st_size
                except OSError:
                    pass
                if offset >= (total or 0) and offset > 0:
                    return  # 本地已有完整文件
                rf.seek(offset)
                done = offset
                with open(local_path, mode) as lf:
                    while True:
                        self._check_cancel(cancel)
                        data = rf.read(_BLOCK)
                        if not data:
                            break
                        lf.write(data)
                        done += len(data)
                        if progress:
                            progress(done, total)

        self._guard(f"下载 {p}", _do)

    def upload(self, local_path, remote_path, *, progress=None, cancel=None) -> None:
        p = rnorm(remote_path)
        total = Path(local_path).stat().st_size

        def _do() -> None:
            self._ensure()
            done = 0
            with self._sftp.open(p, "wb") as wf:
                with open(local_path, "rb") as fh:
                    while True:
                        self._check_cancel(cancel)
                        data = fh.read(_BLOCK)
                        if not data:
                            break
                        wf.write(data)
                        done += len(data)
                        if progress:
                            progress(done, total)
                wf.flush()

        self._guard(f"上传 {p}", _do)
