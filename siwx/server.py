"""Web 控制台：Flask 壳 —— 页面路由 + 任务槽；业务 API 在 api_*.py 模块化蓝图中。"""
import logging
import os
import sys
import threading
import time
import webbrowser
from datetime import datetime
from pathlib import Path

from flask import Flask, Response, abort, g, jsonify, request, send_from_directory
from werkzeug.exceptions import HTTPException

from siwx import extract, keystore, logger as log
from siwx import paths as _paths
from siwx import validate
from siwx.discover import (add_manual_data_dir, find_account_conflicts,
                           find_wechat_data_dirs, find_wechat_pids,
                           load_manual_data_dirs, wxid_of)
from siwx.logging_setup import flush_logs as _flush_logs
from siwx.logging_setup import install_crash_hooks, setup_file_logger
from siwx.sqlcipher import collect_db_files


# ── 文件日志 + 崩溃钩子（已抽到 siwx/logging_setup.py，CLI 模式也统一安装）──
_siwx_logger = setup_file_logger()
install_crash_hooks()
# 启动恢复 Debug 级别（环境变量 SIWX_LOG_LEVEL / 持久化设置）。
# 必须在 setup_file_logger() 之后：其内部会把 "siwx" logger 置回 DEBUG（审计 §2.3）。
from siwx import loglevel as _loglevel  # noqa: E402

_loglevel.startup()


def _ui_dir() -> Path:
    """PyInstaller 打包后资源在 _MEIPASS，源码运行时在 siwx/ui。"""
    if getattr(sys, "frozen", False):
        return Path(getattr(sys, "_MEIPASS", Path(sys.executable).parent)) / "ui"
    return Path(__file__).resolve().parent / "ui"


UI_DIR = _ui_dir()

app = Flask(__name__, static_folder=None)

# ── 请求级 access log（审计 §2.5）──────────────────────────────
# werkzeug access log 被彻底禁用（见 run_server），detailed 模式下用结构化轨
# 补一条 "{method} {path} {status} {ms}ms"。不含 query string（防消息内容/
# 关键词入日志）。404 跳过：与日志页的 404 过滤口径一致，避免浏览器探测刷屏。
@app.before_request
def _access_log_t0():
    g._siwx_t0 = time.perf_counter()
    return None


@app.after_request
def _access_log(response):
    try:
        if response.status_code != 404:
            ms = (time.perf_counter() - getattr(g, "_siwx_t0", time.perf_counter())) * 1000
            log.detailed("http", f"{request.method} {request.path} "
                                 f"{response.status_code} {ms:.0f}ms")
    except Exception:
        pass
    return response


# ── Host 头校验（审计 S2）────────────────────────────────────────
# 默认绑定 127.0.0.1 时，无 Host 校验意味着 DNS rebinding 页面可以把
# <攻击域名> 解析到 127.0.0.1 并跨源读取 /api/status、/api/logs、/api/job。
# 浏览器正常访问的 Host 是 127.0.0.1:port / localhost:port，攻击页的 Host
# 是攻击者域名 → 直接 403。显式 --trust-lan 时跳过校验（用户已确认接受
# 局域网访问，Host 形态不可预知）。
_BOUND = {"host": "127.0.0.1", "trust_lan": False}


def _request_hostname() -> str:
    try:
        from urllib.parse import urlsplit
        return (urlsplit(f"//{request.host or ''}").hostname or "").lower()
    except ValueError:
        return (request.host or "").lower()


@app.before_request
def _host_guard():
    if _BOUND["trust_lan"]:
        return None
    host = _request_hostname()
    if host in ("", "127.0.0.1", "localhost", "::1"):
        return None
    if host == (_BOUND["host"] or "").lower():
        return None
    _siwx_logger.warning("拒绝非本机 Host 请求: %s", host)
    return jsonify({"error": f"Host 校验失败: {host} 不在允许列表内"}), 403

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
_job = {"running": False, "mode": None, "done": False, "ok": False,
        "logs": [], "report": None, "error": None}
