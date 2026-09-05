"""局域网 Web 分享器 —— 让同一网络的人用浏览器下载/上传文件。

纯标准库, 零依赖。用法:
    python scripts/lan_share.py --dir ./shared --port 8000 --password 1234

功能:
  * 目录浏览 + 下载(支持 Range 断点续传/视频拖动)
  * 整文件夹 ZIP 打包下载: 列表里每个目录有 "ZIP" 链接, 一次下载整棵目录树
  * 上传文件: "选择文件"可把文件传到当前目录(多选、自动防覆盖改名)
  * 上传整个文件夹: "选择文件夹"可把整棵目录树传到当前目录(逐段清洗/递归建目录)
  * 中文文件名、目录穿越防护
  * 自动识别局域网 / Tailscale(100.x) 地址并打印; 已登录时给出 Funnel 提示

安全提示:
  * 只监听在局域网时建议设 --password, 并只分享该给人看的文件夹
  * 上传默认开启; 只想给别人下载用 --no-upload 关闭
"""
from __future__ import annotations

import argparse
import base64
import html
import hmac
import json
import mimetypes
import os
import socket
import sys
import tempfile
import urllib.parse
import zipfile
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_SHARE = ROOT / "shared"
CHUNK = 64 * 1024
_CRLF = bytes((13, 10))
_QUOTE = bytes((34,))


def lan_ips() -> list[str]:
    ips: list[str] = []
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("8.8.8.8", 80))
        ips.append(s.getsockname()[0])
    except OSError:
        pass
    finally:
        s.close()
    try:
        for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
            ip = info[4][0]
            if ip not in ips and not ip.startswith("127."):
                ips.append(ip)
    except OSError:
        pass
    return ips


def tailscale_exe() -> str | None:
    """找到 tailscale 命令行; 找不到返回 None。"""
    import shutil
    exe = shutil.which("tailscale")
    if exe:
        return exe
    pf = os.environ.get("ProgramFiles") or "C:/Program Files"
    probe = Path(pf) / "Tailscale" / "tailscale.exe"
    return str(probe) if probe.exists() else None


def tailscale_ips() -> list[str]:
    """登录态下返回 100.x 组网地址; 未登录/未安装返回空。"""
    import subprocess
    exe = tailscale_exe()
    if not exe:
        return []
    try:
        out = subprocess.run([exe, "ip", "-4"], capture_output=True,
                             text=True, timeout=5)
    except (OSError, subprocess.SubprocessError):
        return []
    if out.returncode != 0:
        return []
    return [ln.strip() for ln in out.stdout.splitlines() if ln.strip()]


def fmt_size(n: int) -> str:
    size = float(n)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if size < 1024 or unit == "TB":
            return f"{size:.0f} {unit}" if unit == "B" else f"{size:.1f} {unit}"
        size /= 1024
    return f"{int(size)} B"


def _enc(s: str) -> str:
    return html.escape(s, quote=True)


def sanitize_name(raw: str) -> str | None:
    """上传文件名清洗: 取 basename、去 Windows 非法字符; 空/隐藏名拒绝。"""
    name = Path(raw).name.strip()
    if not name or name in (".", "..") or name.startswith("."):
        return None
    out = []
    for ch in name:
        out.append("_" if (ord(ch) < 32 or ch in '<>:"/|?*') else ch)
    name = "".join(out).rstrip(" .") or "_"
    stem = name.rsplit(".", 1)[0].upper()
    if stem in {"CON", "PRN", "AUX", "NUL"} or (
            stem[:3] in {"COM", "LPT"} and len(stem) == 4 and stem[3:].isdigit()):
        name = name + "_"
    return name


def unique_path(directory: Path, name: str) -> Path:
    """已存在则自动加 (1)、(2)… 防止覆盖。"""
    target = directory / name
    if not target.exists():
        return target
    stem, dot, ext = name.rpartition(".")
    base = stem if dot else name
    suffix = ("." + ext) if dot else ""
    i = 1
    while True:
        cand = directory / (f"{base} ({i}){suffix}")
        if not cand.exists():
            return cand
        i += 1


