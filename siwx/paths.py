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
from pathlib import Path

_PATH_CACHE = {}


def app_root() -> Path:
    # 1. 显式环境变量覆盖
    env_root = os.environ.get("SIWX_ROOT", "")
    if env_root:
        p = Path(env_root)
        if p.is_dir():
            return p

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
    fallback.mkdir(parents=True, exist_ok=True)
    _PATH_CACHE[key] = fallback
    return fallback


def out_root() -> Path:
    return _writable_fallback("output", app_root() / "output")


def exports_root() -> Path:
    return _writable_fallback("exports", app_root() / "exports")
