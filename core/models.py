"""数据模型：站点配置与远端条目。"""
from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import datetime

#: 支持的协议
SUPPORTED_PROTOCOLS = ("ftps", "sftp")

#: FTPS 文件名编码的可选值（老 Windows/IIS 服务器常用 gbk/gb18030）
ENCODINGS = ("auto", "utf-8", "gbk", "gb18030", "latin-1")

DEFAULT_TIMEOUT = 30


@dataclass
class SiteProfile:
    """一个"站点" = 一台服务器 + 凭据 + 行为选项。"""

    name: str = "新站点"
    protocol: str = "ftps"          # "ftps" | "sftp"
    host: str = ""
    port: int = 21
    username: str = ""
    password: str = ""
    encoding: str = "auto"          # FTPS: 服务器文件名编码
    passive: bool = True            # FTPS: 使用被动(PASV)模式
    insecure_tls: bool = False      # FTPS: 不校验服务器证书(自签名测试用)
    auto_accept_key: bool = True    # SFTP: 首次连接自动信任主机密钥
    save_password: bool = True
    timeout: int = DEFAULT_TIMEOUT
    remote_root: str = "/"
    id: str = field(default_factory=lambda: uuid.uuid4().hex[:12])

    def __post_init__(self) -> None:
        self.port = int(self.port or 0)
        if self.protocol == "sftp" and self.port in (0, 21):
            self.port = 22
        elif self.protocol == "ftps" and self.port in (0, 22):
            self.port = 21
        if self.protocol not in SUPPORTED_PROTOCOLS:
            raise ValueError(f"不支持的协议: {self.protocol}")
        if self.encoding not in ENCODINGS:
            self.encoding = "auto"
        if self.remote_root.startswith("/"):
            self.remote_root = self.remote_root.rstrip("/") or "/"
        else:
            self.remote_root = "/" + self.remote_root.strip("/")

    def display(self) -> str:
        cred = f"{self.username}@" if self.username else ""
        return f"{self.name}  [{self.protocol.upper()}] {cred}{self.host}:{self.port}"


@dataclass
class RemoteEntry:
    """远端目录列表里的一项。"""

    name: str
    is_dir: bool = False
    size: int = 0
    mtime: datetime | None = None

    def __post_init__(self) -> None:
        self.size = int(self.size or 0)

    def sort_key(self):
        return (0 if self.is_dir else 1, self.name.casefold())
