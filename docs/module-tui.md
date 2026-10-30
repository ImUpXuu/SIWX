# module-tui.py — rich 驱动的终端 UI

> **文件**: `siwx/tui.py` | **角色**: 可扩展的终端 UI 组件层

---

## 职责

1. **彩色日志**: `[tag]` 前缀着色 + 语义着色
2. **表格组件**: salt 状态表、账号总览表
3. **状态栏**: serve 模式常驻状态栏
4. **可扩展**: TAG_STYLES 注册表，新策略加一行即可

---

## 主题

```python
BANNER = """\
[bold #2f6fdb]╭──────────────────────────────────────────╮
│   [/][bold #e0a400]※[/] [bold]stories[/][dim]-in-[/][bold #2f6fdb]wx[/]   [dim]微信密钥提取 · 解密[/]   [bold #e0a400]※[/][bold #2f6fdb]   │
╰──────────────────────────────────────────╯[/]
[dim]仅供本人数据备份与研究 · 禁止商用与一切非法用途 · 使用前请阅读 README 免责声明[/]"""

console = Console(theme=Theme({
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
}))
```

---

## 关键函数

### `tag_style(tag: str) → str`

**日志前缀 → 主题样式 key**。

```python
def tag_style(tag: str) -> str:
    t = tag.lower()
    if t in ("cipher", "mmkv", "memscan", "keystore", "交叉验证"):
        return f"tag.{t}"
    return "accent"
```

---

### `account_table(dirs_with_counts) → None`

**账号总览表**。

```python
t = Table(box=box.SIMPLE_HEAVY, header_style="bold #2f6fdb", show_lines=False)
t.add_column("微信号", style="bold")
t.add_column("数据目录", style="dim", overflow="fold")
t.add_column("数据库", justify="right")
for wxid, db, cnt in dirs_with_counts:
    t.add_row(wxid, db, str(cnt))
console.print(t)
```

---

### `step(title: str) → None`

**分步标题**。

```python
console.print()
console.print(Panel(f"[bold]{title}[/]", box=box.SQUARE, border_style="#2f6fdb", padding=(0, 2)))
```

---

### `log(msg: str) → None`

**分层彩色日志**。全程用 `Text` 对象构造（2026-10-30 修复）：tag 名与正文
属于动态内容，不进 markup 字符串——`[extract]` 这类前缀不再被 rich 当样式
标签解析后吞掉，调用方文案里的方括号（`[ 42%]`、任务 id 等）也按字面显示。

```python
# [tag] 前缀 → 着色（标签本身按字面显示）
log("[cipher] 扫描完成")    # cyan
log("[mmkv] 解密成功")      # magenta

# 语义着色
log("√ 验证通过")           # green
log("× 失败")               # red
log("缓存命中")             # yellow
```

**TAG_RE**: `^\[([a-zA-Z\u4e00-\u9fff]+)\]\s*(.*)$`

---

### `banner() → None`

打印 ASCII banner（使用 `BANNER` 常量，含 rich 标记）。

```python
BANNER = """\
[bold #2f6fdb]╭──────────────────────────────────────────╮
│   [/][bold #e0a400]※[/] [bold]stories[/][dim]-in-[/][bold #2f6fdb]wx[/]   [dim]微信密钥提取 · 解密[/]   [bold #e0a400]※[/][bold #2f6fdb]   │
╰──────────────────────────────────────────╯[/]
[dim]仅供本人数据备份与研究 · 禁止商用与一切非法用途 · 使用前请阅读 README 免责声明[/]"""

def banner() -> None:
    console.print(BANNER)
```

---

### `salt_table(report: dict) → None`

**salt 状态表**。

```
 √  salt=abcdef1234567890…   3  cipher      abcdef…7890
 ×  salt=fedcba9876543210…   1  -           -
```

---

### `summary_line(verified, total, ms) → None`

**密钥摘要行**。

```
● 密钥 32/32 已验证 (1234 ms)    # green
● 密钥 5/79 已验证 (5678 ms)     # yellow
```

---

### `decrypt_summary(ok, failed, skipped, cached, ms, out) → None`

**解密摘要行**。

```
● 解密完成 31 成功（缓存命中 1） (5678 ms) → output/wxid_xxx
● 解密完成 30 成功（缓存命中 5） 2 失败 1 缺密钥 (9012 ms) → output/wxid_xxx
```

---

### `make_status_bar(getter) → Text`

**底部常驻状态栏**。

```
 ● http://127.0.0.1:8787  │  微信: 运行中(2)  │  wxid: wxid_xxx  │  密钥: 32  │  任务: 空闲
```

**getter 返回**: `{url, wechat, wxid, keys, job}`

---

### `run_live_status(getter, on_start) → None`

**常驻状态栏循环**。