_LOG_RING: list = []          # 环形日志缓冲（供日志页展示）
_LOG_RING_MAX = 2000

# 任务短 ID 生成器（P1：跨日志轨对账）。job-0001 递增，任务入口设置进
# threading.local（logger.set_job_id），同线程内 exporter 的 detailed
# 用 logger.job_prefix() 携带同一 ID，任务轨/结构化轨从此可按 ID 对账
_JOB_SEQ = 0
_JOB_SEQ_LOCK = threading.Lock()


def _next_job_id() -> str:
    global _JOB_SEQ
    with _JOB_SEQ_LOCK:
        _JOB_SEQ += 1
        return f"job-{_JOB_SEQ:04d}"


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
    body = {"error": f"{type(e).__name__}: {e}"}
    # traceback 只在本机 debug 模式回显：默认状态下它是一份完整的源码路径/行号
    # 清单，直接吐给任何触发 500 的调用方（含 --trust-lan 下的局域网 peer）。
    if app.debug:
        body["traceback"] = tb
    return jsonify(body), 500


def _log(msg: str) -> None:
    """写入任务日志 + 全局环形缓冲 + 文件日志。"""
    _siwx_logger.info(msg)
    _flush_logs()
    with _lock:
        ts = int(time.time() * 1000)
        _job["logs"].append([ts, msg])
        if len(_job["logs"]) > 1200:
            del _job["logs"][:400]
        _LOG_RING.append([ts, msg])
        if len(_LOG_RING) > _LOG_RING_MAX:
            del _LOG_RING[:len(_LOG_RING) - _LOG_RING_MAX]


def _now_ms() -> int:
    return int(time.time() * 1000)


def _run_job(mode: str, db_dir=None, out_dir=None, no_cache=False, workers=None,
             export_opts=None, jid: str = "") -> None:
    """任务执行器。keys/decrypt 支持指定 db_dir（引导页单账号流程）。

    jid 由任务入口（/api/run 或 auto-sync 调度）生成并传入，任务轨与
    结构化轨携带同一 ID；缺省（直接调用）时自生成一个。"""
    use_cache = not no_cache
    jid = jid or _next_job_id()
    log.set_job_id(jid)
    _log(f"[{jid}] 模式={mode}, 指定目录={db_dir or '无'}, 缓存={use_cache}, 进程数={workers or '默认'}")
    _emit_task_event("start", mode=mode, db_dir=db_dir, out_dir=out_dir,
                     no_cache=no_cache, workers=workers)
    _t0 = time.time()
    try:
        # 朋友圈导出只依赖已解密产物，无需扫描微信数据目录（省一次全盘发现）
        if mode == "sns_export":
            from siwx import sns_export
            data = export_opts or {}
            account = data.get("account") or ""
            acc_dir = validate.account_dir(account)
            db = acc_dir / "sns" / "sns.db" if acc_dir else None
            if not db or not db.is_file():
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
            return

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
            # account 是请求值，必须过账号名校验 + 越界检查（此前直接拼 out_root）
            acc_dir = validate.account_dir(data.get("account"))
            if acc_dir is None or not (acc_dir / "message").is_dir():
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
                template=data.get("template"),
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
        _emit_task_event("done", mode=mode, ok=True, report=report,
                         duration_ms=int((time.time() - _t0) * 1000))
    except Exception as e:
        _siwx_logger.exception("任务执行失败: %s", e)
        _flush_logs()
        with _lock:
            ts = _now_ms()
            msg = f"[错误] {e}"
            _job["ok"] = False
            # 失败原因单独成字段：前端四处（export/settings/onboarding）此前只能
            # 从 logs 末尾猜，误判成「完成/无导出结果」。
            _job["error"] = f"{type(e).__name__}: {e}"
            _job["logs"].append([ts, msg])
            _LOG_RING.append([ts, msg])
            if len(_LOG_RING) > _LOG_RING_MAX:
                del _LOG_RING[:len(_LOG_RING) - _LOG_RING_MAX]
        _emit_task_event("done", mode=mode, ok=False, error=str(e),
                         duration_ms=int((time.time() - _t0) * 1000))
    finally:
        with _lock:
            _job["running"] = False
            _job["done"] = True
        log.detailed("server", f"[{jid}] job 状态: running=False mode={mode} done=True")
        log.set_job_id("")  # 线程归还线程池/复用前清掉，防跨任务串 ID


