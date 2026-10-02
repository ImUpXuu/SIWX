"""插件注册表 —— 单一中央 Registry + 17 个类型化 hook 命名空间。

设计要点：

1. **内置不动**：内置的 8 种导出格式、4 个密钥策略、6 个 MCP 工具、6 个 UI 页面
   全部留在原处作为"默认层"。注册表**只装插件层**。消费方按「内置 → 插件」合并，
   因此零插件时行为与不引入插件系统时逐字节一致。

2. **零插件零开销**：每个 hook 命名空间都提供 `has()` / `__bool__`，消费方写成
   `if not registry.pages: <原逻辑>` 即可短路，不产生额外分支成本。

3. **隔离**：所有 hook 调用点统一走组合器，组合器内部 try/except；插件的异常
   只记日志并跳过，绝不上抛影响宿主。

4. **确定性排序**：priority 降序 → 插件名字典序 → 声明索引，三级稳定排序。
"""
import threading
from dataclasses import dataclass, field
from typing import Callable, Optional

# ── 数据结构 ────────────────────────────────────────────────────


@dataclass
class PluginMeta:
    """插件级元数据。name 是唯一去重键。"""

    name: str
    version: str = "0.0.0"
    author: str = ""
    description: str = ""
    homepage: str = ""
    requires: list = field(default_factory=list)
    api_version: int = 1
    source: str = ""


@dataclass
class ExportFormat:
    """新增导出格式。writer 签名同内置 stream_export_*。"""

    fmt: str
    ext: str
    writer: Callable
    label: str = ""
    streaming: bool = True
    meta: Optional[PluginMeta] = None


@dataclass
class KeyStrategy:
    """密钥提取策略。fn(ctx) -> int。"""

    name: str
    fn: Callable
    process_dependent: bool = False
    meta: Optional[PluginMeta] = None


@dataclass
class McpTool:
    """MCP 工具。handler(args) -> JSON 字符串。"""

    name: str
    description: str
    handler: Callable
    input_schema: dict = field(default_factory=dict)
    meta: Optional[PluginMeta] = None


@dataclass
class ApiBlueprint:
    """Flask 蓝图。"""

    bp: object
    meta: Optional[PluginMeta] = None


@dataclass
class UiPage:
    """左侧菜单平级页面。

    name  —— 插件内唯一页面名（前端会加插件名前缀成全局 id）
    entry —— 页面资源基名，默认 index → index.html / index.js / index.css
    其余字段（badge/tip/group）为菜单可选装饰，缺省不影响渲染。
    """

    name: str
    title: str
    icon: str = "🔌"
    order: int = 100
    condition: dict = field(default_factory=dict)
    pages_dir: str = ""
    entry: str = "index"
    badge: str = ""
    tip: str = ""
    group: str = ""
    meta: Optional[PluginMeta] = None


@dataclass
class SettingItem:
    """声明式设置项。"""

    plugin: str
    group: str
    key: str
    type: str = "str"
    label: str = ""
    default: object = None
    choices: list = field(default_factory=list)
    min: object = None
    max: object = None
    help: str = ""
    meta: Optional[PluginMeta] = None


@dataclass
class Renderer:
    """消息渲染器（按 local_type 单赢家）。"""

    local_types: list
    render: Callable
    kind: str = ""
    priority: int = 0
    meta: Optional[PluginMeta] = None


@dataclass
class Decorator:
    """消息/会话装饰器（transform，fan-out）。"""

    decorate: Callable
    name: str = ""
    priority: int = 0
    timeout_ms: int = 50
    hot: bool = False            # 是否允许在交互热路径生效
    meta: Optional[PluginMeta] = None


@dataclass
class CliCommand:
    """CLI 子命令。handler(args) -> int。"""

    name: str
    handler: Callable
    help: str = ""
    args: list = field(default_factory=list)
    meta: Optional[PluginMeta] = None


@dataclass
class AfterExport:
    """导出后处理钩子。run(ctx) -> dict|None。"""

    run: Callable
    when: str = "before_zip"
    name: str = ""
    priority: int = 0
    meta: Optional[PluginMeta] = None


@dataclass
class GenericHook:
    """通用钩子（content_transformers / avatar_resolvers / media_providers /
    session_filters / task_listeners / themes / routes 等薄 hook 共用）。"""

    fn: Callable
    name: str = ""
    key: str = ""
    priority: int = 0
    meta: Optional[PluginMeta] = None


