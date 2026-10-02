"""插件契约 —— 校验 PLUGIN 字典并把"字符串函数名"解析回函数对象。

契约设计（最低摩擦）：

    PLUGIN = {
        "name": "myplugin", "version": "1.0",
        "pages": [{"name": "x", "title": "X", "icon": "🎯"}],
        "renderers": [{"local_types": [48], "render": "__render__"}],
    }

函数一律用**同模块顶层函数的字符串名**引用（如 `"__render__"`），而不是函数对象。
这样插件无需 `import siwx`，也不会在 import 期产生循环依赖 —— 宿主在注册时才
把字符串解析成真正的函数对象。

解析规则：优先取模块 `__dict__` 里的同名顶层属性；找不到则解析失败，
整个 hook 条目被跳过并记录 warn（不影响同插件其它 hook）。
"""
from siwx import logger as log

# hook 键 → 注册表命名空间属性名
HOOK_KEYS = {
    "pages": "pages",
    "renderers": "renderers",
    "message_decorators": "message_decorators",
    "session_decorators": "session_decorators",
    "settings": "settings",
    "after_export": "after_export",
    "cli": "cli_commands",
    "content_transformers": "content_transformers",
    "avatar_resolvers": "avatar_resolvers",
    "media_providers": "media_providers",
    "session_filters": "session_filters",
    "task_listeners": "task_listeners",
    "themes": "themes",
    "mcp_tools": "mcp_tools",
    "export_formats": "export_writers",
    "key_strategies": "key_strategies",
    "routes": "routes",
    "api_blueprints": "api_blueprints",
}

SUPPORTED_API_VERSION = 1


def resolve_callable(module, name):
    """把字符串函数名解析为同模块顶层可调用对象；失败返回 None。"""
    if callable(name):
        return name
    if not isinstance(name, str) or not name:
        return None
    fn = getattr(module, name, None)
    return fn if callable(fn) else None


def meta_from_dict(data: dict):
    """从 PLUGIN dict 构造 PluginMeta。兼容扁平写法（name/version 直接在顶层）。"""
    from siwx.plugins.registry import PluginMeta

    raw = data.get("meta") if isinstance(data.get("meta"), dict) else None
    src = raw or data
    name = str(src.get("name") or "").strip()
    if not name:
        return None
    return PluginMeta(
        name=name,
        version=str(src.get("version") or "0.0.0"),
        author=str(src.get("author") or ""),
        description=str(src.get("description") or ""),
        homepage=str(src.get("homepage") or ""),
        requires=list(src.get("requires") or []),
        api_version=int(src.get("api_version") or 1),
    )


def check_requires(requires) -> list:
    """探测缺少的依赖包名（不 import，只看能否找到 spec）。"""
    import importlib.util

    missing = []
    for dep in requires or []:
        pkg = str(dep).split(">=")[0].split("==")[0].split("[")[0].strip()
        if not pkg:
            continue
        try:
            if importlib.util.find_spec(pkg) is None:
                missing.append(pkg)
        except (ImportError, ValueError, ModuleNotFoundError):
            missing.append(pkg)
    return missing


def _resolve_pages_root(module, explicit, hint, auto, plugin_name: str) -> str:
    """解析插件页面资源根目录。

    优先级：PLUGIN["pages_dir"]（绝对/相对） > PLUGIN["pages_hint"]（相对插件文件）
            > loader 自动探测值（<file>.pages/ 或 <pkg>/pages/）。
    相对路径以插件源文件所在目录为基准。找不到时返回自动探测值（可能为空串）。
    """
    from pathlib import Path

    src = str(getattr(module, "__siwx_source__", "") or "")
    base = None
    if src.startswith("dir:"):
        raw = Path(src[4:])
        base = raw.parent if raw.is_file() else raw

    for cand in (explicit, hint):
        if not cand:
            continue
        p = Path(str(cand))
        if not p.is_absolute():
            p = (base / p) if base is not None else p
        if p.is_dir():
            return str(p)
        log.warn("plugin", f"{plugin_name} 页面目录不存在，已忽略: {cand}")
    return auto


