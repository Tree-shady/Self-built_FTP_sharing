"""会话抽象：FTPS 与 SFTP 的统一接口。"""
from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Callable, Optional

from .errors import TransferCancelled
from .models import RemoteEntry, SiteProfile

#: 进度回调 (已传输字节, 总字节或 None)
ProgressCallback = Callable[[int, Optional[int]], None]
#: 取消检查：返回 True 表示用户要求中止
CancelCheck = Callable[[], bool]


class Session(ABC):
    """一个已连接/可连接的远端会话。所有方法都同步阻塞，
    界面层负责把它们丢到工作线程。"""

    protocol: str = "?"

    def __init__(self, site: SiteProfile):
        self.site = site
        self._connected = False

    # ---------- 连接生命周期 ----------
    @abstractmethod
    def connect(self) -> None: ...

    @abstractmethod
    def close(self) -> None: ...

    @property
    def connected(self) -> bool:
        return self._connected

    def __enter__(self):
        self.connect()
        return self

    def __exit__(self, *exc):
        self.close()
        return False

    # ---------- 目录与文件操作 ----------
    @abstractmethod
    def listdir(self, remote_dir: str) -> list[RemoteEntry]: ...

    @abstractmethod
    def stat(self, remote_path: str) -> RemoteEntry | None: ...

    @abstractmethod
    def mkdir(self, remote_dir: str) -> None: ...

    @abstractmethod
    def remove(self, remote_path: str) -> None: ...

    @abstractmethod
    def rmdir(self, remote_dir: str) -> None: ...

    # ---------- 传输 ----------
    @abstractmethod
    def download(self, remote_path: str, local_path: str, *,
                 progress: ProgressCallback | None = None,
                 cancel: CancelCheck | None = None,
                 resume: bool = False) -> None: ...

    @abstractmethod
    def upload(self, local_path: str, remote_path: str, *,
               progress: ProgressCallback | None = None,
               cancel: CancelCheck | None = None) -> None: ...

    # ---------- 工具 ----------
    @staticmethod
    def _check_cancel(cancel: CancelCheck | None) -> None:
        if cancel and cancel():
            raise TransferCancelled("已取消")
