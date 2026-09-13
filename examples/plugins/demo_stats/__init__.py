"""示例插件：统计面板 + 问候工具（包形态，演示全部 13 类 hook）。

安装（Windows）::

    xcopy /E /I examples\\plugins\\demo_stats ^
          "%LOCALAPPDATA%\\stories-in-wx\\plugins\\demo_stats"

安装（macOS / Linux）::

    cp -R examples/plugins/demo_stats "$LOCALAPPDATA/stories-in-wx/plugins/"

重启 stories-in-wx 后：

- 左侧菜单出现「📊 统计面板」（带 DEMO 徽标）与「🧪 导出后动作」
  —— 与内建项完全平级，追加在内建之后
- 设置页多出「统计面板」分组 3 项配置
- 导出格式下拉多出「TSV（制表符，插件提供）」
- CLI 多出 ``python run.py demo-stats --limit 5``
- MCP 工具多出 ``plugin_demo_greet``
- 聊天页：通话消息（local_type 50）用插件渲染器展示；
  每条消息带 ``plugin_demo`` 标记；「哈哈」折叠为「[笑]」；
  公众号会话被过滤掉

开发文档见 docs/plugin-development.md。
"""
from __future__ import annotations

PLUGIN = {
    "name": "demo_stats",
    "version": "1.0.0",
    "description": "示例插件：会话统计面板 + 一个 MCP 工具",
    "author": "stories-in-wx",
    # 依赖缺失时插件降级而非报错（本示例无第三方依赖）
    "requires": [],

    # ── 页面资源目录（相对本文件的 pages/ 子目录）────────────────
    # 单文件插件默认找 <demo_stats>.pages/；此处显式指定同目录 pages/
    "pages_hint": "pages",

    # ── 页面：左侧菜单平级项 ────────────────────────────────────
    "pages": [
        {
            "name": "stats",
            "title": "统计面板",
            "icon": "📊",
            "order": 10,
            "badge": "DEMO",
            "tip": "示例插件提供的统计页",
            "entry": "index",          # → index.html / index.js / index.css
            "condition": {},            # 无条件，始终显示
        },
        {
            "name": "after-export",
            "title": "导出后动作",
            "icon": "🧪",
            "order": 20,
            # 仅在已完成解密时显示
            "condition": {"requires_decrypted": True},
        },
    ],

    # ── 设置项：自动渲染到设置页的「插件」分组 ──────────────────
    "settings": [
        {
            "key": "refresh_seconds",
            "group": "统计面板",
            "type": "int",
            "label": "自动刷新间隔（秒）",
            "default": 30, "min": 5, "max": 600,
            "help": "统计页轮询后端数据的频率",
        },
        {
            "key": "show_media",
            "group": "统计面板",
            "type": "bool",
            "label": "统计中包含媒体消息",
            "default": True,
        },
        {
            "key": "theme",
            "group": "统计面板",
            "type": "choice",
            "label": "图表配色",
            "default": "auto",
            "choices": ["auto", "warm", "cool"],
        },
    ],

    # ── MCP 工具：自动出现在 MCP 工具列表 ───────────────────────
    "mcp_tools": [
        {
            "name": "plugin_demo_greet",
            "description": "示例插件提供的问候工具",
            "input_schema": {
                "type": "object",
                "properties": {"who": {"type": "string", "description": "对象名字"}},
            },
            "handler": "greet",
        },
    ],

    # ── 消息渲染器：接管 local_type 50（通话）的展示 ─────────────
    # 只返回**结构化节点树**（不是 HTML），前端安全渲染。
    "renderers": [
        {
            "local_types": [50],
            "kind": "plugin-call",
            "render": "render_call",
            "priority": 10,
        },
    ],

    # ── 消息装饰器：给每条消息挂插件标记（默认不进热路径）────────
    "message_decorators": [
        {"name": "tag", "decorate": "decorate_message"},
    ],

    # ── 内容转换器：批量改写正文（术语替换示例）──────────────────
    "content_transformers": [
        {"name": "polish", "transform": "transform_content"},
    ],

    # ── 会话过滤器：剔除公众号会话（演示 filter）──────────────────
    "session_filters": [
        {"name": "no_official", "fn": "filter_session"},
    ],

    # ── 导出格式：给宿主增加一个 "tsv" 格式（追加在内置之后）──────
    "export_formats": [
        {
            "fmt": "tsv",
            "label": "TSV（制表符，插件提供）",
            "ext": "tsv",
            "writer": "write_tsv",
        },
    ],

    # ── 导出后处理：zip 打包前写一份统计文件 ─────────────────────
    "after_export": [
        {"name": "stats", "run": "after_export_stats", "when": "before_zip"},
    ],

    # ── CLI 子命令：python -m siwx demo-stats --limit 5 ──────────
    # ── 任务生命周期监听：任务开始/结束会被通知 ───────────────────
    "task_listeners": [
        {"name": "tasklog", "on_event": "on_task_event"},
    ],

    # ── 自定义主题：注入到 index.html 的 <head> ──────────────────
    "themes": [
        {"name": "demo-skin", "css": "theme.css", "priority": 0},
    ],

    # ── 自定义 API 蓝图：直接挂到 Flask 应用 ─────────────────────
    # 值可以是"同模块顶层函数名字符串"（推荐，避免模块加载顺序问题），
    # 也可以直接给 Blueprint 对象 / 返回 Blueprint 的工厂函数。
    "api_blueprints": ["make_blueprint"],

    # ── CLI 子命令：python run.py demo-stats --limit 5 ──────────
    "cli": [
        {
            "name": "demo-stats",
            "help": "[插件] 打印账号/会话概览",
            "handler": "cli_stats",
            "args": [
                {"name": "--account", "type": "str", "default": "", "help": "账号目录名"},
                {"name": "--limit", "type": "int", "default": 10, "help": "最多列出会话数"},
            ],
        },
    ],
}


