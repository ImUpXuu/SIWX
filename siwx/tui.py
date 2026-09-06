"""漂亮 TUI —— rich 驱动的可扩展组件层。

扩展方式：
- TAG_STYLES 注册表：日志前缀 [xxx] → 样式，加一行即可支持新策略的配色；
- 需要新组件时在本模块加函数，cli.py 只消费这里的组件，不直接碰 rich。
"""
import re

from rich import box
from rich.console import Console
from rich.live import Live
from rich.panel import Panel
from rich.table import Table
from rich.text import Text
from rich.theme import Theme
from rich.columns import Columns

console = Console(
    theme=Theme({
        "tag.cipher": "bold cyan",
        "tag.mmkv": "bold magenta",
        "tag.memscan": "bold blue",
        "tag.keystore": "bold yellow",
        "tag.交叉验证": "bold green",
        "ok": "bold green",
        "warn": "bold yellow",
        "err": "bold red",
        "dim": "dim",
        "accent": "bold #2f6fdb",
    })
)

BANNER = """\
[bold #2f6fdb]╭──────────────────────────────────────────╮
│   [/][bold #e0a400]✦[/] [bold]stories[/][dim]-in-[/][bold #2f6fdb]wx[/]   [dim]微信密钥提取 · 解密[/]   [bold #e0a400]✦[/][bold #2f6fdb]   │
╰──────────────────────────────────────────╯[/]"""

# 日志前缀 [tag] → 主题样式 key；新策略在此登记一行即可
TAG_RE = re.compile(r"^\[([a-zA-Z\u4e00-\u9fff]+)\]\s*(.*)$")


def tag_style(tag: str) -> str:
    t = tag.lower()
    if t in ("cipher", "mmkv", "memscan", "keystore", "交叉验证"):
        return f"tag.{t}"
    return "accent"


def log(msg: str) -> None:
    """分层彩色日志：[tag] 前缀着色，结果行按语义着色。"""
    m = TAG_RE.match(msg)
    if m:
        tag, rest = m.group(1), m.group(2)
        console.print(f"[dim]·[/] [{tag_style(tag)}][{tag}][/] {rest}")
        return
    if "✓" in msg or "✔" in msg or "已验证" in msg or "成功" in msg or "通过" in msg:
        console.print(f"[ok]{msg}[/]")
    elif "✗" in msg or "失败" in msg or "错误" in msg or "[错误]" in msg:
        console.print(f"[err]{msg}[/]")
    elif "跳过" in msg or "缓存" in msg:
        console.print(f"[warn]{msg}[/]")
    else:
        console.print(msg)


def banner() -> None:
    console.print(BANNER)


def account_table(dirs_with_counts):
    """账号总览表。dirs_with_counts: [(wxid, db_dir, db_count)]"""
    t = Table(box=box.SIMPLE_HEAVY, header_style="bold #2f6fdb", show_lines=False)
    t.add_column("微信号", style="bold")
    t.add_column("数据目录", style="dim", overflow="fold")
    t.add_column("数据库", justify="right")
    for wxid, db, cnt in dirs_with_counts:
        t.add_row(wxid, db, str(cnt))
    console.print(t)


def step(title: str) -> None:
    console.print()
    console.print(Panel(f"[bold]{title}[/]", box=box.SQUARE, border_style="#2f6fdb",
                        padding=(0, 2)))


def salt_table(report: dict) -> None:
    t = Table(box=box.SIMPLE, header_style="bold", pad_edge=False)
    t.add_column("状态", justify="center")
    t.add_column("salt", style="dim")
    t.add_column("库数", justify="right")
    t.add_column("来源", style="cyan")
    t.add_column("密钥", style="dim")
    for s in report.get("salts", []):
        mark = "[ok]✓[/]" if s["verified"] else "[err]✗[/]"
        t.add_row(mark, s["salt"][:16] + "…", str(len(s["dbs"])),
                  s.get("strategy") or "-", s.get("key_masked") or "-")
    console.print(t)


def summary_line(verified: int, total: int, ms: int) -> None:
    color = "ok" if verified == total else "warn"
    console.print(f"[{color}]▸ 密钥 {verified}/{total} 已验证[/] [dim]({ms} ms)[/]")


def decrypt_summary(ok: int, failed: int, skipped: int, cached: int, ms: int, out: str) -> None:
    console.print(
        f"[ok]▸ 解密完成[/] [bold]{ok}[/] 成功"
        f"[dim]（缓存命中 {cached}）[/]"
        + (f" [err]{failed} 失败[/]" if failed else "")
        + (f" [warn]{skipped} 缺密钥[/]" if skipped else "")
        + f" [dim]({ms} ms) → {out}[/]"
    )


# ── serve 模式状态栏 ─────────────────────────────────────────────────

def make_status_bar(getter) -> Text:
    """底部常驻状态栏。getter 返回 dict:
    {wechat: str, wxid: str, keys: int, job: str, url: str}
    """
    t = Text()
    t.append(" ● ", style="bold green")
    t.append(getter.get("url", ""), style="bold cyan")
    t.append("  │  ", style="dim")
    t.append("微信: ", style="dim")
    t.append(getter.get("wechat", "…"), style="bold")
    t.append("  │  ", style="dim")
    t.append("wxid: ", style="dim")
    t.append(getter.get("wxid", "…"), style="bold yellow")
    t.append("  │  ", style="dim")
    t.append("密钥: ", style="dim")
    t.append(str(getter.get("keys", "…")), style="bold green")
    t.append("  │  ", style="dim")
    t.append("任务: ", style="dim")
    t.append(getter.get("job", "空闲"), style="bold magenta")
    return t


def run_live_status(getter, on_start):
    """启动 Live 状态栏。getter 返回状态 dict；on_start 在 Live 就绪后回调。

    用法（server.py）：
        tui.run_live_status(getter_dict_fn, lambda: app.run(...))
    """
    import threading

    console.print()
    on_start()

    def _render():
        try:
            return make_status_bar(getter())
        except Exception:
            return make_status_bar({})

    with Live(
        _render(),
        console=console,
        refresh_per_second=1,
        vertical_overflow="visible",
    ) as live:
        import time
        while True:
            time.sleep(1)
            try:
                live.update(_render())
            except Exception:
                break
