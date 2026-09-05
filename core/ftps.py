"""FTPS 会话：用标准库 ftplib.FTP_TLS 实现（显式 AUTH TLS）。

Python >= 3.12 才修复了 prot_p() 之后数据连接/控制连接的已知问题,
因此本项目要求 Python 3.12+。
"""
from __future__ import annotations

import ftplib
import ssl
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
)
from .listing import parse_list_line, parse_mlsd_time
from .models import RemoteEntry
from .paths import rname, rnorm

_CODEC = {
    "auto": "utf-8",
    "utf-8": "utf-8",
    "gbk": "gbk",
    "gb18030": "gb18030",
    "latin-1": "latin-1",
}

_BLOCK = 64 * 1024


def _err_code(exc: ftplib.Error) -> str:
    msg = str(exc)
    return msg[:3] if len(msg) >= 3 and msg[:3].isdigit() else ""


def _map_perm_error(what: str, exc: ftplib.Error) -> RemoteError:
    code = _err_code(exc)
    text = str(exc)[3:].strip() or str(exc)
    if code == "530":
        return AuthError(f"认证失败: {text}")
    if code in ("421",):
        return RemoteTimeoutError("连接闲置过久被服务器关闭(421), 请重连")
    if code in ("550", "551"):
        return RemoteNotFoundError(f"{what}: {text}")
    if code in ("553", "532"):
        return RemotePermissionError(f"{what}: {text}")
    if code in ("500", "501", "502", "504"):
        return RemoteProtocolError(f"{what}: 命令不支持: {text}")
    return RemotePermissionError(f"{what}: {text}")


