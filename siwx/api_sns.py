"""朋友圈（SNS）Web API。

基于 sns.py 的只读解析和 sns_cdn.py 的 CDN 下载/ISAAC64 解密。
"""
from __future__ import annotations

import base64
import logging
import re
import sqlite3
import threading
from pathlib import Path

from flask import Blueprint, Response, jsonify, request

from siwx import paths
from siwx import sns
from siwx import sns_cdn

bp = Blueprint("sns_api", __name__, url_prefix="/api/sns")
_ACCOUNT_RE = re.compile(r"^[A-Za-z0-9_.@-]+$")

# 媒体接口的失败原因要落 logs/siwx.log（「运行日志」页会 tail 该文件）
_media_log = logging.getLogger("siwx")

# 关键词搜索需要逐条解析 XML，无法下推 SQL；命中后立即停止，
# 另设扫描上限避免极端情况下把整个请求阻塞住（实测 0.19ms/条）。
KEYWORD_MAX_SCAN = 5000


def _account_dir(account: str) -> Path | None:
    if not account or not _ACCOUNT_RE.fullmatch(account):
        return None
    root = paths.out_root().resolve()
    p = (root / account).resolve()
    try:
        p.relative_to(root)
    except ValueError:
        return None
    if not p.is_dir():
        return None
    return p


def _sns_db(account: str) -> Path | None:
    p = _account_dir(account)
    db = p / "sns" / "sns.db" if p else None
    return db if db and db.is_file() else None


def _feed_json(feed: dict, include_comments: bool = True) -> dict:
    out = dict(feed)
    # card 统一走 sns.public_card()（与导出同一份形状），避免前端猜字段名
    out["card"] = sns.public_card(feed.get("card"))
    if not include_comments:
        out.pop("likes", None)
        out.pop("comments", None)
    return out


def _ts_arg(name: str, end: bool = False):
    """解析 ``start`` / ``end`` 查询参数（unix 秒或 ``YYYY-MM-DD``）。

    ``end`` 传纯日期时补到当日 23:59:59，否则「今天到某天」会少掉一整天。
    """
    raw = request.args.get(name)
    if raw in (None, ""):
        return None
    ts = sns.parse_ts_arg(raw)
    if ts is None:
        return None
    if end and re.fullmatch(r"\s*\d{4}[-/]\d{1,2}[-/]\d{1,2}\s*", str(raw)):
        ts += 86399
    return ts


def _avatar_usernames(acc_dir: Path | None, usernames) -> set:
    """批量查 head_image.db，返回**确实有头像**的 username 集合。

    为什么不让前端逐个头像请求：113 位发布者会有 24 个 404（本机实测覆盖率 89/113），
    既慢又在控制台刷错误。用户名与聊天一致，前端命中后才去取 `/api/chat/avatar`。
    """
    if acc_dir is None or not usernames:
        return set()
    db = acc_dir / "head_image" / "head_image.db"
    if not db.is_file():
        return set()
    wanted = [u for u in usernames if u]
    found = set()
    try:
        con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
        try:
            for i in range(0, len(wanted), 400):
                chunk = wanted[i:i + 400]
                marks = ",".join("?" * len(chunk))
                for (u,) in con.execute(
                        f"SELECT username FROM head_image WHERE username IN ({marks})", chunk):
                    if u:
                        found.add(u)
        finally:
            con.close()
    except sqlite3.Error:
        return set()
    # 自己的头像：SNS 里的 user_name 通常是原始 wxid，但输出目录名可能是 wxid_xxx_6409
    for u in wanted:
        if u in found:
            continue
        for cand in _self_aliases(u, acc_dir.name):
            if cand in found:
                found.add(u)
                break
    return found


def _self_aliases(username: str, account: str):
    """本人 wxid 的候选写法（与 api_chat.avatar 的回退逻辑保持一致）。"""
    from siwx import media
    out = []
    if username == account:
        clean = media.clean_wxid(account)
        out.append(clean)
        if "_" in account:
            out.append(account.rsplit("_", 1)[0])
    return out


