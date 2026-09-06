"""导出 API —— 下载导出产物（导出任务复用全局 /api/job 单槽）。"""
import json
from pathlib import Path

from flask import Blueprint, jsonify, request, send_file

from siwx import paths as _paths

bp = Blueprint("export_api", __name__, url_prefix="/api/export")


@bp.get("/download")
def download():
    """下载导出文件/zip。安全限制：仅允许 cwd/exports 目录内。"""
    p = request.args.get("path", "")
    if not p:
        return jsonify({"error": "缺少 path"}), 400
    from siwx import paths as _paths
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
                zips = [z for z in root.glob(d.name + "*.zip")]
                out.append({"name": d.name, "files": files,
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
