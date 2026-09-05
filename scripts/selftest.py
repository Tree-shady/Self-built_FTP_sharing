"""纯逻辑自测（只依赖标准库, 不需要 GUI）。

运行:  .venv/Scripts/python.exe scripts/selftest.py
"""
from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core.base import Session  # noqa: E402
from core.errors import RemoteError  # noqa: E402
from core.listing import parse_list_line, parse_list_text, parse_mlsd_time  # noqa: E402
from core.models import RemoteEntry, SiteProfile  # noqa: E402
from core.paths import rname  # noqa: E402
from core.tree import download_tree, upload_tree  # noqa: E402
from core.paths import (  # noqa: E402
    rjoin,
    rname,
    rnorm,
    rparent,
    sanitize_windows_name,
)

_failures: list[str] = []


def check(label: str, cond: bool, extra: str = "") -> None:
    if cond:
        print(f"  ok  {label}")
    else:
        _failures.append(label)
        print(f"FAIL  {label}  {extra}")


def test_paths() -> None:
    print("[paths]")
    check("rnorm 去重斜杠", rnorm("a//b///c") == "/a/b/c")
    check("rnorm 根", rnorm("") == "/" and rnorm("/") == "/")
    check("rnorm 去尾斜杠", rnorm("/a/b/") == "/a/b")
    check("rjoin 相对", rjoin("/a/b", "c.txt") == "/a/b/c.txt")
    check("rjoin 绝对覆盖", rjoin("/a", "/x/y") == "/x/y")
    check("rparent", rparent("/a/b/c") == "/a/b" and rparent("/a") == "/" and rparent("/") == "/")
    check("rname", rname("/a/b.txt") == "b.txt" and rname("/") == "/")
    check("windows 非法字符", sanitize_windows_name('a<b>c:"d|e?f*g') == "a_b_c__d_e_f_g")
    check("windows 尾点", sanitize_windows_name("name.") == "name")
    check("windows 保留名", sanitize_windows_name("CON") == "CON_")
    check("windows 中文保留", sanitize_windows_name("CON.txt") == "CON.txt_")


def test_listing() -> None:
    print("[listing]")
    u = parse_list_line("drwxr-xr-x 2 root root 4096 Jan 01 12:34 我的目录")
    check("unix 目录", u is not None and u.is_dir and u.name == "我的目录" and u.size == 4096)
    f = parse_list_line("-rw-r--r-- 1 user staff 12345 Jan 01 2024 notes.txt")
    check("unix 文件", f is not None and not f.is_dir and f.size == 12345
          and f.name == "notes.txt" and f.mtime is not None)
    ms = parse_list_line("07-15-24  03:12PM       <DIR>          backup")
    check("msdos 目录", ms is not None and ms.is_dir and ms.name == "backup")
    mf = parse_list_line("07-15-24  03:12PM              2048        report.pdf")
    check("msdos 文件", mf is not None and not mf.is_dir and mf.size == 2048
          and mf.name == "report.pdf")
    check("忽略总行", parse_list_line("total 48") is None)
    txt = "-rw-r--r-- 1 a b 1 Jan 01 00:00 x\n" + "drwxr-xr-x 1 a b 2 Jan 01 00:00 d\n"
    check("parse_list_text 计数", len(parse_list_text(txt)) == 2)
    check("mlsd 时间", parse_mlsd_time("20240102123456") is not None)


def test_site_defaults() -> None:
    print("[models]")
    s1 = SiteProfile(name="t", protocol="sftp", host="h")
    check("sftp 默认端口 22", s1.port == 22)
    s2 = SiteProfile(name="t", protocol="ftps", host="h")
    check("ftps 默认端口 21", s2.port == 21)
    s3 = SiteProfile(name="t", protocol="ftps", host="h", remote_root="pub/")
    check("remote_root 规范化", s3.remote_root == "/pub")


def test_storage() -> None:
    print("[storage]")
    import shutil

    from core import storage
    # 注: 不用 tempfile.mkdtemp —— 某些受限环境(如本机沙箱)不允许向
    # tempfile 创建的目录里再写文件, 这里手动建唯一目录。
    base = Path(__file__).resolve().parent.parent
    n = 0
    while True:
        tmp = base / ("cfgtmp_" + os.environ.get("SELFTEST_UID", str(n)))
        if not tmp.exists():
            break
        n += 1
    os.mkdir(tmp)
    try:
        old = os.environ.get("FTPSHARING_CONFIG_DIR")
        os.environ["FTPSHARING_CONFIG_DIR"] = str(tmp)
        storage.save_sites([SiteProfile(name="甲", protocol="ftps", host="127.0.0.1")])
        loaded = storage.load_sites()
        check("存储往返", len(loaded) == 1 and loaded[0].name == "甲" and loaded[0].host == "127.0.0.1")
        os.environ.pop("FTPSHARING_CONFIG_DIR", None)
        if old is not None:
            os.environ["FTPSHARING_CONFIG_DIR"] = old
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


