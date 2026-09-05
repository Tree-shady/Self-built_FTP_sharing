"""远程路径与 Windows 本地路径的工具函数（纯逻辑，便于单测）。"""
from __future__ import annotations

import re
import unicodedata

# Windows 不允许出现在文件名里的字符: < > : " / \ | ? * 以及控制字符 0x00-0x1f
_WINDOWS_BAD = re.compile(r'[<>:"/\\|?*\x00-\x1f]')
# Windows 保留设备名（含扩展名也算, 如 CON.txt）
_RESERVED = {"CON", "PRN", "AUX", "NUL"} | {
    f"{base}{i}" for base in ("COM", "LPT") for i in range(1, 10)
}


def rnorm(path: str) -> str:
    """规范化远程路径：以 / 开头、无重复斜杠、除根外无尾部斜杠。"""
    if not path:
        return "/"
    p = "/" + path.replace("\\", "/").strip()
    p = re.sub(r"/+", "/", p)
    if len(p) > 1:
        p = p.rstrip("/")
    return p or "/"


def rjoin(base: str, name: str) -> str:
    """把子项名字拼到目录路径上（正确处理绝对/相对/尾斜杠）。"""
    if name.startswith("/"):
        return rnorm(name)
    return rnorm(rnorm(base) + "/" + name)


def rparent(path: str) -> str:
    p = rnorm(path)
    if p == "/":
        return "/"
    return rnorm(p.rsplit("/", 1)[0]) if "/" in p else "/"


def rname(path: str) -> str:
    p = rnorm(path)
    return p.rsplit("/", 1)[-1] if p != "/" else "/"


def sanitize_windows_name(name: str) -> str:
    """把服务器文件名清洗成 Windows 可落盘的名字。

    远程(Linux)合法但 Windows 非法的字符会被替换；空结果或保留设备名
    会追加下划线避免冲突。
    """
    name = unicodedata.normalize("NFC", name).strip()
    name = _WINDOWS_BAD.sub("_", name)
    name = name.rstrip(" .")          # 结尾的点/空格在 Windows 上非法
    if not name:
        name = "_"
    stem = name.rsplit(".", 1)[0].upper() if "." in name else name.upper()
    if stem in _RESERVED:
        name = name + "_"
    return name
