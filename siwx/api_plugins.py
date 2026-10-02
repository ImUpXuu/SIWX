"""插件 API —— 加载状态、菜单页数据、设置项、主题。

所有端点都遵循"零插件时返回空集合"的原则，前端据此静默跳过，
保证不引入插件系统时行为完全一致。
"""
from pathlib import Path

from flask import Blueprint, jsonify, request, send_from_directory

from siwx import logger as log
from siwx.plugins import ensure_loaded, registry
from siwx.plugins import conditions as _conditions
from siwx.plugins import config as _config

bp = Blueprint("plugins_api", __name__, url_prefix="/api/plugins")


# ── 加载状态 ────────────────────────────────────────────────


@bp.get("")
def list_plugins():
    """全部插件的加载状态与贡献统计。"""
    ensure_loaded()
    rep = registry.report
    return jsonify({
        "plugins": rep.to_dicts() if rep else [],
        "counts": rep.counts() if rep else {"ok": 0, "degraded": 0, "error": 0},
        "hooks": registry.summary_counts(),
    })


# ── 菜单页 ──────────────────────────────────────────────────


@bp.get("/pages")
def pages():
    """左侧菜单的插件页数据。

    默认只返回显示条件已满足的页面；`?all=1` 返回全部并附 conditions_met
    （供设置页调试"为什么某页没出现"）。

    返回项供前端直接构造菜单与路由：
        id        —— 前端路由/菜单 key（全局唯一，插件名命名空间隔离）
        entry     —— 页面资源基名（默认 index → index.html/js/css）
        condition —— 已满足的显示条件原文（仅 ?all=1 时返回）
    """
    ensure_loaded()
    show_all = request.args.get("all") in ("1", "true", "yes")

    ctx = _conditions.ConditionContext.build()
    out = []

    for p in registry.pages.all_pages():
        met = _conditions.evaluate(p.condition, ctx)
        if not met and not show_all:
            continue
        owner = p.meta.name if p.meta else ""
        pid = f"{owner}:{p.name}" if owner else p.name
        item = {
            "id": pid,
            "name": p.name,
            "plugin": owner,
            "title": p.title,
            "icon": p.icon,
            "badge": getattr(p, "badge", "") or "",
            "tip": getattr(p, "tip", "") or "",
            "group": getattr(p, "group", "") or "",
            "entry": getattr(p, "entry", "index") or "index",
            "order": p.order,
            "base": f"/plugin-pages/{owner}" if owner else "",
            "conditions_met": met,
        }
        if show_all:
            item["conditions"] = p.condition or {}
            item["reason"] = _conditions.explain(p.condition, ctx)
        out.append(item)

    return jsonify({"pages": out})


# ── 设置项 ──────────────────────────────────────────────────


@bp.get("/settings")
def get_settings():
    """全部插件的设置项（含 schema 与当前值）。"""
    ensure_loaded()
    groups = {}
    for item in registry.plugin_settings():
        cur = _config.load(item.plugin, registry.schema_for(item.plugin))
        row = {
            "plugin": item.plugin, "group": item.group, "key": item.key,
            "type": item.type, "label": item.label, "default": item.default,
            "choices": list(item.choices or []), "min": item.min,
            "max": item.max, "help": item.help,
            "value": cur.get(item.key, item.default),
        }
        groups.setdefault(item.plugin, []).append(row)

    out = []
    for plugin, items in groups.items():
        meta = registry.metas.get(plugin)
        out.append({
            "plugin": plugin,
            "version": getattr(meta, "version", "") if meta else "",
            "description": getattr(meta, "description", "") if meta else "",
            "items": items,
        })
    return jsonify({"plugins": out})


@bp.post("/settings")
def set_settings():
    """更新某插件的设置（按 schema 校验后原子写）。"""
    ensure_loaded()
    body = request.get_json(silent=True) or {}
    plugin = str(body.get("plugin") or "")
    values = body.get("values") or {}
    if not plugin:
        return jsonify({"error": "缺少 plugin"}), 400
    if not registry.plugin_settings(plugin):
        return jsonify({"error": f"未知插件或该插件无设置项: {plugin}"}), 404

    schema = registry.schema_for(plugin)
    if not isinstance(values, dict):
        return jsonify({"error": "values 必须是对象"}), 400

    try:
        current = _config.apply_update(plugin, schema, values)
    except Exception as e:
        log.error("plugin", f"{plugin} 设置保存失败: {e}")
        return jsonify({"error": f"保存失败: {e}"}), 500
    return jsonify({"plugin": plugin, "values": current})


@bp.get("/config")
def get_config():
    """读取某插件的当前配置（供插件页 JS 便捷调用）。"""
    ensure_loaded()
    plugin = request.args.get("plugin") or ""
    if not plugin:
        return jsonify({"error": "缺少 plugin"}), 400
    schema = registry.schema_for(plugin)
    return jsonify({"plugin": plugin,
                    "values": _config.load(plugin, schema)})


# ── 主题 ────────────────────────────────────────────────────


@bp.get("/themes")
def themes():
    """插件声明的 CSS 主题（供前端注入 link）。"""
    ensure_loaded()
    out = []
    for _i, t in registry.themes.sorted_items():
        out.append({
            "name": t.name,
            "owner": t.meta.name if t.meta else "",
            "priority": t.priority,
            "url": f"/plugin-pages/{t.meta.name}/{t.key}" if (t.meta and t.key) else "",
            "inline": None if t.key else t.fn(),
        })
    return jsonify({"themes": out})


# ── 插件页静态资源 ──────────────────────────────────────────


@bp.get("/assets/<plugin>/<path:filename>")
def plugin_asset(plugin, filename):
    """插件页资源（备用入口；主入口在 server.py 的 /plugin-pages/）。"""
    ensure_loaded()
    page = _find_page_dir(plugin)
    if page is None:
        return jsonify({"error": "未知插件"}), 404
    return send_from_directory(page, filename)


def _find_page_dir(plugin: str):
    """校验插件名并返回其页面资源目录（防路径穿越）。"""
    if not plugin or not all(c.isalnum() or c in "_.-" for c in plugin):
        return None
    pages = [p for p in registry.pages.all_pages()
             if p.meta and p.meta.name == plugin]
    if not pages:
        return None
    root = pages[0].pages_dir
    if not root:
        return None
    p = Path(root)
    return p if p.is_dir() else None