def dir_upload_target(directory: Path, raw: str) -> Path | None:
    """解析"整个文件夹上传"的浏览器相对路径 → 落盘目标文件。

    浏览器目录上传的 filename 形如 "顶层文件夹/子目录/文件.txt"。
    目录段逐段清洗并要求清洗后原样保留(带 : / * ? 等字符的段直接拒绝),
    同时拒绝 .. / 盘符前缀 / 隐藏段; 非法输入返回 None。
    """
    segs = [s for s in raw.replace("\\", "/").split("/") if s not in ("", ".")]
    while segs and ((len(segs[0]) == 2 and segs[0][1] == ":")
                    or segs[0].lower() == "fakepath"):
        segs.pop(0)          # 去掉 Windows 盘符 / 旧浏览器 C:\fakepath 前缀
    if not segs or any(s == ".." for s in segs):
        return None
    leaf = sanitize_name(segs[-1])
    if not leaf:
        return None
    base = directory
    for s in segs[:-1]:
        if not s or s.startswith("."):
            return None
        clean = sanitize_name(s)
        if clean != s:
            return None
        base = base / clean
    return base / leaf


class _TooBig(Exception):
    pass


class _Pb:
    """带 pushback 的原始读取器(自己控制缓冲, 避免混用 readline 的预读问题)。"""

    def __init__(self, f):
        self.f = f
        self.pb = b""

    def read_raw(self):
        if self.pb:
            d, self.pb = self.pb, b""
            return d
        return self.f.read(CHUNK)

    def push(self, data):
        if data:
            self.pb = data + self.pb

    def peek(self, n):
        while len(self.pb) < n:
            d = self.f.read(CHUNK)
            if not d:
                break
            self.pb += d
        return self.pb[:n]


def _read_until(r: _Pb, delim: bytes) -> bytes | None:
    """消耗输入直到 delim, 返回其前的字节; EOF 未遇 delim 返回 None。"""
    parts = []
    tail = b""
    while True:
        chunk = r.read_raw()
        if not chunk:
            return None
        window = tail + chunk
        idx = window.find(delim)
        if idx >= 0:
            parts.append(window[:idx])
            r.push(window[idx + len(delim):])
            return b"".join(parts)
        keep = len(delim) - 1
        if len(window) > keep:
            parts.append(window[:-keep])
            tail = window[-keep:]
        else:
            tail = window


def _stream_until(r: _Pb, delim: bytes, writer, limit: int, counter: list) -> bool:
    """把输入流式写到 writer 直到 delim(不含); 超 limit 抛 _TooBig。"""
    tail = b""
    while True:
        chunk = r.read_raw()
        if not chunk:
            return False
        window = tail + chunk
        idx = window.find(delim)
        if idx >= 0:
            body = window[:idx]
            if body:
                writer.write(body)
                counter[0] += len(body)
            r.push(window[idx + len(delim):])
            if limit and counter[0] > limit:
                raise _TooBig()
            return True
        keep = len(delim) - 1
        if len(window) > keep:
            out = window[:-keep]
            writer.write(out)
            counter[0] += len(out)
            tail = window[-keep:]
        else:
            tail = window
        if limit and counter[0] > limit:
            raise _TooBig()


