"""导出 API —— 下载导出产物（导出任务复用全局 /api/job 单槽）。"""
import json
import os
import subprocess
import sys
from pathlib import Path

from flask import Blueprint, jsonify, request, send_file

from siwx import logger as _log
from siwx import paths as _paths

bp = Blueprint("export_api", __name__, url_prefix="/api/export")

# /render 预览的整包读取上限（P3-11）：预览只需要前 20 条，但旧实现会把
# 整个 JSON 读进内存；超过该大小的文件直接让用户下载，不做预览。
MAX_PREVIEW_BYTES = 64 * 1024 * 1024

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
    except Exception as e:
        _log.warn("plugin", f"插件导出格式列举失败: {e}")
    return jsonify({"formats": out})


@bp.get("/templates")
def templates():
    """可用 HTML 导出模板（用户自定义优先于内置同名）。"""
    from siwx.html_template import list_templates
    return jsonify({"templates": list_templates()})


@bp.get("/download")
def download():
    """下载导出文件/zip。安全限制：仅允许 cwd/exports 目录内。"""
    p = request.args.get("path", "")
    if not p:
        return jsonify({"error": "缺少 path"}), 400
    root = _paths.exports_root().resolve()
    target = Path(p).resolve()
    if not target.is_relative_to(root) or not target.is_file():
        # 审计 §4.7：404 拒绝是安全边界行为（可能被扫描/误触频繁触发），
        # 用 detailed 防 warn 刷文件日志
        _log.detailed("export", f"下载拒绝 path={p}")
        return jsonify({"error": "文件不存在"}), 404
    return send_file(target, as_attachment=True)


def _open_in_file_manager(target: Path) -> None:
    """按平台打开导出目录/在文件管理器中选中文件（P3-10）。

    Windows：explorer /select 选中文件，目录走 os.startfile；
    macOS：文件用 `open -R`（在 Finder 中定位），目录用 `open`；
    其他平台（Linux）：xdg-open 打开目录本身/文件所在目录。

    只负责发起进程，不等待也不校验返回值——命令缺失/无图形会话时由平台
    自身报错，端点仍返回 opened（与旧行为一致）。"""
    if sys.platform == "darwin":
        cmd = (["open", "-R", str(target)] if target.is_file()
               else ["open", str(target)])
        subprocess.Popen(cmd)
    elif sys.platform.startswith("win"):
        if target.is_file():
            subprocess.Popen(["explorer", "/select,", str(target)])
        else:
            os.startfile(str(target))
    else:
        subprocess.Popen(["xdg-open",
                          str(target.parent if target.is_file() else target)])


@bp.post("/open")
def open_dir():
    """在文件资源管理器中打开导出目录/选中文件（仅限 exports 根内）。"""
    data = request.get_json(silent=True) or {}
    p = Path(data.get("path", ""))
    root = _paths.exports_root().resolve()
    target = Path(p).resolve()
    if not target.is_relative_to(root) or not target.exists():
        _log.detailed("export", f"打开拒绝 path={p}")
        return jsonify({"error": "路径无效"}), 404
    _open_in_file_manager(target)
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
            # P3-11：先看大小再决定是否整包读取——超限返回 413，前端提示下载
            try:
                size = target.stat().st_size
            except OSError as e:
                _log.detailed("export", f"预览 stat 失败 path={p}: {e}")
                size = 0
            if size > MAX_PREVIEW_BYTES:
                _log.detailed("export",
                              f"预览拒绝（文件 {size} 字节 > 上限 "
                              f"{MAX_PREVIEW_BYTES}） path={p}")
                return jsonify({
                    "error": f"文件过大（{size / 1048576:.0f} MB），"
                             f"超过预览上限，请直接下载查看"}), 413
            d = json.loads(target.read_text(encoding="utf-8"))
            return jsonify({"session": d.get("session"),
                            "preview": d.get("messages", [])[:20]})
        return jsonify({"error": "仅支持 JSON 预览"}), 400
    except Exception as e:
        _log.warn("export", f"预览失败 path={p}: {type(e).__name__}: {e}")
        return jsonify({"error": str(e)}), 400
