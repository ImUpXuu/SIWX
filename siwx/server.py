"""Web 控制台：Flask 壳 —— 页面路由 + 任务槽；业务 API 在 api_*.py 模块化蓝图中。"""
import sys
import threading
import time
import webbrowser
from datetime import datetime
from pathlib import Path

from flask import Flask, jsonify, request, send_from_directory

from siwx import extract, keystore
from siwx import paths as _paths
from siwx.discover import find_wechat_data_dirs, find_wechat_pids, wxid_of
from siwx.sqlcipher import collect_db_files


def _ui_dir() -> Path:
    """PyInstaller 打包后资源在 _MEIPASS，源码运行时在 siwx/ui。"""
    if getattr(sys, "frozen", False):
        return Path(getattr(sys, "_MEIPASS", Path(sys.executable).parent)) / "ui"
    return Path(__file__).resolve().parent / "ui"


UI_DIR = _ui_dir()

app = Flask(__name__, static_folder=None)

# 模块化 API 蓝图（聊天查看 / 设置 / 导出 / MCP）
from siwx.api_chat import bp as chat_bp  # noqa: E402
from siwx.api_settings import bp as settings_bp  # noqa: E402
from siwx.api_export import bp as export_bp  # noqa: E402
from siwx.api_mcp import bp as mcp_bp  # noqa: E402
app.register_blueprint(chat_bp)
app.register_blueprint(settings_bp)
app.register_blueprint(export_bp)
app.register_blueprint(mcp_bp)

_job = {"running": False, "mode": None, "done": False, "ok": False,
        "logs": [], "report": None}
_lock = threading.Lock()
_LOG_RING: list = []          # 环形日志缓冲（供日志页展示）
_LOG_RING_MAX = 2000


def _log(msg: str) -> None:
    """写入任务日志 + 全局环形缓冲。"""
    with _lock:
        ts = int(time.time() * 1000)
        _job["logs"].append([ts, msg])
        _LOG_RING.append([ts, msg])
        if len(_LOG_RING) > _LOG_RING_MAX:
            del _LOG_RING[:len(_LOG_RING) - _LOG_RING_MAX]


def _now_ms() -> int:
    return int(time.time() * 1000)


def _log(msg: str) -> None:
    with _lock:
        ts = int(time.time() * 1000)
        _job["logs"].append([ts, msg])
        if len(_job["logs"]) > 1200:
            del _job["logs"][:400]
        _LOG_RING.append([ts, msg])
        if len(_LOG_RING) > _LOG_RING_MAX:
            del _LOG_RING[:len(_LOG_RING) - _LOG_RING_MAX]


