"""加密备份 API —— .siwxpack 导出任务（独立任务槽 + 两段式杀进程确认）。

流程契约（与前端配套）：
  1. 前端 POST /start（不带 allowKill）；
  2. 若微信在运行 → 返回 {"needKill": true, "pids": [...]}，任务不启动；
  3. 前端弹确认框，用户允许后带 allowKill=true 重发 → 启动任务；
  4. 任务内部 stop_wechat 的 confirm_kill 只放行「用户确认过的那批 pid」——
     期间微信若重启出新进程，拒绝执行（防 TOCTOU 绕过确认）。

密码只经请求体进内存，绝不进日志/任务状态。
"""
import threading
from datetime import datetime
from pathlib import Path

from flask import Blueprint, jsonify, request

from siwx import logger as _log
from siwx import paths as _paths

bp = Blueprint("backup_api", __name__, url_prefix="/api/backup")

_lock = threading.Lock()
_job = {"running": False, "done": False, "ok": False, "progress": 0, "msg": "",
        "logs": [], "result": None, "error": None}

_TS_FORMATS = ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%d")


def _parse_ts(v):
    if not v:
        return None
    for fmt in _TS_FORMATS:
        try:
            return int(datetime.strptime(str(v), fmt).timestamp())
        except ValueError:
            continue
    raise ValueError(f"时间格式非法（应为 YYYY-MM-DD[ HH:MM[:SS]]）: {v}")


@bp.get("/wechat")
def wechat_state():
    """微信进程 + 各账号密钥覆盖预检（备份页提示行用）。"""
    from siwx import keystore
    from siwx.discover import find_wechat_data_dirs, find_wechat_pids
    from siwx.sqlcipher import collect_db_files
    pids = find_wechat_pids()
    store = keystore.load()
    accounts = []
    for wxid, db in find_wechat_data_dirs():
        try:
            entries = collect_db_files(db)
        except Exception:
            continue
        covered = sum(1 for e in entries if e.salt_hex in store)
        accounts.append({"wxid": wxid, "dbs": len(entries), "keys": covered})
    return jsonify({"running": bool(pids), "pids": pids, "accounts": accounts})


@bp.get("/list")
def list_packs():
    """exports/ 下的 .siwxpack 清单（新→旧，最多 50 个）。"""
    root = _paths.exports_root()
    packs = []
    if root.is_dir():
        for p in root.glob("*.siwxpack"):
            try:
                st = p.stat()
            except OSError:
                continue
            packs.append({"name": p.name, "path": str(p), "size": st.st_size,
                          "mtime": int(st.st_mtime)})
    packs.sort(key=lambda x: x["mtime"], reverse=True)
    return jsonify({"packs": packs[:50]})


@bp.post("/peek")
def peek():
    """免密码读包明文头（格式版本 + KDF 参数）。只允许 exports/ 内的文件。"""
    from siwx.pack import PackError, peek_pack
    data = request.get_json(silent=True) or {}
    raw = Path(data.get("path", ""))
    root = _paths.exports_root().resolve()
    target = (root / raw).resolve() if not raw.is_absolute() else raw.resolve()
    if not target.is_relative_to(root) or not target.is_file():
        return jsonify({"error": "文件不存在"}), 404
    try:
        return jsonify({"info": peek_pack(target)})
    except PackError as e:
        return jsonify({"error": str(e)}), 400


@bp.post("/start")
def start():
    from siwx import backup as _backup
    from siwx.discover import find_wechat_pids

    data = request.get_json(silent=True) or {}
    password = data.get("password")
    if not isinstance(password, str) or len(password) < 8:
        return jsonify({"error": "备份密码至少 8 位"}), 400
    mode = data.get("mode")
    if not mode:
        mode = ("selective" if (data.get("chats") or data.get("types")
                                or data.get("start") or data.get("end"))
                else "full")
    if mode not in ("full", "selective"):
        return jsonify({"error": f"未知模式: {mode}"}), 400
    chats = data.get("chats") or None
    if chats is not None and not (isinstance(chats, list)
                                  and all(isinstance(c, str) and c for c in chats)):
        return jsonify({"error": "chats 需为 username 字符串数组"}), 400
    types = None
    if data.get("types"):
        try:
            types = sorted({int(t) for t in data["types"]})
        except (TypeError, ValueError):
            return jsonify({"error": "types 需为整数数组"}), 400
    try:
        start_ts = _parse_ts(data.get("start"))
        end_ts = _parse_ts(data.get("end"))
    except ValueError as e:
        return jsonify({"error": str(e)}), 400

    # 两段式杀进程确认：未带 allowKill 且微信在跑 → 只报状态，不动手
    pids = [int(p) for p in find_wechat_pids()]
    if pids and not data.get("allowKill"):
        return jsonify({"needKill": True, "pids": pids})

    with _lock:
        if _job["running"]:
            return jsonify({"error": "已有备份任务在运行"}), 409
        _job.update({"running": True, "done": False, "ok": False, "progress": 0,
                     "msg": "", "logs": [], "result": None, "error": None})

    def confirm_kill(pids_now):
        # 用户确认的是快照里的那批 pid；期间新起的进程不算数，必须重新确认
        if set(int(p) for p in pids_now) <= set(pids):
            return True
        raise _backup.BackupAborted("微信进程列表已变化，请重新开始并确认")

    def jlog(msg):
        with _lock:
            _job["logs"].append(f"{datetime.now():%H:%M:%S} {msg}")
            if len(_job["logs"]) > 400:
                del _job["logs"][:150]

    def progress(pct, msg):
        with _lock:
            _job["progress"] = int(pct)
            _job["msg"] = str(msg)

    def runner():
        try:
            rep = _backup.run_backup(
                password, mode=mode, chats=chats, types=types,
                start=start_ts, end=end_ts,
                account=data.get("account") or None,
                confirm_kill=confirm_kill, log=jlog, progress=progress)
            with _lock:
                _job.update({"ok": True, "result": {
                    "path": rep["path"], "size": rep["size"],
                    "files": rep["files"], "mode": rep["mode"],
                    "stats": rep.get("stats") or {},
                    "wal": rep.get("wal") or [],
                }})
            jlog("备份完成")
        except Exception as e:  # noqa: BLE001 — 任务边界：完整类型名进状态
            with _lock:
                _job["ok"] = False
                _job["error"] = f"{type(e).__name__}: {e}"
            jlog(f"备份失败: {_job['error']}")
            _log.warn("backup", f"备份任务失败: {_job['error']}")
        finally:
            with _lock:
                _job["running"] = False
                _job["done"] = True

    threading.Thread(target=runner, daemon=True).start()
    return jsonify({"started": True, "mode": mode})


@bp.get("/status")
def status():
    """任务状态（前端轮询）：progress/msg/logs/result。"""
    with _lock:
        return jsonify(_job)