class FakeSession(Session):
    """内存目录树, 用于测 core/tree 的递归下载/上传逻辑。

    root: 嵌套 dict; 目录→dict, 文件→bytes。
    """

    protocol = "fake"

    def __init__(self, root):
        self.root = root
        self.uploaded = []

    def _parts(self, p):
        return [x for x in rnorm(p).split("/") if x]

    def _get(self, p):
        node = self.root
        for part in self._parts(p):
            if not isinstance(node, dict) or part not in node:
                return None
            node = node[part]
        return node

    def _set(self, p, value):
        node = self.root
        parts = self._parts(p)
        for part in parts[:-1]:
            nxt = node.setdefault(part, {})
            if not isinstance(nxt, dict):
                raise RemoteError("路径被文件占用: " + part)
            node = nxt
        node[parts[-1]] = value

    # ---- Session 原语 ----
    def connect(self):
        pass

    def close(self):
        pass

    def listdir(self, remote_dir):
        node = self._get(rnorm(remote_dir))
        if not isinstance(node, dict):
            raise RemoteError("不是目录: " + remote_dir)
        out = []
        for name, v in node.items():
            out.append(RemoteEntry(name=name, is_dir=isinstance(v, dict),
                                   size=0 if isinstance(v, dict) else len(v)))
        return out

    def stat(self, remote_path):
        node = self._get(rnorm(remote_path))
        if node is None:
            return None
        return RemoteEntry(name=rname(rnorm(remote_path)),
                           is_dir=isinstance(node, dict),
                           size=0 if isinstance(node, dict) else len(node))

    def mkdir(self, remote_dir):
        self._set(rnorm(remote_dir), {})

    def remove(self, remote_path):
        p = rnorm(remote_path)
        node = self._get(p)
        if not isinstance(node, bytes):
            raise RemoteError("不是文件")
        parent = self._get(rnorm(p.rsplit("/", 1)[0]) if "/" in p else "/")
        parent.pop(rname(p), None)

    def rmdir(self, remote_dir):
        p = rnorm(remote_dir)
        parent = self._get(rnorm(p.rsplit("/", 1)[0]) if "/" in p else "/")
        node = self._get(p)
        if not isinstance(node, dict) or node:
            raise RemoteError("不是空目录")
        parent.pop(rname(p), None)

    def download(self, remote_path, local_path, *, progress=None, cancel=None,
                 resume=False):
        from pathlib import Path
        node = self._get(rnorm(remote_path))
        if not isinstance(node, bytes):
            raise RemoteError("不是文件")
        Path(local_path).write_bytes(node)
        if progress:
            progress(len(node), len(node))

    def upload(self, local_path, remote_path, *, progress=None, cancel=None):
        from pathlib import Path
        data = Path(local_path).read_bytes()
        self._set(rnorm(remote_path), data)
        self.uploaded.append((rnorm(remote_path), data))
        if progress:
            progress(len(data), len(data))


def test_tree() -> None:
    print("[tree 递归下载/上传]")
    import tempfile
    from pathlib import Path

    remote = {
        "资料": {
            "报告.pdf": b"PDF",
            "a:b.txt": b"one",      # 清洗后 a_b.txt
            "a?b.txt": b"two",      # 清洗后 a_b (1).txt
            "图": {"a.png": b"PNG"},
        },
        "空夹": {},
    }
    sess = FakeSession(remote)

    base = Path(__file__).resolve().parent.parent
    n = 0
    tmp = base / ("treetmp_" + os.environ.get("SELFTEST_UID", str(n)))
    while tmp.exists():
        n += 1
        tmp = base / ("treetmp_" + str(n))
    tmp.mkdir()

    try:
        # 下载整棵 /资料 → tmp/dl
        dl = tmp / "dl"
        st = download_tree(sess, "/资料", dl)
        check("下载目录树: 目录数", st.dirs == 2 and (dl / "图").is_dir())
        check("下载目录树: 文件数", st.files == 4)
        check("下载内容一致",
              (dl / "报告.pdf").read_bytes() == b"PDF"
              and (dl / "图" / "a.png").read_bytes() == b"PNG")
        check("清洗名 a_b.txt", (dl / "a_b.txt").exists())
        check("清洗重名加序号", (dl / "a_b (1).txt").exists()
              and (dl / "a_b (1).txt").read_bytes() == b"two")
        # 空目录也要被创建
        dl2 = tmp / "dl2"
        st2 = download_tree(sess, "/空夹", dl2)
        check("空目录下载后本地存在", dl2.is_dir() and st2.dirs == 1 and st2.files == 0)

        # 上传本地目录树 → 假远端 /上
        src = tmp / "src"
        src.mkdir()
        (src / "x.txt").write_bytes(b"x1")
        (src / "sub").mkdir()
        (src / "sub" / "y.bin").write_bytes(b"y2")
        (src / "emptyz").mkdir()
        (src / ".hidden").write_text("skip me")
        sess2 = FakeSession({})
        st3 = upload_tree(sess2, src, "/上")
        check("上传目录树: 目录数", st3.dirs == 3)   # 上 + sub + emptyz
        check("上传目录树: 文件数", st3.files == 2)
        check("上传字节一致",
              sess2._get("/上/x.txt") == b"x1"
              and sess2._get("/上/sub/y.bin") == b"y2")
        check("空目录已创建", isinstance(sess2._get("/上/emptyz"), dict))
        check("隐藏文件被跳过", sess2._get("/上/.hidden") is None)
    finally:
        import shutil
        shutil.rmtree(tmp, ignore_errors=True)


def main() -> int:
    test_paths()
    test_listing()
    test_site_defaults()
    test_storage()
    test_tree()
    print()
    if _failures:
        print(f"失败 {len(_failures)} 项: {_failures}")
        return 1
    print("全部通过")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
