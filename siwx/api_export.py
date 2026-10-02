"""导出 API —— 下载导出产物（导出任务复用全局 /api/job 单槽）。"""
import json
from pathlib import Path

from flask import Blueprint, jsonify, request, send_file

from siwx import paths as _paths

bp = Blueprint("export_api", __name__, url_prefix="/api/export")

# 内置导出格式（顺序即前端下拉顺序）
BUILTIN_FORMATS = [
    {"fmt": "json", "label": "JSON（结构化全量）"},
    {"fmt": "html", "label": "HTML（自包含网页）"},
    {"fmt": "txt", "label": "TXT（纯文本）"},
    {"fmt": "csv", "label": "CSV（表格）"},
    {"fmt": "xlsx", "label": "XLSX（Excel）"},
    {"fmt": "markdown", "label": "Markdown"},
    {"fmt": "toml", "label": "TOML"},
    {"fmt": "sqlite", "label": "SQLite 数据库"},
]


@bp.get("/formats")
def formats():
    """可用导出格式 = 内置 + 插件贡献（插件追加在内置之后）。

    返回 owner 便于前端标注来源；内置格式 owner 为空串。
    """
    out = [{**f, "owner": "", "ext": f["fmt"]} for f in BUILTIN_FORMATS]
    builtin = {f["fmt"] for f in BUILTIN_FORMATS}
    try:
        from siwx.plugins import ensure_loaded, registry
        ensure_loaded()
        for _i, w in registry.export_writers.sorted_items():
            if w.fmt in builtin:
                continue                       # 内置同名额优先
            out.append({
                "fmt": w.fmt, "label": w.label or w.fmt,
                "ext": w.ext or w.fmt,
                "owner": w.meta.name if w.meta else "",
            })
    except Exception:
        pass
    return jsonify({"formats": out})


@bp.get("/download")
def download():
    """下载导出文件/zip。安全限制：仅允许 cwd/exports 目录内。"""
    p = request.args.get("path", "")
    if not p:
        return jsonify({"error": "缺少 path"}), 400
    root = _paths.exports_root().resolve()
    target = Path(p).resolve()
    if not target.is_relative_to(root) or not target.is_file():
        return jsonify({"error": "文件不存在"}), 404
    return send_file(target, as_attachment=True)


@bp.post("/open")
def open_dir():
    """在文件资源管理器中打开导出目录/选中文件（仅限 exports 根内）。"""
    import os
    import subprocess
    data = request.get_json(silent=True) or {}
    p = Path(data.get("path", ""))
    root = _paths.exports_root().resolve()
    target = Path(p).resolve()
    if not target.is_relative_to(root) or not target.exists():
        return jsonify({"error": "路径无效"}), 404
    if target.is_file():
        subprocess.Popen(["explorer", "/select,", str(target)])
    else:
        os.startfile(str(target))
    return jsonify({"opened": True})


@bp.get("/list")
def list_exports():
    root = _paths.exports_root()
    out = []
    if root.is_dir():
        for d in sorted(root.iterdir(), reverse=True):
            if d.is_dir():
                files = [f.name for f in d.glob("*") if f.is_file()]
                subs = [s.name for s in d.glob("*") if s.is_dir()]
                zips = [z.name for z in root.glob(d.name + "*.zip")]
                out.append({"name": d.name, "files": files, "dirs": subs,
                            "zips": [z.name for z in zips]})
    return jsonify({"exports": out[:30]})


@bp.post("/render")
def render():
    """读取一个导出 JSON 的前 N 条消息（预览用）。"""
    data = request.get_json(silent=True) or {}
    p = Path(data.get("path", ""))
    root = _paths.exports_root().resolve()
    target = (root / p).resolve() if not Path(p).is_absolute() else Path(p)
    try:
        if not target.is_relative_to(root) or not target.is_file():
            return jsonify({"error": "文件不存在"}), 404
        if target.suffix == ".json":
            d = json.loads(target.read_text(encoding="utf-8"))
            return jsonify({"session": d.get("session"),
                            "preview": d.get("messages", [])[:20]})
        return jsonify({"error": "仅支持 JSON 预览"}), 400
    except Exception as e:
        return jsonify({"error": str(e)}), 400