def _build_pages(module, meta, entries):
    from siwx.plugins.registry import UiPage

    default_dir = str(getattr(module, "__siwx_pages_dir__", "") or "")

    out = []
    for e in entries or []:
        if not isinstance(e, dict):
            continue
        name = str(e.get("name") or "").strip()
        if not name:
            log.warn("plugin", f"{meta.name}.pages 缺少 name，已跳过")
            continue
        pages_dir = e.get("pages_dir") or default_dir
        out.append(UiPage(
            name=name,
            title=str(e.get("title") or name),
            icon=str(e.get("icon") or "🔌"),
            order=int(e.get("order") or 100),
            condition=e.get("condition") or {},
            pages_dir=str(pages_dir),
            entry=str(e.get("entry") or "index"),
            badge=str(e.get("badge") or ""),
            tip=str(e.get("tip") or ""),
            group=str(e.get("group") or ""),
            meta=meta,
        ))
    return out


def _build_renderers(module, meta, entries):
    from siwx.plugins.registry import Renderer

    out = []
    for e in entries or []:
        if not isinstance(e, dict):
            continue
        fn = resolve_callable(module, e.get("render"))
        if fn is None:
            log.warn("plugin", f"{meta.name}.renderers 无法解析 render="
                               f"{e.get('render')!r}，已跳过")
            continue
        types = e.get("local_types") or []
        if isinstance(types, int):
            types = [types]
        out.append(Renderer(
            local_types=[int(t) for t in types],
            render=fn,
            kind=str(e.get("kind") or ""),
            priority=int(e.get("priority") or 0),
            meta=meta,
        ))
    return out


def _build_decorators(module, meta, entries, default_hot=False):
    from siwx.plugins.registry import Decorator

    out = []
    for e in entries or []:
        if not isinstance(e, dict):
            continue
        fn = resolve_callable(module, e.get("decorate"))
        if fn is None:
            log.warn("plugin", f"{meta.name} 装饰器无法解析 decorate="
                               f"{e.get('decorate')!r}，已跳过")
            continue
        out.append(Decorator(
            decorate=fn,
            name=str(e.get("name") or meta.name),
            priority=int(e.get("priority") or 0),
            timeout_ms=int(e.get("timeout_ms") or 50),
            hot=bool(e.get("hot", default_hot)),
            meta=meta,
        ))
    return out


def _build_settings(module, meta, entries):
    from siwx.plugins.registry import SettingItem

    out = []
    seen = set()
    for e in entries or []:
        if not isinstance(e, dict):
            continue
        key = str(e.get("key") or "").strip()
        if not key or key in seen:
            log.warn("plugin", f"{meta.name}.settings 键缺失或重复: {key!r}")
            continue
        seen.add(key)
        out.append(SettingItem(
            plugin=meta.name,
            group=str(e.get("group") or meta.name),
            key=key,
            type=str(e.get("type") or "str").lower(),
            label=str(e.get("label") or key),
            default=e.get("default"),
            choices=list(e.get("choices") or []),
            min=e.get("min"), max=e.get("max"),
            help=str(e.get("help") or ""),
            meta=meta,
        ))
    return out


