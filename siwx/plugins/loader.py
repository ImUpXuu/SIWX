"""插件发现与加载 —— 用户级目录扫描 + importlib 装载 + 隔离。

扫描位置（唯一）：%LOCALAPPDATA%\\stories-in-wx\\plugins\\
与 keystore.bin / mcp_config.json / media_key.json 同源，跨安装持久，
PyInstaller 打包后依然可用。

支持两种形态：
- 单文件：plugins/<name>.py        （页面资源放同目录 <name>.pages/）
- 包：    plugins/<name>/__init__.py（页面资源放包内 pages/）

跳过规则：以 `_` 或 `.` 开头、`__pycache__`、以 `.disabled` 结尾。

隔离：每个候选文件单独 try/except；import 期抛异常的插件被记录为 error 并跳过，
不影响其余插件与宿主。

⚠️ 本模块**严禁在顶层 import 任何业务模块**（strategies / exporter / server /
mcp_server / api_*），否则会形成循环依赖。只依赖 paths + logger。
"""
import hashlib
import importlib.util
import os
import sys
from pathlib import Path

from siwx import logger as log
from siwx.plugins import report as _report

_SKIP_PREFIX = ("_", ".")

# 关闭插件加载的开关：SIWX_NO_PLUGINS=1 时视为"零插件"宿主。
# 用于回归测试与排障（隔离用户已安装插件对宿主行为的影响）。
_NO_PLUGINS_ENV = "SIWX_NO_PLUGINS"


def plugins_enabled() -> bool:
    """插件系统总开关（默认开启）。"""
    v = (os.environ.get(_NO_PLUGINS_ENV) or "").strip().lower()
    return v not in ("1", "true", "yes", "on")


def plugins_root() -> Path:
    """用户级插件目录。"""
    base = (os.environ.get("LOCALAPPDATA")
            or os.environ.get("USERPROFILE")
            or ".")
    return Path(base) / "stories-in-wx" / "plugins"


def ensure_root() -> Path:
    """确保插件目录存在（便于用户直接投放）。"""
    root = plugins_root()
    try:
        root.mkdir(parents=True, exist_ok=True)
    except OSError:
        pass
    return root


def _iter_candidates(root: Path) -> list:
    """列出候选插件（单文件 .py 与包目录），已排序保证确定性。"""
    if not root.is_dir():
        return []
    out = []
    try:
        entries = sorted(root.iterdir(), key=lambda p: p.name)
    except OSError:
        return []

    for p in entries:
        name = p.name
        if name.startswith(_SKIP_PREFIX) or name == "__pycache__":
            continue
        if name.endswith(".disabled"):
            continue
        if p.is_file() and p.suffix == ".py":
            out.append(("file", p, p.stem))
        elif p.is_dir() and (p / "__init__.py").is_file():
            out.append(("pkg", p, name))
    return out


def _module_name(kind: str, name: str, path: Path) -> str:
    """生成唯一模块名，避免与 sys.modules 中同名包冲突。"""
    digest = hashlib.md5(str(path).encode("utf-8")).hexdigest()[:8]
    return f"siwx_plugin_{name}_{digest}"


def _pages_dir_for(kind: str, name: str, path: Path) -> str:
    """插件页面资源目录：包插件用 <pkg>/pages，单文件用 <file>.pages/。"""
    if kind == "pkg":
        cand = path / "pages"
        return str(cand) if cand.is_dir() else str(path)
    cand = path.parent / f"{name}.pages"
    return str(cand) if cand.is_dir() else ""


def _load_module(kind: str, name: str, path: Path):
    """用 importlib 装载插件模块（已完成校验，调用方负责 try/except）。"""
    mod_name = _module_name(kind, name, path)
    target = path / "__init__.py" if kind == "pkg" else path

    if kind == "pkg":
        spec = importlib.util.spec_from_file_location(
            mod_name, target, submodule_search_locations=[str(path)])
    else:
        spec = importlib.util.spec_from_file_location(mod_name, target)

    if spec is None or spec.loader is None:
        raise ImportError(f"无法为 {target} 创建 importer")

    mod = importlib.util.module_from_spec(spec)
    # 先注册进 sys.modules：支持包内相对导入，也避免重复加载
    sys.modules[mod_name] = mod
    try:
        spec.loader.exec_module(mod)
    except BaseException:
        sys.modules.pop(mod_name, None)
        raise

    # 注入宿主元信息，供 contract 构造 UiPage.pages_dir 与报告 source
    mod.__siwx_source__ = f"dir:{target}"
    try:
        mod.__siwx_pages_dir__ = _pages_dir_for(kind, name, path)
    except Exception:
        pass
    return mod


