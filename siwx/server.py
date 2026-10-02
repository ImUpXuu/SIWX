"""Web 控制台：Flask 壳 —— 页面路由 + 任务槽；业务 API 在 api_*.py 模块化蓝图中。"""
import logging
import os
import sys
import threading
import time
import webbrowser
from datetime import datetime
from logging.handlers import RotatingFileHandler
from pathlib import Path

from flask import Flask, Response, abort, jsonify, request, send_from_directory
from werkzeug.exceptions import HTTPException

from siwx import extract, keystore, logger as log
from siwx import paths as _paths
from siwx.discover import (add_manual_data_dir, find_account_conflicts,
                           find_wechat_data_dirs, find_wechat_pids,
                           load_manual_data_dirs, wxid_of)
from siwx.sqlcipher import collect_db_files


# ── 文件日志（详细）──────────────────────────────────────────────
def _setup_file_logger():
    log_dir = _paths.app_root() / "logs"
    log_dir.mkdir(exist_ok=True)
    logger = logging.getLogger("siwx")
    logger.setLevel(logging.DEBUG)
    # 避免重复添加
    if logger.handlers:
        return logger
    fh = RotatingFileHandler(log_dir / "siwx.log", maxBytes=10 * 1024 * 1024,
                              backupCount=5, encoding="utf-8")
    fh.setLevel(logging.DEBUG)
    fh.setFormatter(logging.Formatter(
        "%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S"))
    logger.addHandler(fh)
    # 控制台也输出 INFO+
    ch = logging.StreamHandler()
    ch.setLevel(logging.INFO)
    ch.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(message)s"))
    logger.addHandler(ch)
    return logger

_siwx_logger = _setup_file_logger()
_CRASH_FH = None


def _flush_logs() -> None:
    """尽量把日志落盘；logging 的 handler 通常会自动 flush，这里用于异常路径兜底。"""
    for h in _siwx_logger.handlers:
        try:
            h.flush()
        except Exception:
            pass


def _install_crash_hooks() -> None:
    """记录非 Flask/任务线程里的未捕获异常和 Python fatal traceback。"""
    global _CRASH_FH
    log_dir = _paths.app_root() / "logs"
    log_dir.mkdir(exist_ok=True)

    def _sys_excepthook(exc_type, exc, tb):
        _siwx_logger.critical("未捕获主线程异常", exc_info=(exc_type, exc, tb))
        _flush_logs()
        sys.__excepthook__(exc_type, exc, tb)

    sys.excepthook = _sys_excepthook

    if hasattr(threading, "excepthook"):
        def _thread_excepthook(args):
            _siwx_logger.critical("未捕获线程异常: %s", args.thread.name,
                                  exc_info=(args.exc_type, args.exc_value, args.exc_traceback))
            _flush_logs()
        threading.excepthook = _thread_excepthook

    try:
        import faulthandler
        _CRASH_FH = open(log_dir / "crash.log", "a", encoding="utf-8")
        faulthandler.enable(_CRASH_FH, all_threads=True)
    except Exception:
        _CRASH_FH = None


_install_crash_hooks()


def _ui_dir() -> Path:
    """PyInstaller 打包后资源在 _MEIPASS，源码运行时在 siwx/ui。"""
    if getattr(sys, "frozen", False):
        return Path(getattr(sys, "_MEIPASS", Path(sys.executable).parent)) / "ui"
    return Path(__file__).resolve().parent / "ui"


UI_DIR = _ui_dir()

app = Flask(__name__, static_folder=None)

# 模块化 API 蓝图（聊天查看 / 设置 / 导出 / MCP）
from siwx.api_chat import bp as chat_bp  # noqa: E402
from siwx.api_settings import bp as settings_bp, load_auto_sync, mark_auto_sync_result  # noqa: E402
from siwx.api_export import bp as export_bp  # noqa: E402
from siwx.api_mcp import bp as mcp_bp  # noqa: E402
from siwx.api_update import bp as update_bp  # noqa: E402
from siwx.api_plugins import bp as plugins_bp  # noqa: E402
from siwx.api_stats import bp as stats_bp  # noqa: E402
from siwx.api_sns import bp as sns_bp  # noqa: E402
app.register_blueprint(chat_bp)
app.register_blueprint(settings_bp)
app.register_blueprint(export_bp)
app.register_blueprint(mcp_bp)
app.register_blueprint(update_bp)
app.register_blueprint(plugins_bp)
app.register_blueprint(stats_bp)
app.register_blueprint(sns_bp)