def _emit_task_event(event: str, **ctx) -> None:
    """广播任务生命周期事件给插件监听器（start / done）。

    契约：`listener(event: str, ctx: dict) -> None`。
    逐个隔离：插件异常只写日志，绝不影响任务本身。
    """
    try:
        from siwx.plugins import registry
    except Exception as e:
        # 插件系统注册表本身导入失败 → 任务事件不广播给插件监听器
        log.warn("plugin", f"插件系统注册表导入失败，任务事件不广播: {e}")
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
    except Exception as e:
        # 与 _emit_task_event 的失败影响面不同：这里只影响主题 CSS 注入
        log.warn("plugin", f"插件系统注册表导入失败，主题 CSS 注入失败: {e}")
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


@app.get("/app.css")
def css():
    return send_from_directory(UI_DIR, "app.css", mimetype="text/css")


@app.get("/app.js")
def js():
    return send_from_directory(UI_DIR, "app.js", mimetype="text/javascript")


@app.get("/common.js")
def common_js():
    return send_from_directory(UI_DIR, "common.js", mimetype="text/javascript")


@app.get("/widgets.js")
def widgets_js():
    """自绘控件（下拉/日期/菜单）：被 pages/*.js 以 ESM 静态 import 引用。"""
    return send_from_directory(UI_DIR, "widgets.js", mimetype="text/javascript")


@app.get("/onboarding.js")
def onboarding_js():
    """首启全屏引导向导（壳层组件）：由 app.js 动态 import 调起。"""
    return send_from_directory(UI_DIR, "onboarding.js", mimetype="text/javascript")


@app.get("/pages/<path:filename>")
def pages(filename: str):
    """模块化页面资源：pages/<name>.html / .js / .css

    审计 F4：JS 里的静态资源缓存版本号（?v=…）由 siwx.__version__ 在响应时
    注入，发版不再依赖手工改 5 个页面的版本串（漏改即用户端缓存旧 JS）。"""
    target = (UI_DIR / "pages" / filename)
    try:
        target.resolve().relative_to((UI_DIR / "pages").resolve())
    except (ValueError, OSError):
        abort(404)
    if not target.is_file():
        abort(404)
    if filename.endswith(".js"):
        try:
            from siwx import __version__ as _ver
            text = target.read_text(encoding="utf-8").replace(
                "?v=2026100203", f"?v={_ver}")
            return Response(text, mimetype="text/javascript")
        except OSError:
            pass
    return send_from_directory(UI_DIR / "pages", filename)


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
    return send_from_directory(root, filename)


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
    return send_from_directory(root, filename)


@app.get("/api/status")
def status():
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
        except ValueError as e:
            log.detailed("server", f"[status] 账号={wxid} db 扫描失败: "
                                   f"{type(e).__name__}: {e}")
        except Exception as e:
            log.detailed("server", f"[status] 账号={wxid} db 扫描失败: "
                                   f"{type(e).__name__}: {e}")
        try:
            is_manual = str(Path(db).resolve()).casefold() in manual_set
        except OSError:
            is_manual = str(db).casefold() in manual_set
        accounts.append({"wxid": wxid, "db_dir": db, "db_count": total,
                         "keys_cached": cached, "total_salts": total,
                         "manual": is_manual})
    return jsonify({
        "wechat_running": bool(pids),
        "pids": pids,
        "accounts": accounts,
        "stored_salts": len(store),
        # 同名账号冲突：多个 db_dir 共用 output/<wxid>/，解密会互相覆盖
        "conflicts": find_account_conflicts(all_dirs),
    })


