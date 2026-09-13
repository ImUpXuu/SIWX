"""声明式显示条件求值器（服务端）。

为什么不用谓词函数：插件不允许 import siwx，若让插件提供 `lambda: ...` 判断
菜单项是否显示，就等于在 HTTP 请求路径里执行插件代码 —— 既慢（每个请求都要跑），
又凭空增加一个任意代码执行面。因此改为**声明式条件 dict**，由宿主结构化求值：

    "condition": {"requires_decrypted": True, "platform": "windows"}

求值成本极低（只读环境/目录），结果可缓存，且能在设置页解释"这页为什么被隐藏"。

所有原子条件之间是 AND 语义：任一不满足即整个条件为假。
"""
import os
import platform as _platform
from dataclasses import dataclass, field
from pathlib import Path

# 支持的原子条件键（用于文档与校验提示）
KNOWN_KEYS = (
    "requires_decrypted", "requires_account", "wechat_running",
    "env", "platform", "min_version", "config", "any_of",
)


@dataclass
class ConditionContext:
    """一次求值所需的上下文（同一请求内可复用于多个页面）。"""

    decrypted_accounts: list = field(default_factory=list)
    wechat_running: bool = False
    version: str = "0.0.0"
    plugin_config: dict = field(default_factory=dict)   # {key: value} 当前插件配置

    @classmethod
    def build(cls, plugin_config: dict = None) -> "ConditionContext":
        """从当前运行环境采集上下文。任何异常都降级为保守值。"""
        accounts = []
        try:
            from siwx import paths
            root = paths.out_root()
            if root.is_dir():
                for d in root.iterdir():
                    if (d / "message").is_dir():
                        accounts.append(d.name)
        except Exception:
            pass

        running = False
        try:
            if _platform.system() == "Windows":
                from siwx.discover import find_wechat_pids
                running = bool(find_wechat_pids())
        except Exception:
            pass

        version = "0.0.0"
        try:
            from siwx import __version__
            version = __version__
        except Exception:
            pass

        return cls(decrypted_accounts=accounts, wechat_running=running,
                   version=version, plugin_config=dict(plugin_config or {}))


def _version_tuple(v: str):
    """把版本串解析成可比较的元组；非法片段忽略。"""
    out = []
    for part in str(v or "").lstrip("vV").split("."):
        num = ""
        for ch in part:
            if ch.isdigit():
                num += ch
            else:
                break
        out.append(int(num) if num else 0)
    return tuple(out)


def _match_env(spec, ctx: ConditionContext) -> bool:
    """env 条件：字符串 → 变量存在且非空/false；列表 → 全部满足。"""
    if isinstance(spec, str):
        val = os.environ.get(spec)
        return bool(val) and val.strip().lower() not in ("0", "false", "no", "")
    if isinstance(spec, (list, tuple)):
        return all(_match_env(s, ctx) for s in spec)
    if isinstance(spec, dict):
        # {"NAME": "expected"}
        for k, want in spec.items():
            if os.environ.get(k) != str(want):
                return False
        return True
    return False


def _match_platform(spec) -> bool:
    """platform 条件：windows / macos / linux（大小写不敏感）。"""
    cur = _platform.system().lower()
    alias = {"win": "windows", "win32": "windows", "darwin": "macos",
             "osx": "macos", "mac": "macos"}
    cur = alias.get(cur, cur)

    def _one(s):
        s = alias.get(str(s).lower(), str(s).lower())
        return s == cur

    if isinstance(spec, str):
        return _one(spec)
    if isinstance(spec, (list, tuple)):
        return any(_one(s) for s in spec)
    return False


def evaluate(condition, ctx: ConditionContext = None) -> bool:
    """求值一个条件 dict；空/None 视为真（无条件显示）。

    任何意外都不应让页面消失 —— 异常时保守返回 False 并交由上层记录。
    """
    if not condition:
        return True
    if not isinstance(condition, dict):
        return True
    if ctx is None:
        ctx = ConditionContext.build()

    try:
        if condition.get("requires_decrypted") and not ctx.decrypted_accounts:
            return False

        req_account = condition.get("requires_account")
        if req_account and req_account not in ctx.decrypted_accounts:
            return False

        if condition.get("wechat_running") and not ctx.wechat_running:
            return False

        if "env" in condition and not _match_env(condition["env"], ctx):
            return False

        if "platform" in condition and not _match_platform(condition["platform"]):
            return False

        if "min_version" in condition:
            if _version_tuple(ctx.version) < _version_tuple(condition["min_version"]):
                return False

        cfg = condition.get("config")
        if isinstance(cfg, dict):
            for k, want in cfg.items():
                if str(ctx.plugin_config.get(k)) != str(want):
                    return False

        any_of = condition.get("any_of")
        if isinstance(any_of, list) and any_of:
            if not any(evaluate(c, ctx) for c in any_of):
                return False

        return True
    except Exception:
        return False


def explain(condition, ctx: ConditionContext = None) -> str:
    """给出一条人类可读的"为什么显示/隐藏"，供设置页调试。"""
    if not condition:
        return "无显示条件"
    if evaluate(condition, ctx):
        return "条件已满足"
    reasons = []
    for key in ("requires_decrypted", "requires_account", "wechat_running",
                "env", "platform", "min_version", "config"):
        if key in condition:
            sub = {key: condition[key]}
            if not evaluate(sub, ctx):
                reasons.append(key)
    return "未满足: " + ", ".join(reasons) if reasons else "条件未满足"
