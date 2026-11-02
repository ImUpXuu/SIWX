"""Minimal live edition CLI. No background install or remote bind options."""
import argparse
from pathlib import Path
import sys
import webbrowser

from .live import Engine, discover_accounts
from .local_web import LocalServer


def main(argv=None):
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")
    parser = argparse.ArgumentParser(description="SIWX × wexport 精简版：本人微信4.x自动提取、本机网页与导出")
    parser.add_argument("--version", action="version", version="1.1.0")
    parser.add_argument("--data", type=Path, help="本人账号目录（包含db_storage），不指定则查找当前用户常用目录")
    parser.add_argument("--port", type=int, default=8787)
    parser.add_argument("--no-open", action="store_true", help="只打印本机网址，不自动打开浏览器")
    args = parser.parse_args(argv)
    if sys.platform != "win32":
        parser.exit(1, "自动提取版仅支持Windows微信4.x；已有JSON可用离线版。\n")
    if not 0 <= args.port <= 65535:
        parser.error("端口范围为0至65535，0表示自动选择")
    try:
        engine = Engine(discover_accounts(args.data))
        try:
            server = LocalServer(engine, args.port)
        except OSError:
            server = LocalServer(engine, 0)
    except (OSError, ValueError) as exc:
        parser.exit(1, f"启动未完成：{exc}\n")
    print("仅处理本人有权访问的数据。提取会读取本人微信进程，仍可能触发安全软件。")
    print("保留此窗口，网页点击退出或按Ctrl+C停止。仅本机可访问，完整网址请勿转发：")
    print(server.url)
    if not args.no_open:
        try:
            webbrowser.open(server.url)
        except Exception:
            print("浏览器未打开，请复制上方完整网址。")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0
