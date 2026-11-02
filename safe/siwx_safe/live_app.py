"""Command-line launch; anonymous browser controls; no desktop UI or auto-open."""
import argparse
from pathlib import Path
import sys

from .live import Engine, discover_accounts
from .local_web import LocalServer


def main(argv=None):
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")
    parser = argparse.ArgumentParser(description="SIWX × wexport 精简版：命令行启动，网页提取、查看与导出；默认网络匿名访问")
    parser.add_argument("--version", action="version", version="1.2.0")
    parser.add_argument("--data", type=Path, help="本人账号目录（包含db_storage），不指定则查找当前用户常用目录")
    parser.add_argument("--port", type=int, default=8787)
    parser.add_argument("--host", choices=("0.0.0.0", "127.0.0.1"), default="0.0.0.0",
                        help="默认0.0.0.0，允许连接本机网络接口；127.0.0.1只允许本机")
    parser.add_argument("--no-open", action="store_true", help=argparse.SUPPRESS)
    args = parser.parse_args(argv)
    if sys.platform != "win32":
        parser.exit(1, "自动提取版仅支持Windows微信4.x；已有JSON可用离线版。\n")
    if not 0 <= args.port <= 65535:
        parser.error("端口范围为0至65535，0表示自动选择")
    try:
        engine = Engine(discover_accounts(args.data))
        server = LocalServer(engine, args.port, args.host)
    except (OSError, ValueError) as exc:
        parser.exit(1, f"启动未完成：{exc}\n")
    print("仅处理本人有权访问的数据。提取会读取本人微信进程，仍可能触发安全软件。")
    print(f"监听 {args.host}:{server.server_port}；不使用令牌或密码。")
    print("任何能连接此端口的设备都可提取、查看、导出或停止程序；HTTP未加密。")
    print("命令行启动，无独立桌面窗口，不自动打开浏览器；保留本窗口，Ctrl+C可停止。")
    print("请手动打开网址，通过网页操作：")
    for url in server.urls:
        print(url)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0
