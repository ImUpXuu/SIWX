"""插件加载报告 —— 记录每个插件的加载结果与 hook 贡献。

设计目标：插件加载过程中的任何失败都必须"可见但不致命"。
- 加载失败的插件：status="error"，记录阶段与异常摘要（已脱敏）
- 依赖缺失的插件：status="degraded"，hook 仍注册但标注
- 正常插件：status="ok"，记录贡献了哪些 hook 各几个

报告同时供 `GET /api/plugins` 与设置页可视化消费。
"""
from dataclasses import dataclass, field


@dataclass
class PluginStatus:
    """单个插件的加载状态。"""

    name: str
    version: str = "0.0.0"
    source: str = ""                                   # "dir:<abs path>"
    status: str = "ok"                                 # ok | error | degraded
    hooks: dict = field(default_factory=dict)          # {"pages": 1, "mcp_tools": 2}
    missing_requires: list = field(default_factory=list)
    error: str = ""

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "version": self.version,
            "source": self.source,
            "status": self.status,
            "hooks": dict(self.hooks),
            "missing_requires": list(self.missing_requires),
            "error": self.error,
        }


class PluginLoadReport:
    """全部插件的加载报告。"""

    def __init__(self):
        self.statuses: list = []
        self._seen: dict = {}          # name -> PluginStatus（首次为准）

    # ── 记录 ────────────────────────────────────────────────

    def add_ok(self, name: str, version: str = "0.0.0", source: str = "",
               hooks: dict = None, missing: list = None) -> PluginStatus:
        """记录一个成功加载的插件。"""
        missing = list(missing or [])
        st = PluginStatus(
            name=name, version=version, source=source,
            status="degraded" if missing else "ok",
            hooks=dict(hooks or {}), missing_requires=missing,
        )
        self._add(st)
        return st

    def add_error(self, name: str, phase: str, exc: BaseException) -> PluginStatus:
        """记录一个加载失败的插件。phase 如 "import" / "contract" / "register"。"""
        from siwx import logger as log

        detail = log.desensitize_msg(f"{type(exc).__name__}: {exc}")
        st = PluginStatus(
            name=name, source="", status="error",
            error=f"[{phase}] {detail}"[:300],
        )
        self._add(st)
        return st

    def add_skip(self, name: str, reason: str) -> PluginStatus:
        """记录一个被跳过的插件（如名字冲突、被显式禁用）。"""
        st = PluginStatus(name=name, status="degraded", error=reason[:300])
        self._add(st)
        return st

    def _add(self, st: PluginStatus) -> None:
        """同名插件只保留首个（确定性：加载顺序在先者胜）。"""
        if st.name in self._seen:
            return
        self._seen[st.name] = st
        self.statuses.append(st)

    def has(self, name: str) -> bool:
        return name in self._seen

    # ── 查询 ────────────────────────────────────────────────

    def counts(self) -> dict:
        out = {"ok": 0, "degraded": 0, "error": 0}
        for st in self.statuses:
            out[st.status] = out.get(st.status, 0) + 1
        return out

    def to_dicts(self) -> list:
        return [st.to_dict() for st in self.statuses]

    def summary(self) -> str:
        c = self.counts()
        return (f"插件 {len(self.statuses)} 个"
                f"（成功 {c.get('ok', 0)}、降级 {c.get('degraded', 0)}、"
                f"失败 {c.get('error', 0)}）")