def _build_themes(module, meta, entries):
    """主题声明 → GenericHook（key 为 CSS 文件名，或 fn 为返回 CSS 文本的函数）。

    支持两种形态：
      {"name": "dark", "css": "theme.css"}        ← 页面资源目录内的 CSS 文件名
      {"name": "dark", "css": custom_css_fn}      ← 函数，返回 CSS 文本（内联注入）
    """
    from siwx.plugins.registry import GenericHook

    out = []
    for e in entries or []:
        if isinstance(e, str):                       # 简写：直接给 CSS 文件名
            e = {"name": e, "css": e}
        if not isinstance(e, dict):
            continue
        name = str(e.get("name") or "").strip()
        spec = e.get("css") or e.get("fn")
        if not name or not spec:
            log.warn("plugin", f"{meta.name}.themes 条目缺少 name/css，已跳过")
            continue
        prio = int(e.get("priority") or 0)

        fn = resolve_callable(module, spec) if isinstance(spec, str) else spec
        if callable(fn):
            # 函数形态：内联 CSS 文本
            out.append(GenericHook(fn=fn, name=name, key="", priority=prio, meta=meta))
        elif isinstance(spec, str):
            # 文件名形态：只允许纯文件名（防路径穿越），由宿主注入 <link>
            if "/" in spec or "\\" in spec or ".." in spec:
                log.warn("plugin", f"{meta.name}.themes 的 css 含路径分隔符，已跳过")
                continue
            out.append(GenericHook(fn=lambda: "", name=name, key=spec,
                                   priority=prio, meta=meta))
        else:
            log.warn("plugin", f"{meta.name}.themes 的 css 类型不支持，已跳过")
    return out


def _build_cli(module, meta, entries):
    from siwx.plugins.registry import CliCommand

    out = []
    for e in entries or []:
        if not isinstance(e, dict):
            continue
        name = str(e.get("name") or "").strip()
        fn = resolve_callable(module, e.get("handler"))
        if not name or fn is None:
            log.warn("plugin", f"{meta.name}.cli 无效条目（name={name!r}）")
            continue
        out.append(CliCommand(
            name=name, handler=fn,
            help=str(e.get("help") or ""),
            args=list(e.get("args") or []),
            meta=meta,
        ))
    return out


def _build_after_export(module, meta, entries):
    from siwx.plugins.registry import AfterExport

    out = []
    for e in entries or []:
        if not isinstance(e, dict):
            continue
        fn = resolve_callable(module, e.get("run"))
        if fn is None:
            log.warn("plugin", f"{meta.name}.after_export 无法解析 run，已跳过")
            continue
        when = str(e.get("when") or "before_zip")
        if when not in ("before_zip", "after_zip"):
            log.warn("plugin", f"{meta.name}.after_export when={when!r} 非法，"
                               f"回退 before_zip")
            when = "before_zip"
        out.append(AfterExport(run=fn, when=when,
                               name=str(e.get("name") or meta.name),
                               priority=int(e.get("priority") or 0), meta=meta))
    return out


def _build_mcp(module, meta, entries):
    from siwx.plugins.registry import McpTool

    out = []
    for e in entries or []:
        if not isinstance(e, dict):
            continue
        name = str(e.get("name") or "").strip()
        fn = resolve_callable(module, e.get("handler"))
        if not name or fn is None:
            log.warn("plugin", f"{meta.name}.mcp_tools 无效条目（name={name!r}）")
            continue
        schema = e.get("inputSchema") or e.get("input_schema") or {}
        out.append(McpTool(
            name=name,
            description=str(e.get("description") or ""),
            handler=fn,
            input_schema=schema if isinstance(schema, dict) else {},
            meta=meta,
        ))
    return out


def _build_export(module, meta, entries):
    from siwx.plugins.registry import ExportFormat

    out = []
    for e in entries or []:
        if not isinstance(e, dict):
            continue
        fmt = str(e.get("fmt") or "").strip().lower()
        fn = resolve_callable(module, e.get("writer"))
        if not fmt or fn is None:
            log.warn("plugin", f"{meta.name}.export_formats 无效条目（fmt={fmt!r}）")
            continue
        out.append(ExportFormat(
            fmt=fmt, ext=str(e.get("ext") or fmt), writer=fn,
            label=str(e.get("label") or fmt),
            streaming=bool(e.get("streaming", True)), meta=meta,
        ))
    return out


def _build_keys(module, meta, entries):
    from siwx.plugins.registry import KeyStrategy

    out = []
    for e in entries or []:
        if not isinstance(e, dict):
            continue
        fn = resolve_callable(module, e.get("fn") or e.get("extract"))
        if fn is None:
            log.warn("plugin", f"{meta.name}.key_strategies 无法解析 fn，已跳过")
            continue
        out.append(KeyStrategy(
            name=str(e.get("name") or meta.name), fn=fn,
            process_dependent=bool(e.get("process_dependent", False)), meta=meta,
        ))
    return out


