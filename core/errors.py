"""统一异常层：界面代码只捕获 RemoteError 一族，不直接接触 ftplib/paramiko。"""
from __future__ import annotations


class RemoteError(Exception):
    """所有远端操作异常的基类。"""


class ConnectError(RemoteError):
    """建立连接失败（网络不可达、握手失败）。"""


class AuthError(RemoteError):
    """认证失败：账号密码错误、无权限登录。"""


class RemoteTimeoutError(RemoteError):
    """连接或传输超时。"""


class RemoteNotFoundError(RemoteError):
    """远端路径不存在。"""


class RemotePermissionError(RemoteError):
    """远端权限不足。"""


class RemoteProtocolError(RemoteError):
    """服务器返回了无法处理的响应（含旧式编码乱码、命令不支持等）。"""


class TransferCancelled(RemoteError):
    """传输被用户取消。"""


class UnsupportedFeature(RemoteError):
    """当前协议/服务器不支持该能力。"""