class FTPTlsSession(Session):
    """FTPS(显式 TLS) 会话。"""

    protocol = "ftps"

    def __init__(self, site):
        super().__init__(site)
        self._ftp: ftplib.FTP_TLS | None = None
        self._broken = False

    # ---------------- 连接 ----------------
    def connect(self) -> None:
        site = self.site
        codec = _CODEC.get(site.encoding, "utf-8")
        ctx = ssl.create_default_context()
        if site.insecure_tls:
            ctx.check_hostname = False
            ctx.verify_mode = ssl.CERT_NONE
        ftp = None
        try:
            ftp = ftplib.FTP_TLS(context=ctx, encoding=codec, timeout=site.timeout)
            ftp.connect(site.host, site.port, timeout=site.timeout)
            ftp.auth()                      # AUTH TLS: 加密控制通道
            ftp.set_pasv(site.passive)
            ftp.login(site.username, site.password or None)
            ftp.prot_p()                    # 数据通道也走 TLS
            if site.encoding in ("auto", "utf-8"):
                # RFC 2640: 请求 UTF-8 文件名; 旧服务器不支持时忽略错误
                try:
                    ftp.sendcmd("OPTS UTF8 ON")
                except ftplib.all_errors:
                    pass
        except ftplib.error_perm as e:
            self._drop(ftp)
            raise _map_perm_error("连接", e) from e
        except ftplib.all_errors as e:
            self._drop(ftp)
            raise ConnectError(f"连接失败: {e}") from e
        except OSError as e:
            self._drop(ftp)
            raise ConnectError(f"网络错误: {e}") from e
        self._ftp = ftp
        self._broken = False
        self._connected = True

    def _drop(self, ftp=None) -> None:
        self._connected = False
        obj = ftp if ftp is not None else self._ftp
        self._ftp = None
        if obj is not None:
            try:
                obj.close()
            except Exception:
                pass

    def close(self) -> None:
        if self._ftp is not None:
            try:
                self._ftp.quit()
            except Exception:
                try:
                    self._ftp.close()
                except Exception:
                    pass
        self._ftp = None
        self._connected = False

    # ---------------- 内部辅助 ----------------
    def _guard(self, what: str, fn):
        """把 ftplib 异常统一映射到 RemoteError 一族。"""
        try:
            return fn()
        except (TransferCancelled, RemoteError):
            raise
        except ftplib.error_perm as e:
            raise _map_perm_error(what, e) from e
        except ftplib.error_temp as e:
            code = _err_code(e)
            if code == "421":
                raise RemoteTimeoutError("服务器关闭了闲置连接(421), 请重连") from e
            raise RemoteProtocolError(f"{what}: {e}") from e
        except (ftplib.Error, EOFError) as e:
            raise RemoteProtocolError(f"{what}: {e}") from e
        except TimeoutError as e:
            raise RemoteTimeoutError(f"{what}: 操作超时") from e
        except OSError as e:
            raise ConnectError(f"{what}: 网络错误: {e}") from e

    def _ensure(self) -> None:
        if self._broken:
            self._drop()
        if not self._connected or self._ftp is None:
            self.connect()

    def set_encoding(self, code: str) -> None:
        """热切换文件名编码（只影响之后的数据通道解码）。"""
        if self._ftp is not None and code in _CODEC:
            self._ftp.encoding = _CODEC[code]

    # ---------------- 目录 ----------------
    def listdir(self, remote_dir: str) -> list[RemoteEntry]:
        p = rnorm(remote_dir)

        def _do() -> list[RemoteEntry]:
            self._ensure()
            entries: list[RemoteEntry] = []
            mlsd_ok = False
            try:
                for name, facts in self._ftp.mlsd(p):
                    mlsd_ok = True
                    if name in (".", ".."):
                        continue
                    t = facts.get("type", "")
                    entries.append(RemoteEntry(
                        name=name,
                        is_dir=t in ("dir", "cdir", "pdir"),
                        size=int(facts.get("size") or 0),
                        mtime=parse_mlsd_time(facts.get("modify")),
                    ))
            except (ftplib.all_errors, OSError, TimeoutError):
                mlsd_ok = False
                entries.clear()   # 丢弃 MLSD 的部分结果
            if mlsd_ok:
                return entries
            # 旧服务器: 回退到 LIST 输出解析
            lines: list[str] = []
            try:
                self._ftp.retrlines("LIST " + p, lines.append)
            except ftplib.error_perm as e:
                raise _map_perm_error(f"列目录 {p}", e) from e
            except (ftplib.Error, EOFError) as e:
                raise RemoteProtocolError(f"列目录 {p}: {e}") from e
            except TimeoutError as e:
                raise RemoteTimeoutError(f"列目录 {p}: 超时") from e
            for line in lines:
                e = parse_list_line(line)
                if e:
                    entries.append(e)
            return entries

        return self._guard(f"列目录 {p}", _do)

    def stat(self, remote_path: str) -> RemoteEntry | None:
        p = rnorm(remote_path)
        if p == "/":
            return RemoteEntry(name="/", is_dir=True)
        parent, name = p.rsplit("/", 1)
        parent = parent or "/"
        for e in self.listdir(parent):
            if e.name == name:
                return e
        return None

    # ---------------- 文件操作 ----------------
    def mkdir(self, remote_dir: str) -> None:
        p = rnorm(remote_dir)

        def _do() -> None:
            self._ensure()
            self._ftp.mkd(p)

        self._guard(f"新建目录 {p}", _do)

    def remove(self, remote_path: str) -> None:
        p = rnorm(remote_path)

        def _do() -> None:
            self._ensure()
            self._ftp.delete(p)

        self._guard(f"删除 {p}", _do)

    def rmdir(self, remote_dir: str) -> None:
        p = rnorm(remote_dir)

        def _do() -> None:
            self._ensure()
            self._ftp.rmd(p)

        self._guard(f"删除目录 {p}", _do)

    # ---------------- 传输 ----------------
    def download(self, remote_path, local_path, *, progress=None, cancel=None, resume=False) -> None:
        p = rnorm(remote_path)
        broken = {"flag": False}

        def _do() -> None:
            self._ensure()
            try:
                total = self._remote_size(p)
                offset = 0
                mode = "wb"
                if resume:
                    lp = Path(local_path)
                    try:
                        offset = lp.stat().st_size
                        if total is not None and offset > total:
                            offset = 0
                        if offset:
                            mode = "ab"
                    except OSError:
                        offset = 0
                        mode = "wb"
                if total is not None and offset == total:
                    return  # 本地已有完整文件
                done = [offset]

                def _on_chunk(data: bytes) -> None:
                    self._check_cancel(cancel)
                    done[0] += len(data)
                    if progress:
                        progress(done[0], total)

                with open(local_path, mode) as fh:
                    self._ftp.retrbinary(
                        "RETR " + p,
                        lambda d: (fh.write(d), _on_chunk(d)),
                        blocksize=_BLOCK,
                        rest=(offset or None),
                    )
            except BaseException:
                broken["flag"] = True
                raise

        try:
            self._guard(f"下载 {p}", _do)
        except RemoteError:
            if broken["flag"]:
                self._drop()          # 传输中断后控制连接状态未知, 丢弃待重连
            raise

    def upload(self, local_path, remote_path, *, progress=None, cancel=None) -> None:
        p = rnorm(remote_path)
        broken = {"flag": False}

        def _do() -> None:
            try:
                total = Path(local_path).stat().st_size
            except OSError as e:
                raise RemoteNotFoundError(f"本地文件不存在: {e}") from e
            self._ensure()
            try:
                done = [0]

                def _cb(data: bytes) -> None:
                    self._check_cancel(cancel)
                    done[0] += len(data)
                    if progress:
                        progress(done[0], total)

                with open(local_path, "rb") as fh:
                    self._ftp.storbinary(
                        "STOR " + p, fh, blocksize=_BLOCK, callback=_cb)
            except BaseException:
                broken["flag"] = True
                raise

        try:
            self._guard(f"上传 {p}", _do)
        except RemoteError:
            if broken["flag"]:
                self._drop()
            raise

    # ---------------- 工具 ----------------
    def _remote_size(self, remote_path: str) -> int | None:
        """SIZE 命令取文件大小；目录或不支持时返回 None。"""
        try:
            return int(self._ftp.size(remote_path))
        except ftplib.all_errors:
            return None
