"""聊天统计 API —— 为统计面板提供数据。

端点：
    GET  /api/stats/accounts            列出可统计的账号
    GET  /api/stats/overview            概览 + 图表数据（支持 start/end 月份过滤）
    GET  /api/stats/types               完整类型分布（未归并）
    POST /api/stats/refresh             清缓存并强制重算

`start` / `end` 接受 `YYYY-MM-DD` 或 `YYYY-MM`；后端会基于日级聚合缓存
过滤整页指标，不再只裁剪月度趋势。

设计说明：统计计算成本较高（需扫全部分片），因此 `/overview` 默认走
`stats.compute_stats()` 的签名缓存；只有显式 `refresh=1` 或 POST /refresh 才重算。
"""
import re

from flask import Blueprint, jsonify, request

from siwx import stats as _stats


bp = Blueprint("stats_api", __name__, url_prefix="/api/stats")

_DATE_RE = re.compile(r"^(\d{4})-(\d{2})(?:-(\d{2}))?")


def _date_arg(name: str):
    """把 `YYYY-MM` / `YYYY-MM-DD` 归一化；非法输入返回 None。"""
    v = (request.args.get(name) or "").strip()
    if not v:
        return None
    m = _DATE_RE.match(v)
    if not m:
        return None
    y, mo = int(m.group(1)), int(m.group(2))
    day = int(m.group(3) or 1)
    if not (1 <= mo <= 12) or not (1 <= day <= 31) or not (1970 <= y <= 2999):
        return None
    return f"{y:04d}-{mo:02d}-{day:02d}" if m.group(3) else f"{y:04d}-{mo:02d}"


@bp.get("/accounts")
def accounts():
    """列出有解密产物、可统计的账号。"""
    out = []
    root = _stats._out_root()
    if root.is_dir():
        for d in sorted(root.iterdir()):
            msg_dir = d / "message"
            if not msg_dir.is_dir():
                continue
            shards = len(list(msg_dir.glob("*.db")))
            if not shards:
                continue
            cached = (d / ".siwx_stats.json").is_file()
            out.append({"wxid": d.name, "shards": shards, "cached": cached})
    return jsonify({"accounts": out})


@bp.get("/overview")
def overview():
    """统计概览：总量、类型分布、月度趋势、活跃度、私聊发送者排行。"""
    account = request.args.get("account", "")
    if not account:
        return jsonify({"error": "缺少 account 参数"}), 400
    start = _date_arg("start")
    end = _date_arg("end")
    if start and end and start > end:
        start, end = end, start          # 容错：用户把起止填反了
    force = request.args.get("refresh") == "1"

    try:
        raw = _stats.compute_stats(account, force=force)
    except FileNotFoundError as e:
        return jsonify({"error": str(e)}), 404
    except Exception as e:                       # 统计失败不应拖垮面板
        return jsonify({"error": f"{type(e).__name__}: {e}"}), 500

    return jsonify(_stats.summarize(raw, start=start, end=end))


@bp.get("/types")
def types():
    """完整类型分布（未归并），供明细表使用。"""
    account = request.args.get("account", "")
    if not account:
        return jsonify({"error": "缺少 account 参数"}), 400
    try:
        raw = _stats.compute_stats(account)
    except FileNotFoundError as e:
        return jsonify({"error": str(e)}), 404
    su = _stats.summarize(raw)
    return jsonify({"total": su["total"], "types": su["type_raw"]})


@bp.post("/refresh")
def refresh():
    """强制清除统计缓存并重算。"""
    data = request.get_json(silent=True) or {}
    account = data.get("account") or ""
    try:
        _stats.clear_cache(account or None)
        if not account:
            return jsonify({"ok": True, "cleared": "all"})
        raw = _stats.compute_stats(account, force=True)
        return jsonify({"ok": True, "account": account, "total": raw.get("total", 0)})
    except FileNotFoundError as e:
        return jsonify({"error": str(e)}), 404
    except Exception as e:
        return jsonify({"error": f"{type(e).__name__}: {e}"}), 500