# 插件发现与加载（目录自动发现，逐插件隔离；失败不阻塞启动）
from siwx.plugins import load_all as _load_plugins  # noqa: E402
PLUGIN_REPORT = _load_plugins()


def _register_plugin_blueprints(flask_app) -> None:
    """把插件声明的 api_blueprints 挂到 Flask 应用上。

    契约：`"api_blueprints": [make_bp]` 或 `[{"bp": make_bp()}]`，
    即直接给出（或调用后得到）flask.Blueprint 对象；url_prefix 由蓝图自带。
    逐个 try/except，插件失败不影响宿主启动。
    """
    from siwx.plugins import registry
    if not registry.api_blueprints:
        return
    existing = set(flask_app.blueprints.keys())
    for _i, entry in registry.api_blueprints.sorted_items():
        plugin = entry.meta.name if entry.meta else "?"
        bp_obj = entry.bp
        try:
            # 允许工厂形式：调用后返回 Blueprint
            if callable(bp_obj) and not hasattr(bp_obj, "register"):
                bp_obj = bp_obj()
            name = getattr(bp_obj, "name", None)
            if not name:
                log.warn("plugin", f"{plugin} 提供的对象不是 Blueprint，跳过")
                continue
            if name in existing:
                log.warn("plugin", f"{plugin} 的蓝图 {name} 与已注册重名，跳过")
                continue
            flask_app.register_blueprint(bp_obj)
            existing.add(name)
            log.info("plugin", f"已挂载插件蓝图 {name}")
        except Exception as e:
            log.error("plugin", f"{plugin} 蓝图挂载失败: {e}")


# 插件自带蓝图 / 路由（内置优先，插件失败逐个隔离）
_register_plugin_blueprints(app)


# ── 全局状态（必须在路由和错误处理之前定义）──────────────────────
_lock = threading.Lock()
_job = {"running": False, "mode": None, "done": False, "ok": False, "pct": 0,
        "log_trimmed": 0, "logs": [], "report": None}
_LOG_RING: list = []          # 环形日志缓冲（供日志页展示）
_LOG_RING_MAX = 2000
_CANCEL = threading.Event()   # S-3：协作式取消标志（仅 export/sync 响应）


# ── 全局错误处理：确保所有异常都有日志 + JSON 响应 ──────────────
@app.errorhandler(Exception)
def _handle_exception(e):
    """未捕获异常 → 记录日志 + 返回 JSON（避免白屏 500）。

    404/405 等 HTTPException 是正常路由结果，不应作为“未捕获异常”写进日志页；
    否则浏览器探测 favicon、旧缓存资源或误输地址都会刷屏。
    """
    if isinstance(e, HTTPException):
        code = e.code or 500
        if code >= 500:
            _siwx_logger.error("HTTP %s: %s", code, e)
            _flush_logs()
        return jsonify({"error": e.description, "code": code}), code

    import traceback
    tb = traceback.format_exc()
    _siwx_logger.error(f"未捕获异常: {e}\n{tb}")
    _flush_logs()
    with _lock:
        ts = int(time.time() * 1000)
        msg = f"[错误] {type(e).__name__}: {e}"
        _job["logs"].append([ts, msg])
        _LOG_RING.append([ts, msg])
        if len(_LOG_RING) > _LOG_RING_MAX:
            del _LOG_RING[:len(_LOG_RING) - _LOG_RING_MAX]
    # S-5：完整 traceback 只进日志文件，不回传页面——本地单用户威胁模型下
    # 不算漏洞，但配合 DNS rebinding（既有审计 M-1）会放大信息泄露
    return jsonify({"error": f"{type(e).__name__}: {e}"}), 500