def _build_generic(module, meta, entries, fn_keys):
    """通用薄 hook：从多个可能的键里解析出函数。"""
    from siwx.plugins.registry import GenericHook

    out = []
    for e in entries or []:
        if isinstance(e, str):
            e = {fn_keys[0]: e}
        if not isinstance(e, dict):
            continue
        fn = None
        for k in fn_keys:
            fn = resolve_callable(module, e.get(k))
            if fn is not None:
                break
        if fn is None:
            log.warn("plugin", f"{meta.name} hook 无法解析函数（{fn_keys[0]}），已跳过")
            continue
        out.append(GenericHook(
            fn=fn,
            name=str(e.get("name") or meta.name),
            key=str(e.get("kind") or e.get("key") or ""),
            priority=int(e.get("priority") or 0),
            meta=meta,
        ))
    return out


def register_plugin(module, registry, data: dict) -> dict:
    """把一份 PLUGIN dict 注册进 registry，返回 {hook_name: 成功条数}。

    单个 hook 出错不影响其它 hook（逐 hook 隔离）。
    """
    meta = meta_from_dict(data)
    if meta is None:
        raise ValueError("PLUGIN 缺少 name")

    if meta.api_version > SUPPORTED_API_VERSION:
        raise ValueError(f"api_version={meta.api_version} 高于宿主支持"
                         f"（{SUPPORTED_API_VERSION}）")

    meta.source = str(getattr(module, "__siwx_source__", "") or "")
    registry.metas[meta.name] = meta
    counts = {}

    # 页面目录：优先 PLUGIN 的 pages_dir，其次模块级 __siwx_pages_dir__，
    # 最后由 loader 在导入前写入（单文件插件 → <file>.pages/，包插件 → <pkg>/pages/）
    default_pages_dir = _resolve_pages_root(
        module,
        data.get("pages_dir"),
        data.get("pages_hint"),
        str(getattr(module, "__siwx_pages_dir__", "") or ""),
        meta.name,
    )
    for entry in data.get("pages") or []:
        if isinstance(entry, dict) and not entry.get("pages_dir"):
            entry["pages_dir"] = default_pages_dir

    builders = (
        ("pages", lambda: _build_pages(module, meta, data.get("pages"))),
        ("renderers", lambda: _build_renderers(module, meta, data.get("renderers"))),
        ("message_decorators", lambda: _build_decorators(
            module, meta, data.get("message_decorators"))),
        ("session_decorators", lambda: _build_decorators(
            module, meta, data.get("session_decorators"))),
        ("settings", lambda: _build_settings(module, meta, data.get("settings"))),
        ("cli_commands", lambda: _build_cli(module, meta, data.get("cli"))),
        ("after_export", lambda: _build_after_export(
            module, meta, data.get("after_export"))),
        ("mcp_tools", lambda: _build_mcp(module, meta, data.get("mcp_tools"))),
        ("export_writers", lambda: _build_export(
            module, meta, data.get("export_formats"))),
        ("key_strategies", lambda: _build_keys(
            module, meta, data.get("key_strategies"))),
        ("content_transformers", lambda: _build_generic(
            module, meta, data.get("content_transformers"), ("transform", "fn"))),
        ("avatar_resolvers", lambda: _build_generic(
            module, meta, data.get("avatar_resolvers"), ("resolve", "fn"))),
        ("media_providers", lambda: _build_generic(
            module, meta, data.get("media_providers"), ("provide", "fn"))),
        ("session_filters", lambda: _build_generic(
            module, meta, data.get("session_filters"), ("keep", "filter", "fn"))),
        ("task_listeners", lambda: _build_generic(
            module, meta, data.get("task_listeners"), ("on_event", "fn"))),
        ("themes", lambda: _build_themes(module, meta, data.get("themes"))),
        ("routes", lambda: _build_generic(
            module, meta, data.get("routes"), ("handler", "fn"))),
    )

    for hook_name, builder in builders:
        try:
            items = builder()
        except Exception as e:
            log.warn("plugin", f"{meta.name}.{hook_name} 注册失败: {e}")
            continue
        if not items:
            continue
        if hook_name == "settings":
            # settings 是普通 list，直接 extend
            got = 0
            for it in items:
                if registry.plugin_settings(meta.name) and any(
                        s.key == it.key for s in registry.plugin_settings(meta.name)):
                    log.warn("plugin", f"{meta.name}.{it.key} 与已加载插件重名，跳过")
                    continue
                registry.settings.append(it)
                got += 1
            if got:
                counts[hook_name] = got
            continue
        target = getattr(registry, hook_name, None)
        if target is None:
            log.warn("plugin", f"注册表缺少命名空间 {hook_name}")
            continue
        got = 0
        for it in items:
            if _is_duplicate(target, it):
                log.warn("plugin", f"{meta.name} 的 {hook_name} 条目与已加载插件冲突，"
                                   f"跳过: {_dup_key(it)}")
                continue
            target.add(it)
            got += 1
        if got:
            counts[hook_name] = got

    # 蓝图单独处理（支持字符串函数名 / Blueprint 对象 / 工厂函数 / dict 包壳）
    for bp in (data.get("api_blueprints") or []):
        try:
            from siwx.plugins.registry import ApiBlueprint
            if isinstance(bp, dict):
                obj = bp.get("bp") or bp.get("factory") or bp.get("fn")
            else:
                obj = bp
            # 字符串 → 解析为同模块顶层对象（通常是返回 Blueprint 的工厂）
            if isinstance(obj, str):
                obj = resolve_callable(module, obj)
            if obj is None:
                log.warn("plugin", f"{meta.name}.api_blueprints 条目无法解析，已跳过")
                continue
            registry.api_blueprints.add(ApiBlueprint(bp=obj, meta=meta))
            counts["api_blueprints"] = counts.get("api_blueprints", 0) + 1
        except Exception as e:
            log.warn("plugin", f"{meta.name}.api_blueprints 注册失败: {e}")

    return counts