@app.post("/api/run")
def run():
    data = request.get_json(silent=True)
    if data is None:
        log.warn("server", "/api/run 请求体不是合法 JSON，已按空参数处理")
    data = data or {}
    # 输入校验：db_dir 是「密钥提取/解密要读哪个目录」，out_dir 是「明文写到哪」。
    # 二者都来自请求体，不校验就等于把任意目录读/写原语暴露给本机任意进程
    # 与 --trust-lan 下的局域网peer。UI 只传发现列表里的 db_dir、从不传 out_dir。
    db_dir = data.get("db_dir")
    if db_dir:
        from siwx import discover
        v = discover.validate_db_path(str(db_dir))
        if not v.get("ok"):
            log.warn("server", f"/api/run 拒绝非法 db_dir: {v.get('error')}")
            return jsonify({"error": f"db_dir 无效：{v.get('error') or '未识别到微信数据目录'}"}), 400
    out_dir = data.get("out_dir")
    if out_dir:
        if not validate.within(out_dir, _paths.out_root()):
            log.warn("server", "/api/run 拒绝越界 out_dir（必须位于输出根目录内）")
            return jsonify({"error": "out_dir 必须位于输出根目录内"}), 400
    log.detailed("server", f"/api/run mode={data.get('mode', '')} "
                           f"db_dir={data.get('db_dir') or ''} "
                           f"out_dir={data.get('out_dir') or ''} "
                           f"no_cache={bool(data.get('no_cache'))} "
                           f"workers={data.get('workers')}")
    with _lock:
        if _job["running"]:
            log.warn("server", "任务被拒绝: 已有任务在运行")
            return jsonify({"error": "已有任务在运行"}), 409
        _job.update({"running": True, "mode": data.get("mode", ""), "done": False,
                     "ok": False, "logs": [], "report": None, "error": None})
    jid = _next_job_id()
    log.detailed("server", f"[{jid}] job 状态: running=True "
                           f"mode={data.get('mode', '')} done=False")
    args = (data.get("mode", ""), data.get("db_dir"), data.get("out_dir"),
            bool(data.get("no_cache")), data.get("workers"),
            data.get("export_opts") or {}, jid)
    threading.Thread(target=_run_job, args=args, daemon=True).start()
    return jsonify({"started": True})


# tail 读取失败的一次性告警标志：/api/logs 会被前端轮询，失败若每次都记会刷屏。
# P0 后 log.error 会桥接进 siwx.log（正是被读的文件），但写文件不触发再次
# 读取，无递归；保留一次性标志只为防轮询刷量。
_TAIL_ERR_REPORTED = {"app": False, "mcp": False}


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
    except Exception as e:
        if not _TAIL_ERR_REPORTED["app"]:
            _TAIL_ERR_REPORTED["app"] = True
            log.error("server", f"读取 siwx.log 失败（日志页文件日志缺失）: "
                                f"{type(e).__name__}: {e}")
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
    except Exception as e:
        if not _TAIL_ERR_REPORTED["mcp"]:
            _TAIL_ERR_REPORTED["mcp"] = True
            log.error("server", f"读取 mcp.log 失败（日志页 MCP 日志缺失）: "
                                f"{type(e).__name__}: {e}")
        return []


def _log_item_text(item) -> str:
    return str(item[3] if len(item) >= 4 else item[1])


def _desensitize_item(item):
    """对单条日志条目的文本部分脱敏。

    重建列表而非原地改：环形缓冲 / 文件缓冲与这里共享同一批 entry 对象，
    原地改会把脱敏写回底层缓冲（污染 export_logs 的 desensitize=0 通道）。"""
    if len(item) >= 4:
        return [item[0], item[1], item[2], log.desensitize_msg(str(item[3]))]
    return [item[0], log.desensitize_msg(str(item[1]))]