def _log(msg: str) -> None:
    """写入任务日志 + 全局环形缓冲 + 文件日志。"""
    _siwx_logger.info(msg)
    _flush_logs()
    with _lock:
        ts = int(time.time() * 1000)
        _job["logs"].append([ts, msg])
        if len(_job["logs"]) > 1200:
            del _job["logs"][:400]
            # E-11：记录累计裁剪条数，增量拉取的 offset 按绝对条数计算
            _job["log_trimmed"] = _job.get("log_trimmed", 0) + 400
        _LOG_RING.append([ts, msg])
        if len(_LOG_RING) > _LOG_RING_MAX:
            del _LOG_RING[:len(_LOG_RING) - _LOG_RING_MAX]


def _now_ms() -> int:
    return int(time.time() * 1000)


def _job_progress(pct, msg: str) -> None:
    """导出进度回调：结构化记录 pct（E-11），前端进度条不再解析日志正则。"""
    _log(f"[export] {pct}% {msg}")
    with _lock:
        _job["pct"] = int(pct or 0)


def _run_job(mode: str, db_dir=None, out_dir=None, no_cache=False, workers=None,
             export_opts=None) -> bool:
    """任务执行器。keys/decrypt 支持指定 db_dir（引导页单账号流程）。

    返回任务是否成功完成（S-4：调用方据此记录结果，不再回头读 _job["ok"]——
    那存在 TOCTOU：_run_job 释放任务槽与调用方获锁之间，/api/run 可能已把
    _job 重置为新任务，读到的是别人的结果）。
    """
    use_cache = not no_cache
    _log(f"[job] 模式={mode}, 指定目录={db_dir or '无'}, 缓存={use_cache}, 进程数={workers or '默认'}")
    _emit_task_event("start", mode=mode, db_dir=db_dir, out_dir=out_dir,
                     no_cache=no_cache, workers=workers)
    _t0 = time.time()
    try:
        # 朋友圈导出只依赖已解密产物，无需扫描微信数据目录（省一次全盘发现）
        if mode == "sns_export":
            from siwx import sns_export
            data = export_opts or {}
            account = data.get("account") or ""
            acc_dir = _paths.out_root() / account
            db = acc_dir / "sns" / "sns.db"
            if not db.is_file():
                raise RuntimeError("该账号还没有朋友圈数据库，请先完成引导")
            fmt = data.get("format", "json")
            if fmt not in sns_export.FORMATS:
                raise RuntimeError(f"不支持的导出格式: {fmt}")
            users = data.get("usernames") or None
            if not users and data.get("username"):
                users = [data["username"]]
            _log(f"[sns] 账号={account} 格式={fmt} 媒体={bool(data.get('media'))} "
                 f"图片={data.get('images', True)} 视频={data.get('videos', True)} "
                 f"实况={data.get('livephotos', True)} "
                 f"并发={data.get('concurrency') or sns_export.DEFAULT_CONCURRENCY}")
            if users:
                _log(f"[sns] 发布者筛选: {', '.join(map(str, users))}")
            if data.get("keyword"):
                _log(f"[sns] 关键词: {data['keyword']}")
            res = sns_export.run_sns_export(
                db, account, fmt=fmt,
                usernames=users,
                start=data.get("start"), end=data.get("end"),
                want_media=bool(data.get("media")),
                want_images=bool(data.get("images", True)),
                want_videos=bool(data.get("videos", True)),
                want_livephotos=bool(data.get("livephotos", True)),
                concurrency=data.get("concurrency") or sns_export.DEFAULT_CONCURRENCY,
                cache_dir=acc_dir / "sns_media",
                limit=data.get("limit"),
                keyword=data.get("keyword"),
                progress=lambda done, total, msg: _log(f"[sns] {done}/{total} {msg}"),
            )
            if not res.get("ok"):
                raise RuntimeError(res.get("error") or "朋友圈导出失败")
            m = res.get("media") or {}
            _log(f"[sns] 导出完成：{res['count']} 条动态，"
                 f"媒体 {m.get('ok', 0)}/{m.get('total', 0)}（失败 {m.get('fail', 0)}），"
                 f"耗时 {res.get('duration_ms', 0)}ms")
            # 失败原因必须进任务日志：否则用户只看到「失败 N」，无从判断是
            # CDN 已无此图（http-404）还是密钥/格式问题（undecodable）。
            if m.get("reasons"):
                _log(f"[sns] 媒体失败原因分布：{m['reasons']}")
            report = {"kind": "sns_export", **res}
            with _lock:
                _job["ok"] = True
                _job["report"] = report
            _emit_task_event("done", mode=mode, ok=True, report=report,
                             duration_ms=int((time.time() - _t0) * 1000))
            return True

        dirs = ([(wxid_of(db_dir), db_dir)] if db_dir else find_wechat_data_dirs())
        if not dirs:
            raise RuntimeError("未找到微信数据目录 — 请确认本机登录过微信")
        _log(f"[job] 发现 {len(dirs)} 个账号")

        # 同名账号告警：多个 db_dir 映射到同一 output/<wxid>/ 时，后跑的会覆盖
        # 先跑的产物。这里只提示，不阻断（用户可能确实想重新解密某一副本）。
        for c in find_account_conflicts(dirs):
            _log(f"[job] ⚠ 账号 {c['wxid']} 发现 {len(c['dirs'])} 个副本目录，"
                 f"它们共用同一输出目录，解密会互相覆盖：")
            for i, d in enumerate(c["dirs"], 1):
                _log(f"[job]      {i}. {d}")

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
            """增量同步：密钥缓存优先 → 收割缺失 → 只解密变更库。

            S-2：直接用函数入口已发现的 dirs（原先无条件重新
            find_wechat_data_dirs()，每次同步多一次全盘扫描；且若将来带
            db_dir 调 sync，会静默丢弃单账号语义改扫全部账号）。
            S-3：支持协作式取消，在账号边界检查取消标志。"""
            for wxid, db in dirs:
                if _CANCEL.is_set():
                    _log("[sync] 收到取消请求，停止剩余账号")
                    break
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
            # E-2：「导出所选」精确导出——按 localId 列表而非日期范围
            local_ids = data.get("local_ids") or None
            if local_ids:
                try:
                    local_ids = {int(x) for x in local_ids}
                except (TypeError, ValueError):
                    raise RuntimeError("local_ids 参数应为 localId 数组")
                if len(local_ids) > 100000:
                    raise RuntimeError("local_ids 数量超出上限")
                _log(f"[export] 精确导出所选 {len(local_ids)} 条消息（忽略日期范围）")
            _log(f"[export] 账号={data.get('account')} 会话数={len(chats)} 格式={data.get('format', 'json')}")
            _log(f"[export] 消息={data.get('messages', True)} 图片={data.get('media', False)} "
                 f"语音={data.get('voice', False)} 头像={data.get('avatars', False)} "
                 f"打包={data.get('pack', 'folder')}")
            if start_ts:
                _log(f"[export] 时间范围: {start} ~ {end or '现在'}")
            res = exporter.run_export_multi(
                acc_dir, data.get("account"), chats,
                fmt=data.get("format", "json"),
                start_ts=start_ts, end_ts=end_ts,
                want_messages=data.get("messages", True),
                want_media=data.get("media", False),
                want_voice=data.get("voice", False),
                want_avatars=data.get("avatars", False),
                export_root=_paths.exports_root(),
                pack=data.get("pack", "folder"),
                only_local_ids=local_ids,
                should_cancel=_CANCEL.is_set,
                progress=_job_progress)
            _log(f"[export] 全部完成：{res.get('ok_count', 0)}/{len(chats)} 个会话，"
                 f"消息 {res.get('message_count', 0)}，媒体 {res.get('media_count', 0)}，"
                 f"耗时 {res.get('duration_ms', 0)}ms")
            if res.get("cancelled"):
                _log("[export] 任务已被用户取消，结果仅含已完成部分")
            report = {"kind": "export", **res}

        else:
            raise RuntimeError(f"未知模式: {mode}")

        with _lock:
            _job["ok"] = True
            _job["report"] = report
        _emit_task_event("done", mode=mode, ok=True, report=report,
                         duration_ms=int((time.time() - _t0) * 1000))
        return True
    except Exception as e:
        _siwx_logger.exception("任务执行失败: %s", e)
        _flush_logs()
        with _lock:
            ts = _now_ms()
            msg = f"[错误] {e}"
            _job["ok"] = False
            _job["logs"].append([ts, msg])
            _LOG_RING.append([ts, msg])
            if len(_LOG_RING) > _LOG_RING_MAX:
                del _LOG_RING[:len(_LOG_RING) - _LOG_RING_MAX]
        _emit_task_event("done", mode=mode, ok=False, error=str(e),
                         duration_ms=int((time.time() - _t0) * 1000))
        return False
    finally:
        with _lock:
            _job["running"] = False
            _job["done"] = True
        # 任务可能新增/更新了密钥缓存与解密产物 → 状态快照立即失效
        _invalidate_status_cache()
        _CANCEL.clear()   # S-3：任务结束（含取消）后复位取消标志


