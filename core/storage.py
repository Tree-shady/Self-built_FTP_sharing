"""站点配置持久化（JSON）。

默认位置: Windows 为 %APPDATA%/FTPSharing/sites.json。
可用环境变量 FTPSHARING_CONFIG_DIR 覆盖（测试时隔离用）。
"""
from __future__ import annotations

import json
import os
from dataclasses import asdict
from pathlib import Path

from .models import SiteProfile

APP_DIR_NAME = "FTPSharing"


def config_dir() -> Path:
    override = os.environ.get("FTPSHARING_CONFIG_DIR")
    if override:
        return Path(override)
    base = os.environ.get("APPDATA")
    if base:
        return Path(base) / APP_DIR_NAME
    return Path.home() / ("." + APP_DIR_NAME)


def sites_file() -> Path:
    return config_dir() / "sites.json"


def known_hosts_file() -> Path:
    return config_dir() / "known_hosts"


def load_sites() -> list[SiteProfile]:
    try:
        raw = json.loads(sites_file().read_text("utf-8"))
    except (OSError, ValueError):
        return []
    out: list[SiteProfile] = []
    fields = SiteProfile.__dataclass_fields__
    for item in raw.get("sites", []):
        try:
            site = SiteProfile(**{k: v for k, v in item.items() if k in fields})
        except (TypeError, ValueError):
            continue
        out.append(site)
    return out


def save_sites(sites: list[SiteProfile]) -> None:
    cfg = config_dir()
    cfg.mkdir(parents=True, exist_ok=True)
    payload = {"version": 1, "sites": [asdict(s) for s in sites]}
    target = sites_file()
    tmp = target.with_name(target.name + ".tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), "utf-8")
    tmp.replace(target)