@bp.get("/accounts")
def accounts():
    out = []
    root = paths.out_root()
    if root.is_dir():
        for d in sorted(root.iterdir()):
            db = d / "sns" / "sns.db"
            if db.is_file():
                try:
                    st = sns.timeline_stats(db)
                    out.append({"wxid": d.name, **st})
                except Exception:
                    out.append({"wxid": d.name, "count": 0})
    return jsonify({"accounts": out})


@bp.get("/timeline")
def timeline():
    account = request.args.get("account", "")
    db = _sns_db(account)
    if db is None:
        return jsonify({"error": "账号或朋友圈数据库不存在"}), 404
    try:
        limit = max(1, min(int(request.args.get("limit", 20)), 100))
    except ValueError:
        limit = 20
    before = request.args.get("before_tid")
    keyword = (request.args.get("keyword") or "").strip().lower()
    user = (request.args.get("username") or "").strip()
    start_ts, end_ts = _ts_arg("start"), _ts_arg("end", end=True)
    rows = []
    con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    con.text_factory = bytes
    try:
        # 发布者过滤下推到 SQL（user_name 列），否则「取 N 条候选再过滤」
        # 会让冷门好友每页只剩几条，用户要翻很多页。关键词仍需解析 XML。
        where, args = [], []
        if user:
            where.append("user_name = ?")
            args.append(user)
        if before:
            try:
                where.append("tid < ?")
                args.append(int(before))
            except ValueError:
                return jsonify({"error": "before_tid 无效"}), 400
        # 时间范围：tid 内含毫秒时间戳，直接下推成 tid 区间（比解析 XML 再过滤快得多）
        lo, hi = sns.ts_to_tid_bounds(start_ts, end_ts)
        if lo is not None and hi is not None and (lo >= 0) != (hi >= 0):
            lo = hi = None      # 跨 2004-11 的正负分界（SNS 不可能）→ 回退 Python 过滤
        if lo is not None:
            where.append("tid >= ?")
            args.append(lo)
        if hi is not None:
            where.append("tid <= ?")
            args.append(hi)
        sql = ("SELECT tid, user_name, content FROM SnsTimeLine"
               + (" WHERE " + " AND ".join(where) if where else ""))
        sql += " ORDER BY tid DESC"
        # 无关键词：直接 LIMIT，走主键索引。
        # 有关键词：关键字可能只出现在卡片字段/媒体描述里（contentDesc 常为空），
        # SQL 无法过滤，只能逐条解析；命中 limit 条即停，另设扫描上限兜底
        # （实测 0.19ms/条，5000 条约 1s，全库 5684 条）。
        if keyword:
            sql += " LIMIT ?"
            args.append(KEYWORD_MAX_SCAN)
        else:
            sql += " LIMIT ?"
            args.append(limit)
        scanned = 0
        for tid, who, content in con.execute(sql, args):
            scanned += 1
            if keyword and scanned > KEYWORD_MAX_SCAN:
                break
            ts_sec = sns.sns_id_to_seconds(tid)
            # 时间范围兜底（正常已下推到 SQL；仅正负分界那种异常区间才生效）
            if start_ts is not None and ts_sec < start_ts:
                continue
            if end_ts is not None and ts_sec > end_ts:
                continue
            feed = sns.parse_timeline(content)
            if feed is None:
                continue
            feed.update(tid=tid, ts_ms=sns.sns_id_to_ms(tid), ts=ts_sec,
                        user_name=(who or b"").decode("utf-8", "replace") if isinstance(who, bytes) else (who or ""))
            # 关键词匹配卡片文本（标题/歌手/视频号昵称/位置/媒体描述），
            # 不只看 contentDesc —— 实测卡片类动态的 contentDesc 常为空。
            if keyword and keyword not in sns.search_text(feed):
                continue
            rows.append(feed)
            if len(rows) >= limit:
                break
    finally:
        con.close()
    next_tid = rows[-1]["tid"] if rows else None
    return jsonify({"timeline": [_feed_json(x) for x in rows], "next_before_tid": next_tid,
                    "has_more": len(rows) >= limit})