_UPLOAD_JS = """
<script>
var PREFIX = __PREFIX__;
var _depth = 0;

function setStatus(txt, ok) {
    var el = document.getElementById('status');
    el.style.display = 'block';
    el.textContent = txt;
    el.style.color = ok ? '#18794e' : '#c0392b';
}

function uploadFiles(fileList) {
    if (!fileList.length) return;
    var fd = new FormData();
    for (var i = 0; i < fileList.length; i++) {
        fd.append('file', fileList[i]);
    }
    setStatus('正在上传 ' + fileList.length + ' 个文件 …', true);
    var xhr = new XMLHttpRequest();
    xhr.open('POST', PREFIX, true);
    xhr.upload.onprogress = function (ev) {
        if (ev.lengthComputable) {
            var pct = Math.round(ev.loaded / ev.total * 100);
            setStatus('上传中 ' + pct + '% …', true);
        }
    };
    xhr.onload = function () {
        if (xhr.status >= 200 && xhr.status < 400) {
            setStatus('上传完成, 正在刷新列表 …', true);
            setTimeout(function () {
                window.location.href = PREFIX + '?ok';
            }, 500);
        } else {
            setStatus('上传失败: HTTP ' + xhr.status, false);
        }
    };
    xhr.onerror = function () {
        setStatus('上传失败: 网络错误', false);
    };
    xhr.send(fd);
}

function showDrop() { document.getElementById('dropmsg').style.display = 'block'; }
function hideDrop() { document.getElementById('dropmsg').style.display = 'none'; }

document.addEventListener('dragenter', function (ev) {
    if (!ev.dataTransfer || !ev.dataTransfer.types || ev.dataTransfer.types.indexOf('Files') === -1) return;
    ev.preventDefault();
    _depth++;
    showDrop();
}, false);
document.addEventListener('dragover', function (ev) {
    ev.preventDefault();
}, false);
document.addEventListener('dragleave', function (ev) {
    ev.preventDefault();
    _depth--;
    if (_depth <= 0) { _depth = 0; hideDrop(); }
}, false);
document.addEventListener('drop', function (ev) {
    ev.preventDefault();
    _depth = 0;
    hideDrop();
    uploadFiles(ev.dataTransfer.files);
}, false);
</script>
"""


def _decode_filename(raw: bytes) -> str:
    """multipart 里文件名多为 UTF-8 原字节, 兜底 latin-1。"""
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError:
        return raw.decode("latin-1")


