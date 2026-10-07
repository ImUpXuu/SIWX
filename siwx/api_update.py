"""自动更新 API —— 版本检查 + 触发更新。"""
import json

from flask import Blueprint, jsonify, request

from siwx.auto_update import (
    current_version, has_update, is_frozen, run_update, system_platform,
)

bp = Blueprint("update_api", __name__, url_prefix="/api/update")


@bp.get("/check")
def check():
    """检查是否有新版本；响应禁止缓存，避免继续显示旧判断。"""
    has, remote, cur = has_update()
    response = jsonify({
        "has_update": has,
        "current": cur,
        "remote": remote,
        "frozen": is_frozen(),
        "platform": system_platform(),
        "update_available": has and is_frozen(),
    })
    response.headers["Cache-Control"] = "no-store, no-cache, must-revalidate"
    response.headers["Pragma"] = "no-cache"
    response.headers["Expires"] = "0"
    return response


@bp.post("/do")
def do_update():
    """执行更新。

    安全（审计 S5，P0）：本接口不接受客户端提交的版本 manifest——旧实现把
    POST JSON 原样当作远程 version.json 传给 run_update，下载 URL、哈希 URL
    全由客户端提供，配合哈希校验 fail-open 即构成未鉴权 RCE 链。现在一律由
    服务端从 VERSION_URLS 白名单域名自行拉取，客户端提交的内容仅做忽略处理。
    """
    if request.get_json(silent=True):
        # 前端旧版会把 /check 拿到的 remote 原样回传；服务端一律忽略并记录
        from siwx import logger as _log
        _log.warn("update", "[update] 忽略客户端提交的更新 manifest（安全策略，"
                            "更新源仅从服务端白名单域名拉取）")
    remote = has_update()[1]  # 服务端自行拉取
    if not remote:
        return jsonify({"ok": False, "message": "无法获取远程版本信息（服务端拉取失败）"})
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
