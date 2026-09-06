"""自动更新 API —— 版本检查 + 触发更新。"""
import json

from flask import Blueprint, jsonify, request

from siwx.auto_update import (
    current_version, has_update, is_frozen, run_update, system_platform,
)

bp = Blueprint("update_api", __name__, url_prefix="/api/update")


@bp.get("/check")
def check():
    """检查是否有新版本。"""
    has, remote, cur = has_update()
    return jsonify({
        "has_update": has,
        "current": cur,
        "remote": remote,
        "frozen": is_frozen(),
        "platform": system_platform(),
        "update_available": has and is_frozen(),
    })


@bp.post("/do")
def do_update():
    """执行更新。"""
    remote = request.get_json(silent=True) or {}
    if not remote:
        remote = has_update()[1]  # 重新拉取
    if not remote:
        return jsonify({"ok": False, "message": "无法获取远程版本信息"})
    result = run_update(remote)
    return jsonify(result)


@bp.get("/current")
def current():
    """当前版本信息。"""
    return jsonify({
        "version": current_version(),
        "frozen": is_frozen(),
        "platform": system_platform(),
    })
