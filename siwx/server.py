"""Web 控制台：Flask 壳 —— 页面路由 + 任务槽；业务 API 在 api_*.py 模块化蓝图中。"""
import logging
import os
import re
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


# ── Origin / Sec-Fetch-Site 校验（审计 P2-11，防 CSRF）──────────────
# Host 头校验只挡 DNS rebinding，挡不住跨源表单：任意网页可以
# <form method=POST action="http://127.0.0.1:port/api/update/do">，浏览器
# 照发不误（表单提交不受 CORS 限制）。这里只对"写"请求校验浏览器自带的
# 来源头：
#   - Origin 存在：scheme 必须是 http/https，netloc 必须与 Host 完全一致
#     （大小写不敏感）；"Origin: null"（沙箱 iframe / file://）一律 403；
#   - Sec-Fetch-Site 存在：只允许 same-origin / none，same-site 也拒绝
#     （同站子域同样不该读本机数据）；
#   - 两个头都没有：curl / 脚本 / 旧客户端，放行（不加 token 的既定决策）。
# 本机 UI 的同源 fetch 会带 Origin 与 Sec-Fetch-Site: same-origin，不受影响。
_SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})
_ALLOWED_FETCH_SITES = frozenset({"same-origin", "none"})


def _parse_origin(origin: str):
    """解析 Origin 头，返回 netloc；非法（null / 解析失败 / 非 http(s)）返回 None。"""
    from urllib.parse import urlsplit
    try:
        parts = urlsplit(origin)
    except ValueError:
        return None
    if (parts.scheme or "").lower() not in ("http", "https"):
        return None
    netloc = (parts.netloc or "").lower()
    return netloc or None


@app.before_request
def _origin_guard():
    if request.method in _SAFE_METHODS:
        return None
    origin = (request.headers.get("Origin") or "").strip()
    fetch_site = (request.headers.get("Sec-Fetch-Site") or "").strip().lower()
    if not origin and not fetch_site:
        return None
    if origin:
        netloc = _parse_origin(origin)
        # 只记 netloc（不含 query/path），避免把可疑 URL 里的敏感参数写进日志
        if netloc is None:
            _siwx_logger.warning("拒绝非法 Origin 的写请求: %s %s",
                                 request.method, request.path)
            return jsonify({"error": "Origin 头非法，已拒绝请求"}), 403
        if netloc != (request.host or "").lower():
            _siwx_logger.warning("拒绝跨源写请求: %s %s origin=%s host=%s",
                                 request.method, request.path, netloc,
                                 request.host)
            return jsonify({"error": "跨源请求被拒绝（Origin 与 Host 不一致）"}), 403
    if fetch_site and fetch_site not in _ALLOWED_FETCH_SITES:
        _siwx_logger.warning("拒绝跨源写请求: %s %s Sec-Fetch-Site=%s",
                             request.method, request.path, fetch_site)
        return jsonify({"error": f"跨源请求被拒绝（Sec-Fetch-Site={fetch_site}）"}), 403
    return None

# 模块化 API 蓝图（聊天查看 / 设置 / 导出 / MCP）
from siwx.api_chat import bp as chat_bp  # noqa: E402
from siwx.api_settings import bp as settings_bp, load_auto_sync, mark_auto_sync_result  # noqa: E402
from siwx.api_export import bp as export_bp  # noqa: E402
from siwx.api_mcp import bp as mcp_bp  # noqa: E402
from siwx.api_update import bp as update_bp  # noqa: E402
from siwx.api_plugins import bp as plugins_bp  # noqa: E402
from siwx.api_stats import bp as stats_bp  # noqa: E402
from siwx.api_sns import bp as sns_bp  # noqa: E402
from siwx.api_backup import bp as backup_bp  # noqa: E402
app.register_blueprint(chat_bp)
app.register_blueprint(settings_bp)
app.register_blueprint(export_bp)
app.register_blueprint(mcp_bp)
app.register_blueprint(update_bp)
app.register_blueprint(plugins_bp)
app.register_blueprint(stats_bp)
app.register_blueprint(sns_bp)
app.register_blueprint(backup_bp)

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
        "logs": [], "report": None, "error": None, "export_dir": None,
        "progress": {"pct": 0, "msg": ""}}