def _run_job(mode: str, db_dir=None, out_dir=None, no_cache=False, workers=None,
             export_opts=None) -> None:
    """任务执行器。keys/decrypt 支持指定 db_dir（引导页单账号流程）。"""
    use_cache = not no_cache
    _log(f"[job] 模式={mode}, 指定目录={db_dir or '无'}, 缓存={use_cache}, 进程数={workers or '默认'}")
    try:
        dirs = ([(wxid_of(db_dir), db_dir)] if db_dir else find_wechat_data_dirs())
        if not dirs:
            raise RuntimeError("未找到微信数据目录 — 请确认本机登录过微信")
        _log(f"[job] 发现 {len(dirs)} 个账号")

        if mode == "keys":
            _log("[job] 步骤1/2: 收集数据库文件…")
            entries_by_dir = {db: collect_db_files(db) for _w, db in dirs}
            total_dbs = sum(len(v) for v in entries_by_dir.values())
            _log(f"[job] 共 {total_dbs} 个数据库")
            _log("[job] 步骤2/2: 提取密钥…")
            preset_full = extract._keystore_preset(entries_by_dir, _log) if use_cache else None
            if preset_full is not None:
                preset = preset_full
                _log("[job] 密钥缓存全覆盖，跳过内存扫描")
            else:
                _log("[job] 全局收割: 一次内存扫描联合验证…")
                gm, _ga = extract.global_harvest(dirs, entries_by_dir, _log)
                store = keystore.load()
                preset = {**{s: r["key"] for s, r in store.items()}, **gm}
                _log(f"[job] 收割完成, 预置 {len(preset)} 个密钥")
            accounts = []
            for wxid, db in dirs:
                _log(f"[job] 提取账号 {wxid}…")
                accounts.append(extract.extract_keys_for_dir(
                    db, _log, preset=preset,
                    entries=entries_by_dir.get(db), use_memory=False))
            report = {"kind": "keys", "accounts": accounts}
            total_ok = sum(a["verified"] for a in accounts)
            total_salts = sum(a["total_salts"] for a in accounts)
            _log(f"[job] 密钥提取完成: {total_ok}/{total_salts} 已验证")

        elif mode == "decrypt":
            out_root = out_dir or str(_paths.out_root())
            _log(f"[job] 解密输出目录: {out_root}")
            accounts = []
            for wxid, db in dirs:
                _log(f"[job] 解密账号 {wxid}…")
                accounts.append({"wxid": wxid,
                                 "decrypt": extract.decrypt_dir(
                                     db, str(Path(out_root) / wxid), _log,
                                     workers=workers, use_cache=use_cache)})
            report = {"kind": "decrypt", "accounts": accounts}
            total_ok = sum(a["decrypt"]["ok"] for a in accounts)
            total_cached = sum(a["decrypt"].get("cached", 0) for a in accounts)
            _log(f"[job] 解密完成: {total_ok} 成功, {total_cached} 缓存命中")

        elif mode == "auto":
            out_root = out_dir or str(_paths.out_root())
            _log(f"[job] 全自动: 输出目录={out_root}")
            accounts = []
            for wxid, db in dirs:
                _log(f"[job] 账号 {wxid}: 提取密钥…")
                rep = extract.extract_keys_for_dir(db, _log)
                _log(f"[job] 账号 {wxid}: 密钥 {rep['verified']}/{rep['total_salts']}")
                dec = None
                if rep["verified"] > 0:
                    _log(f"[job] 账号 {wxid}: 开始解密…")
                    dec = extract.decrypt_dir(db, str(Path(out_root) / wxid), _log,
                                              workers=workers, use_cache=use_cache)
                    _log(f"[job] 账号 {wxid}: 解密完成 {dec['ok']} 成功")
                accounts.append({**rep, "decrypt": dec})
            report = {"kind": "auto", "accounts": accounts}
            _log(f"[job] 全自动完成")

        elif mode == "sync":
            """增量同步：密钥缓存优先 → 收割缺失 → 只解密变更库。"""
            dirs = find_wechat_data_dirs()
            if not dirs:
                raise RuntimeError("未找到微信数据目录")
            for wxid, db in dirs:
                _log(f"[sync] 同步 {wxid}…")
                entries = collect_db_files(db)
                rep = extract.extract_keys_for_dir(db, _log,
                                                   entries=entries, use_memory=True)
                _log(f"[sync] {wxid}: 密钥 {rep['verified']}/{rep['total_salts']}")
                if rep["verified"] > 0:
                    dec = extract.decrypt_dir(
                        db, str(_paths.out_root() / wxid), _log,
                        entries=entries, use_cache=True,
                        workers=min(8, (os.cpu_count() or 4)))
                    _log(f"[sync] {wxid}: 解密 {dec['ok']} 成功 / "
                         f"缓存 {dec['cached']} / 新增 {dec['failed']}")
            report = {"kind": "sync", "message": "增量同步完成"}

        elif mode == "export":
            from siwx import exporter
            data = export_opts or {}
            acc_dir = _paths.out_root() / (data.get("account") or "")
            if not (acc_dir / "message").is_dir():
                raise RuntimeError("该账号还没有解密产物，请先完成引导")
            chats = data.get("chats") or []
            if not chats and data.get("chat"):
                chats = [{"chat": data.get("chat"), "display": data.get("display") or ""}]
            if not chats:
                raise RuntimeError("未选择要导出的会话")
            start = data.get("start")
            end = data.get("end")
            start_ts = int(datetime.strptime(start, "%Y-%m-%d").timestamp()) if start else None
            end_ts = int(datetime.strptime(end, "%Y-%m-%d").replace(
                hour=23, minute=59, second=59).timestamp()) if end else None
            _log(f"[export] 账号={data.get('account')} 会话数={len(chats)} 格式={data.get('format', 'json')}")
            _log(f"[export] 消息={data.get('messages', True)} 媒体={data.get('media', False)} "
                 f"头像={data.get('avatars', False)} 打包={data.get('pack', 'folder')}")
            if start_ts:
                _log(f"[export] 时间范围: {start} ~ {end or '现在'}")
            res = exporter.run_export_multi(
                acc_dir, data.get("account"), chats,
                fmt=data.get("format", "json"),
                start_ts=start_ts, end_ts=end_ts,
                want_messages=data.get("messages", True),
                want_media=data.get("media", False),
                want_avatars=data.get("avatars", False),
                export_root=_paths.exports_root(),
                pack=data.get("pack", "folder"),
                progress=lambda pct, msg: _log(f"[export] {pct}% {msg}"))
            _log(f"[export] 全部完成：{res.get('ok_count', 0)}/{len(chats)} 个会话，"
                 f"消息 {res.get('message_count', 0)}，媒体 {res.get('media_count', 0)}，"
                 f"耗时 {res.get('duration_ms', 0)}ms")
            report = {"kind": "export", **res}

        else:
            raise RuntimeError(f"未知模式: {mode}")

        with _lock:
            _job["ok"] = True
            _job["report"] = report
    except Exception as e:
        with _lock:
            _job["ok"] = False
            _job["logs"].append([_now_ms(), f"[错误] {e}"])
    finally:
        with _lock:
            _job["running"] = False
            _job["done"] = True


