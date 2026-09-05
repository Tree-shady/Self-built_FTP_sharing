"""按站点协议创建会话实例。"""
from __future__ import annotations

from .base import Session
from .errors import UnsupportedFeature
from .models import SiteProfile


def create_session(site: SiteProfile) -> Session:
    if site.protocol == "ftps":
        from .ftps import FTPTlsSession
        return FTPTlsSession(site)
    if site.protocol == "sftp":
        from .sftp import SFTPSession
        return SFTPSession(site)
    raise UnsupportedFeature(f"不支持的协议: {site.protocol}")
