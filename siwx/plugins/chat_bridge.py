"""插件 ↔ 聊天 API 的桥接层。

把 registry 里的 renderers / 装饰器 / 内容转换器 应用到 api_chat 的消息与会话
数据上，集中处理**热路径守卫**，避免插件拖慢聊天页。

设计要点
--------
1. **消息渲染器（renderers）**：按 local_type 单赢家替换 `kind` 与附加 `render` 节点树。
   渲染器只返回**结构化节点树**（不是 HTML 字符串），由前端 SX.renderNodes 渲染，
   从根上杜绝插件注入 HTML 造成的 XSS。
2. **消息/会话装饰器（decorators）**：对每条消息/每个会话做浅合并补丁（fan-out）。
   默认只在 `hot=False` 下运行；声明 `hot=True` 的才会进入聊天热路径，
   且带超时 + 60s 熔断（见 DecoratorNS.apply）。
3. **会话过滤器（session_filters）**：`fn(session, ctx) -> bool|None`，返回 False 剔除。
4. **内容转换器（content_transformers）**：批量改写消息正文（如术语替换、脱敏）。
5. **头像解析器（avatar_resolvers）**：`fn(username, ctx) -> bytes|None`，优先于内置。

所有插件调用都裹在 try/except 里：插件抛异常只会让该条数据"少一个补丁"，
绝不会 500。
"""
from __future__ import annotations

from typing import Optional

from siwx import logger as log
from siwx.plugins import ensure_loaded, registry


# ── 渲染器 ──────────────────────────────────────────────────

def apply_renderer(msg: dict, ctx: dict = None) -> None:
    """就地应用匹配的插件渲染器（按消息完整形状，便于插件读取 sender/ts）。"""
    ensure_loaded()
    if not registry.renderers:
        return
    local_type = msg.get("type")
    r = registry.renderers.for_type(local_type)
    if r is None:
        return
    plugin = r.meta.name if r.meta else "?"
    try:
        out = r.render(dict(msg), ctx or {})
    except Exception as e:
        log.warn("plugin", f"{plugin}.render 失败（type={local_type}）: {e}")
        return
    if not isinstance(out, dict):
        return
    # 只接受约定字段，避免插件污染整条消息
    for key in ("kind", "render", "text", "extra"):
        if key in out:
            msg[key] = out[key]
    if not msg.get("kind"):
        msg["kind"] = r.kind or msg.get("kind") or "text"


# ── 装饰器 ──────────────────────────────────────────────────

def decorate_message(msg: dict, ctx: dict = None, hot: bool = False) -> dict:
    """对单条消息应用装饰器补丁（浅合并）。"""
    ensure_loaded()
    if not registry.message_decorators:
        return msg
    return registry.message_decorators.apply(msg, ctx or {}, hot=hot)


def decorate_session(session: dict, ctx: dict = None, hot: bool = False) -> dict:
    """对单个会话应用装饰器补丁（浅合并）。"""
    ensure_loaded()
    if not registry.session_decorators:
        return session
    return registry.session_decorators.apply(session, ctx or {}, hot=hot)


# ── 内容转换器 ──────────────────────────────────────────────

def transform_content(text: str, msg: dict = None, ctx: dict = None) -> str:
    """按优先级串联全部内容转换器。任一插件失败即跳过该层。"""
    ensure_loaded()
    if not registry.content_transformers or not text:
        return text
    out = text
    for _i, h in registry.content_transformers.sorted_items():
        plugin = h.meta.name if h.meta else (h.name or "?")
        try:
            res = h.fn(out, msg or {}, ctx or {})
        except Exception as e:
            log.warn("plugin", f"{plugin}.transform_content 失败: {e}")
            continue
        if isinstance(res, str):
            out = res
    return out


# ── 会话过滤器 ──────────────────────────────────────────────

def filter_sessions(sessions: list, ctx: dict = None) -> list:
    """按会话过滤器筛除会话。返回 False 即剔除；None/True 保留。"""
    ensure_loaded()
    if not registry.session_filters or not sessions:
        return sessions
    ctx = ctx or {}
    out = []
    for s in sessions:
        keep = True
        for _i, h in registry.session_filters.sorted_items():
            plugin = h.meta.name if h.meta else (h.name or "?")
            try:
                if h.fn(s, ctx) is False:
                    keep = False
                    break
            except Exception as e:
                log.warn("plugin", f"{plugin}.filter_session 失败: {e}")
        if keep:
            out.append(s)
    return out


# ── 头像解析器 ──────────────────────────────────────────────

def resolve_avatar(username: str, account: str, ctx: dict = None) -> Optional[bytes]:
    """插件头像解析：返回图片字节则采用，None 表示交给内置实现。"""
    ensure_loaded()
    if not registry.avatar_resolvers:
        return None
    ctx = dict(ctx or {})
    ctx.setdefault("account", account)
    for _i, h in registry.avatar_resolvers.sorted_items():
        plugin = h.meta.name if h.meta else (h.name or "?")
        try:
            data = h.fn(username, ctx)
        except Exception as e:
            log.warn("plugin", f"{plugin}.resolve_avatar 失败: {e}")
            continue
        if isinstance(data, (bytes, bytearray)) and data:
            return bytes(data)
    return None


# ── 上下文 ──────────────────────────────────────────────────

def chat_ctx(account: str = "", chat: str = "", is_group: bool = False,
             **extra) -> dict:
    """构造传给插件的数据上下文（只放插件需要的元信息）。"""
    ctx = {"account": account, "chat": chat, "is_group": bool(is_group)}
    ctx.update(extra)
    return ctx


def has_message_hooks() -> bool:
    """是否存在需要作用于消息的插件钩子（供调用方提前短路）。"""
    ensure_loaded()
    return bool(registry.renderers or registry.message_decorators
                or registry.content_transformers)


def has_session_hooks() -> bool:
    ensure_loaded()
    return bool(registry.session_decorators or registry.session_filters)