def _emit_task_event(event: str, **ctx) -> None:
    """广播任务生命周期事件给插件监听器（start / done）。

    契约：`listener(event: str, ctx: dict) -> None`。
    逐个隔离：插件异常只写日志，绝不影响任务本身。
    """
    try:
        from siwx.plugins import registry
    except Exception:
        return
    if not registry.task_listeners:
        return
    for _i, h in registry.task_listeners.sorted_items():
        plugin = h.meta.name if h.meta else (h.name or "?")
        try:
            h.fn(event, dict(ctx))
        except Exception as e:
            log.warn("plugin", f"{plugin}.task_listener({event}) 失败: {e}")


@app.get("/")
def index():
    """首页壳：把插件声明的主题 CSS 注入 head（无插件时原样返回）。"""
    page = (UI_DIR / "index.html").read_text(encoding="utf-8")
    links = _plugin_theme_links()
    if links:
        page = page.replace("</head>", f"{links}\n</head>", 1)
    return Response(page, mimetype="text/html")


def _plugin_theme_links() -> str:
    """插件主题 → <link> 标签串（按 priority 排序，内置主题之后加载）。"""
    try:
        from siwx.plugins import registry
    except Exception:
        return ""
    if not registry.themes:
        return ""
    parts = []
    for _i, t in registry.themes.sorted_items():
        plugin = t.meta.name if t.meta else ""
        if not plugin or not t.key:
            continue
        # 只允许页面资源目录内的相对 css 名（防路径穿越）
        if "/" in t.key or "\\" in t.key or ".." in t.key:
            continue
        parts.append(f'<link rel="stylesheet" '
                     f'href="/plugin-pages/{plugin}/{t.key}">')
    return "\n".join(parts)