```python
def run_live_status(getter, on_start):
    # Windows: 启用 ANSI 转义码
    # 1 秒刷新
    # Ctrl+C 退出
```

---

## 扩展方式

### 添加新策略的日志配色

```python
# 在 Theme 中添加
"tag.my_strategy": "bold red",

# 在 tag_style() 中添加
if t == "my_strategy":
    return "tag.my_strategy"
```

### 添加新组件

```python
def my_component(data):
    """新组件：输出用 _print，边框用 box_or_ascii，单元格里的装饰字符用 glyph。
    源码里用 GB2312 自带符号（√ × ● ※ ▲）；脆弱字符（✓ ✗ 等旧符号）尽量
    别以字面量进源码（tests/test_console_glyphs.py 源码扫描会以 warning 提示）。"""
    t = Table(box=box_or_ascii(box.SIMPLE))
    _print(t)
```

---

## 控制台编码兜底（console_glyphs）

**第一层：源头换字（2026-10-30）**。结论符号已整体换成 GB2312 自带字符：
`✓ ✔` → `√`、`✗ ✘` → `×`、`▸` → `●`、`⚠` → `▲`、`✦` → `※`。这些字符在
中文 Windows 默认的 GBK/cp936 码表里，主战场上不再触发任何降级；`×` 甚至在
cp1252 里也有。换字纪律由 `tests/test_console_glyphs.py::TestGlyphSourceHygiene`
源码扫描守着——脆弱字符若以字面量回到 siwx 源码，扫描以 **warning 提示**
（提示级，不阻塞 CI：保险丝兜得住旧符号，只是建议顺手换回 GB2312 自带字符）。

**第二层：保险丝**。`run.py` 启动时把 stdout/stderr 重配成 `errors="replace"`
（否则 argparse 打 `--help` 会直接 UnicodeEncodeError 崩掉），编不了的字符
会**静默变成 `?`**。`siwx/console_glyphs.py` 按目标流编码探测，容不下就换成
等宽 ASCII 记号：

| 原字符 | 降级 | 说明 |
|---|---|---|
| `✓` `✔` `√` | `[OK]` | 成功（前两个是插件残留的旧符号） |
| `✗` `✘` | `[X]` | 失败（同上） |
| `●` `※` `✦` | `*` | 小结/装饰 |
| `▸` | `>` | 旧小结行前缀 |
| `⚠` `▲` | `!` | 警告 |
| `─` `│` `╭╮╰╯` | `-` `|` `+` | 框线（等宽，不影响排版） |

入口分工：

- `_print(renderable)`：本模块内所有输出的唯一入口（str 与 `Text` 两条路径
  都提前翻译）；
- `box_or_ascii(unicode_box)`：只在**框线本身也编不了**（cp1252）时换 `box.ASCII`，
  GBK 的框线在码表里，不必降级；
- `glyph(text)`：表格单元格等不经 `_print` 的字符串；
- `logger._cprint` / `run_live_status` 的裸 ANSI 输出也走同一道闸。

UTF-8 控制台（含重定向到文件、MCP 改道的 stderr）一个字节都不改。
行为钩子在 `tests/test_console_glyphs.py`，其中包括一个反向用例（直接写
`console.print` 确实会变 `?`）钉住“必须提前翻译、不能指望 except”这个前提。

已知局限：**中文在 cp1252 这类单字节编码上仍会变 `?`** —— 编码本身放不下
中文，任何降级表都救不了。按 PEP 528，Python 3.6+ 的真实控制台走
`WriteConsoleW`（UTF-16 API），中文本来就能显示；会变 `?` 的主要是
**重定向/管道场景**（Git Bash、CI 日志、IDE 输出面板）与
`PYTHONLEGACYWINDOWSSTDIO` 遗留模式。规避：`PYTHONUTF8=1` 或控制台切 UTF-8
（`chcp 65001`）；Python 3.15 起 UTF-8 模式默认开启（PEP 686），该场景自然消失。

---

## 使用示例

```python
from siwx import tui

tui.banner()
tui.step("密钥提取")
tui.log("[cipher] 扫描完成")
tui.summary_line(32, 32, 1234)
tui.decrypt_summary(31, 0, 0, 1, 5678, "output/wxid_xxx")
tui.account_table([("wxid_xxx", "C:/.../db_storage", 32)])
tui.make_status_bar(lambda: {"url": "...", "wechat": "...", "wxid": "...", "keys": "...", "job": "..."})
tui.run_live_status(getter, on_start)
```

---

## 注意事项

- `make_status_bar` 的 `getter` 参数是一个**无参函数**，返回状态字典
- `run_live_status` 的 `on_start` 参数是一个**无参函数**，在状态栏启动前调用
- Windows 上 `run_live_status` 会自动启用 ANSI 转义码（`SetConsoleMode`）