def _merged_logs(limit: int, *, desensitize: bool = True,
                 start_ts=None, end_ts=None) -> list:
    """合并四源日志并按时间排序（日志页与日志导出共用同一份数据）。

    四源：siwx.log 文件轨 + _LOG_RING 任务轨 + mcp.log + 进程内结构化轨。
    旧实现里「查看」走这四源、「导出」只走 logger._FILE_LOG（进程内列表，
    重启即空）——用户看着的任务轨和 MCP 行永远不在导出文件里，而 bug 报告
    模板恰恰要求粘贴日志。
    """
    with _lock:
        ring_logs = list(_LOG_RING)
    app_logs = _tail_app_log(max(800, limit))
    mcp_logs = _tail_mcp_log(500)
    structured_logs = log.get_logs(limit=limit)

    # 去重：同一条任务日志会同时进入 _LOG_RING 和 siwx.log。
    # P0 后 siwx.log 恒收 DEBUG（detailed 始终落盘），ROUGH 模式下日志页
    # 按开关口径过滤文件轨里的 DEBUG 行——开关只控展示，留存不受影响。
    rough = log.get_level() == log.LogLevel.ROUGH
    seen = set()
    merged = []
    for item in sorted(app_logs + ring_logs + mcp_logs + structured_logs,
                       key=lambda x: x[0] or 0):
        text = _log_item_text(item)
        if "404 Not Found" in text:
            continue
        if rough and "] [DEBUG]" in text:
            continue
        if start_ts and (item[0] or 0) < start_ts:
            continue
        if end_ts and (item[0] or 0) > end_ts:
            continue
        key = (item[0], text)
        if key in seen:
            continue
        seen.add(key)
        merged.append(item)
    if desensitize:
        merged = [_desensitize_item(item) for item in merged]
    return merged


def _log_line_text(item) -> str:
    """合并条目 → 纯文本行（导出用）。条目形状是混合的：[ts,msg] 或 [ts,level,module,msg]。"""
    ts = (item[0] if item else 0) or 0
    body = item[3] if len(item) >= 4 else (item[1] if len(item) > 1 else "")
    if len(item) >= 4:
        stamp = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(ts / 1000)) if ts else "-"
        return f"{stamp}.{ts % 1000 if ts else 0:03d} [{str(item[1]):7}] [{item[2]}] {body}"
    # 文件轨 / MCP 轨条目只带 [ts, 原文]，原文已含自己的时间与级别，不再套一层
    if not ts:
        return str(body)
    stamp = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(ts / 1000))
    return f"{stamp}.{ts % 1000:03d} {body}"


@app.get("/api/logs")
def api_logs():
    """返回文件日志 + 环形任务日志 + MCP 调用日志（合并按时间排序）。"""
    try:
        limit = max(1, min(int(request.args.get("limit", "2000") or 2000), 5000))
    except (TypeError, ValueError):
        return jsonify({"error": "limit 参数无效（必须是整数）"}), 400
    merged = _merged_logs(limit)
    return jsonify({"logs": merged[-limit:], "level": log.get_level().value})


@app.get("/api/logs/settings")
def api_log_settings():
    """获取日志设置。"""
    return jsonify({"level": log.get_level().value})


@app.post("/api/logs/settings")
def api_log_settings_save():
    """设置日志模式（持久化 + 双通道同步，见 siwx/loglevel.py）。"""
    data = request.get_json(silent=True) or {}
    level = data.get("level", "rough")
    if not isinstance(level, str):
        level = "rough"
    _loglevel.apply(level, persist=True)
    return jsonify({"level": log.get_level().value})