@app.get("/")
def index():
    return send_from_directory(UI_DIR, "index.html")


@app.get("/app.css")
def css():
    return send_from_directory(UI_DIR, "app.css", mimetype="text/css")


@app.get("/app.js")
def js():
    return send_from_directory(UI_DIR, "app.js", mimetype="text/javascript")


@app.get("/common.js")
def common_js():
    return send_from_directory(UI_DIR, "common.js", mimetype="text/javascript")


@app.get("/pages/<path:filename>")
def pages(filename: str):
    """模块化页面资源：pages/<name>.html / .js / .css"""
    return send_from_directory(UI_DIR / "pages", filename)


@app.get("/api/status")
def status():
    pids = find_wechat_pids()
    accounts = []
    store = keystore.load()
    from siwx.sqlcipher import parse_key, verify_enc_key
    for wxid, db in find_wechat_data_dirs():
        total = cached = 0
        try:
            for e in collect_db_files(db):
                total += 1
                rec = store.get(e.salt_hex)
                if rec:
                    try:
                        if verify_enc_key(parse_key(rec["key"]), e.page1):
                            cached += 1
                    except ValueError:
                        pass
        except Exception:
            pass
        accounts.append({"wxid": wxid, "db_dir": db, "db_count": total,
                         "keys_cached": cached, "total_salts": total})
    return jsonify({
        "wechat_running": bool(pids),
        "pids": pids,
        "accounts": accounts,
        "stored_salts": len(store),
    })


@app.post("/api/run")
def run():
    data = request.get_json(silent=True) or {}
    with _lock:
        if _job["running"]:
            return jsonify({"error": "已有任务在运行"}), 409
        _job.update({"running": True, "mode": data.get("mode", ""), "done": False,
                     "ok": False, "logs": [], "report": None})
    args = (data.get("mode", ""), data.get("db_dir"), data.get("out_dir"),
            bool(data.get("no_cache")), data.get("workers"),
            data.get("export_opts") or {})
    threading.Thread(target=_run_job, args=args, daemon=True).start()
    return jsonify({"started": True})