def _no_cache(resp):
    """静态资源协商缓存：保留 ETag/Last-Modified，但禁止启发式缓存。

    F-2：前端模块 URL 已固定为 ?v=UI_VERSION（不再是每次导航的时间戳），
    软件更新覆盖文件后浏览器必须回源校验，否则"更新后不生效"。
    """
    resp.headers["Cache-Control"] = "no-cache"
    return resp


@app.get("/app.css")
def css():
    return _no_cache(send_from_directory(UI_DIR, "app.css", mimetype="text/css"))


@app.get("/app.js")
def js():
    return _no_cache(send_from_directory(UI_DIR, "app.js",
                                         mimetype="text/javascript"))


@app.get("/common.js")
def common_js():
    return _no_cache(send_from_directory(UI_DIR, "common.js",
                                         mimetype="text/javascript"))


@app.get("/widgets.js")
def widgets_js():
    """自绘控件（下拉/日期/菜单）：被 pages/*.js 以 ESM 静态 import 引用。"""
    return _no_cache(send_from_directory(UI_DIR, "widgets.js",
                                         mimetype="text/javascript"))


@app.get("/pages/<path:filename>")
def pages(filename: str):
    """模块化页面资源：pages/<name>.html / .js / .css"""
    return _no_cache(send_from_directory(UI_DIR / "pages", filename))


@app.get("/vendor/<path:filename>")
def vendor(filename: str):
    """前端第三方库（如 vue.global.prod.js）：只读 + 路径逃逸防护。"""
    root = UI_DIR / "vendor"
    try:
        (root / filename).resolve().relative_to(root.resolve())
    except (ValueError, OSError):
        abort(404)
    if not (root / filename).is_file():
        abort(404)
    return _no_cache(send_from_directory(root, filename))


@app.get("/plugin-pages/<plugin>/<path:filename>")
def plugin_pages(plugin: str, filename: str):
    """插件页面资源：plugins/<plugin>/ui/<filename>（只读，路径逃逸防护）。"""
    from siwx.plugins.loader import plugin_ui_dir
    root = plugin_ui_dir(plugin)
    if root is None:
        abort(404)
    try:
        target = (root / filename).resolve()
        target.relative_to(root.resolve())
    except (ValueError, OSError):
        abort(404)
    if not target.is_file():
        abort(404)
    return _no_cache(send_from_directory(root, filename))


