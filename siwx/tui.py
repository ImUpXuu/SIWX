"""漂亮 TUI —— rich 驱动的可扩展组件层。

扩展方式：
- TAG_STYLES 注册表：日志前缀 [xxx] → 样式，加一行即可支持新策略的配色；
- 需要新组件时在本模块加函数，cli.py 只消费这里的组件，不直接碰 rich：
  输出走 `print_line`，表格边框走 `box_or_ascii`，单元格里的装饰字符走 `glyph`，
  这样控制台编码兜底（见 console_glyphs）不会在新增组件上失效。
- 本模块内的新输出请一律用 `_print`，不要直接 `console.print`。
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

from siwx import logger as log
from siwx import console_glyphs

# 不绑定 file：rich 默认延迟取 sys.stdout，这样打包成 --noconsole（stdout 为
# None）时导入不炸，只在打印时被 _print 的 except 兜住；编码由 _print/glyph
# 现场探 `console.file`（延迟解析后就是当前 sys.stdout）。
# THEME 单独抽出：测试需要新建一个同样样式的 Console 接到临时流上。
THEME = Theme({
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

console = Console(theme=THEME)

BANNER = """\
[bold #2f6fdb]╭──────────────────────────────────────────╮
│   [/][bold #e0a400]※[/] [bold]stories[/][dim]-in-[/][bold #2f6fdb]wx[/]   [dim]微信密钥提取 · 解密[/]   [bold #e0a400]※[/][bold #2f6fdb]   │
╰──────────────────────────────────────────╯[/]
[dim]仅供本人数据备份与研究 · 禁止商用与一切非法用途 · 使用前请阅读 README 免责声明[/]"""

# 日志前缀 [tag] → 主题样式 key；新策略在此登记一行即可
TAG_RE = re.compile(r"^\[([a-zA-Z\u4e00-\u9fff]+)\]\s*(.*)$")


def tag_style(tag: str) -> str:
    t = tag.lower()
    if t in ("cipher", "mmkv", "memscan", "keystore", "交叉验证"):
        return f"tag.{t}"
    return "accent"


def _encoding():
    """当前输出流的编码名（rich 延迟取 sys.stdout，这里跟着它走）。"""
    return console_glyphs.target_encoding(getattr(console, "file", None))


def box_or_ascii(unicode_box) -> "box.Box":
    """表格/面板边框选型：目标控制台编不了框线时用 ASCII 边框。

    GBK 只缺对勾/叉这类结果符号，框线在码表里，所以不能一见 needs_translation 就把表格
    降级；只有 cp1252 这类西文控制台才需要。给 cli.py 自建表格也用。
    """
    enc = _encoding()
    if console_glyphs.needs_translation(enc) and not console_glyphs.box_is_safe(enc):
        return box.ASCII
    return unicode_box


def glyph(text: str) -> str:
    """把字符串里的装饰字符按控制台编码降级（无需降级时原样返回）。

    用在表格单元格等“不经 _print 的字符串”上：它们在 Table 内部渲染，
    到不了 _print 的翻译入口。降级只动文本字符，不动 [ok]/[/] 标记名。
    """
    enc = _encoding()
    return console_glyphs.safe_text(text, enc) if enc else text


def _print(renderable) -> None:
    """rich 输出的统一入口。

    目标流编码放不下装饰字符（GBK 控制台遇到降级表里的旧符号、cp1252 遇到
    框线 ╭ ─）时，整行降级成
    ASCII 记号再打，而不是让 run.py 的 errors="replace" 把它们吐成 `?`（成功行
    看着像报错）。探测不到编码或本来就能容纳时，原样输出。

    注意：run.py 把流重配成 errors="replace" 后，编不了的字符只会静默变 `?`，
    **不会抛 UnicodeEncodeError**，所以必须提前翻译，不能指望 except；下面
    的 except 只是严格流（未经 run.py 重配，或探测集之外的新字符）的兜底。
    """
    enc = _encoding()
    if console_glyphs.needs_translation(enc):
        if isinstance(renderable, str):
            renderable = console_glyphs.safe_text(renderable, enc)
        elif isinstance(renderable, Text):
            # Text（log/状态栏等 Text 构造路径）里的字符也要过同一道闸；
            # 降级重建会丢分段样式，只在真有编不了的残留时才走（罕见）
            plain = renderable.plain
            if console_glyphs.translate(plain, enc) != plain:
                renderable = Text(console_glyphs.safe_text(plain, enc),
                                  style=renderable.style)
    try:
        console.print(renderable)
    except UnicodeEncodeError:
        try:
            log.detailed("tui", "控制台无法编码部分字符，本行以纯文本降级输出")
            console.print(_flat_text(renderable, enc), plain=True, crop=False)
        except Exception:  # noqa: BLE001 — 输出路径不许把命令打崩
            log.detailed("tui", "控制台无法渲染本行，已跳过")


def _flat_text(renderable, enc) -> str:
    """把渲染物（Table/Panel/Text）拆成纯文本供降级输出。

    不能用 str(renderable)——那是 `<Table object at 0x…>`；走 rich 自己的渲染器
    把分段拼回文本，再做一次可编码性降级。"""
    try:
        flat = "".join(seg.text for seg in console.render(renderable, console.options))
    except Exception:  # noqa: BLE001 — 兜底路径自己不能再抛
        flat = getattr(renderable, "plain", None) or "(本行无法在当前控制台渲染)"
    return console_glyphs.safe_text(flat, enc)


def print_line(renderable) -> None:
    """对外输出口：cli.py 自建的表格也走这里，拿到同样的编码兜底。

    模块注释说 cli.py 不直接碰 rich，这个口子就是给它留的唯一出口。
    """
    _print(renderable)


def log(msg: str) -> None:
    """分层彩色日志：[tag] 前缀着色，结果行按语义着色。

    全程用 Text 构造输出——tag 名与正文属于动态内容，一律不进 markup 字符串：
    否则 `[extract]` 这类前缀会被 rich 当样式标签解析后整个吞掉（2026-10-30
    复核缺陷）；`Text.append` 不解析 markup，调用方文案里的方括号（进度
    `[ 42%]`、任务 id 等）也按字面显示。语义关键词以 \\u 转义保留旧符号判定
    （脆弱字符以字面量回源码时，tests/test_console_glyphs.py 的源码扫描会以
    warning 提示，不阻塞），兼容插件经任务轨转发的历史文案。
    """
    m = TAG_RE.match(msg)
    if m:
        tag, rest = m.group(1), m.group(2)
        line = Text()
        line.append("· ", style="dim")
        line.append(f"[{tag}]", style=tag_style(tag))
        line.append(f" {rest}")
        _print(line)
        return
    if "√" in msg or "\u2713" in msg or "\u2714" in msg or "已验证" in msg or "成功" in msg or "通过" in msg:
        _print(Text(msg, style="ok"))
    elif "×" in msg or "\u2717" in msg or "\u2718" in msg or "失败" in msg or "错误" in msg:
        _print(Text(msg, style="err"))
    elif "跳过" in msg or "缓存" in msg:
        _print(Text(msg, style="warn"))
    else:
        _print(Text(msg))


def banner() -> None:
    """开局横幅。框线/星号编不了时由 _print 逐字降级为 ASCII（─→-、※→*），
    宽度不变，不需要额外的 ASCII 版横幅文本。"""
    _print(BANNER)


def account_table(dirs_with_counts):
    """账号总览表。dirs_with_counts: [(wxid, db_dir, db_count)]"""
    t = Table(box=box_or_ascii(box.SIMPLE_HEAVY), header_style="bold #2f6fdb",
              show_lines=False)
    t.add_column("微信号", style="bold")
    t.add_column("数据目录", style="dim", overflow="fold")
    t.add_column("数据库", justify="right")
    for wxid, db, cnt in dirs_with_counts:
        t.add_row(wxid, db, str(cnt))
    _print(t)


def step(title: str) -> None:
    console.print()
    _print(Panel(f"[bold]{glyph(title)}[/]", box=box_or_ascii(box.SQUARE),
                 border_style="#2f6fdb", padding=(0, 2)))


def salt_table(report: dict) -> None:
    t = Table(box=box_or_ascii(box.SIMPLE), header_style="bold", pad_edge=False)
    t.add_column("状态", justify="center")
    t.add_column("salt", style="dim")
    t.add_column("库数", justify="right")
    t.add_column("来源", style="cyan")
    t.add_column("密钥", style="dim")
    for s in report.get("salts", []):
        mark = glyph("[ok]√[/]" if s["verified"] else "[err]×[/]")
        t.add_row(mark, glyph(s["salt"][:16] + "…"), str(len(s["dbs"])),
                  s.get("strategy") or "-", s.get("key_masked") or "-")
    _print(t)


def summary_line(verified: int, total: int, ms: int) -> None:
    color = "ok" if verified == total else "warn"
    _print(f"[{color}]● 密钥 {verified}/{total} 已验证[/] [dim]({ms} ms)[/]")


def decrypt_summary(ok: int, failed: int, skipped: int, cached: int, ms: int, out: str) -> None:
    _print(
        f"[ok]● 解密完成[/] [bold]{ok}[/] 成功"
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
    """常驻状态栏：后台线程 + ANSI 转义码，Windows cmd 可靠。"""
    import os
    import sys
    import threading
    import time

    if os.name == "nt":
        import ctypes
        try:
            kernel32 = ctypes.windll.kernel32
            kernel32.SetConsoleMode(kernel32.GetStdHandle(-11), 7)
        except Exception:
            pass

    on_start()

    status = ["初始化…"]
    running = True

    # 每秒轮询一次，必须节流：连续失败 N 次才 warn（审计 §7.5 热路径纪律）。
    # 1（首次即报，"状态栏长期 …"的根因第一现场）+ 之后每 60 次（约 1 分钟）。
    bar_fail = {"n": 0}

    def _bar():
        try:
            bar = make_status_bar(getter())
            bar_fail["n"] = 0
            return bar
        except Exception as e:
            bar_fail["n"] += 1
            if bar_fail["n"] == 1 or bar_fail["n"] % 60 == 0:
                log.warn("server", f"TUI 状态栏渲染连续失败 {bar_fail['n']} 次"
                                   f"（状态栏将显示默认值）: {type(e).__name__}: {e}")
            return make_status_bar({})

    def _loop():
        while running:
            text = _bar()
            # 状态栏走裸 ANSI 输出（不经 _print），job 字段带 √/×（server.py），
            # 两者均在 GBK 码表里；这里的降级只兜降级表内的残留字符
            line = console_glyphs.safe_text(str(text),
                                            console_glyphs.target_encoding(sys.stdout))
            sys.stdout.write(f"\0337\033[999;0H\033[2K{line}\0338")
            sys.stdout.flush()
            time.sleep(1)

    t = threading.Thread(target=_loop, daemon=True)
    t.start()

    try:
        while True:
            time.sleep(0.5)
    except KeyboardInterrupt:
        running = False
        raise