@bp.get("/detail")
def detail():
    account = request.args.get("account", "")
    db = _sns_db(account)
    if db is None:
        return jsonify({"error": "账号或朋友圈数据库不存在"}), 404
    try:
        tid = int(request.args.get("tid", ""))
    except ValueError:
        return jsonify({"error": "tid 无效"}), 400
    con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    con.text_factory = bytes
    try:
        row = con.execute("SELECT tid,user_name,content FROM SnsTimeLine WHERE tid=?", (tid,)).fetchone()
    finally:
        con.close()
    if not row:
        return jsonify({"error": "动态不存在"}), 404
    feed = sns.parse_timeline(row[2])
    if feed is None:
        return jsonify({"error": "动态 XML 无法解析"}), 422
    feed.update(tid=tid, ts_ms=sns.sns_id_to_ms(tid), ts=sns.sns_id_to_seconds(tid),
                user_name=(row[1] or b"").decode("utf-8", "replace") if isinstance(row[1], bytes) else (row[1] or ""))
    return jsonify({"post": _feed_json(feed)})


@bp.get("/search")
def search():
    # 复用时间线过滤，限制返回规模，避免一次性读取过多 XML。
    return timeline()


@bp.get("/emoji")
def emoji():
    """按需获取评论表情（明文 url 优先，实测无需 AES 解密）。"""
    import json as _json
    raw = request.args.get("emoji", "")
    if not raw:
        return jsonify({"error": "缺少 emoji 参数"}), 400
    try:
        spec = _json.loads(raw)
    except (ValueError, TypeError):
        return jsonify({"error": "emoji 参数不是合法 JSON"}), 400
    if not isinstance(spec, dict) or not (spec.get("url") or spec.get("encrypt_url")):
        return jsonify({"error": "emoji 缺少 url"}), 400

    acc_dir = _account_dir(request.args.get("account", ""))
    cache = (acc_dir / "sns_media" / "emoji") if acc_dir else None
    r = sns_cdn.fetch_emoji(spec, cache_dir=cache)
    if not r["ok"]:
        return jsonify({"error": r["error"]}), 502
    return Response(r["data"], mimetype=r["mime"],
                    headers={"X-SIWX-SNS-Via": r["via"] or ""})


@bp.get("/friends")
def friends():
    """按发布者聚合（纯 SQL GROUP BY，实测 ~111ms vs 全量解析 1230ms）。

    可选解析联系人备注/昵称（复用 api_chat 的实现），并批量标记**是否有头像**
    （前端据此决定 `<img>` 还是首字母圆形占位，避免一堆 404）。

    排序在前端做：拼音排序依赖浏览器 `Intl.Collator(..., {collation:'pinyin'})`，
    Python 标准库没有拼音能力，而发布者只有百量级，前端排序没有性能问题。
    """
    account = request.args.get("account", "")
    db = _sns_db(account)
    if db is None:
        return jsonify({"error": "账号或朋友圈数据库不存在"}), 404
    try:
        limit = max(1, min(int(request.args.get("limit", 200)), 2000))
    except ValueError:
        limit = 200
    rows = sns.iter_authors(db, limit=limit)

    acc_dir = _account_dir(account)
    if request.args.get("names", "1") != "0" and acc_dir:
        try:
            from siwx.api_chat import _contact_names
            names = _contact_names(acc_dir)
            for r in rows:
                r["display"] = names.get(r["username"]) or r["username"]
        except Exception:  # noqa: BLE001  昵称解析失败不影响列表
            pass
    for r in rows:
        r.setdefault("display", r["username"])

    with_avatar = _avatar_usernames(acc_dir, [r["username"] for r in rows])
    for r in rows:
        r["has_avatar"] = r["username"] in with_avatar

    return jsonify({"friends": rows, "total": len(rows)})


@bp.get("/stats")
def stats():
    db = _sns_db(request.args.get("account", ""))
    if db is None:
        return jsonify({"error": "账号或朋友圈数据库不存在"}), 404
    return jsonify(sns.timeline_stats(db))


@bp.get("/formats")
def formats():
    from siwx import sns_export
    return jsonify({"formats": list(sns_export.FORMATS)})