# ── hook 命名空间 ───────────────────────────────────────────────


class _Namespace:
    """命名空间基类：装一组条目 + 提供确定性排序。"""

    kind = "generic"

    def __init__(self):
        self.items: list = []

    def add(self, item) -> None:
        self.items.append(item)

    def append(self, item) -> None:
        """add 的别名（列表式写法更顺手）。"""
        self.items.append(item)

    def entries(self) -> list:
        """全部登记项（声明顺序，未排序）。"""
        return list(self.items)

    def __len__(self) -> int:
        return len(self.items)

    def __bool__(self) -> bool:
        return bool(self.items)

    def sorted_items(self) -> list:
        """priority 降序 → 插件名 → 声明索引（稳定）。"""
        return sorted(
            enumerate(self.items),
            key=lambda pair: (
                -getattr(pair[1], "priority", 0),
                (getattr(pair[1], "meta", None).name
                 if getattr(pair[1], "meta", None) else ""),
                pair[0],
            ),
        )


class KeyStrategyNS(_Namespace):
    kind = "key_strategies"

    def process_dependent(self) -> set:
        return {s.fn for s in self.items if s.process_dependent}


class RendererNS(_Namespace):
    kind = "renderers"

    def for_type(self, local_type) -> Optional[Renderer]:
        """按 local_type 取单赢家（priority 最高者）。"""
        best = None
        for _i, r in self.sorted_items():
            if local_type in (r.local_types or []):
                return r          # 已按 priority 降序，首个即赢家
        return best


class DecoratorNS(_Namespace):
    kind = "decorators"

    def __init__(self):
        super().__init__()
        self._breaker: dict = {}          # plugin -> 熔断到期时间戳
        self._stuck: set = set()          # 已超时被放弃、仍在后台跑的线程池
        self._guard = threading.Lock()

    def _tripped(self, plugin: str) -> bool:
        import time
        with self._guard:
            until = self._breaker.get(plugin, 0)
            if until and time.time() < until:
                return True
            if until:
                self._breaker.pop(plugin, None)
            return False

    def _trip(self, plugin: str, seconds: int = 60) -> None:
        import time
        with self._guard:
            self._breaker[plugin] = time.time() + seconds

    #: 超过此时长仍未结束的装饰器线程数 → 不再新建线程（防线程泄漏）
    _MAX_STUCK = 8

    def apply(self, target: dict, ctx, hot: bool = False,
              max_ms: int = 200) -> dict:
        """把全部装饰器的补丁浅合并到 target，返回合并后的 target。

        hot=False 时只跑声明了 hot=True 的装饰器（保护交互热路径）。

        超时语义（重要）：超时后**必须立刻返回**，不能等插件线程跑完。
        因此这里不用 `with ThreadPoolExecutor(...)` —— 它的 `__exit__` 会
        `shutdown(wait=True)`，把已经超时的调用重新阻塞回来，让 timeout 形同虚设。
        改为手动 submit + 不复用池：超时即放弃该 future（线程仍在后台跑完，
        由熔断保证不会再被调用，从而最多泄漏有限个线程）。
        """
        import concurrent.futures as _cf
        from siwx import logger as log

        if not self.items:
            return target

        for _i, dec in self.sorted_items():
            if hot and not dec.hot:
                continue
            plugin = dec.meta.name if dec.meta else (dec.name or "?")
            if self._tripped(plugin):
                continue
            with self._guard:
                if len(self._stuck) >= self._MAX_STUCK:
                    # 已有过多插件线程卡住：整体熔断，避免拖垮宿主
                    log.warn("plugin",
                             f"插件装饰器卡住线程过多（{len(self._stuck)}），"
                             f"暂停调用")
                    return target
            budget = min(int(dec.timeout_ms or 50), max_ms) / 1000.0
            ex = _cf.ThreadPoolExecutor(max_workers=1)
            try:
                patch = ex.submit(dec.decorate, target, ctx).result(
                    timeout=budget)
                ex.shutdown(wait=False)
                if isinstance(patch, dict) and patch:
                    target.update(patch)
            except _cf.TimeoutError:
                # 不 shutdown(wait=True)：让它自己跑完，登记为"卡住线程"
                self._mark_stuck(ex)
                self._trip(plugin, 60)
                log.warn("plugin", f"{plugin}.decorate 超时（{budget:.2f}s），熔断 60s")
            except Exception as e:
                ex.shutdown(wait=False)
                log.warn("plugin", f"{plugin}.decorate 失败: {e}")
        return target

    def _mark_stuck(self, ex) -> None:
        """登记一个超时后被放弃的线程池（任务结束即自动移除）。"""
        with self._guard:
            self._stuck.add(ex)

        def _cleanup(_f):
            with self._guard:
                self._stuck.discard(ex)

        # 借一个哨兵 future 感知线程池何时空闲，从而释放登记
        try:
            ex.submit(lambda: None).add_done_callback(_cleanup)
        except Exception:
            pass