@app.get("/api/logs/export")
def api_log_export():
    """导出日志（与日志页同源：文件轨 + 任务轨 + MCP 轨 + 结构化轨）。"""
    start_ts = request.args.get("start")
    end_ts = request.args.get("end")
    desensitize = request.args.get("desensitize", "1") == "1"
    try:
        start = int(start_ts) if start_ts else None
        end = int(end_ts) if end_ts else None
    except (TypeError, ValueError):
        return jsonify({"error": "start/end 参数无效（必须是整数毫秒时间戳）"}), 400
    items = _merged_logs(20000, desensitize=desensitize, start_ts=start, end_ts=end)
    text = "\n".join(_log_line_text(it) for it in items)
    return Response(text, mimetype="text/plain",
                    headers={"Content-Disposition": "attachment; filename=siwx_log.txt"})


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
            "error": _job["error"],
        })


@app.post("/api/discover/validate")
def api_validate_path():
    """验证并保存手动输入的微信存储路径。"""
    data = request.get_json(silent=True) or {}
    result = add_manual_data_dir(data.get("path", ""))
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
                if not find_wechat_pids():
                    continue
                with _lock:
                    if _job["running"]:
                        log.warn("server", "auto-sync 跳过本轮: 已有任务在运行")
                        continue
                    _job.update({"running": True, "mode": "sync", "done": False,
                                 "ok": False, "logs": [], "report": None})
                jid = _next_job_id()
                log.detailed("server", f"[{jid}] job 状态: running=True "
                                       f"mode=sync done=False (auto-sync)")
                _log(f"[{jid}] [auto-sync] 微信在线，开始定时增量同步"
                     f"（间隔 {interval // 60} 分钟）")

                def _worker():
                    _run_job("sync", jid=jid)
                    with _lock:
                        ok = bool(_job.get("ok"))
                    mark_auto_sync_result(ok, "增量同步完成" if ok else "增量同步失败")

                threading.Thread(target=_worker, daemon=True).start()
            except Exception as e:
                mark_auto_sync_result(False, str(e))
                _siwx_logger.exception("auto-sync 调度失败: %s", e)
                _flush_logs()

    threading.Thread(target=_loop, name="siwx-auto-sync", daemon=True).start()


def run_server(host="127.0.0.1", port=8787, open_browser=True,
               trust_lan=False) -> None:
    """serve 模式：rich TUI 状态栏 + 日志流，Flask 完全静默。

    trust_lan：绑定非回环地址时必须由 CLI 显式确认（审计 S2）——局域网内
    任何设备都能无凭证触发导出等操作，需用户知情方可开启。"""
    import logging
    import time as _time

    from siwx import media, tui, keystore

    _BOUND["host"] = host
    _BOUND["trust_lan"] = bool(trust_lan)

    tui.banner()
    tui.log(f"控制台 http://{host}:{port} · 按 Ctrl+C 停止")

    # 媒体解密事件 → 结构化轨（默认实现已走 logger.detailed）+ TUI 实时流
    _base_event = media.event
    media.event = lambda msg: (_base_event(msg), tui.log(msg))

    # 彻底关闭 Flask/werkzeug 所有日志
    logging.getLogger("werkzeug").handlers = []
    logging.getLogger("werkzeug").propagate = False
    logging.getLogger("werkzeug").disabled = True

    _start_auto_sync_scheduler()

    # URL 打开
    url = f"http://{host}:{port}"
    if open_browser:
        threading.Timer(0.8, lambda: webbrowser.open(url)).start()

    # 状态 getter（供 TUI 状态栏轮询）
    status_fail = {"n": 0}

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
            status_fail["n"] = 0
        except Exception as e:
            # 每秒轮询一次，必须节流：首次 + 之后每连续 60 次（约 1 分钟）warn 一条
            status_fail["n"] += 1
            if status_fail["n"] == 1 or status_fail["n"] % 60 == 0:
                log.warn("server", f"TUI 状态栏数据采集连续失败 {status_fail['n']} 次"
                                   f"（状态栏将长期显示 …）: {type(e).__name__}: {e}")
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
