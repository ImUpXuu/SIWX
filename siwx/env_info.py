"""环境信息采集 —— 供 bug 报告与问题排查一键粘贴。

设计约束：
- **只读**：不写任何文件、不改变任何状态。
- **不泄露隐私**：不读取聊天内容与密钥明文；路径中的用户名默认打码。
- **快速**：不扫描微信数据目录，不做重量级探测。

用法：
    from siwx.env_info import collect, format_text
    print(format_text())
"""
from __future__ import annotations

import contextlib
import io
import platform
import re
import sys

from siwx import __version__

# 打码用户名：C:\Users\xxx / /Users/xxx / /home/xxx → <user>
_HOME_PATTERNS = (
    re.compile(r"(?i)([a-z]:\\users\\)[^\\/]+"),
    re.compile(r"(/users/)[^/]+", re.I),
    re.compile(r"(/home/)[^/]+", re.I),
)


def mask_path(value) -> str:
    """把路径中的用户名替换为 <user>，避免在公开 issue 中泄露账号名。"""
    text = str(value)
    for pat in _HOME_PATTERNS:
        text = pat.sub(r"\1<user>", text)
    return text


def _os_display() -> tuple[str, str]:
    """返回 (操作系统, 系统版本) 两个展示用字符串。"""
    system = platform.system() or "unknown"
    if system == "Darwin":
        # macOS 用 mac_ver()，platform.release() 给的是 Darwin 内核版本，参考价值低
        return "macOS", platform.mac_ver()[0] or platform.release() or "unknown"
    if system == "Windows":
        release = platform.release() or ""
        return (f"Windows {release}".strip(), platform.version() or "unknown")
    return system, platform.release() or platform.version() or "unknown"


def _plugin_line() -> str | None:
    """插件系统状态；插件不可用时返回 None。"""
    try:
        from siwx.plugins import loader
    except Exception:
        return None
    if not loader.plugins_enabled():
        return "已关闭（SIWX_NO_PLUGINS=1）"
    try:
        counts = loader.load_all().counts()
        return (f"开启（成功 {counts.get('ok', 0)}，"
                f"降级 {counts.get('degraded', 0)}，"
                f"失败 {counts.get('error', 0)}）")
    except Exception:
        return "开启（状态不可读）"


def collect(mask_user: bool = True, quiet: bool = False) -> dict:
    """采集环境信息，返回按展示顺序排列的有序 dict。

    键为中文标签，值均为字符串，便于直接粘贴到 issue。

    mask_user=True 时对路径中的用户名打码。
    quiet=True 时吞掉采集过程中的控制台输出（插件加载会打印日志），
    供 CLI / 需要干净 stdout 的场景使用。**不要**在多线程请求里开启，
    否则可能误吞其它线程的日志。
    """
    if not quiet:
        return _collect(mask_user)
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        return _collect(mask_user)


def _collect(mask_user: bool) -> dict:
    def show(p) -> str:
        return mask_path(p) if mask_user else str(p)

    info: dict[str, str] = {}
    info["siwx 版本"] = __version__

    try:
        from siwx.auto_update import is_frozen
        frozen = bool(is_frozen())
    except Exception:
        frozen = False
    info["运行模式"] = "打包产物" if frozen else "源码运行"

    os_name, os_version = _os_display()
    info["操作系统"] = os_name
    info["系统版本"] = os_version
    info["系统架构"] = platform.machine() or "unknown"
    info["Python"] = f"{platform.python_version()} ({platform.python_implementation()})"

    try:
        from siwx import paths
        info["程序目录"] = show(paths.app_root())
        info["数据目录"] = show(paths.data_dir())
        info["输出目录"] = show(paths.out_root())
        info["日志目录"] = show(paths.app_root() / "logs")
    except Exception:
        pass

    try:
        from siwx import keystore
        info["密钥库"] = f"{len(keystore.load())} 条密钥"
    except Exception:
        info["密钥库"] = "不可读"

    plugins = _plugin_line()
    if plugins is not None:
        info["插件系统"] = plugins

    return info


def format_text(info: dict | None = None, mask_user: bool = True,
                quiet: bool = False) -> str:
    """渲染成可直接粘贴到 issue 的 Markdown 文本块。"""
    info = info if info is not None else collect(mask_user=mask_user, quiet=quiet)
    lines = ["### 环境信息", ""]
    lines.extend(f"- {key}: {value}" for key, value in info.items())
    return "\n".join(lines)


def one_line() -> str:
    """单行摘要，用于日志。"""
    info = collect(mask_user=False, quiet=True)
    return (f"siwx {info.get('siwx 版本')} · {info.get('运行模式')} · "
            f"{info.get('操作系统')} {info.get('系统版本')} · {info.get('系统架构')} · "
            f"Python {info.get('Python')}")


if __name__ == "__main__":  # pragma: no cover - 手动排障用
    sys.stdout.write(format_text() + "\n")
