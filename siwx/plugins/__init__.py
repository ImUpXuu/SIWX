"""siwx 插件系统 —— 目录自动发现 + 声明式契约 + 17 个 hook 命名空间。

用法（宿主侧）::

    from siwx.plugins import registry, load_all, ensure_loaded
    ensure_loaded()                  # 入口处调用一次（幂等）
    for page in registry.pages.after_builtin(): ...

用法（插件作者侧，放 %LOCALAPPDATA%/stories-in-wx/plugins/）::

    PLUGIN = {
        "name": "myplugin", "version": "1.0",
        "pages": [{"name": "mine", "title": "我的页", "icon": "🎯"}],
    }
"""
from siwx.plugins.registry import PluginMeta, PluginRegistry, registry    # noqa: F401
from siwx.plugins.loader import (                                          # noqa: F401
    discover_entrypoints, ensure_loaded, ensure_root, load_all, plugins_root,
)
from siwx.plugins.report import PluginLoadReport, PluginStatus              # noqa: F401

__all__ = [
    "registry", "PluginRegistry", "PluginMeta",
    "load_all", "ensure_loaded", "ensure_root", "plugins_root",
    "discover_entrypoints",
    "PluginLoadReport", "PluginStatus",
]