def _apply_module(mod, registry, rep: _report.PluginLoadReport, hint: str) -> bool:
    """从模块取出 PLUGIN/register 并注册；返回是否成功。"""
    from siwx.plugins import contract

    data = getattr(mod, "PLUGIN", None)
    register_fn = getattr(mod, "register", None)

    if data is None and callable(register_fn):
        # 进阶形态：def register(reg): ... 由插件直接调用注册 API
        try:
            register_fn(registry)
        except Exception as e:
            rep.add_error(hint, "register", e)
            log.error("plugin", f"{hint} 注册失败: {e}")
            return False
        meta = registry.metas.get(hint)
        rep.add_ok(hint, getattr(meta, "version", "0.0.0"),
                   f"dir:{getattr(mod, '__siwx_source__', '')}",
                   {k: v for k, v in registry.summary_counts().items()})
        return True

    if not isinstance(data, dict):
        rep.add_error(hint, "contract", ValueError("模块缺少 PLUGIN 字典或 register()"))
        log.error("plugin", f"{hint} 缺少 PLUGIN 字典或 register() 函数")
        return False

    meta = contract.meta_from_dict(data)
    if meta is None:
        rep.add_error(hint, "contract", ValueError("PLUGIN 缺少 name"))
        log.error("plugin", f"{hint} 的 PLUGIN 缺少 name")
        return False
    if registry.report and registry.report.has(meta.name):
        rep.add_skip(meta.name, "与已加载插件同名（先加载者优先）")
        log.warn("plugin", f"{meta.name} 与已加载插件同名，跳过")
        return False

    missing = contract.check_requires(meta.requires)
    try:
        counts = contract.register_plugin(mod, registry, data)
    except Exception as e:
        rep.add_error(meta.name, "register", e)
        log.error("plugin", f"{meta.name} 注册失败: {e}")
        return False

    rep.add_ok(meta.name, meta.version, str(getattr(mod, "__siwx_source__", "")),
               counts, missing)
    if missing:
        log.warn("plugin", f"{meta.name} 缺少依赖 {', '.join(missing)}，标记为降级")
    return True


def load_all(reset: bool = False) -> _report.PluginLoadReport:
    """扫描并加载全部插件（幂等）。返回加载报告。"""
    from siwx.plugins.registry import registry

    if registry.is_loaded() and not reset:
        return registry.report or _report.PluginLoadReport()

    rep = _report.PluginLoadReport()

    if not plugins_enabled():
        registry.report = rep
        registry._loaded = True
        log.info("plugin", f"插件加载已通过 {_NO_PLUGINS_ENV} 关闭（零插件模式）")
        return rep

    root = ensure_root()
    registry.report = rep
    candidates = _iter_candidates(root)

    if not candidates:
        registry._loaded = True
        log.detailed("plugin", f"插件目录为空: {root}")
        return rep

    log.info("plugin", f"发现 {len(candidates)} 个插件候选（{root}）")
    for kind, path, name in candidates:
        try:
            mod = _load_module(kind, name, path)
        except BaseException as e:          # 插件 import 期任何异常都不外泄
            rep.add_error(name, "import", e)
            log.error("plugin", f"{name} 导入失败，已跳过: {type(e).__name__}: {e}")
            continue
        try:
            _apply_module(mod, registry, rep, name)
        except BaseException as e:
            rep.add_error(name, "register", e)
            log.error("plugin", f"{name} 注册异常，已跳过: {e}")

    registry._loaded = True
    log.info("plugin", rep.summary())
    return rep


def ensure_loaded() -> _report.PluginLoadReport:
    """确保插件已加载（供 server / mcp / cli 入口调用）。"""
    from siwx.plugins.registry import registry

    if registry.is_loaded():
        return registry.report or _report.PluginLoadReport()
    return load_all()


def discover_entrypoints() -> list:
    """（预留）pip 安装的插件发现。

    后续增强：通过 importlib.metadata.entry_points(group="siwx.plugins") 加载
    `pyproject.toml` 里声明的入口点。当前版本仅支持用户级目录投放。
    """
    return []


def plugin_ui_dir(name: str) -> Path | None:
    """按插件名返回其页面资源目录（供 /plugin-pages 静态路由使用）。

    只认已加载插件登记的 pages_dir，避免任意路径读取。
    """
    from siwx.plugins.registry import registry

    for page in registry.pages.entries():
        owner = page.meta.name if page.meta else ""
        if owner == name and page.pages_dir:
            p = Path(page.pages_dir)
            if p.is_dir():
                return p
    # 兜底：按标准布局推断（单文件 <name>.py → <name>.pages/；包 <name>/ → pages/）
    root = plugins_root()
    for kind, path, cand_name in _iter_candidates(root):
        if cand_name != name:
            continue
        d = _pages_dir_for(kind, name, path)
        if d:
            p = Path(d)
            if p.is_dir():
                return p
    return None
