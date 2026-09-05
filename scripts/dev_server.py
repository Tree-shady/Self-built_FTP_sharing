"""本地开发/联调用 FTPS 服务器（基于 pyftpdlib）。

作用: 在本机起一个支持 TLS 的 FTP 服务器, 用图形客户端连
      127.0.0.1:2121 即可全流程调试(下载/上传/目录), 不需要真服务器。

首次运行会自动在 scripts/certs 下生成自签名证书(cryptography)。

用法:
    python scripts/dev_server.py                     # 默认端口 2121
    python scripts/dev_server.py --port 2121 --user test --pass 123456
    python scripts/dev_server.py --plain             # 明文 FTP(仅本机测试对照用)
"""
from __future__ import annotations

import argparse
import ssl
import sys
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SHARE = ROOT / "devshare"


def _ensure_cert(cert_dir: Path) -> tuple[Path, Path]:
    cert_dir.mkdir(parents=True, exist_ok=True)
    cert, key = cert_dir / "cert.pem", cert_dir / "key.pem"
    if cert.exists() and key.exists():
        return cert, key

    print("生成自签名证书 …")
    try:
        from cryptography import x509
        from cryptography.hazmat.primitives import hashes, serialization
        from cryptography.hazmat.primitives.asymmetric import rsa
        from cryptography.x509.oid import NameOID
        import datetime as _dt

        name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "localhost")])
        now = _dt.datetime.now(_dt.timezone.utc)
        skey = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        builder = (
            x509.CertificateBuilder()
            .subject_name(name)
            .issuer_name(name)
            .public_key(skey.public_key())
            .serial_number(x509.random_serial_number())
            .not_valid_before(now - _dt.timedelta(days=1))
            .not_valid_after(now + _dt.timedelta(days=3650))
            .add_extension(x509.SubjectAlternativeName([x509.DNSName("localhost")]), False)
        )
        pem = builder.sign(skey, hashes.SHA256())
        key.write_bytes(skey.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.TraditionalOpenSSL,
            serialization.NoEncryption()))
        cert.write_bytes(pem.public_bytes(serialization.Encoding.PEM))
    except ImportError:
        print("未安装 cryptography, 无法生成证书。请先: pip install cryptography")
        sys.exit(1)
    return cert, key


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=2121)
    ap.add_argument("--user", default="test")
    ap.add_argument("--pass", dest="password", default="123456")
    ap.add_argument("--dir", default=str(SHARE))
    ap.add_argument("--plain", action="store_true", help="明文 FTP(不启用 TLS)")
    args = ap.parse_args()

    share = Path(args.dir)
    share.mkdir(parents=True, exist_ok=True)
    (share / "说明.txt").write_text(
        "这是本地测试共享目录。\r\n可以放一些文件来测试下载/中文文件名/断点续传。\r\n",
        encoding="utf-8")

    from pyftpdlib.authorizers import DummyAuthorizer
    from pyftpdlib.servers import FTPServer

    authorizer = DummyAuthorizer()
    authorizer.add_user(args.user, args.password, str(share), perm="elradfmwMT")

    if args.plain:
        from pyftpdlib.handlers import FTPHandler
        handler = FTPHandler
        print("警告: 明文 FTP 仅用于本机对照测试!")
    else:
        from pyftpdlib.handlers.ftps.control import TLS_FTPHandler
        cert, key = _ensure_cert(Path(__file__).resolve().parent / "certs")
        handler = TLS_FTPHandler
        handler.certfile = str(cert)
        handler.keyfile = str(key)
        handler.tls_control_required = True
        print("FTPS (显式 TLS, 数据通道按客户端 PROT P 决定)")

    handler.authorizer = authorizer
    handler.banner = "pyftpdlib dev server"

    server = FTPServer((args.host, args.port), handler)
    print(f"共享目录: {share}")
    print(f"监听:     {args.host}:{args.port}   (本机客户端连 127.0.0.1:{args.port})")
    print(f"账号:     {args.user} / {args.password}")
    print("按 Ctrl+C 停止")
    try:
        server.serve_forever(timeout=5)
    except KeyboardInterrupt:
        pass
    finally:
        server.close_all()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