@app.get("/api/status")
def status():
    return jsonify(_status_snapshot())


# ── 微信状态快照缓存（S-1）───────────────────────────────────────
# /api/status（前端 5s 轮询）、TUI 状态栏（1s 轮询）、auto-sync 调度（30s）
# 原先各自独立全盘扫描（find_wechat_pids + collect_db_files + HMAC 验证），
# 常驻磁盘 IO。收敛为带 TTL 的共享快照，三处共用。
_STATUS_TTL = 12.0
_STATUS_LOCK = threading.Lock()
_STATUS_CACHE: dict = {"ts": 0.0, "data": None}


def _compute_status_snapshot() -> dict:
    pids = find_wechat_pids()
    accounts = []
    manual_set = {str(Path(db).resolve()).casefold()
                  for _wxid, db in load_manual_data_dirs()}
    store = keystore.load()
    from siwx.sqlcipher import parse_key, verify_enc_key
    all_dirs = find_wechat_data_dirs()
    for wxid, db in all_dirs:
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
        try:
            is_manual = str(Path(db).resolve()).casefold() in manual_set
        except OSError:
            is_manual = str(db).casefold() in manual_set
        accounts.append({"wxid": wxid, "db_dir": db, "db_count": total,
                         "keys_cached": cached, "total_salts": total,
                         "manual": is_manual})
    return {
        "wechat_running": bool(pids),
        "pids": pids,
        "accounts": accounts,
        "stored_salts": len(store),
        # 同名账号冲突：多个 db_dir 共用 output/<wxid>/，解密会互相覆盖
        "conflicts": find_account_conflicts(all_dirs),
    }


def _status_snapshot() -> dict:
    now = time.monotonic()
    with _STATUS_LOCK:
        cached = _STATUS_CACHE["data"]
        if cached is not None and now - _STATUS_CACHE["ts"] < _STATUS_TTL:
            return cached
    # 慢扫描放锁外：并发时最坏重复算一次，不阻塞 /api/run、/api/job
    data = _compute_status_snapshot()
    with _STATUS_LOCK:
        _STATUS_CACHE["ts"] = time.monotonic()
        _STATUS_CACHE["data"] = data
    return data


def _invalidate_status_cache() -> None:
    """解密类任务结束 / 手动新增数据目录后立即失效，避免最长滞后一个 TTL。"""
    with _STATUS_LOCK:
        _STATUS_CACHE["ts"] = 0.0
        _STATUS_CACHE["data"] = None


@app.post("/api/run")
def run():
    data = request.get_json(silent=True) or {}
    with _lock:
        if _job["running"]:
            return jsonify({"error": "已有任务在运行"}), 409
        _CANCEL.clear()   # S-3：新任务启动前复位取消标志
        _job.update({"running": True, "mode": data.get("mode", ""), "done": False,
                     "ok": False, "pct": 0, "log_trimmed": 0,
                     "logs": [], "report": None})
    args = (data.get("mode", ""), data.get("db_dir"), data.get("out_dir"),
            bool(data.get("no_cache")), data.get("workers"),
            data.get("export_opts") or {})
    threading.Thread(target=_run_job, args=args, daemon=True).start()
    return jsonify({"started": True})


