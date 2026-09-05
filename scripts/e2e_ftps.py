"""端到端 FTPS 冒烟测试（需要先启动 scripts/dev_server.py）。

验证: 连接(显式TLS+数据通道加密) → 列目录(中文文件名) →
下载 → 上传 → 下载比对 → 断点续传。
"""
from __future__ import annotations

import os
import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from core.factory import create_session  # noqa: E402
from core.models import SiteProfile  # noqa: E402
from core.tree import download_tree, upload_tree  # noqa: E402

WORK = ROOT / ".e2e_tmp"
PORT = int(os.environ.get("FTP_PORT", "2121"))


def _cleanup() -> None:
    shutil.rmtree(WORK, ignore_errors=True)


def main() -> int:
    _cleanup()
    WORK.mkdir()

    site = SiteProfile(
        name="本地测试", protocol="ftps", host="127.0.0.1", port=PORT,
        username="test", password="123456",
        insecure_tls=True, encoding="auto")
    sess = create_session(site)
    sess.connect()
    print("[1] 连接成功 (FTPS 显式TLS)")

    entries = sess.listdir("/")
    print("[2] 列目录:")
    for e in entries:
        print("     ", e.name, "dir" if e.is_dir else "file", e.size)

    # --- 下载服务器里预先放好的文件 ---
    src = next((e for e in entries if not e.is_dir), None)
    if src is None:
        print("FAIL: 服务器目录是空的")
        return 1
    local1 = WORK / ("dl_" + src.name)
    sess.download("/" + src.name, str(local1))
    print(f"[3] 下载完成: {src.name} -> {local1.stat().st_size} 字节")

    # --- 上传随机内容文件, 再下载回来比对 ---
    blob = os.urandom(300_000)
    up_name = "上传测试.bin"
    up_local = WORK / up_name
    up_local.write_bytes(blob)
    sess.upload(str(up_local), "/" + up_name)
    print(f"[4] 上传完成: {up_name} ({len(blob)} 字节)")
    st = sess.stat("/" + up_name)
    assert st is not None and st.size == len(blob), "上传后 stat 大小不符"
    dl2 = WORK / "roundtrip.bin"
    sess.download("/" + up_name, str(dl2))
    assert dl2.read_bytes() == blob, "往返内容不一致"
    print("[5] 下载校验通过 (字节一致)")

    # --- 断点续传: 先放一半内容, resume 补齐 ---
    half = blob[:100_000]
    part = WORK / "resume.bin"
    part.write_bytes(half)
    sess.download("/" + up_name, str(part), resume=True)
    assert part.stat().st_size == len(blob), f"续传后大小 {part.stat().st_size} != {len(blob)}"
    assert part.read_bytes() == blob, "续传内容不一致"
    print("[6] 断点续传通过")

    # --- 中文文件名编码(UTF-8 服务器) ---
    try:
        lst = sess.listdir("/")
        names = [e.name for e in lst]
        assert any("上传测试.bin" == n for n in names), "中文文件名列出失败"
        print("[7] UTF-8 中文文件名正常")
    except Exception as e:  # noqa: BLE001
        print(f"[7] 中文文件名检查异常: {e}")

    # --- 目录递归上传/下载 round-trip ---
    def rm_remote(p: str) -> None:
        """递归删除远端目录(测试清理用)。"""
        for e in sess.listdir(p):
            child = "/" + e.name if p == "/" else p + "/" + e.name
            if e.is_dir:
                rm_remote(child)
                sess.rmdir(child)
            else:
                sess.remove(child)

    local_src = WORK / "src_tree"
    (local_src / "doc").mkdir(parents=True)
    (local_src / "doc" / "a.txt").write_bytes(b"A" * 100)
    (local_src / "doc" / "子 目录").mkdir()
    (local_src / "doc" / "子 目录" / "b.bin").write_bytes(bytes(range(256)))
    (local_src / "空夹").mkdir()
    remote_dir = "/treetest_" + str(os.getpid())
    try:
        st = upload_tree(sess, local_src, remote_dir)
        assert st.dirs == 4 and st.files == 2, f"上传统计不符: {st}"
        dl = WORK / "dl_tree"
        st2 = download_tree(sess, remote_dir, dl)
        assert st2.dirs == 4 and st2.files == 2, f"下载统计不符: {st2}"
        assert (dl / "doc" / "a.txt").read_bytes() == b"A" * 100
        assert (dl / "doc" / "子 目录" / "b.bin").read_bytes() == bytes(range(256))
        assert (dl / "空夹").is_dir()
        print("[8] 目录递归上传/下载 round-trip 通过")
    finally:
        try:
            rm_remote(remote_dir)
            sess.rmdir(remote_dir)
        except Exception as e:  # noqa: BLE001
            print(f"[8] 清理远端测试目录失败: {e}")

    sess.close()
    print("E2E 全部通过")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    finally:
        _cleanup()
