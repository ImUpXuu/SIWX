"""统一的应用路径 —— 与 cwd 完全解耦，防弹版。

解析优先级（从高到低）：
1. 环境变量 SIWX_ROOT（显式覆盖，最高优先级）
2. PyInstaller 打包：exe 所在目录（sys.frozen + sys.executable）
3. 源码运行：入口脚本所在目录（sys.argv[0]，即 run.py 的位置）
4. 兜底：从 __file__ 推导（siwx/ 的上一级）

绝不使用 Path.cwd() / os.getcwd() —— 用户的启动目录不可控，
从 System32 或任何地方启动都不能导致产物写到错误位置。
"""
import os
import sys
import tempfile
from pathlib import Path

from siwx import logger as _log

_PATH_CACHE = {}


def app_root() -> Path:
    # 1. 显式环境变量覆盖
    env_root = os.environ.get("SIWX_ROOT", "")
    if env_root:
        p = Path(env_root)
        if p.is_dir():
            return p
        # 用户显式指定的根目录无效却被静默忽略——排"产物去哪了"的第一现场。
        # 路径过脱敏（日志页/导出时展示层处理）
        _log.warn("paths", _log.desensitize_msg(
            f"SIWX_ROOT 指向的目录不存在，已忽略: {env_root}"))

    # 2. PyInstaller 打包：exe 所在目录
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent

    # 3. 源码运行：入口脚本（run.py）所在目录
    #    比 __file__ 更可靠：即使用户从别的目录 import siwx，
    #    入口脚本的位置才是"项目根"的语义。
    try:
        main_mod = sys.modules.get("__main__")
        if main_mod and getattr(main_mod, "__file__", None):
            entry = Path(main_mod.__file__).resolve()
            # entry = .../run.py → 项目根 = run.py 所在目录
            if entry.name == "run.py" or (entry.parent / "siwx").is_dir():
                return entry.parent
    except (AttributeError, OSError, ValueError):
        pass

    # 4. 兜底：从 __file__ 推导（siwx/paths.py → 上一级 = 项目根）
    return Path(__file__).resolve().parent.parent


def data_dir() -> Path:
    """跨平台应用数据目录 —— 持久化配置 / 密钥库 / 缓存的规范位置。

    与 app_root()（程序安装位置）刻意分离：macOS 的 .app bundle、
    Program Files 等安装位置可能只读或随升级被整体替换，用户数据
    必须落在系统数据目录，绝不能依赖启动时的工作目录。

    Windows: %LOCALAPPDATA%/stories-in-wx        （与历史版本行为一致）
    macOS:   ~/Library/Application Support/stories-in-wx
    Linux:   $XDG_DATA_HOME/stories-in-wx（默认 ~/.local/share/stories-in-wx）
    """
    if os.name == "nt":
        base = (os.environ.get("LOCALAPPDATA")
                or os.environ.get("USERPROFILE")
                or tempfile.gettempdir())
        if base == tempfile.gettempdir() and not (
                os.environ.get("LOCALAPPDATA") or os.environ.get("USERPROFILE")):
            # 极端环境兜底到 TEMP：重启即失，至少留个痕迹
            _log.detailed("paths", "LOCALAPPDATA/USERPROFILE 均不可用，"
                                   "数据目录兜底到系统 TEMP（重启即失）")
        return Path(base) / "stories-in-wx"
    home = Path.home()
    if sys.platform == "darwin":
        return home / "Library" / "Application Support" / "stories-in-wx"
    xdg = os.environ.get("XDG_DATA_HOME") or str(home / ".local" / "share")
    return Path(xdg) / "stories-in-wx"


def _writable_fallback(subdir: str, primary: Path) -> Path:
    """主路径不可创建/不可写时，回退到 %USERPROFILE%\\stories-in-wx\\<subdir>。

    这个函数在 Web API 热路径里会频繁调用。旧实现每次都创建并删除
    `.siwx_probe` 做写探针；在 Windows/杀软环境下这个操作会明显拖慢
    `/api/chat/sessions`。同一进程内路径可用性不会频繁变化，因此首
    次探测后缓存结果即可。
    """
    key = (subdir, str(primary))
    cached = _PATH_CACHE.get(key)
    if cached is not None:
        return cached
    try:
        # 热路径优化：若目录已存在且系统判断可写，直接返回；避免每个新进程
        # 首次请求都创建/删除 .siwx_probe（Windows 上可慢到数百 ms）。
        if primary.is_dir() and os.access(primary, os.W_OK):
            _PATH_CACHE[key] = primary
            return primary
        primary.mkdir(parents=True, exist_ok=True)
        # 新建目录或权限不明确时再真正试写一次。
        probe = primary / ".siwx_probe"
        probe.write_bytes(b"")
        probe.unlink(missing_ok=True)
        _PATH_CACHE[key] = primary
        return primary
    except OSError:
        pass
    fallback = Path(os.environ.get("USERPROFILE", ".")) / "stories-in-wx" / subdir
    # 主路径不可写、产物位置悄然改变——"找不到导出的文件"的根因。
    # 注意：不要包 desensitize_msg（warn 控制台路径本就不脱敏，包了反而双重
    # 脱敏；脱敏规则在日志页/导出的展示层统一处理）。有 _PATH_CACHE，每进程
    # 只触发一次，不会刷屏。
    _log.warn("paths", f"{subdir} 主路径不可写，回退: {fallback}")
    fallback.mkdir(parents=True, exist_ok=True)
    _PATH_CACHE[key] = fallback
    return fallback


def out_root() -> Path:
    return _writable_fallback("output", app_root() / "output")


def exports_root() -> Path:
    return _writable_fallback("exports", app_root() / "exports")


def templates_root() -> Path:
    """用户自定义 HTML 导出模板根目录（<SIWX_ROOT>/templates/）。

    跟 exports_root 同思路：跟随 SIWX_ROOT/程序根，用户放这里同名目录
    即覆盖内置模板；内置包在 siwx/templates/，由 html_template 解析。"""
    return _writable_fallback("templates", app_root() / "templates")