@bp.post("/export")
def export():
    """启动朋友圈导出任务（异步，前端轮询 /api/job 获取进度）。

    改为走任务槽的原因：导出全量 + 媒体时可能持续数分钟，
    同步返回会一直占住 HTTP 请求，且无法展示进度、无法与其它任务互斥。
    """
    from siwx import sns_export
    from siwx import server as _server

    data = request.get_json(silent=True) or {}
    account = str(data.get("account") or "")
    db = _sns_db(account)
    if db is None:
        return jsonify({"error": "账号或朋友圈数据库不存在"}), 404

    fmt = str(data.get("format") or "json")
    if fmt not in sns_export.FORMATS:
        return jsonify({"error": f"不支持的格式: {fmt}"}), 400

    def _int_arg(key):
        v = data.get(key)
        try:
            return int(v) if v not in (None, "") else None
        except (TypeError, ValueError):
            return None

    opts = {
        "account": account,
        "format": fmt,
        "media": bool(data.get("media")),
        "images": bool(data.get("images", True)),
        "videos": bool(data.get("videos", True)),
        "livephotos": bool(data.get("livephotos", True)),
        "concurrency": sns_export.clamp_concurrency(data.get("concurrency")),
        "keyword": str(data.get("keyword") or "").strip() or None,
        "username": str(data.get("username") or "").strip() or None,
        "usernames": data.get("usernames") or None,
        "start": _int_arg("start"),
        "end": _int_arg("end"),
        "limit": _int_arg("limit"),
    }

    with _server._lock:
        if _server._job["running"]:
            return jsonify({"error": "已有任务在运行"}), 409
        _server._job.update({"running": True, "mode": "sns_export", "done": False,
                             "ok": False, "logs": [], "report": None})
    threading.Thread(
        target=_server._run_job,
        args=("sns_export", None, None, False, None, opts),
        daemon=True).start()
    return jsonify({"started": True})


@bp.get("/export/download")
def export_download():
    """下载导出产物（限制在 exports 根目录内）。"""
    raw = request.args.get("path", "")
    if not raw:
        return jsonify({"error": "缺少 path"}), 400
    root = paths.exports_root().resolve()
    target = Path(raw).resolve()
    try:
        target.relative_to(root)
    except ValueError:
        return jsonify({"error": "路径越界"}), 403
    if not target.is_file():
        return jsonify({"error": "文件不存在"}), 404
    from flask import send_file
    return send_file(target, as_attachment=True, download_name=target.name)


@bp.get("/media")
def media():
    """代理下载朋友圈媒体（图片/视频/实况），带磁盘缓存。

    只在微信 CDN 域名上取数：
    * XML 里的媒体一定是 ``*.qpic.cn`` / ``*.video.qq.com``；
    * 外链卡片（如 b23.tv）走这条路只会 400，而且会让接口变成任意 URL 代理。
    失败时返回 ``reason`` / ``hosts_tried``，前端据此提示而不是静默隐藏。
    """
    account = request.args.get("account", "")
    url = request.args.get("url", "")
    key = request.args.get("key")
    token = request.args.get("token")
    if not url:
        return jsonify({"error": "缺少 url"}), 400
    if not sns_cdn.is_wechat_cdn(url):
        _media_log.warning("[sns-media] 拒绝非微信 CDN 地址：%s",
                           sns_cdn.safe_url(url))
        return jsonify({"error": "只允许微信 CDN 地址（外链卡片不是可下载的媒体）",
                        "reason": "not-cdn"}), 400
    cache = None
    acc_dir = _account_dir(account)
    if acc_dir:
        cache = acc_dir / "sns_media"
    result = sns_cdn.fetch_media(url, key=key, token=token, cache_dir=cache)
    if not result["ok"]:
        # 失败原因由 sns_cdn 落 logs/siwx.log（含 reason / 试了几个域名 / HTTP 码）
        return jsonify({"error": result["error"], "status": result["status"],
                        "reason": result.get("reason"),
                        "hosts_tried": result.get("hosts_tried")}), result["status"] or 502
    data = sns_cdn.strip_wechat_tail(result["data"])
    return Response(data, mimetype=result["mime"], headers={
        "X-SIWX-SNS-Encrypted": "1" if result["encrypted"] else "0",
        "X-SIWX-SNS-Cached": "1" if result.get("cached") else "0",
        "Cache-Control": "private, max-age=86400",
    })