class BlueprintNS(_Namespace):
    kind = "api_blueprints"


class PageNS(_Namespace):
    kind = "pages"

    def after_builtin(self, ctx=None) -> list:
        """按 order 升序返回可见页面（服务端已求值显示条件）。"""
        from siwx.plugins import conditions

        out = []
        for _i, p in self.sorted_items():
            if conditions.evaluate(p.condition, ctx):
                out.append(p)
        return out

    def all_pages(self) -> list:
        return [p for _i, p in self.sorted_items()]

    def get(self, name: str) -> Optional[UiPage]:
        for p in self.items:
            if p.name == name:
                return p
        return None

    def owner_of(self, name: str) -> str:
        p = self.get(name)
        return p.meta.name if (p and p.meta) else ""


class PluginRegistry:
    """中央注册表。全部 hook 命名空间的唯一持有者。"""

    def __init__(self):
        self.metas: dict = {}

        # 第一批（用户点名）
        self.pages = PageNS()
        self.renderers = RendererNS()
        self.message_decorators = DecoratorNS()
        self.session_decorators = DecoratorNS()
        self.settings = _Namespace()
        self.after_export = _Namespace()
        self.cli_commands = _Namespace()

        # 第二批（补充）
        self.content_transformers = _Namespace()
        self.avatar_resolvers = _Namespace()
        self.media_providers = _Namespace()
        self.session_filters = _Namespace()
        self.task_listeners = _Namespace()
        self.themes = _Namespace()
        self.mcp_tools = _Namespace()
        self.export_writers = _Namespace()
        self.key_strategies = KeyStrategyNS()
        self.routes = _Namespace()

        # API 蓝图（插件自带）
        self.api_blueprints = BlueprintNS()

        self.report = None
        self._loaded = False
        self._lock = threading.Lock()

    # ── 查询辅助 ────────────────────────────────────────────

    def is_loaded(self) -> bool:
        return self._loaded

    def find_export_format(self, fmt: str) -> Optional[ExportFormat]:
        for _i, w in self.export_writers.sorted_items():
            if w.fmt == fmt:
                return w
        return None

    def find_media_provider(self, kind: str) -> Optional[GenericHook]:
        for _i, p in self.media_providers.sorted_items():
            if p.key == kind:
                return p
        return None

    def plugin_settings(self, plugin: str = None) -> list:
        items = self.settings.entries()
        if plugin is None:
            return items
        return [s for s in items if s.plugin == plugin]

    def schema_for(self, plugin: str) -> list:
        return [
            {"key": s.key, "type": s.type, "label": s.label,
             "default": s.default, "choices": list(s.choices or []),
             "min": s.min, "max": s.max, "help": s.help, "group": s.group}
            for s in self.settings.entries() if s.plugin == plugin
        ]

    def summary_counts(self) -> dict:
        """各命名空间的插件贡献计数，供诊断页展示。"""
        ns = {
            "pages": self.pages, "renderers": self.renderers,
            "message_decorators": self.message_decorators,
            "session_decorators": self.session_decorators,
            "after_export": self.after_export, "cli_commands": self.cli_commands,
            "content_transformers": self.content_transformers,
            "avatar_resolvers": self.avatar_resolvers,
            "media_providers": self.media_providers,
            "session_filters": self.session_filters,
            "task_listeners": self.task_listeners, "themes": self.themes,
            "mcp_tools": self.mcp_tools, "export_writers": self.export_writers,
            "key_strategies": self.key_strategies, "routes": self.routes,
        }
        out = {k: len(v) for k, v in ns.items() if len(v)}
        if self.settings:
            out["settings"] = len(self.settings)
        if self.api_blueprints:
            out["api_blueprints"] = len(self.api_blueprints)
        return out


#: 模块级单例 —— 全系统唯一的注册表实例
registry = PluginRegistry()
