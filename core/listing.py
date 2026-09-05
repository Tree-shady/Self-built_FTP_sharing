"""FTP LIST 输出的解析（仅在没有 MLSD 的旧服务器上使用）。

两种常见格式：
  unix:  drwxr-xr-x 2 owner group 4096 Jan 01 12:34 name
  msdos: 07-15-24  03:12PM       <DIR>          name
"""
from __future__ import annotations

import re
from datetime import datetime

from .models import RemoteEntry

_UNIX_RE = re.compile(
    r"^(?P<perm>[bcdlps-][rwxstStT-]{9})\s+\d+\s+\S+\s+\S+\s+"
    r"(?P<size>\d+)\s+"
    r"(?P<date>[A-Za-z]{3}\s+\d{1,2}\s+(?:\d{1,2}:\d{2}|\d{4}))\s+"
    r"(?P<name>.+)$"
)
_MSDOS_RE = re.compile(
    r"^(?P<date>\d{2}-\d{2}-\d{2})\s+"
    r"(?P<time>\d{2}:\d{2}(?:AM|PM))\s+"
    r"(?:(?P<dir><DIR>)|(?P<size>\d+))\s+"
    r"(?P<name>.+)$",
    re.IGNORECASE,
)
_MONTHS = {m: i + 1 for i, m in enumerate(
    ["Jan", "Feb", "Mar", "Apr", "May", "Jun",
     "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"])}


def _parse_unix_date(token: str) -> datetime | None:
    """'Jan 01 12:34' 或 'Jan 01 2024' → datetime(当年)"""
    parts = token.split()
    if len(parts) != 3:
        return None
    month = _MONTHS.get(parts[0][:3].capitalize())
    if not month:
        return None
    try:
        day = int(parts[1])
        if ":" in parts[2]:
            hh, mm = parts[2].split(":")
            return datetime(datetime.now().year, month, day, int(hh), int(mm))
        return datetime(int(parts[2]), month, day)
    except ValueError:
        return None


def _parse_msdos_date(token: str, time_token: str) -> datetime | None:
    try:
        mm, dd, yy = (int(x) for x in token.split("-"))
        ampm = time_token[-2:].upper()
        hh, mi = (int(x) for x in time_token[:-2].split(":"))
        if ampm == "PM" and hh != 12:
            hh += 12
        elif ampm == "AM" and hh == 12:
            hh = 0
        year = 2000 + yy if yy < 80 else 1900 + yy
        return datetime(year, mm, dd, hh, mi)
    except (ValueError, IndexError):
        return None


def parse_list_line(line: str) -> RemoteEntry | None:
    """解析一行 LIST 输出；无法识别时返回 None。"""
    line = line.rstrip()
    if not line or line.startswith("total ") or line.startswith("总用量"):
        return None

    m = _UNIX_RE.match(line)
    if m:
        g = m.groupdict()
        name = g["name"]
        if name in (".", ".."):
            return None
        return RemoteEntry(
            name=name,
            is_dir=g["perm"][0] == "d",
            size=int(g["size"] or 0),
            mtime=_parse_unix_date(g["date"]),
        )

    m = _MSDOS_RE.match(line)
    if m:
        g = m.groupdict()
        name = g["name"].strip()
        if name in (".", ".."):
            return None
        return RemoteEntry(
            name=name,
            is_dir=bool(g["dir"]),
            size=int(g["size"] or 0),
            mtime=_parse_msdos_date(g["date"], g["time"]),
        )
    return None


def parse_list_text(text: str) -> list[RemoteEntry]:
    return [e for e in (parse_list_line(l) for l in text.splitlines()) if e]


def parse_mlsd_time(token: str | None) -> datetime | None:
    """MLSD modify 事实: YYYYMMDDHHMMSS(.sss)(+/-HHMM) → datetime"""
    if not token:
        return None
    tok = token.split(".")[0]
    sign = tok.find("+")
    if sign == -1:
        sign = tok.find("-")
    if sign != -1:
        tok = tok[:sign]
    if len(tok) < 14:
        return None
    try:
        return datetime(
            int(tok[0:4]), int(tok[4:6]), int(tok[6:8]),
            int(tok[8:10]), int(tok[10:12]), int(tok[12:14]))
    except ValueError:
        return None