def _tail_app_log(limit: int = 800) -> list:
    """读取 siwx.log 文件末尾，转换为 [ts_ms, message] 格式。

    日志页此前只读进程内 _LOG_RING；但大量日志（如 api_chat 的列表/消息查询）
    是直接写入 logging.getLogger("siwx") 的文件日志，不会进入 _LOG_RING，导致
    用户打开“运行日志”时经常看到空白。这里把文件日志也纳入 /api/logs。
    """
    paths = []
    for h in logging.getLogger("siwx").handlers:
        p = getattr(h, "baseFilename", None)
        if p:
            paths.append(Path(p))
    if not paths:
        paths.append(_paths.app_root() / "logs" / "siwx.log")
    p = paths[0]
    if not p.is_file():
        return []
    try:
        with open(p, "rb") as f:
            f.seek(0, 2)
            size = f.tell()
            block = min(size, 256 * 1024)
            f.seek(-block, 2)
            tail = f.read(block).decode("utf-8", errors="replace")
        lines = [ln for ln in tail.splitlines() if ln.strip()][-limit:]
        out = []
        for ln in lines:
            # 历史版本曾把浏览器探测/旧资源 404 记录成 ERROR；日志页不展示这类噪音。
            if "404 Not Found" in ln:
                continue
            ts_ms = 0
            for fmt, n in (("%Y-%m-%d %H:%M:%S", 19),
                           ("%Y-%m-%d %H:%M:%S,%f", 23)):
                try:
                    ts_ms = int(datetime.strptime(ln[:n], fmt).timestamp() * 1000)
                    break
                except (ValueError, IndexError):
                    pass
            out.append([ts_ms, ln])
        return out
    except Exception:
        return []


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


def _log_item_text(item) -> str:
    return str(item[3] if len(item) >= 4 else item[1])


@app.get("/api/logs")
def api_logs():
    """返回文件日志 + 环形任务日志 + MCP 调用日志（合并按时间排序）。"""
    limit = min(int(request.args.get("limit", "2000") or 2000), 5000)
    with _lock:
        ring_logs = list(_LOG_RING)
    app_logs = _tail_app_log(800)
    mcp_logs = _tail_mcp_log(500)
    structured_logs = log.get_logs(limit=limit)

    # 去重：同一条任务日志会同时进入 _LOG_RING 和 siwx.log。
    seen = set()
    merged = []
    for item in sorted(app_logs + ring_logs + mcp_logs + structured_logs, key=lambda x: x[0]):
        text = _log_item_text(item)
        if "404 Not Found" in text:
            continue
        key = (item[0], text)
        if key in seen:
            continue
        seen.add(key)
        merged.append(item)
    return jsonify({"logs": merged[-limit:], "level": log.get_level().value})


@app.get("/api/logs/settings")
def api_log_settings():
    """获取日志设置。"""
    return jsonify({"level": log.get_level().value})


@app.post("/api/logs/settings")
def api_log_settings_save():
    """设置日志模式。"""
    data = request.get_json(silent=True) or {}
    level = data.get("level", "rough")
    log.set_level(log.LogLevel.DETAILED if level == "detailed" else log.LogLevel.ROUGH)
    return jsonify({"level": log.get_level().value})


@app.get("/api/logs/export")
def api_log_export():
    """导出脱敏日志。"""
    start_ts = request.args.get("start")
    end_ts = request.args.get("end")
    desensitize = request.args.get("desensitize", "1") == "1"
    start = int(start_ts) if start_ts else None
    end = int(end_ts) if end_ts else None
    text = log.export_logs(start_ts=start, end_ts=end, desensitize=desensitize)
    return Response(text, mimetype="text/plain",
                    headers={"Content-Disposition": "attachment; filename=siwx_log.txt"})


@app.get("/api/job")
def api_job():
    """返回当前任务状态（供前端轮询）。

    E-11：带 offset 参数时只返回其后的增量日志（offset 为绝对条数，
    含已被环形缓冲裁剪的早期条数），供导出页追加渲染；不带 offset 时
    返回全量日志（向后兼容旧调用方）。
    """
    try:
        offset = int(request.args.get("offset", "-1") or -1)
    except ValueError:
        offset = -1
    with _lock:
        logs = _job["logs"]
        trimmed = int(_job.get("log_trimmed", 0))
        payload = {
            "running": _job["running"],
            "done": _job["done"],
            "ok": _job["ok"],
            "mode": _job["mode"],
            "pct": _job.get("pct", 0),
            "log_total": trimmed + len(logs),
            "log_trimmed": trimmed,
            "report": _job["report"],
        }
        if offset >= 0:
            payload["logs"] = list(logs[max(0, offset - trimmed):])
        else:
            payload["logs"] = list(logs)
        return jsonify(payload)