def _tail_mcp_log(limit: int = 500) -> list:
    """读取 MCP 日志文件末尾，转换为 [ts_ms, message] 格式。"""
    from siwx.mcp_server import _mcp_log_path
    p = _mcp_log_path()
    if not p.is_file():
        return []
    try:
        with open(p, "rb") as f:
            f.seek(0, 2)
            size = f.tell()
            block = min(size, 64 * 1024)
            f.seek(-block, 2)
            tail = f.read(block).decode("utf-8", errors="replace")
        lines = [ln for ln in tail.splitlines() if ln.strip()][-limit:]
        result = []
        for ln in lines:
            # 解析 "2026-09-06 20:00:00 [INFO] ..." → ts_ms
            try:
                dt = datetime.strptime(ln[:19], "%Y-%m-%d %H:%M:%S")
                ts_ms = int(dt.timestamp() * 1000)
            except (ValueError, IndexError):
                ts_ms = 0
            result.append([ts_ms, f"[MCP] {ln}"])
        return result
    except Exception:
        return []


@app.get("/api/logs")
def api_logs():
    """返回环形日志缓冲 + MCP 调用日志（合并按时间排序）。"""
    with _lock:
        web_logs = list(_LOG_RING)
    mcp_logs = _tail_mcp_log(500)
    merged = sorted(web_logs + mcp_logs, key=lambda x: x[0])
    return jsonify({"logs": merged})


@app.get("/api/job")
def api_job():
    """返回当前任务状态（供前端轮询）。"""
    with _lock:
        return jsonify({
            "running": _job["running"],
            "done": _job["done"],
            "ok": _job["ok"],
            "mode": _job["mode"],
            "logs": _job["logs"],
            "report": _job["report"],
        })


def run_server(host="127.0.0.1", port=8787, open_browser=True) -> None:
    """serve 模式：rich TUI 状态栏 + 日志流，Flask 完全静默。"""
    import logging
    import time as _time

    from siwx import media, tui, keystore

    tui.banner()
    tui.log(f"控制台 http://{host}:{port} · 按 Ctrl+C 停止")

    # 媒体解密事件 → TUI
    media.event = tui.log

    # 彻底关闭 Flask/werkzeug 所有日志
    logging.getLogger("werkzeug").handlers = []
    logging.getLogger("werkzeug").propagate = False
    logging.getLogger("werkzeug").disabled = True

    # URL 打开
    url = f"http://{host}:{port}"
    if open_browser:
        threading.Timer(0.8, lambda: webbrowser.open(url)).start()

    # 状态 getter（供 TUI 状态栏轮询）
    def _status_getter() -> dict:
        d = {}
        try:
            d["url"] = url
            pids = find_wechat_pids()
            d["wechat"] = f"运行中({len(pids)})" if pids else "未运行"
            accs = find_wechat_data_dirs()
            d["wxid"] = accs[0][0] if accs else "未检测"
            d["keys"] = str(len(keystore.load()))
            with _lock:
                if _job["running"]:
                    d["job"] = f"{_job['mode']}…"
                elif _job["done"]:
                    d["job"] = "✓完成" if _job["ok"] else "✗失败"
                else:
                    d["job"] = "空闲"
        except Exception:
            pass
        return d

    # Flask 后台线程（完全静默：启动横幅+运行时日志全部吞掉）
    import io
    import contextlib as _cl

    def _run_flask():
        with _cl.redirect_stdout(io.StringIO()), \
             _cl.redirect_stderr(io.StringIO()):
            app.run(host=host, port=port, threaded=True,
                    debug=False, use_reloader=False)

    server_thread = threading.Thread(target=_run_flask, daemon=True)
    server_thread.start()

    # 主线程：常驻状态栏（Ctrl+C 退出）
    tui.run_live_status(_status_getter, lambda: None)
