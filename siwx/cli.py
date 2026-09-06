"""命令行入口 —— rich 漂亮 TUI。

组件集中在 siwx/tui.py（可扩展），本模块只做参数解析与流程编排。
"""
import argparse
import json
import sys
from pathlib import Path

from siwx import extract, keystore, tui


def _resolve_dirs(db_dir):
    if db_dir:
        return [(extract.wxid_of(db_dir), db_dir)]
    return extract.find_wechat_data_dirs()


def cmd_keys_extract(args) -> int:
    tui.banner()
    reports = extract.extract_all(log=tui.log, use_cache=not args.no_cache)
    if not reports:
        tui.log("✗ 未找到微信数据目录，请确认本机登录过微信")
        return 1
    for r in reports:
        tui.step(f"账号 {r['wxid']}")
        tui.salt_table(r)
        tui.summary_line(r["verified"], r["total_salts"], r["duration_ms"])
    all_ok = all(r["verified"] == r["total_salts"] for r in reports)
    return 0 if all_ok else 2


def cmd_keys_list(_args) -> int:
    tui.banner()
    store = keystore.load()
    if not store:
        tui.log("密钥库为空（先执行 python run.py keys extract）")
        return 0
    tui.step(f"密钥库 · {len(store)} 条 · DPAPI 加密")
    tui.log(f"路径: {keystore.store_path()}")
    t = tui.salt_table  # 复用表格组件的样式基调
    from rich.table import Table
    from rich import box
    table = Table(box=box.SIMPLE, header_style="bold")
    table.add_column("salt", style="dim")
    table.add_column("来源", style="cyan")
    table.add_column("更新时间")
    table.add_column("密钥", style="dim")
    for salt, rec in sorted(store.items()):
        table.add_row(salt[:16] + "…", rec.get("strategy", "-"),
                      str(rec.get("updated", "-")),
                      extract.mask_key(rec.get("key", "")))
    tui.console.print(table)
    return 0


def cmd_decrypt(args) -> int:
    tui.banner()
    dirs = _resolve_dirs(args.db_dir)
    if not dirs:
        tui.log("✗ 未找到微信数据目录")
        return 1
    out_root = args.out or str(Path.cwd() / "output")
    code = 0
    for wxid, db in dirs:
        rep = extract.decrypt_dir(db, str(Path(out_root) / wxid), log=tui.log,
                                  workers=args.workers,
                                  use_cache=not args.no_cache)
        tui.decrypt_summary(rep["ok"], rep["failed"], rep["skipped"],
                            rep["cached"], rep["duration_ms"],
                            rep["out_dir"])
        if rep["ok"] == 0 and rep["cached"] == 0:
            code = 2
    return code


def cmd_auto(args) -> int:
    tui.banner()
    out_root = args.out or str(Path.cwd() / "output")
    accounts = extract.auto_all(out_root, log=tui.log,
                                use_cache=not args.no_cache,
                                workers=args.workers)
    if not accounts:
        return 1
    tui.step("总览")
    table_ok = True
    for a in accounts:
        dec = a.get("decrypt")
        tui.summary_line(a["verified"], a["total_salts"], a["duration_ms"])
        if dec:
            tui.decrypt_summary(dec["ok"], dec["failed"], dec["skipped"],
                                dec.get("cached", 0), dec["duration_ms"],
                                dec["out_dir"])
        if a["verified"] != a["total_salts"]:
            table_ok = False
        if dec is not None and dec["ok"] == 0 and dec.get("cached", 0) == 0:
            table_ok = False
    if table_ok:
        tui.log("✔ 全自动流水线完成")
        return 0
    tui.log("部分账号未完成 — 未登录账号的密钥不在微信内存中，切换登录后重跑即可")
    return 2


def cmd_serve(args) -> int:
    from siwx.server import run_server
    run_server(args.host, args.port, open_browser=not args.no_open)
    return 0


def main() -> int:
    import os
    if os.name != "nt":
        print("stories-in-wx 依赖 Windows 平台接口（微信进程读取 / DPAPI），"
              "当前系统不受支持。macOS 版本仅为构建产物占位。")
        return 1
    ap = argparse.ArgumentParser(
        prog="siwx",
        description="stories-in-wx — 微信 4.x 密钥提取与解密 (自研)")
    # 全局参数通过 parent 注入每个子命令，前后放置均可
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--workers", type=int, default=None,
                        help="并行解密进程数 (默认 CPU 核数, 上限 8)")
    common.add_argument("--no-cache", action="store_true",
                        help="忽略缓存强制重跑")
    sub = ap.add_subparsers(dest="cmd")

    p_auto = sub.add_parser("auto", parents=[common],
                            help="全自动：扫描 → 提取 → 保存 → 解密")
    p_auto.add_argument("--out", default=None, help="解密输出根目录 (默认 ./output)")
    p_auto.set_defaults(fn=cmd_auto)

    p_keys = sub.add_parser("keys", parents=[common], help="密钥操作")
    keys_sub = p_keys.add_subparsers(dest="keys_cmd", required=True)
    p_ext = keys_sub.add_parser("extract", parents=[common], help="提取密钥")
    p_ext.add_argument("--db-dir", default=None)
    p_ext.add_argument("--json", action="store_true")
    p_ext.set_defaults(fn=cmd_keys_extract)
    p_list = keys_sub.add_parser("list", parents=[common], help="查看密钥库（打码）")
    p_list.set_defaults(fn=cmd_keys_list)

    p_dec = sub.add_parser("decrypt", parents=[common], help="解密数据库")
    p_dec.add_argument("--db-dir", default=None)
    p_dec.add_argument("--out", default=None)
    p_dec.set_defaults(fn=cmd_decrypt)

    p_serve = sub.add_parser("serve", parents=[common], help="Web 控制台")
    p_serve.add_argument("--host", default="127.0.0.1")
    p_serve.add_argument("--port", type=int, default=8787)
    p_serve.add_argument("--no-open", action="store_true")
    p_serve.set_defaults(fn=cmd_serve)

    args = ap.parse_args()
    if not getattr(args, "cmd", None):
        # 裸跑（双击 exe）→ 默认启动 Web 控制台
        return cmd_serve(argparse.Namespace(host="127.0.0.1", port=8787,
                                            no_open=False))
    try:
        return args.fn(args)
    except KeyboardInterrupt:
        tui.log("\n中断")
        return 130


if __name__ == "__main__":
    sys.exit(main())