def make_blueprint():
    """返回一个 Flask 蓝图（宿主会挂载它）。"""
    from flask import Blueprint, jsonify

    bp = Blueprint("demo_stats_api", __name__, url_prefix="/api/demo-stats")

    @bp.get("/ping")
    def _ping():
        return jsonify({"ok": True, "plugin": "demo_stats"})

    @bp.get("/summary")
    def _summary():
        return jsonify({"note": "这是 demo_stats 插件自带的 API"})

    return bp


def on_task_event(event: str, ctx: dict) -> None:
    """任务生命周期回调：追加一行插件日志。"""
    from siwx import logger as log
    if event == "start":
        log.info("plugin", f"[demo_stats] 任务开始 mode={ctx.get('mode')}")
    elif event == "done":
        ok = ctx.get("ok")
        log.info("plugin", f"[demo_stats] 任务结束 ok={ok} "
                           f"耗时={ctx.get('duration_ms')}ms")


def greet(args: dict) -> dict:
    """MCP 工具处理函数：必须接受 dict 返回 dict。"""
    who = (args or {}).get("who") or "世界"
    return {"content": [{"type": "text", "text": f"你好，{who}！来自 demo_stats 插件。"}]}


def render_call(msg: dict, ctx: dict) -> dict:
    """渲染通话消息：返回结构化节点树。"""
    is_video = "视频" in (msg.get("text") or "")
    return {
        "kind": "plugin-call",
        "render": [
            {"tag": "span", "cls": "plg-call",
             "c": [
                 {"t": "text", "v": "📞 "},
                 {"tag": "b", "v": "视频通话" if is_video else "语音通话"},
                 {"t": "text", "v": f"（{msg.get('text') or '未知时长'}）"},
             ]},
        ],
    }


def decorate_message(msg: dict, ctx: dict) -> dict:
    """给消息补一个 flag，供前端/其它插件使用（浅合并进消息）。"""
    return {"plugin_demo": True}


def transform_content(text: str, msg: dict, ctx: dict) -> str:
    """内容转换示例：把 "哈哈" 折叠成 "[笑]"。"""
    if not text:
        return text
    return text.replace("哈哈哈哈", "[狂笑]").replace("哈哈", "[笑]")


def filter_session(session: dict, ctx: dict):
    """返回 False 表示从会话列表剔除。此处剔除公众号。"""
    return not session.get("is_official")


def write_tsv(path, ctx: dict) -> int:
    """导出写入器契约：writer(path, ctx) -> 写入条数。

    ctx["stream"] 是**导出形状**的惰性生成器（键名见 exporter._run_plugin_writer
    文档）：localId / createTime / localType / content / senderDisplayName ...
    """
    n = 0
    with open(path, "w", encoding="utf-8", newline="") as f:
        f.write("localId\tcreateTime\tlocalType\ttypeName\tsender\tcontent\n")
        for m in ctx.get("stream") or []:
            sender = (m.get("senderDisplayName") or m.get("senderUsername") or "")
            text = (m.get("content") or "").replace("\t", " ").replace("\n", " ")
            f.write(f"{m.get('localId', 0)}\t{m.get('createTime', 0)}\t"
                    f"{m.get('localType', '')}\t{m.get('typeName', '')}\t"
                    f"{sender}\t{text}\n")
            n += 1
    return n


def after_export_stats(ctx: dict) -> None:
    """导出后处理：在导出目录写一份 _plugin_stats.txt（zip 打包前）。"""
    import json
    import os

    out_dir = ctx.get("out_dir") or ctx.get("export_dir")
    if not out_dir or not os.path.isdir(out_dir):
        return
    info = {
        "plugin": "demo_stats",
        "format": ctx.get("format"),
        "chat": ctx.get("chat"),
        "message_count": ctx.get("message_count"),
    }
    with open(os.path.join(out_dir, "_plugin_stats.txt"), "w",
              encoding="utf-8") as f:
        f.write(json.dumps(info, ensure_ascii=False, indent=2))


def cli_stats(args) -> int:
    """CLI 命令：打印账号与会话概览。"""
    import json
    import sys
    from pathlib import Path

    sys.path.insert(0, str(Path(__file__).resolve().parent))
    root = Path.home()
    # 复用宿主 paths（源码运行时可用）
    try:
        from siwx import paths as _p
        out_root = _p.out_root()
    except Exception:
        out_root = Path.cwd() / "output"

    account = getattr(args, "account", "") or ""
    limit = int(getattr(args, "limit", 10) or 10)

    if not out_root.is_dir():
        print(f"未找到解密输出目录: {out_root}")
        return 1
    accs = [d.name for d in sorted(out_root.iterdir()) if (d / "message").is_dir()]
    if not accs:
        print("还没有解密产物。")
        return 1
    print(f"解密输出根目录: {out_root}")
    print(f"账号数: {len(accs)}")
    for a in accs[:limit]:
        print(f"  - {a}")
    if account:
        sdb = out_root / account / "session" / "session.db"
        print(f"\n账号 {account} 会话库: {'存在' if sdb.is_file() else '不存在'}")
    return 0