def make_handler(share_root: Path, password: str | None,
                 allow_upload: bool = True, max_bytes: int = 0):
    root = share_root.resolve()

    def authorized(self: BaseHTTPRequestHandler) -> bool:
        if not password:
            return True
        header = self.headers.get("Authorization", "")
        if not header.startswith("Basic "):
            return False
        try:
            decoded = base64.b64decode(header[6:].strip()).decode("utf-8")
            _, _, given = decoded.partition(":")
        except Exception:
            return False
        return hmac.compare_digest(given, password)

    def resolve_path(self: BaseHTTPRequestHandler) -> Path | None:
        raw = urllib.parse.urlsplit(self.path).path
        rel = urllib.parse.unquote(raw).lstrip("/")
        target = (root / rel).resolve()
        if target != root and root not in target.parents:
            return None
        return target

    class Handler(BaseHTTPRequestHandler):
        server_version = "LANShare/1.1"

        # ---------- 通用 ----------
        def _send_error_page(self, code: int, text: str) -> None:
            body = ("<!doctype html><html><head><meta charset='utf-8'>"
                    "<title>" + str(code) + "</title></head><body>"
                    "<h1>" + str(code) + "</h1><p>" + _enc(text) + "</p>"
                    "<p><a href='/'>返回首页</a></p></body></html>").encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            if self.command != "HEAD":
                self.wfile.write(body)

        def _listing(self, target: Path) -> None:
            rel = str(target.relative_to(root)).replace(os.sep, "/")
            if rel == ".":
                rel = ""
            prefix = "/" + rel + ("/" if rel else "")
            parts = rel.split("/") if rel else []
            crumbs = ["<a href='/'>根目录</a>"]
            acc = ""
            for part in parts:
                acc += "/" + part
                crumbs.append("<a href='" + _enc(acc) + "'>" + _enc(part) + "</a>")
            rows = []
            if rel:
                up = "/".join(parts[:-1]) if len(parts) > 1 else ""
                rows.append("<tr><td><a href='/"
                            + _enc(up) + "'>.. (上级目录)</a></td>"
                            + "<td></td><td></td><td></td></tr>")
            items = []
            try:
                for p in sorted(target.iterdir(),
                                key=lambda x: (not x.is_dir(), x.name.lower())):
                    if p.name.startswith("."):
                        continue
                    items.append(p)
            except OSError as e:
                self._send_error_page(403, "无法读取目录: " + str(e))
                return
            for p in items:
                is_dir = p.is_dir()
                href = _enc(prefix + p.name + ("/" if is_dir else ""))
                nm = p.name + ("/" if is_dir else "")
                try:
                    size = "" if is_dir else fmt_size(p.stat().st_size)
                    mtime = datetime.fromtimestamp(
                        p.stat().st_mtime).strftime("%Y-%m-%d %H:%M")
                except OSError:
                    size, mtime = "", ""
                action = ""
                if is_dir:
                    action = ("<a href='" + _enc(prefix + p.name + "/?zip")
                              + "' title='打包下载整个文件夹(含子目录)'>ZIP</a>")
                rows.append("<tr><td><a href='" + href + "'>" + _enc(nm)
                            + "</a></td><td class='n'>" + size
                            + "</td><td class='n'>" + mtime
                            + "</td><td>" + action + "</td></tr>")
            banner = ""
            if "?ok" in self.path:
                banner = "<p class='ok'>上传完成</p>"
            form = ""
            drop_ui = ""
            if allow_upload:
                form = ("<form method='post' action='" + _enc(prefix)
                        + "' enctype='multipart/form-data'>"
                        + "<input type='file' name='file' multiple> "
                        + "<button>上传文件</button></form>"
                        + "<form method='post' action='" + _enc(prefix)
                        + "' enctype='multipart/form-data'>"
                        + "<input type='file' name='dir' webkitdirectory multiple> "
                        + "<button>上传整个文件夹</button></form>")
                safe_prefix = json.dumps(prefix)
                drop_ui = (
                    "<div id='status' style='display:none;margin:8px 0;"
                    "font-weight:600'></div>"
                    + "<div id='dropmsg' style='display:none;position:fixed;"
                    "left:10px;right:10px;top:10px;bottom:10px;"
                    "border:3px dashed #1e88e5;background:rgba(33,150,243,0.10);"
                    "text-align:center;font-size:24px;color:#0d47a1;"
                    "padding-top:18%;pointer-events:none;z-index:999;'>"
                    "松开鼠标即可上传到当前目录</div>"
                    + "<script>"
                    + _UPLOAD_JS.replace("__PREFIX__", safe_prefix)
                    + "</script>"
                )
            body = (
                "<!doctype html><html lang='zh'><head><meta charset='utf-8'>"
                "<title>文件分享</title>"
                "<style>body{font-family:system-ui,Segoe UI,sans-serif;margin:2em auto;"
                "max-width:920px;color:#222}table{border-collapse:collapse;width:100%}"
                "td,th{border-bottom:1px solid #eee;padding:6px 10px;text-align:left}"
                ".n{color:#888;white-space:nowrap}.ok{color:#18794e;font-weight:600}"
                "a{text-decoration:none}a:hover{text-decoration:underline}"
                "form{margin:14px 0 6px}input[type=file]{margin-right:8px}"
                "</style></head><body>"
                "<h2>文件分享</h2>" + banner + "<p>" + " / ".join(crumbs) + "</p>"
                + form + drop_ui
                + "<table><tr><th>名称</th><th>大小</th><th>修改时间</th>"
                + "<th>操作</th></tr>"
                + "".join(rows)
                + "</table>"
                + "<p style='color:#999'>点击文件下载; 目录点 ZIP 可整包下载; "
                + "上传可用上面的两个框(文件 / 整个文件夹)</p>"
                + "</body></html>").encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            if self.command != "HEAD":
                self.wfile.write(body)

        # ---------- 下载 ----------
        def _send_file(self, target: Path) -> None:
            try:
                size = target.stat().st_size
            except OSError as e:
                self._send_error_page(404, "文件不存在: " + str(e))
                return
            ctype = mimetypes.guess_type(target.name)[0] or "application/octet-stream"
            filename = urllib.parse.quote(target.name)
            start = 0
            end = size - 1
            status = 200
            rng = self.headers.get("Range")
            if rng and rng.startswith("bytes="):
                try:
                    a, _, b = rng[6:].partition("-")
                    if a:
                        start = int(a)
                    if b:
                        end = min(int(b), size - 1)
                    if start > end or start >= size:
                        self.send_response(416)
                        self.send_header("Content-Range", f"bytes */{size}")
                        self.end_headers()
                        return
                    status = 206
                except ValueError:
                    pass
            length = end - start + 1
            self.send_response(status)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(length))
            self.send_header("Accept-Ranges", "bytes")
            if status == 206:
                self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
            q = chr(34)
            self.send_header("Content-Disposition",
                             "attachment; filename=" + q + filename + q
                             + "; filename*=UTF-8''" + filename)
            self.end_headers()
            if self.command == "HEAD":
                return
            try:
                with open(target, "rb") as fh:
                    fh.seek(start)
                    remaining = length
                    while remaining > 0:
                        data = fh.read(min(CHUNK, remaining))
                        if not data:
                            break
                        self.wfile.write(data)
                        remaining -= len(data)
            except (BrokenPipeError, ConnectionResetError):
                pass

        # ---------- 整目录 ZIP 打包下载 ----------
        def _send_zip(self, target: Path) -> None:
            """把目录整棵打成 zip 发给客户端。

            为保证 CRC/中央目录正确, 先写入系统临时文件, 再按 Content-Length
            整包发出; 打包跳过以 . 开头的隐藏项与打不开的文件, 空目录也会保留。
            """
            if target == root:
                base = (root.name or "share").lstrip(".") or "share"
            else:
                base = target.name
            arc_top = base.replace("\\", "/").rstrip("/") or "share"
            tmp = tempfile.TemporaryFile(prefix="lanzip_", suffix=".zip")
            try:
                with zipfile.ZipFile(tmp, "w", zipfile.ZIP_DEFLATED,
                                     allowZip64=True) as zf:
                    for dirpath, dirnames, filenames in os.walk(target):
                        dp = Path(dirpath)
                        rel = dp.relative_to(target)
                        arc_dir = (arc_top if rel == Path(".")
                                   else arc_top + "/" + rel.as_posix())
                        try:
                            zf.write(dp, arcname=arc_dir + "/")
                        except OSError:
                            pass
                        dirnames[:] = sorted(d for d in dirnames
                                             if not d.startswith("."))
                        for fn in sorted(f for f in filenames
                                         if not f.startswith(".")):
                            try:
                                zf.write(dp / fn, arcname=arc_dir + "/" + fn)
                            except OSError:
                                continue
                tmp.flush()
                size = os.fstat(tmp.fileno()).st_size
            except OSError as e:
                tmp.close()
                self._send_error_page(500, "打包失败: " + str(e))
                return
            filename = urllib.parse.quote(base + ".zip")
            self.send_response(200)
            self.send_header("Content-Type", "application/zip")
            self.send_header("Content-Length", str(size))
            q = chr(34)
            self.send_header("Content-Disposition",
                             "attachment; filename=" + q + filename + q
                             + "; filename*=UTF-8''" + filename)
            self.end_headers()
            if self.command == "HEAD":
                tmp.close()
                return
            try:
                tmp.seek(0)
                while True:
                    data = tmp.read(CHUNK)
                    if not data:
                        break
                    self.wfile.write(data)
            except (BrokenPipeError, ConnectionResetError):
                pass
            finally:
                tmp.close()

        # ---------- 上传 ----------
        def _do_upload(self, target: Path) -> None:
            if not allow_upload:
                self._send_error_page(403, "本服务器已关闭上传(--no-upload)")
                return
            if not target.is_dir():
                self._send_error_page(400, "只能上传到目录路径")
                return
            ct = self.headers.get("Content-Type") or ""
            if "multipart/form-data" not in ct:
                self._send_error_page(400, "上传内容必须是 multipart/form-data")
                return
            try:
                length = int(self.headers.get("Content-Length") or 0)
            except ValueError:
                length = 0
            if max_bytes and length > max_bytes:
                self._send_error_page(413, f"超过大小上限 {fmt_size(max_bytes)}")
                return
            boundary = ct.partition("boundary=")[2].strip().strip(chr(34))
            if not boundary:
                self._send_error_page(400, "缺少 boundary")
                return
            marker = b"--" + boundary.encode("ascii", "ignore")
            end_marker = _CRLF + marker
            tmp = tempfile.TemporaryFile()
            try:
                total = 0
                remaining = length
                # 注意: http.server 的 rfile 不按 Content-Length 截断,
                # 必须自己只读 length 字节, 否则会阻塞等待下一个请求。
                while remaining > 0:
                    chunk = self.rfile.read(min(CHUNK, remaining))
                    if not chunk:
                        break
                    total += len(chunk)
                    if max_bytes and total > max_bytes:
                        raise _TooBig()
                    tmp.write(chunk)
                    remaining -= len(chunk)
                tmp.flush()
                tmp.seek(0)
                r = _Pb(tmp)
                if _read_until(r, marker) is None:
                    self._send_error_page(400, "未找到 multipart 起始标记")
                    return
                saved = []
                while True:
                    if r.peek(2) == b"--":
                        break
                    if _read_until(r, _CRLF) is None:
                        break
                    header_lines = []
                    line = b""
                    while True:
                        line = _read_until(r, _CRLF)
                        if line is None or not line:
                            break
                        header_lines.append(line)
                    if line is None:
                        break
                    name = b""
                    filename = b""
                    for hline in header_lines:
                        head, _, value = hline.partition(b":")
                        if head.lower() != b"content-disposition":
                            continue
                        for seg in value.split(b";"):
                            key, eq, val = seg.strip().partition(b"=")
                            if not eq:
                                continue
                            val = val.strip().strip(_QUOTE)
                            if key == b"name":
                                name = val
                            elif key == b"filename":
                                filename = val
                    field = name.decode("utf-8", "replace")
                    raw_name = _decode_filename(filename) if filename else None
                    counter = [0]
                    target_path = None
                    try:
                        if field == "dir" and raw_name:
                            # —— 整个文件夹上传: filename 是相对路径 ——
                            mapped = dir_upload_target(target, raw_name)
                            if mapped is None:
                                self._send_error_page(
                                    400, "文件夹上传含非法路径(.. / 盘符 / 隐藏名), 已取消")
                                return
                            try:
                                resolved = mapped.resolve()
                            except OSError:
                                resolved = None
                            if resolved is None or (resolved != root
                                                   and root not in resolved.parents):
                                self._send_error_page(403, "文件夹上传目标越出共享目录")
                                return
                            try:
                                mapped.parent.mkdir(parents=True, exist_ok=True)
                            except OSError as e:
                                self._send_error_page(500, "创建目录失败: " + str(e))
                                return
                            write_dir = mapped.parent
                            use_name = mapped.name
                        elif raw_name:
                            write_dir = target
                            use_name = sanitize_name(raw_name)
                        else:
                            write_dir = target
                            use_name = None
                        if use_name:
                            target_path = unique_path(write_dir, use_name)
                            wf = open(target_path, "wb")
                        else:
                            wf = open(os.devnull, "wb")
                        try:
                            found = _stream_until(r, end_marker, wf,
                                                  max_bytes, counter)
                        finally:
                            wf.close()
                        if target_path is not None:
                            if found:
                                saved.append(target_path.name)
                            else:
                                try:
                                    target_path.unlink()
                                except OSError:
                                    pass
                    except _TooBig:
                        if target_path is not None:
                            try:
                                target_path.unlink()
                            except OSError:
                                pass
                        self._send_error_page(413, "超过大小上限, 已取消该文件")
                        return
                    except OSError as e:
                        if target_path is not None:
                            try:
                                target_path.unlink()
                            except OSError:
                                pass
                        self._send_error_page(500, "写入失败: " + str(e))
                        return
                if not saved:
                    self._send_error_page(400, "没有收到任何文件")
                    return
                location = urllib.parse.urlsplit(self.path).path
                if not location.endswith("/"):
                    location += "/"
                self.send_response(303)
                self.send_header("Location", location + "?ok")
                self.end_headers()
            except _TooBig:
                self._send_error_page(413, f"超过大小上限 {fmt_size(max_bytes)}")
            finally:
                tmp.close()

        # ---------- 路由 ----------
        def _handle(self) -> None:
            if not authorized(self):
                self.send_response(401)
                realm = chr(34) + "LAN Share" + chr(34)
                self.send_header("WWW-Authenticate", "Basic realm=" + realm)
                self.send_header("Content-Length", "0")
                self.end_headers()
                return
            target = resolve_path(self)
            if target is None:
                self._send_error_page(403, "禁止访问该路径")
                return
            if not target.exists():
                self._send_error_page(404, "路径不存在")
                return
            if self.command == "POST":
                self._do_upload(target)
                return
            want_zip = target.is_dir() and "zip" in urllib.parse.parse_qs(
                urllib.parse.urlsplit(self.path).query, keep_blank_values=True)
            if want_zip:
                self._send_zip(target)
                return
            if target.is_dir():
                if not self.path.endswith("/"):
                    self.send_response(301)
                    self.send_header("Location", self.path + "/")
                    self.end_headers()
                    return
                self._listing(target)
                return
            self._send_file(target)

        def do_GET(self) -> None:
            self._handle()

        def do_HEAD(self) -> None:
            self._handle()

        def do_POST(self) -> None:
            self._handle()

        def log_message(self, fmt: str, *args) -> None:
            print("[{}] {}".format(self.log_date_time_string(), fmt % args),
                  file=sys.stderr)

    return Handler


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--dir", default=str(DEFAULT_SHARE), help="要分享的文件夹")
    ap.add_argument("--port", type=int, default=8000)
    ap.add_argument("--host", default="0.0.0.0", help="监听地址")
    ap.add_argument("--password", default="", help="访问密码(留空则不设)")
    ap.add_argument("--no-upload", action="store_true", help="关闭上传(只读分享)")
    ap.add_argument("--max-upload", type=int, default=0,
                    help="单次上传大小上限(MB), 0 为不限制")
    args = ap.parse_args()

    share = Path(args.dir)
    if not share.exists():
        share.mkdir(parents=True, exist_ok=True)
        (share / "欢迎.txt").write_text(
            "把想分享给别人的文件放到这个文件夹。", encoding="utf-8")
    print("共享目录: " + str(share))
    if args.password:
        print("已启用密码访问")
    else:
        print("警告: 未设密码, 同一网络内任何人都能访问!")
    if args.no_upload:
        print("上传: 已关闭(只读分享)")
    else:
        limit = "不限" if args.max_upload <= 0 else str(args.max_upload) + " MB"
        print("上传: 已开启(支持文件与整个文件夹, 单次上限 " + limit
              + ", 同名自动加序号不覆盖)")

    Handler = make_handler(share, args.password or None,
                           allow_upload=not args.no_upload,
                           max_bytes=args.max_upload * 1024 * 1024)
    try:
        httpd = ThreadingHTTPServer((args.host, args.port), Handler)
    except OSError as e:
        print("启动失败: " + str(e))
        return 1
    port = httpd.server_address[1]

    print()
    print("访问地址(浏览器打开):")
    for ip in lan_ips():
        print("    http://" + ip + ":" + str(port) + "    (局域网)")
    if args.host == "0.0.0.0":
        print("    http://127.0.0.1:" + str(port) + "  (本机自测)")
    tail = tailscale_ips()
    exe = tailscale_exe()
    if tail:
        print("    Tailscale 已登录, 组网内成员可用:")
        for ip in tail:
            print("    http://" + ip + ":" + str(port) + "    (Tailscale)")
        if exe:
            print()
            print("Tailscale Funnel(可选): 让对方无需装 Tailscale, 用 https 公网链接访问")
            print("    管理员执行: " + exe + " funnel " + str(port))
            print("    然后把 https://<你的机器名>.<你的tailnet名>.ts.net 发给他们")
            print("    (需先在 https://login.tailscale.com/admin 启用 Funnel; 建议保留密码)")
    print()
    print("Windows 防火墙: 若别人打不开, 请以管理员运行一次:")
    q = chr(34)
    print("    netsh advfirewall firewall add rule name=" + q + "LANShare" + q
          + " dir=in action=allow protocol=TCP localport=" + str(port))
    print()
    print("按 Ctrl+C 停止")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        httpd.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