def _dup_key(item) -> str:
    """条目的去重身份。

    显式名字优先（页面/工具/命令/格式/设置项）；没有名字的 hook 用其**函数标识**
    （`插件名:函数名`）兜底 —— 曾经这里只取函数名，导致没有 name 的 Renderer /
    Decorator / AfterExport 全部退化成同一个 "?"，不同插件的无关 hook 会被误判为
    "重名冲突"而丢弃。函数标识必须带上插件名，否则两个插件各自定义 `render` 时
    又会互相顶掉。
    """
    for attr in ("name", "fmt", "key"):
        v = getattr(item, attr, None)
        if v:
            return f"{attr}={v}"
    fn = _item_fn(item)
    if fn is None:
        return f"id={id(item)}"          # 无函数也无名字：按对象身份区分，不去重
    meta = getattr(item, "meta", None)
    owner = getattr(meta, "name", "") if meta else ""
    fname = getattr(fn, "__name__", "") or repr(fn)
    return f"fn={owner}:{fname}"


def _item_fn(item):
    """取出条目承载的主函数（不同 dataclass 字段名不同）。"""
    for attr in ("fn", "render", "decorate", "writer", "handler", "run", "extract"):
        fn = getattr(item, attr, None)
        if callable(fn):
            return fn
    return None


def _is_duplicate(target, item) -> bool:
    """同名条目视为冲突（内置优先于插件、先加载优先于后加载）。"""
    key = _dup_key(item)
    for existing in target.items:
        if _dup_key(existing) == key:
            return True
    # 页面额外与内置 6 项比对（前端也会挡一层，这里是服务端保证）
    if isinstance(item, object) and getattr(item, "name", None) in BUILTIN_PAGE_NAMES:
        return True
    return False


#: 内置 UI 页面名 —— 插件不得占用（前端也应保持内置优先）
BUILTIN_PAGE_NAMES = frozenset(
    {"guide", "chat", "export", "mcp", "logs", "settings"})
