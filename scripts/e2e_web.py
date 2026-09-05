"""lan_share.py 的端到端测试(下载 + 上传)。

覆盖: 认证 / 目录列出 / 下载字节一致 / 目录穿越 / Range / HEAD /
多文件上传 / 中文文件名 / 大文件跨块 / 同名自动加序号 / 上传穿越 /
未认证 401 / 大小上限 413 / 只读 403 / 上传到文件 400 /
目录 ZIP 打包下载(嵌套/中文/空目录/根目录) /
整个文件夹上传(相对路径递归建目录 / 清洗拒绝)。
"""
from __future__ import annotations

import base64
import http.client
import io
import os
import shutil
import sys
import threading
import urllib.parse
import zipfile
from http.server import ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from scripts.lan_share import make_handler  # noqa: E402

WORK = ROOT / ".web_test"
PASSWORD = "s3cret"
BOUNDARY = "----e2eBoundary7d1f"
CRLF = b"\r\n"


def main() -> int:
    shutil.rmtree(WORK, ignore_errors=True)
    os.makedirs(WORK / "sub")
    (WORK / "hello.txt").write_bytes(b"hello world")
    (WORK / "中文 名字.txt").write_bytes("内容内容".encode("utf-8"))
    (WORK / "sub" / "nested.bin").write_bytes(bytes(range(256)))
    os.makedirs(WORK / "照片集" / "2024")
    (WORK / "照片集" / "2024" / "风景.jpg").write_bytes(b"jpeg-bytes")
    (WORK / "照片集" / "说明.txt").write_bytes("说明文字".encode("utf-8"))
    os.makedirs(WORK / "空目录")

    def start(handler, port=0):
        httpd = ThreadingHTTPServer(("127.0.0.1", port), handler)
        threading.Thread(target=httpd.serve_forever, daemon=True).start()
        return httpd, httpd.server_address[1]

    httpd, port = start(make_handler(WORK, PASSWORD))
    httpd_small, port_small = start(
        make_handler(WORK, PASSWORD, allow_upload=True, max_bytes=1024))
    httpd_ro, port_ro = start(make_handler(WORK, PASSWORD, allow_upload=False))

    def request(method, path, port=port, password=None, headers=None, body=None):
        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=30)
        h = dict(headers or {})
        if password is not None:
            token = base64.b64encode(("share:" + password).encode()).decode()
            h["Authorization"] = "Basic " + token
        conn.request(method, path, body=body, headers=h)
        resp = conn.getresponse()
        data = resp.read()
        conn.close()
        return resp.status, dict(resp.getheaders()), data

    fails = []

    def check(label, cond, extra=""):
        print(("  ok  " if cond else "FAIL  ") + label + (("  " + extra) if extra else ""))
        if not cond:
            fails.append(label)

    def make_upload(files, boundary=BOUNDARY):
        q = chr(34)
        body = bytearray()
        for name, data in files:
            if name is None:
                continue
            disp = ("Content-Disposition: form-data; name=" + q + "file" + q
                    + "; filename=" + q + name + q)
            part = (b"--" + boundary.encode() + CRLF
                    + disp.encode("utf-8") + CRLF
                    + b"Content-Type: application/octet-stream" + CRLF + CRLF
                    + data + CRLF)
            body += part
        body += b"--" + boundary.encode() + b"--" + CRLF
        return bytes(body)

    def make_upload_fields(fields, boundary=BOUNDARY):
        """fields: [(表单字段名, 文件名, 内容bytes)] — 文件用 file, 文件夹用 dir。"""
        q = chr(34)
        body = bytearray()
        for fld, filename, data in fields:
            disp = ("Content-Disposition: form-data; name=" + q + fld + q
                    + "; filename=" + q + filename + q)
            part = (b"--" + boundary.encode() + CRLF
                    + disp.encode("utf-8") + CRLF
                    + b"Content-Type: application/octet-stream" + CRLF + CRLF
                    + data + CRLF)
            body += part
        body += b"--" + boundary.encode() + b"--" + CRLF
        return bytes(body)

    upload_headers = {"Content-Type": "multipart/form-data; boundary=" + BOUNDARY}

    # ================= 下载侧 =================
    s, _, _ = request("GET", "/")
    check("未认证返回 401", s == 401)
    s, _, body = request("GET", "/", password=PASSWORD)
    text = body.decode("utf-8")
    check("列表 200 且含英文文件名", s == 200 and "hello.txt" in text)
    check("列表含中文文件名", "中文 名字.txt" in text)
    check("目录行含 ZIP 打包入口", "ZIP" in text and "照片集/" in text)
    check("页面含拖拽上传组件", "dropmsg" in text and "uploadFiles" in text)
    s, _, body = request("GET", "/", port=port_ro, password=PASSWORD)
    check("只读模式不含拖拽组件", "dropmsg" not in body.decode("utf-8"))
    s, _, body = request("GET", "/hello.txt", password=PASSWORD)
    check("下载字节一致", s == 200 and body == b"hello world")
    path = urllib.parse.quote("/中文 名字.txt")
    s, _, body = request("GET", path, password=PASSWORD)
    check("中文名下载一致", s == 200 and body == "内容内容".encode("utf-8"))
    s, _, body = request("GET", "/sub/nested.bin", password=PASSWORD)
    check("子目录下载一致", s == 200 and body == bytes(range(256)))
    s, _, _ = request("GET", "/..%2f..%2f..%2fWindows%2fwin.ini", password=PASSWORD)
    check("目录穿越被拦", s in (403, 404))
    s, hdrs, body = request("GET", "/hello.txt", password=PASSWORD,
                            headers={"Range": "bytes=0-4"})
    check("Range 206+前5字节", s == 206 and body == b"hello"
          and hdrs.get("Content-Range") == "bytes 0-4/11")
    s, _, body = request("HEAD", "/hello.txt", password=PASSWORD)
    check("HEAD 200 无正文", s == 200 and body == b"")

    # ================= 整文件夹 ZIP 打包下载 =================
    qurl = urllib.parse.quote
    s, hdrs, body = request("GET", qurl("/照片集/") + "?zip", password=PASSWORD)
    zf = zipfile.ZipFile(io.BytesIO(body))
    znames = zf.namelist()
    check("文件夹 ZIP 200+类型", s == 200
          and hdrs.get("Content-Type") == "application/zip")
    check("ZIP 顶层目录项", "照片集/" in znames)
    check("ZIP 含嵌套文件", "照片集/2024/风景.jpg" in znames)
    check("ZIP 内容一致", zf.read("照片集/2024/风景.jpg") == b"jpeg-bytes"
          and zf.read("照片集/说明.txt") == "说明文字".encode("utf-8"))
    check("ZIP 项都在顶层目录内",
          all(n.split("/", 1)[0] == "照片集" for n in znames if n != "照片集/"))
    s, _, body = request("GET", "/sub/?zip", password=PASSWORD)
    zf = zipfile.ZipFile(io.BytesIO(body))
    check("子目录 ZIP 内容", s == 200 and "sub/nested.bin" in zf.namelist()
          and zf.read("sub/nested.bin") == bytes(range(256)))
    s, _, body = request("GET", qurl("/空目录/") + "?zip", password=PASSWORD)
    zf = zipfile.ZipFile(io.BytesIO(body))
    check("空目录 ZIP 保留目录项", s == 200 and "空目录/" in zf.namelist())
    s, _, body = request("GET", "/?zip", password=PASSWORD)
    zf = zipfile.ZipFile(io.BytesIO(body))
    znames = zf.namelist()
    check("根目录 ZIP 递归", s == 200
          and "web_test/hello.txt" in znames
          and "web_test/sub/nested.bin" in znames
          and "web_test/照片集/2024/风景.jpg" in znames)
    check("根 ZIP 项带顶层前缀",
          all(n.split("/", 1)[0] == "web_test" for n in znames if n != "web_test/"))
    s, _, _ = request("GET", "/..%2f..%2f..%2f/?zip", password=PASSWORD)
    check("ZIP 目录穿越被拦", s in (403, 404))

    # ================= 上传侧 =================
    big = bytes(range(256)) * 1200   # ~307KB, 远超 64KB 块
    s, _, _ = request("POST", "/", password=PASSWORD, headers=upload_headers,
                      body=make_upload([("报告.pdf", big),
                                        ("作业 提交.txt", "第一份作业".encode("utf-8"))]))
    check("上传成功 303", s == 303)
    s, _, body = request("GET", "/", password=PASSWORD)
    text = body.decode("utf-8")
    check("列表出现新文件", s == 200 and "报告.pdf" in text and "作业 提交.txt" in text)
    p1 = urllib.parse.quote("/报告.pdf")
    s, _, body = request("GET", p1, password=PASSWORD)
    check("上传大文件字节一致", s == 200 and body == big)
    p2 = urllib.parse.quote("/作业 提交.txt")
    s, _, body = request("GET", p2, password=PASSWORD)
    check("中文名上传内容一致", s == 200 and body == "第一份作业".encode("utf-8"))
    # 同名再传 → 自动加 (1)
    s, _, _ = request("POST", "/", password=PASSWORD, headers=upload_headers,
                      body=make_upload([("报告.pdf", b"second")]))
    check("同名上传 303", s == 303)
    s, _, body = request("GET", "/", password=PASSWORD)
    text = body.decode("utf-8")
    check("防覆盖: 出现 (1) 副本", "报告 (1).pdf" in text)
    s, _, body = request("GET", urllib.parse.quote("/报告.pdf"), password=PASSWORD)
    check("原文件未被覆盖", s == 200 and body == big)
    s, _, body = request("GET", urllib.parse.quote("/报告 (1).pdf"), password=PASSWORD)
    check("副本内容正确", s == 200 and body == b"second")
    # 上传到子目录
    s, _, _ = request("POST", "/sub/", password=PASSWORD, headers=upload_headers,
                      body=make_upload([("inside.txt", b"deep")]))
    check("上传到子目录 303", s == 303)
    s, _, body = request("GET", "/sub/inside.txt", password=PASSWORD)
    check("子目录上传内容一致", s == 200 and body == b"deep")
    # 穿越文件名: 存为 basename, 不越界
    evil = r"..\..\evil.txt"
    s, _, _ = request("POST", "/", password=PASSWORD, headers=upload_headers,
                      body=make_upload([(evil, b"escape")]))
    check("穿越文件名上传 303", s == 303)
    s, _, body = request("GET", "/", password=PASSWORD)
    text = body.decode("utf-8")
    check("穿越名被清洗落在根内", s == 200 and "evil.txt" in text)
    outside = WORK.parent / "evil.txt"
    check("没有文件写到共享目录外", not outside.exists())
    # 未认证上传 → 401
    s, _, _ = request("POST", "/", headers=upload_headers,
                      body=make_upload([("x.txt", b"x")]))
    check("未认证上传 401", s == 401)
    # 上传到文件路径 → 400
    s, _, _ = request("POST", "/hello.txt", password=PASSWORD,
                      headers=upload_headers,
                      body=make_upload([("x.txt", b"x")]))
    check("上传到文件 400", s == 400)

    # ============ 上传整个文件夹(相对路径递归建目录) ============
    s, _, _ = request("POST", "/", password=PASSWORD, headers=upload_headers,
                      body=make_upload_fields([
                          ("dir", "我的相册/2024/旅行.jpg", b"photo1"),
                          ("dir", "我的相册/2024/夜景.jpg", b"photo2"),
                          ("dir", "我的相册/笔记.txt", "好记性".encode("utf-8"))]))
    check("整文件夹上传 303", s == 303)
    for pth, want in (("/我的相册/2024/旅行.jpg", b"photo1"),
                      ("/我的相册/2024/夜景.jpg", b"photo2"),
                      ("/我的相册/笔记.txt", "好记性".encode("utf-8"))):
        s, _, body = request("GET", qurl(pth), password=PASSWORD)
        check("目录上传落盘 " + pth, s == 200 and body == want)
    s, _, body = request("GET", "/", password=PASSWORD)
    text = body.decode("utf-8")
    check("列表出现顶层目录", s == 200 and "我的相册" in text)
    # 同目录再传同名 → 文件加序号, 不覆盖旧文件
    s, _, _ = request("POST", "/", password=PASSWORD, headers=upload_headers,
                      body=make_upload_fields(
                          [("dir", "我的相册/2024/旅行.jpg", b"newer")]))
    check("文件夹同名再传 303", s == 303)
    s, _, body = request("GET", qurl("/我的相册/2024/旅行.jpg"), password=PASSWORD)
    check("文件夹内同名不覆盖", s == 200 and body == b"photo1")
    s, _, body = request("GET", qurl("/我的相册/2024/旅行 (1).jpg"), password=PASSWORD)
    check("副本内容为新文件", s == 200 and body == b"newer")
    # 非法相对路径: .. 逃逸 → 400 且不落盘
    s, _, _ = request("POST", "/", password=PASSWORD, headers=upload_headers,
                      body=make_upload_fields([("dir", r"..\..\逃逸\e.txt", b"x")]))
    check("文件夹上传 .. 拒绝 400", s == 400)
    check("逃逸未落盘", not (WORK.parent / "逃逸").exists()
          and not (WORK / "逃逸").exists())
    # 隐藏目录段 → 400
    s, _, _ = request("POST", "/", password=PASSWORD, headers=upload_headers,
                      body=make_upload_fields([("dir", ".secret/h.txt", b"x")]))
    check("隐藏段拒绝 400", s == 400)
    # 盘符 / fakepath 前缀剥落后正常落盘
    s, _, _ = request("POST", "/", password=PASSWORD, headers=upload_headers,
                      body=make_upload_fields(
                          [("dir", "C:/fakepath/盘符目录/f.txt", b"deep")]))
    check("盘符前缀剥离 303", s == 303)
    s, _, body = request("GET", qurl("/盘符目录/f.txt"), password=PASSWORD)
    check("盘符剥离后内容正确", s == 200 and body == b"deep")
    # 上传到子目录
    s, _, _ = request("POST", "/sub/", password=PASSWORD, headers=upload_headers,
                      body=make_upload_fields(
                          [("dir", "深层/再深/leaf.bin", b"zz")]))
    check("子目录内整夹上传 303", s == 303)
    s, _, body = request("GET", qurl("/sub/深层/再深/leaf.bin"), password=PASSWORD)
    check("子目录内落盘正确", s == 200 and body == b"zz")
    # 一次请求混合 文件 + 文件夹
    s, _, _ = request("POST", "/", password=PASSWORD, headers=upload_headers,
                      body=make_upload_fields([
                          ("file", "混合文件.txt", b"m"),
                          ("dir", "混合夹/子/内.txt", b"n")]))
    check("混合 file+dir 303", s == 303)
    s, _, body = request("GET", qurl("/混合文件.txt"), password=PASSWORD)
    check("混合 file 落盘", s == 200 and body == b"m")
    s, _, body = request("GET", qurl("/混合夹/子/内.txt"), password=PASSWORD)
    check("混合 dir 落盘", s == 200 and body == b"n")

    # 只读服务器 → 403
    s, _, _ = request("POST", "/", port=port_ro, password=PASSWORD,
                      headers=upload_headers,
                      body=make_upload([("x.txt", b"x")]))
    check("只读服务器上传 403", s == 403)
    # 大小上限 → 413
    s, _, _ = request("POST", "/", port=port_small, password=PASSWORD,
                      headers=upload_headers,
                      body=make_upload([("toobig.bin", b"z" * 4096)]))
    check("超过大小上限 413", s == 413)

    for srv in (httpd, httpd_small, httpd_ro):
        srv.shutdown()
    shutil.rmtree(WORK, ignore_errors=True)
    print()
    if fails:
        print("失败项:", fails)
        return 1
    print("Web 分享(下载+上传)测试全部通过")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