# 协作式取消：/api/job/cancel 置位，任务循环在各检查点轮询。优雅停止——
# 已完成会话/文件的产物保留，报告带 cancelled=True，与失败（error）区分。
_cancel_event = threading.Event()
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


# 审计 §4.1：全局任务槽 409 拒绝时文案带上运行中任务的类型——被导出/
# 同步/引导提取占着槽时，用户只看得到"已有任务在运行"却不知是什么在跑、
# 去哪看。label 对齐各发起页按钮/入口的叫法，未登记的 mode 原样透出。
_JOB_MODE_LABELS = {
    "keys": "密钥提取",
    "decrypt": "解密",
    "auto": "提取并解密",
    "sync": "增量同步",
    "export": "导出",
    "convert": "补生成",
    "sns_export": "朋友圈导出",
}


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
            _log(f"[job] ▲ 账号 {c['wxid']} 发现 {len(c['dirs'])} 个副本目录，"
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
                if _cancel_event.is_set():
                    _log("[job] 已按用户请求取消：剩余账号跳过")
                    break
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
                if _cancel_event.is_set():
                    _log("[job] 已按用户请求取消：剩余账号跳过")
                    break
                _log(f"[job] 解密账号 {wxid}…")
                accounts.append({"wxid": wxid,
                                 "decrypt": extract.decrypt_dir(
                                     db, str(Path(out_root) / wxid), _log,
                                     workers=workers, use_cache=use_cache,
                                     should_cancel=_cancel_event.is_set)})
            report = {"kind": "decrypt", "accounts": accounts}
            total_ok = sum(a["decrypt"]["ok"] for a in accounts)
            total_cached = sum(a["decrypt"].get("cached", 0) for a in accounts)
            _log(f"[job] 解密完成: {total_ok} 成功, {total_cached} 缓存命中")

        elif mode == "auto":
            out_root = out_dir or str(_paths.out_root())
            _log(f"[job] 全自动: 输出目录={out_root}")
            accounts = []
            for wxid, db in dirs:
                if _cancel_event.is_set():
                    _log("[job] 已按用户请求取消：剩余账号跳过")
                    break
                _log(f"[job] 账号 {wxid}: 提取密钥…")
                rep = extract.extract_keys_for_dir(db, _log)
                _log(f"[job] 账号 {wxid}: 密钥 {rep['verified']}/{rep['total_salts']}")
                dec = None
                if rep["verified"] > 0:
                    _log(f"[job] 账号 {wxid}: 开始解密…")
                    dec = extract.decrypt_dir(db, str(Path(out_root) / wxid), _log,
                                              workers=workers, use_cache=use_cache,
                                              should_cancel=_cancel_event.is_set)
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
                if _cancel_event.is_set():
                    _log("[sync] 已按用户请求取消：剩余账号跳过")
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
            def _export_progress(pct, msg):
                # 进度同时走两条通道：结构化 progress（前端直接读字段，不再
                # 从日志文本里正则抠百分比）+ 任务日志（留痕与旧前端兼容）
                with _lock:
                    _job["progress"] = {"pct": int(pct), "msg": str(msg or "")}
                _log(f"[export] {pct}% {msg or ''}")

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
                progress=_export_progress,
                should_cancel=_cancel_event.is_set)
            if res.get("cancelled"):
                _log(f"[export] 已按用户请求取消：完成 {res.get('ok_count', 0)} 个会话，"
                     f"已完成部分的产物保留在 {res.get('total_dir', '')}")
            else:
                _log(f"[export] 全部完成：{res.get('ok_count', 0)}/{len(chats)} 个会话，"
                     f"消息 {res.get('message_count', 0)}，媒体 {res.get('media_count', 0)}，"
                     f"耗时 {res.get('duration_ms', 0)}ms")
            report = {"kind": "export", **res}

        elif mode == "convert":
            # 方案 A：对已有 JSON 导出目录就地补生成 Markdown/HTML（不重跑解密）。
            # 目录来自请求体，必须位于导出根目录内（防任意目录读/写原语）。
            from siwx import json_export_convert
            data = export_opts or {}
            d = str(data.get("dir") or "")
            if not d or not validate.within(d, _paths.exports_root()):
                raise RuntimeError("convert 目录无效（必须位于导出根目录内）")
            root = Path(d)
            if not root.is_dir():
                raise RuntimeError(f"导出目录不存在: {d}")
            formats = [str(f).strip().lower()
                       for f in (data.get("formats") or ["md", "html"])]
            formats = [f for f in formats if f in ("md", "html")]
            if not formats:
                raise RuntimeError("formats 里没有受支持的格式（md/html）")
            _log(f"[convert] 目录={root.name} 格式={','.join(formats)} "
                 f"overwrite={bool(data.get('overwrite'))}")

            def _convert_progress(done, total, msg):
                pct = int(done * 100 / total) if total else 0
                with _lock:
                    _job["progress"] = {"pct": pct, "msg": str(msg or "")}
                _log(f"[convert] {done}/{total} {msg or ''}")

            try:
                stats = json_export_convert.convert_dir_report(
                    root, formats,
                    template=data.get("template"),
                    overwrite=bool(data.get("overwrite")),
                    log=_log, progress=_convert_progress,
                    should_cancel=_cancel_event.is_set)
            except Exception as e:
                # 与 export 打包失败同一策略：目标目录在上面已验证存在，
                # 随异常带回，前端失败分支据此渲染「打开目录」
                try:
                    e.export_dir = d
                except Exception:
                    pass
                raise
            if stats.get("cancelled"):
                _log("[convert] 已按用户请求取消：已完成会话的产物保留")
            report = {"kind": "convert", "dir": d, "formats": formats, **stats}

        else:
            raise RuntimeError(f"未知模式: {mode}")

        if _cancel_event.is_set() and isinstance(report, dict) \
                and not report.get("cancelled"):
            # keys/decrypt/auto/sync 在账号循环中途取消会走到这里：
            # 部分结果保留，显式标注 cancelled，前端按"已取消"而非失败呈现
            report = dict(report, cancelled=True)
            _log("[job] 任务已取消（已完成部分保留）")

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
            # 导出/补生成任务的输出目录若已创建，随失败状态一并带回
            # （exporter/convert 段把目录挂到异常对象的 export_dir 属性上）：
            # 产物可能已完整落盘，前端失败分支据此渲染「打开目录」。
            _export_dir = getattr(e, "export_dir", None)
            if _export_dir:
                _job["export_dir"] = str(_export_dir)
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
        _cancel_event.clear()
        # 任务期间密钥/解密产物可能已变化，/api/status 的账号扫描缓存作废
        _invalidate_status_scan()
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
    # 首页壳无版本号指纹（JS/CSS 才有 ?v=），必须禁缓存：否则浏览器
    # 拿旧壳配新脚本，页面 ID 对不上 → init 里 addEventListener(null)
    # 这类"偶现"脚本报错。
    resp = Response(page, mimetype="text/html")
    resp.headers["Cache-Control"] = "no-store"
    return resp


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

    审计 F4 / §5.1：JS 里的静态资源缓存版本号（?v=<数字>）在响应时统一
    改写为 siwx.__version__——页面源码里写任何数字占位都会被替换，发版
    不依赖手工同步各页面的版本串（漏改即用户端缓存旧 JS 的坑不再可能；
    历史实现只替换字面量 "?v=2026100203"，而仓库里早已没有该字符串，
    注入实为空操作）。"""
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
            # 用 lambda 组装替换串：万一版本号未来含 \ 或 \g 序列，
            # 直接 f-string 作 re.sub 的 repl 会被当反向引用解析。
            text = re.sub(r"\?v=\d+", lambda _m: f"?v={_ver}",
                          target.read_text(encoding="utf-8"))
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


# ── /api/status 账号扫描短 TTL 缓存（审计 §4.6）──────────────────
# 侧栏每 5 秒轮询 /api/status，但只用 wechat_running/pids 两个字段；账号
# 扫描却要对每个账号 os.walk 数据目录、逐库读首页验证密钥（被微信占用的
# .db 还会整文件复制后读取，见 sqlcipher.py）。扫描结果加短 TTL 缓存：
# wechat_running/pids 每次现算（进程枚举开销小、状态要新鲜），扫描大头
# 在 TTL 内复用；任务结束与手动目录变更时主动失效（_invalidate_status_scan）。
_STATUS_SCAN_TTL = 8.0          # 秒；≥ 轮询间隔才真能合并 consecutive 轮询
_status_scan_cache = {"ts": 0.0, "value": None}
_status_scan_lock = threading.Lock()


def _invalidate_status_scan() -> None:
    """密钥/账号可能已变化（任务结束、手动目录增删）：下个 /api/status 重新扫描。"""
    with _status_scan_lock:
        _status_scan_cache["ts"] = 0.0
        _status_scan_cache["value"] = None


def _scan_status_snapshot() -> dict:
    """全量账号扫描（原 /api/status 内联段，抽出以便缓存复用）。"""
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
    return {"accounts": accounts, "stored_salts": len(store),
            # 同名账号冲突：多个 db_dir 共用 output/<wxid>/，解密会互相覆盖
            "conflicts": find_account_conflicts(all_dirs)}


@app.get("/api/status")
def status():
    pids = find_wechat_pids()
    # 持锁扫描：并发轮询合并为一次全量扫描（此前每次轮询各自全扫）
    with _status_scan_lock:
        snap = _status_scan_cache["value"]
        if snap is None or time.monotonic() - _status_scan_cache["ts"] >= _STATUS_SCAN_TTL:
            snap = _scan_status_snapshot()
            _status_scan_cache["ts"] = time.monotonic()
            _status_scan_cache["value"] = snap
    return jsonify({
        "wechat_running": bool(pids),
        "pids": pids,
        **snap,
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
            running_mode = _job.get("mode") or ""
            label = _JOB_MODE_LABELS.get(running_mode, running_mode or "未知")
            log.warn("server", f"任务被拒绝: 已有任务在运行（mode={running_mode}）")
            return jsonify({"error": f"已有任务在运行（{label}），请等它完成后再试"}), 409
        _job.update({"running": True, "mode": data.get("mode", ""), "done": False,
                     "ok": False, "logs": [], "report": None, "error": None,
                     "export_dir": None,
                     "progress": {"pct": 0, "msg": ""}})
    jid = _next_job_id()
    log.detailed("server", f"[{jid}] job 状态: running=True "
                           f"mode={data.get('mode', '')} done=False")
    _cancel_event.clear()
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


_FILE_LINE_RE = re.compile(
    r"^\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}(?:[,.]\d+)?\s*\[(\w+)\]\s*siwx:\s*(.*)$",
    re.DOTALL)


def _log_item_level(item) -> str:
    """混合形状日志条目的级别（P2-19，粗略模式过滤用）。

    - 结构化轨 [ts, level, module, msg]：级别在元组第 2 项，直接取；
    - 文件轨 [ts, "YYYY-mm-dd HH:MM:SS[,ms] [LEVEL] siwx: ..."]：解析前缀；
    - MCP 轨（"[MCP] " 前缀）与纯文本：无法判定 → 空串（不参与过滤，
      避免把无级别信息的行误杀）。
    """
    try:
        if len(item) >= 4:
            return str(item[1]).upper()
        m = _FILE_LINE_RE.match(str(item[1] or ""))
    except (TypeError, IndexError):
        return ""
    return m.group(1).upper() if m else ""


def _dedup_key(item) -> tuple:
    """跨轨归一化去重键（秒级时间 + 模块 + 消息正文）。

    同一条日志会同时出现在 siwx.log 文件轨（"… [DEBUG] siwx: [模块] 正文"）
    与结构化轨（[ts, level, module, 正文]）。旧键 = (毫秒时间戳, 原始文本)，
    两轨前缀与毫秒永远不同 → 去重恒失效，详细模式下日志页每条成对出现。
    文件轨剥掉前缀后与结构化轨按 (秒, "[模块] 正文") 对齐。
    """
    if len(item) >= 4:
        return (int((item[0] or 0) // 1000), f"[{item[2]}] {item[3]}")
    text = str(item[1] or "")
    m = _FILE_LINE_RE.match(text)
    if m:
        return (int((item[0] or 0) // 1000), m.group(2))
    return (int((item[0] or 0) // 1000), text)


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
    # 按开关口径过滤文件轨/结构化轨里的 DEBUG 行——开关只控展示，留存不受
    # 影响。级别判定走 _log_item_level：文件轨是 "[DEBUG] siwx:"、结构化轨
    # 在元组第 2 项，旧的 "] [DEBUG]" 子串匹配对两轨都不命中（P2-19）。
    # 每次调用现读 get_level：切换后不重启即生效，不缓存。
    rough = log.get_level() == log.LogLevel.ROUGH
    seen = set()
    merged = []
    for item in sorted(app_logs + ring_logs + mcp_logs + structured_logs,
                       key=lambda x: x[0] or 0):
        text = _log_item_text(item)
        if "404 Not Found" in text:
            continue
        if rough and _log_item_level(item) == "DEBUG":
            continue
        if start_ts and (item[0] or 0) < start_ts:
            continue
        if end_ts and (item[0] or 0) > end_ts:
            continue
        key = _dedup_key(item)
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


@app.post("/api/logs/client")
def api_log_client():
    """前端 JS 异常上报：window.onerror/unhandledrejection 钩子回传，
    写入结构化日志轨，日志页/文件日志直接可见（定位偶发页面脚本报错）。

    字段全部来自浏览器运行时，按不可信输入处理：截断 + 去敏后再落盘。
    """
    data = request.get_json(silent=True) or {}
    msg = str(data.get("message") or "")[:500]
    if not msg:
        return jsonify({"ok": False, "error": "message 为空"}), 400
    parts = [msg]
    src = str(data.get("source") or "")[:300]
    if src:
        loc = f"{data.get('line', '?')}:{data.get('col', '?')}"
        parts.append(f"{src}@{loc}")
    stack = str(data.get("stack") or "")[:2000]
    if stack:
        parts.append(stack)
    kind = str(data.get("kind") or "error")[:20]
    log.warn("ui", f"[前端{kind}] {log.desensitize_msg(' | '.join(parts))}")
    return jsonify({"ok": True})


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
    """返回当前任务状态（供前端轮询）。progress 为结构化进度，日志归日志。"""
    with _lock:
        return jsonify({
            "running": _job["running"],
            "done": _job["done"],
            "ok": _job["ok"],
            "mode": _job["mode"],
            "logs": _job["logs"],
            "report": _job["report"],
            "error": _job["error"],
            "export_dir": _job.get("export_dir"),
            "progress": _job.get("progress") or {"pct": 0, "msg": ""},
        })


@app.post("/api/job/cancel")
def api_job_cancel():
    """协作式取消当前任务。优雅停止：各检查点轮询，已完成部分保留。

    响应 cancelling=True 只代表"取消请求已受理"，实际停止要等任务线程
    走到最近的检查点（媒体解密一批之后、当前会话结束/下一会话开始前）。
    无运行中任务时返回 409，前端据此置灰按钮。

    P2-16：可选 JSON body `{"mode": "export"|"convert"}`。全局 job 槽是
    共享的，导出页的取消按钮可能打到设置页转换任务上——调用方声明自己的
    页面模式后，与运行中任务的 mode 不一致就拒绝（409），且不置位取消事件，
    避免误停别的任务。不传 mode 保持旧行为（脚本/旧客户端兼容）。"""
    data = request.get_json(silent=True) or {}
    req_mode = data.get("mode") if isinstance(data, dict) else ""
    if not isinstance(req_mode, str):
        req_mode = ""
    with _lock:
        if not _job["running"]:
            return jsonify({"cancelling": False, "error": "当前没有运行中的任务"}), 409
        job_mode = _job.get("mode") or ""
    if req_mode and job_mode and req_mode != job_mode:
        log.warn("server", f"拒绝取消请求: 运行中 mode={job_mode}, "
                           f"请求 mode={req_mode}")
        return jsonify({
            "cancelling": False,
            "error": f"当前运行的是 {job_mode} 任务，不是本页面发起的任务",
        }), 409
    _cancel_event.set()
    _log(f"[{job_mode}] 收到取消请求——等待当前步骤收尾后停止（已完成部分保留）")
    return jsonify({"cancelling": True})


# ── 全量媒体备份（media_backup.py，设置页入口）───────────────────
_backup_lock = threading.Lock()
_backup_cancel = threading.Event()
_backup_job = {"running": False, "done": False, "ok": False, "account": None,
               "out_dir": None, "logs": [], "report": None, "error": None}


def _run_media_backup(account: str, out_dir, jlog):
    from siwx import media_backup
    try:
        report = media_backup.backup_all(account, out_dir, log=jlog,
                                         should_cancel=_backup_cancel.is_set)
        _backup_job["report"] = report
        # P2-9：账号目录/媒体目录缺失时 report.ok=False，任务必须显式失败，
        # 不再无条件报"备份完成"（前端读 ok/error 渲染失败态）。
        ok = bool(report.get("ok", True))
        _backup_job["ok"] = ok
        if not ok:
            _backup_job["error"] = report.get("error") or "备份失败（原因未知）"
            jlog(f"备份失败: {_backup_job['error']}")
        elif report.get("cancelled"):
            jlog("备份已取消（已完成部分保留）")
        else:
            jlog("备份完成")
    except Exception as e:
        _backup_job["ok"] = False
        _backup_job["error"] = f"{type(e).__name__}: {e}"
        jlog(f"备份失败: {_backup_job['error']}")
    finally:
        _backup_job["running"] = False
        _backup_job["done"] = True
        _backup_cancel.clear()


@app.post("/api/media/backup/start")
def api_media_backup_start():
    """对指定账号做文件级全量媒体备份（删除微信本地数据前的保全手段）。"""
    data = request.get_json(silent=True) or {}
    account = (data.get("account") or "").strip()
    if not account:
        return jsonify({"error": "参数缺失: account"}), 400
    acc_dir = validate.account_dir(account, must_exist=True)
    if acc_dir is None:
        return jsonify({"error": "账号不存在或未解密"}), 404
    out_dir = Path(acc_dir) / "media_backup"
    with _backup_lock:
        if _backup_job["running"]:
            return jsonify({"error": "已有备份在运行"}), 409
        _backup_job.update({"running": True, "done": False, "ok": False,
                            "account": account, "out_dir": str(out_dir),
                            "logs": [], "report": None, "error": None})
    _backup_cancel.clear()

    def jlog(msg):
        _backup_job["logs"].append(str(msg))
        if len(_backup_job["logs"]) > 300:
            del _backup_job["logs"][:100]

    threading.Thread(target=_run_media_backup, args=(account, out_dir, jlog),
                     daemon=True).start()
    return jsonify({"started": True, "out_dir": str(out_dir)})


@app.post("/api/media/backup/cancel")
def api_media_backup_cancel():
    """协作式取消全量媒体备份（逐文件检查点，已完成文件保留）。"""
    with _backup_lock:
        if not _backup_job["running"]:
            return jsonify({"cancelling": False, "error": "当前没有运行中的备份"}), 409
    _backup_cancel.set()
    _backup_job["logs"].append("收到取消请求——当前文件处理完后停止（已完成部分保留）")
    return jsonify({"cancelling": True})


@app.get("/api/media/backup/status")
def api_media_backup_status():
    """返回全量媒体备份任务状态（供前端轮询）。"""
    with _backup_lock:
        return jsonify(_backup_job)


@app.post("/api/media/backup/open")
def api_media_backup_open():
    """在资源管理器中打开最近一次备份的输出目录。

    不接受 path 参数（杜绝目录穿越面），只打开任务自身的 out_dir——
    它由服务端从账号输出目录推导，必然位于输出根目录内。
    """
    with _backup_lock:
        out_dir = _backup_job["out_dir"]
    if not out_dir or not Path(out_dir).is_dir():
        return jsonify({"error": "备份目录不存在（先完成一次备份）"}), 404
    import subprocess
    if os.name == "nt":
        os.startfile(out_dir)                                   # noqa: S606
    elif sys.platform == "darwin":
        subprocess.Popen(["open", out_dir])
    else:
        subprocess.Popen(["xdg-open", out_dir])
    return jsonify({"opened": True})


@app.post("/api/discover/validate")
def api_validate_path():
    """验证并保存手动输入的微信存储路径。"""
    data = request.get_json(silent=True) or {}
    result = add_manual_data_dir(data.get("path", ""))
    if result.get("saved"):
        # 发现列表变了，/api/status 的账号扫描缓存作废（设置页保存后立刻可见）
        _invalidate_status_scan()
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
                    d["job"] = "√完成" if _job["ok"] else "×失败"
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