@app.post("/api/cancel")
def api_cancel():
    """S-3：协作式取消当前任务。

    仅 export/sync 响应：导出在会话边界停止（已完成部分保留并照常打包），
    同步在账号边界停止。keys/decrypt 不支持（单次内存扫描/解密流水线，
    中途停止收益低、复杂度高）。
    """
    with _lock:
        running = _job["running"]
        mode = _job["mode"]
    if not running:
        return jsonify({"error": "当前没有运行中的任务"}), 409
    if mode not in ("export", "sync"):
        return jsonify({"error": f"模式 {mode} 不支持取消"}), 400
    _CANCEL.set()
    _log("[job] 收到取消请求，将在当前会话/账号完成后停止…")
    return jsonify({"cancelling": True})


@app.post("/api/discover/validate")
def api_validate_path():
    """验证并保存手动输入的微信存储路径。"""
    data = request.get_json(silent=True) or {}
    result = add_manual_data_dir(data.get("path", ""))
    # 手动新增数据目录 → 状态快照立即失效，否则引导页最长滞后一个 TTL
    _invalidate_status_cache()
    return jsonify(result)


_AUTO_SYNC_STARTED = False


def _start_auto_sync_scheduler() -> None:
    """设置页可开启的后台增量同步：微信在线且到达间隔时执行 sync。"""
    global _AUTO_SYNC_STARTED
    if _AUTO_SYNC_STARTED:
        return
    _AUTO_SYNC_STARTED = True

    def _loop():
        while True:
            time.sleep(30)
            try:
                cfg = load_auto_sync()
                if not cfg.get("enabled"):
                    continue
                last = int(cfg.get("last_run") or 0)
                interval = max(1, int(cfg.get("interval_minutes") or 30)) * 60
                if last and time.time() - last < interval:
                    continue
                if not _status_snapshot().get("pids"):
                    continue
                with _lock:
                    if _job["running"]:
                        continue
                    _job.update({"running": True, "mode": "sync", "done": False,
                                 "ok": False, "pct": 0, "log_trimmed": 0,
                                 "logs": [], "report": None})
                _log(f"[auto-sync] 微信在线，开始定时增量同步（间隔 {interval // 60} 分钟）")

                def _worker():
                    # S-4：直接使用 _run_job 的返回值，不再回头读 _job["ok"]——
                    # 释放任务槽与获锁之间 /api/run 可能已重置 _job（TOCTOU）
                    ok = _run_job("sync")
                    mark_auto_sync_result(ok, "增量同步完成" if ok else "增量同步失败")

                threading.Thread(target=_worker, daemon=True).start()
            except Exception as e:
                mark_auto_sync_result(False, str(e))
                _siwx_logger.exception("auto-sync 调度失败: %s", e)
                _flush_logs()

    threading.Thread(target=_loop, name="siwx-auto-sync", daemon=True).start()


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

    _start_auto_sync_scheduler()

    # URL 打开
    url = f"http://{host}:{port}"
    if open_browser:
        threading.Timer(0.8, lambda: webbrowser.open(url)).start()

    # 状态 getter（供 TUI 状态栏轮询）；走共享快照缓存（S-1），
    # 状态栏 1s 一轮不再独立全盘扫描
    def _status_getter() -> dict:
        d = {}
        try:
            d["url"] = url
            snap = _status_snapshot()
            pids = snap.get("pids") or []
            d["wechat"] = f"运行中({len(pids)})" if pids else "未运行"
            accs = snap.get("accounts") or []
            d["wxid"] = accs[0]["wxid"] if accs else "未检测"
            d["keys"] = str(snap.get("stored_salts") or 0)
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
        # 自动更新重启时旧进程还占着端口：先探活等它退出再绑定，
        # 否则新进程的 Flask 线程直接 EADDRINUSE 死掉。最多等 ~30s。
        import socket as _socket
        deadline = time.time() + 30
        while time.time() < deadline:
            try:
                with _socket.create_connection((host, port), timeout=0.5):
                    time.sleep(0.5)  # 端口仍被监听 → 等旧进程退出
            except OSError:
                break  # 连不上 = 端口已释放
        with _cl.redirect_stdout(io.StringIO()), \
             _cl.redirect_stderr(io.StringIO()):
            app.run(host=host, port=port, threaded=True,
                    debug=False, use_reloader=False)

    server_thread = threading.Thread(target=_run_flask, daemon=True)
    server_thread.start()

    # 主线程：常驻状态栏（Ctrl+C 退出）
    tui.run_live_status(_status_getter, lambda: None)
