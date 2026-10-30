"""控制台装饰字符的可编码性兜底。

2026-10-30 起源头符号已整体换成 GB2312 自带字符（√ × ● ※ ▲，中文 Windows
默认的 GBK/cp936 控制台全能编码），主战场（GBK 控制台）上**不再触发降级**。
本模块降为两道保险丝：
- 插件/历史文案经任务轨转发来的旧符号（✓ ✔ ✗ ✘ ▸ ✦ ⚠，均不在 GBK/cp1252
  码表里）仍按探测降级；
- cp1252 这类西文编码连框线都放不下，表格/横幅靠替换表换成 ASCII
  （`tui.box_or_ascii` 据此选边框）。

背景：`run.py` 启动时把 stdout/stderr 重配成 `errors="replace"`（否则
argparse 打 --help 会直接 UnicodeEncodeError 崩掉），编不了的字符在这类流上
会**静默显示成 `?`**：`? 密钥 29/30 已验证` 里，行首的中文正常、结论符号却
变成问号，用户完全无从判断这一行是成功还是失败。

原则：**编码能容纳时原样输出**（UTF-8 控制台、重定向到文件、MCP 改道的
stderr 都不受影响），**容纳不了时先降级成 ASCII 记号**（`√` → `[OK]`、
`×` 保留（cp1252 自带）、框线 → `-`/`|`/`+`），只有降级表也没覆盖到的字符
才交给 `?`。

注意 `errors="replace"` 的流**不会抛 UnicodeEncodeError**（编不了就静默变
`?`），所以调用方必须主动按编码判断，不能靠 try/except。

- `safe_text()`：唯一入口，返回该编码下能安全输出的字符串；
- `print_line()`：给 `siwx.logger._cprint` 用（它外层是 `except Exception:
  pass`，崩不了但会悄悄丢行，所以在这里就把目标流处理好）；
- `needs_translation()` / `box_is_safe()`：给 rich 层决定"要不要翻译""表格
  边框是否要换成 ASCII"。

编码在进程生命周期内不会变，探测结果按编码名缓存。
"""
import sys

# 探测集：新旧结果标记 + 表格/横幅框线（旧符号保留是为了兜插件残留）
_SEMANTIC_GLYPHS = ("✓", "✔", "✗", "✘", "▸", "✦", "⚠",
                    "√", "●", "※", "▲")
_BOX_GLYPHS = ("─", "│", "╭", "╰", "╮", "╯", "┼", "┤", "├")
_GLYPHS = _SEMANTIC_GLYPHS + _BOX_GLYPHS

# 不可编码时的替换表：框线保持等宽（1 列），否则横幅/表格会错位
_REPLACEMENTS = (
    ("╭", "+"), ("╮", "+"), ("╰", "+"), ("╯", "+"),
    ("─", "-"), ("│", "|"),
    ("✓", "[OK]"), ("✔", "[OK]"),
    ("✗", "[X]"), ("✘", "[X]"),
    ("▸", ">"), ("✦", "*"), ("⚠", "!"),
    ("√", "[OK]"), ("●", "*"), ("※", "*"), ("▲", "!"),
)

_probe_cache = {}


def _encodable(text, enc):
    try:
        text.encode(enc)
        return True
    except (UnicodeEncodeError, LookupError):
        # LookupError：编码名在本机 codec 表里不存在，视同"什么都编不了"
        return False


def _unsupported(encoding):
    """该编码放不下哪些装饰字符（结果按编码名缓存）。"""
    enc = (encoding or "").lower()
    if not enc:
        return frozenset()
    cached = _probe_cache.get(enc)
    if cached is None:
        cached = frozenset(ch for ch in _GLYPHS if not _encodable(ch, enc))
        _probe_cache[enc] = cached
    return cached


def needs_translation(encoding) -> bool:
    """目标流编码是否放不下装饰字符（True → 调用方应做 ASCII 降级）。"""
    return bool(_unsupported(encoding))


def box_is_safe(encoding) -> bool:
    """框线字符能否直接输出（True → 表格/面板可以继续用 Unicode 边框）。

    与 needs_translation 分开：GBK 控制台的框线在码表里（只是 ✓ ✗ 编不了），
    不该因此把表格边框也降级；只有 cp1252 这类西文控制台才需要。
    """
    enc = (encoding or "").lower()
    if not enc:
        return True
    return not any(ch in _unsupported(enc) for ch in _BOX_GLYPHS)


def translate(text: str, encoding) -> str:
    """把 text 中目标编码放不下的装饰字符换成 ASCII 记号。"""
    unsupported = _unsupported(encoding)
    if not unsupported:
        return text
    for src, dst in _REPLACEMENTS:
        if src in unsupported and src in text:
            text = text.replace(src, dst)
    return text


def safe_text(text: str, encoding) -> str:
    """输出前的一道闸：先按降级表翻译，仍有编不了的残留才交给 `?`。

    残留只可能是降级表之外的字符（例如英语区控制台上整段中文日志）——那种
    情况没有任何"更友好"的写法，保证不抛异常、不丢行即可。
    """
    out = translate(text, encoding)
    enc = (encoding or "").lower()
    if enc and not _encodable(out, enc):
        out = out.encode(enc, "replace").decode(enc, "replace")
    return out


def target_encoding(stream=None):
    """目标输出流的编码名。

    stream 为空时取 sys.stdout（打包成 --noconsole 时可能是 None）；
    stream 没有 encoding 属性（StringIO 之类）时返回 None = 不降级。
    """
    if stream is None:
        stream = getattr(sys, "stdout", None)
    return getattr(stream, "encoding", None)


def print_line(line: str, stream=None) -> None:
    """按目标流编码打印一行，装饰字符放不下就降级成 ASCII。

    stream 为 None 时用内置 print（跟随 `sys.stdout`）；传入流时直接写到该流，
    供 `logger._cprint` 的 `_CONSOLE` 通道（MCP 改道 stderr）复用同一套逻辑。
    """
    out = safe_text(line, target_encoding(stream))
    if stream is None:
        print(out)
        return
    try:
        stream.write(out + "\n")
        stream.flush()
    except (AttributeError, OSError, ValueError):
        # 流已关闭/无效：控制台只是附加通道，失败静默（与 _cprint 原语义一致）
        pass
