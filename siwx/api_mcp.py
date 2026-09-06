"""MCP 配置 API —— 工具开关 + 客户端接入配置生成 + 调用日志。"""
import json
import sys

from flask import Blueprint, jsonify, request

from siwx import paths
from siwx.mcp_server import TOOLS, _mcp_log_path, config_path, load_config, save_config

bp = Blueprint("mcp_api", __name__, url_prefix="/api/mcp")


def _command() -> dict:
    """MCP 客户端拉起本服务器的命令。"""
    if getattr(sys, "frozen", False):
        return {"command": sys.executable, "args": ["mcp"]}
    return {"command": sys.executable,
            "args": [str(paths.app_root() / "run.py"), "mcp"]}


@bp.get("/info")
def info():
    cmd = _command()
    client_cfg = {"mcpServers": {"stories-in-wx": cmd}}
    cfg = load_config()
    tools = [{"name": t["name"], "description": t["description"],
              "enabled": bool(cfg.get("tools", {}).get(t["name"], True))}
             for t in TOOLS]
    return jsonify({
        "command": cmd,
        "client_config": json.dumps(client_cfg, ensure_ascii=False, indent=2),
        "config_path": str(config_path()),
        "tools": tools,
    })


@bp.post("/config")
def save():
    data = request.get_json(silent=True) or {}
    tools = data.get("tools") or {}
    cfg = load_config()
    cfg.setdefault("tools", {})
    for k, v in tools.items():
        cfg["tools"][k] = bool(v)
    save_config(cfg)
    return jsonify({"saved": True})


@bp.get("/logs")
def logs():
    """返回 MCP 调用日志（最近 N 行）。"""
    limit = min(int(request.args.get("limit", "200")), 2000)
    p = _mcp_log_path()
    if not p.is_file():
        return jsonify({"logs": [], "path": str(p)})
    try:
        # 读最后 limit 行（从文件末尾向前扫描换行）
        with open(p, "rb") as f:
            f.seek(0, 2)
            size = f.tell()
            block = min(size, 64 * 1024)
            f.seek(-block, 2)
            tail = f.read(block).decode("utf-8", errors="replace")
        lines = [ln for ln in tail.splitlines() if ln.strip()]
        lines = lines[-limit:]
        return jsonify({"logs": lines, "path": str(p), "total": len(lines)})
    except Exception as e:
        return jsonify({"logs": [f"读取日志失败: {e}"], "path": str(p)})
