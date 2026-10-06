"""MCP (Model Context Protocol) stdio 服务器。

把 stories-in-wx 的能力以 MCP 工具暴露给 AI 客户端
（Claude Desktop / ZCode 等任何支持 MCP 的宿主）。

传输: stdio, newline-delimited JSON-RPC 2.0（MCP 2024-11-05 规范）
启动: python run.py mcp
说明: 与 Web 控制台共用密钥库与解密产物，只读查询，不做解密操作。
工具开关配置: Windows=%LOCALAPPDATA%/stories-in-wx/mcp_config.json
             macOS=~/Library/Application Support/stories-in-wx/mcp_config.json
"""
import hashlib
import json
import logging
import os
import re
import sqlite3
import sys
import time
from logging.handlers import RotatingFileHandler
from pathlib import Path

from siwx import __version__, paths, sns
from siwx.api_chat import (
    _contact_names, _decode_content, build_messages, message_tables_by_shard,
)

SERVER_NAME = "stories-in-wx"
# 版本唯一来源是 siwx.__version__，避免与发布版本脱节
SERVER_VERSION = __version__
PROTOCOL_VERSION = "2024-11-05"
SCAN_CAP = 200_000          # 全库搜索最多扫描的行数
JSON = "application/json"

# ── MCP 专用日志（轮转文件 + 详细调用记录）──────────────────────

def _mcp_log_path() -> Path:
    # 跨平台数据目录（Windows=LOCALAPPDATA, macOS=~/Library/Application Support,
    # Linux=XDG）。旧实现兜底 "."，双击 .app 启动时 cwd 为只读的 /，
    # mkdir 直接 Errno 30 导致应用秒退 (issue #11)。
    return paths.data_dir() / "mcp.log"

def _setup_mcp_logger() -> logging.Logger:
    logger = logging.getLogger("siwx.mcp")
    if logger.handlers:
        return logger
    logger.setLevel(logging.DEBUG)
    try:
        p = _mcp_log_path()
        p.parent.mkdir(parents=True, exist_ok=True)
        fh = RotatingFileHandler(p, maxBytes=2 * 1024 * 1024, backupCount=3,
                                  encoding="utf-8")
        fh.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(message)s",
                                          datefmt="%Y-%m-%d %H:%M:%S"))
        logger.addHandler(fh)
    except OSError as e:
        # 日志文件不可用（只读卷/权限/磁盘满）绝不能阻塞应用启动：
        # 退化为无文件日志，仅保留控制台输出
        print(f"[mcp] MCP 日志文件初始化失败({e})，本轮无文件日志", file=sys.stderr)
        logger.addHandler(logging.NullHandler())
    return logger

mcp_log = _setup_mcp_logger()


# ── 配置（工具开关）─────────────────────────────────────────────

def config_path() -> Path:
    return paths.data_dir() / "mcp_config.json"


def load_config() -> dict:
    try:
        return json.loads(config_path().read_text(encoding="utf-8"))
    except Exception:
        return {}


def save_config(cfg: dict) -> None:
    p = config_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".tmp")
    tmp.write_text(json.dumps(cfg, ensure_ascii=False, indent=1), encoding="utf-8")
    os.replace(tmp, p)


def tool_enabled(name: str) -> bool:
    # "tools" 若被手改配置写成 null，.get("tools", {}) 拿到的是 None → 每次工具调用都会崩
    return bool((load_config().get("tools") or {}).get(name, True))


# ── 数据助手（与 api_chat 同源，独立于 flask）──────────────────

def _accounts() -> list:
    root = paths.out_root()
    out = []
    if root.is_dir():
        for d in sorted(root.iterdir()):
            if (d / "message").is_dir():
                out.append(d.name)
    return out


def _session_list(account: str) -> list:
    acc_dir = paths.out_root() / account
    if not (acc_dir / "message").is_dir():
        raise ValueError(f"账号不存在或未解密: {account}")
    names = _contact_names(acc_dir)
    items = {}
    sdb = acc_dir / "session" / "session.db"
    if sdb.is_file():
        conn = sqlite3.connect(sdb)
        try:
            for un, summary, ts in conn.execute(
                    "SELECT username, summary, sort_timestamp FROM SessionTable"):
                un = (un or "").strip()
                if un:
                    items[un] = {"summary": (summary or "").strip(), "ts": ts or 0}
        except sqlite3.Error:
            pass
        finally:
            conn.close()
    out = [{"username": un, "display": names.get(un) or un,
            "is_group": un.endswith("@chatroom"),
            "preview": (it["summary"] or "")[:80], "last_time": it["ts"]}
           for un, it in items.items()]
    out.sort(key=lambda x: x["last_time"], reverse=True)
    return out


def _json(obj) -> str:
    return json.dumps(obj, ensure_ascii=False, indent=1)


# ── 工具实现 ────────────────────────────────────────────────────

def tool_get_status(_args) -> str:
    from siwx import keystore
    accs = []
    for acc in _accounts():
        n = len(list((paths.out_root() / acc / "message").glob("message_*.db")))
        accs.append({"wxid": acc, "message_dbs": n})
    store = keystore.load()
    wechat_running = False
    try:
        from siwx.discover import find_wechat_pids
        wechat_running = bool(find_wechat_pids())
    except Exception:
        pass
    return _json({"wechat_running": wechat_running,
                  "accounts": accs, "keystore_salts": len(store)})


def tool_list_accounts(_args) -> str:
    return _json({"accounts": _accounts()})


def tool_list_sessions(args) -> str:
    account = (args or {}).get("account", "")
    limit = min(int((args or {}).get("limit", 100) or 100), 500)
    ss = _session_list(account)
    return _json({"account": account, "total": len(ss),
                  "sessions": ss[:limit]})


def tool_get_messages(args) -> str:
    account = args["account"]
    chat = args["chat"]
    limit = min(int(args.get("limit", 100) or 100), 500)
    acc_dir = paths.out_root() / account
    if not (acc_dir / "message").is_dir():
        raise ValueError(f"账号不存在或未解密: {account}")
    msgs = build_messages(acc_dir, chat, account=account)
    page = msgs[-limit:] if len(msgs) > limit else msgs
    slim = [{"ts": m["createTime"], "sender": m["senderDisplayName"],
             "is_me": bool(m["isSend"]), "type": m["typeName"],
             "content": m["content"]} for m in page]
    return _json({"chat": chat, "total": len(msgs), "returned": len(slim),
                  "note": "ts 为 Unix 秒; 时间正序; rawContent 未包含以节省 token",
                  "messages": slim})


def tool_search_messages(args) -> str:
    account = args["account"]
    kw = args["keyword"]
    chat = args.get("chat") or None
    limit = min(int(args.get("limit", 30) or 30), 100)
    acc_dir = paths.out_root() / account
    if not (acc_dir / "message").is_dir():
        raise ValueError(f"账号不存在或未解密: {account}")
    kw_l = kw.lower()

    if chat:
        msgs = build_messages(acc_dir, chat, account=account)
        hits = [{"chat": chat, "ts": m["createTime"],
                 "sender": m["senderDisplayName"], "type": m["typeName"],
                 "content": (m["content"] or "")[:300]}
                for m in msgs
                if kw_l in (m.get("content") or "").lower()
                or kw_l in (m.get("rawContent") or "").lower()]
        return _json({"scope": chat, "scanned": len(msgs), "matches": hits[:limit]})

    # 全库搜索：建 md5(chat) → chat 反查表
    names = _contact_names(acc_dir)
    cands = set(names)
    try:
        for s in _session_list(account):
            cands.add(s["username"])
    except ValueError:
        pass
    table_map = {"Msg_" + hashlib.md5(un.encode()).hexdigest(): un for un in cands}

    hits, scanned = [], 0
    # 分片索引：跳过不含 Msg_ 表的库（media_*/fts/resource/weclaw 等）。
    # smap 是分片级的，原来写在表循环内部 —— biz_message_0.db 有 67 张表，
    # 等于把 Name2Id 重查了 67 次。此处提到分片循环外。
    for db, tables in message_tables_by_shard(acc_dir).items():
        if len(hits) >= limit or scanned >= SCAN_CAP:
            break
        conn = sqlite3.connect(db)
        try:
            smap = {}
            try:
                smap = {rid: un for rid, un in
                        conn.execute("SELECT rowid, user_name FROM Name2Id")}
            except sqlite3.Error:
                pass
            for t in tables:
                if len(hits) >= limit or scanned >= SCAN_CAP:
                    break
                chat_name = table_map.get(t, "")
                for _lid, _sid, ltype, ts, rsid, content in conn.execute(
                        f"SELECT local_id, server_id, local_type, create_time, "
                        f"real_sender_id, message_content FROM [{t}] "
                        f"ORDER BY create_time"):
                    scanned += 1
                    if scanned >= SCAN_CAP or len(hits) >= limit:
                        break
                    text = _decode_content(content)
                    if not text or kw_l not in text.lower():
                        continue
                    sender = smap.get(int(rsid), "") if rsid else ""
                    hits.append({"chat": chat_name, "ts": ts or 0,
                                 "sender": sender, "type": ltype & 0xFFFF,
                                 "content": text[:300]})
        except sqlite3.Error:
            pass
        finally:
            conn.close()
    return _json({"scope": "全部会话", "scanned": scanned, "matches": hits[:limit],
                  "note": f"扫描上限 {SCAN_CAP} 行，命中即停"})


def tool_export_chat(args) -> str:
    from siwx import exporter
    account = args["account"]
    chat = args["chat"]
    fmt = args.get("format", "json")
    acc_dir = paths.out_root() / account
    if not (acc_dir / "message").is_dir():
        raise ValueError(f"账号不存在或未解密: {account}")
    res = exporter.run_export(acc_dir, account, chat, "", fmt,
                              want_messages=True,
                              want_media=bool(args.get("media", False)),
                              want_voice=bool(args.get("voice", False)),
                              want_avatars=bool(args.get("avatars", False)),
                              export_root=paths.exports_root(),
                              pack=args.get("pack", "single"))
    return _json(res)


# ── 朋友圈（SNS）工具实现 ────────────────────────────────────────
#
# 与 api_sns.py 同源但独立于 flask：直接复用 sns.py 的非 flask 函数
# （parse_ts_arg / ts_to_tid_bounds / parse_timeline / search_text /
#  public_card / iter_authors / timeline_stats）。
#
# 设计红线（与聊天工具一致）：AI 客户端拿不到 CDN 媒体（MCP 返回纯文本），
# 所以**所有形状都去掉 URL** —— 长 token 对 AI 只是无意义噪音。

SNS_ACCOUNT_RE = re.compile(r"^[A-Za-z0-9_.@-]+$")
SNS_KEYWORD_SCAN = 5000     # 关键词搜索最多解析的 XML 条数（实测 0.19ms/条，全库 5684 条约 1s）


def _sns_db(account: str) -> Path:
    """校验账号名并返回其 sns.db 路径；无效抛 ValueError。

    与 api_sns._account_dir 同一条正则：account 只作为 output/<account>/
    的单个路径组件使用，`../..` / `a\\b` 一律拒绝（路径穿越防护）。
    """
    if not account or not SNS_ACCOUNT_RE.fullmatch(account):
        raise ValueError(f"账号名非法: {account!r}")
    db = paths.out_root() / account / "sns" / "sns.db"
    if not db.is_file():
        raise ValueError(f"账号不存在或没有朋友圈数据库: {account}")
    return db


def _sns_ts(args: dict, key: str, end: bool = False):
    """解析 start / end 参数（unix 秒或 YYYY-MM-DD）；end 传纯日期补到当日 23:59:59，
    否则「今天到某天」会少掉一整天（与 api_sns._ts_arg 同规则）。"""
    v = args.get(key)
    if v in (None, ""):
        return None
    ts = sns.parse_ts_arg(v)
    if ts is None:
        return None
    if end and isinstance(v, str) and re.fullmatch(r"\s*\d{4}[-/]\d{1,2}[-/]\d{1,2}\s*", v):
        ts += 86399
    return ts


def _slim_card(card: dict | None) -> dict | None:
    """public_card 统一形状再去掉 CDN URL（AI 取不了媒体，URL 只浪费 token）。"""
    out = sns.public_card(card)
    if not out:
        return None
    for k in ("url", "cover"):
        out.pop(k, None)
    f = out.get("finder")
    if f:
        for k in ("avatar", "video_url", "media"):
            f.pop(k, None)
    return out


def _slim_post(feed: dict) -> dict:
    """feed → 精简形状（tid/ts/正文/卡片/位置/互动计数）。

    评论与点赞正文**不在此返回**（timeline 一页 20 条 × 全量评论会淹没 token），
    完整互动见 get_sns_detail。
    """
    medias = feed.get("medias") or []
    loc = feed.get("location")
    return {
        "tid": feed.get("tid"),
        "ts": feed.get("ts"),
        "user_name": feed.get("user_name"),
        "kind": feed.get("content_kind"),
        "content_desc": feed.get("content_desc") or "",
        "card": _slim_card(feed.get("card")),
        "media_count": len(medias),
        "has_live_photo": any(m.get("live_photo") for m in medias),
        "location": (loc.get("name") or loc.get("address") or "") if loc else "",
        "like_count": len(feed.get("likes") or []),
        "comment_count": len(feed.get("comments") or []),
    }


def _detail_post(feed: dict) -> dict:
    """feed → 完整详情（评论带正文，点赞只列人；URL 同样省略）。"""
    out = _slim_post(feed)
    out["comments"] = [
        {"username": c.get("username"), "nickname": c.get("nickname"),
         "content": c.get("content") or "", "ts": c.get("create_time"),
         "reply_to": c.get("ref_username") or "",
         "has_emoji": bool(c.get("emojis")),
         "image_count": len(c.get("images") or [])}
        for c in (feed.get("comments") or [])]
    out["likes"] = [l.get("nickname") or l.get("username") or ""
                    for l in (feed.get("likes") or [])]
    return out


def tool_list_sns_accounts(_args) -> str:
    out = []
    root = paths.out_root()
    if root.is_dir():
        for d in sorted(root.iterdir()):
            db = d / "sns" / "sns.db"
            if db.is_file():
                try:
                    st = sns.timeline_stats(db)
                except Exception:
                    st = {"count": 0}
                out.append({"wxid": d.name, **st})
    return _json({"accounts": out,
                  "note": "count 为动态总数; newest 为最新动态 unix 秒"})


def tool_get_sns_timeline(args) -> str:
    account = args.get("account", "")
    db = _sns_db(account)
    limit = min(int(args.get("limit", 20) or 20), 100)
    before = args.get("before_tid")
    keyword = (str(args.get("keyword") or "")).strip().lower()
    user = (str(args.get("username") or "")).strip()
    start_ts = _sns_ts(args, "start")
    end_ts = _sns_ts(args, "end", end=True)

    # 发布者与时间范围下推 SQL（tid 内含毫秒时间戳，见 sns.ts_to_tid_bounds）；
    # 关键词需逐条解析 XML，命中 limit 即停 + 扫描上限兜底（与 api_sns.timeline 同策略）。
    where, qargs = [], []
    if user:
        where.append("user_name = ?")
        qargs.append(user)
    if before not in (None, ""):
        try:
            where.append("tid < ?")
            qargs.append(int(before))
        except (TypeError, ValueError):
            raise ValueError("before_tid 无效（应为上次返回的 tid 整数）")
    lo, hi = sns.ts_to_tid_bounds(start_ts, end_ts)
    if lo is not None and hi is not None and (lo >= 0) != (hi >= 0):
        lo = hi = None          # 跨 2004-11 正负分界（SNS 不可能）→ 回退 Python 过滤
    if lo is not None:
        where.append("tid >= ?")
        qargs.append(lo)
    if hi is not None:
        where.append("tid <= ?")
        qargs.append(hi)
    sql = ("SELECT tid, user_name, content FROM SnsTimeLine"
           + (" WHERE " + " AND ".join(where) if where else "")
           + " ORDER BY tid DESC LIMIT ?")
    qargs.append(SNS_KEYWORD_SCAN if keyword else limit)

    con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    con.text_factory = bytes
    rows, scanned = [], 0
    try:
        for tid, who, content in con.execute(sql, qargs):
            scanned += 1
            if keyword and scanned > SNS_KEYWORD_SCAN:
                break
            feed = sns.parse_timeline(content)
            if feed is None:
                continue
            ts_sec = sns.sns_id_to_seconds(tid)
            if start_ts is not None and ts_sec < start_ts:
                continue
            if end_ts is not None and ts_sec > end_ts:
                continue
            feed.update(tid=tid, ts=ts_sec,
                        user_name=(who or b"").decode("utf-8", "replace")
                        if isinstance(who, bytes) else (who or ""))
            if keyword and keyword not in sns.search_text(feed):
                continue
            rows.append(_slim_post(feed))
            if len(rows) >= limit:
                break
    finally:
        con.close()
    return _json({
        "account": account, "total": len(rows), "scanned": scanned,
        "next_before_tid": rows[-1]["tid"] if rows else None,
        "has_more": len(rows) >= limit,
        "note": "tid 可作 before_tid 游标加载更早; SQLite 中 tid 为有符号 int64，可能是负数，原样传回即可",
        "posts": rows,
    })


def tool_get_sns_detail(args) -> str:
    account = args.get("account", "")
    db = _sns_db(account)
    try:
        tid = int(args.get("tid", ""))
    except (TypeError, ValueError):
        raise ValueError("tid 无效（应为整数，来自 get_sns_timeline）")
    con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    con.text_factory = bytes
    try:
        row = con.execute("SELECT tid, user_name, content FROM SnsTimeLine WHERE tid=?",
                          (tid,)).fetchone()
    finally:
        con.close()
    if not row:
        raise ValueError(f"动态不存在: {tid}")
    feed = sns.parse_timeline(row[2])
    if feed is None:
        raise ValueError(f"动态 XML 无法解析: {tid}")
    feed.update(tid=tid, ts=sns.sns_id_to_seconds(tid),
                user_name=(row[1] or b"").decode("utf-8", "replace")
                if isinstance(row[1], bytes) else (row[1] or ""))
    return _json(_detail_post(feed))


def tool_get_sns_friends(args) -> str:
    account = args.get("account", "")
    db = _sns_db(account)
    limit = min(int(args.get("limit", 200) or 200), 2000)
    rows = sns.iter_authors(db, limit=limit)
    # 备注解析失败不影响列表（与 api_sns.friends 一致）
    try:
        names = _contact_names(paths.out_root() / account)
        for r in rows:
            r["display"] = names.get(r["username"]) or r["username"]
    except Exception:  # noqa: BLE001
        pass
    for r in rows:
        r.setdefault("display", r["username"])
    return _json({"account": account, "total": len(rows),
                  "note": "count 为该发布者的动态数; username 可用于 get_sns_timeline 的 username 过滤",
                  "friends": rows})


def tool_export_sns(args) -> str:
    from siwx import sns_export
    account = args.get("account", "")
    db = _sns_db(account)
    fmt = args.get("format", "json")
    if fmt not in sns_export.FORMATS:
        raise ValueError(f"不支持的格式: {fmt}（可用: {'/'.join(sns_export.FORMATS)}）")
    usernames = args.get("username") or args.get("usernames")
    if isinstance(usernames, str):
        usernames = [usernames]
    limit = args.get("limit")
    try:
        limit = int(limit) if limit not in (None, "") else None
    except (TypeError, ValueError):
        limit = None
    keyword = str(args.get("keyword") or "").strip() or None
    res = sns_export.run_sns_export(
        db, account, fmt=fmt,
        export_root=paths.exports_root(),
        usernames=usernames,
        start=_sns_ts(args, "start"),
        end=_sns_ts(args, "end", end=True),
        want_media=bool(args.get("media", False)),
        limit=limit,
        keyword=keyword,
    )
    return _json(res)


TOOLS = [
    {"name": "get_status",
     "description": "获取运行状态：微信是否在线、已解密账号列表、密钥库条数",
     "inputSchema": {"type": "object", "properties": {}}},
    {"name": "list_accounts",
     "description": "列出已解密的账号 wxid 列表",
     "inputSchema": {"type": "object", "properties": {}}},
    {"name": "list_sessions",
     "description": "列出某账号的全部会话（聊天列表），含最后消息预览与时间",
     "inputSchema": {"type": "object", "required": ["account"],
                     "properties": {
                         "account": {"type": "string", "description": "账号 wxid"},
                         "limit": {"type": "integer", "description": "返回条数上限，默认 100"}}}},
    {"name": "get_messages",
     "description": "读取某会话的消息（最新 N 条，按时间正序返回）",
     "inputSchema": {"type": "object", "required": ["account", "chat"],
                     "properties": {
                         "account": {"type": "string", "description": "账号 wxid"},
                         "chat": {"type": "string", "description": "会话 username，来自 list_sessions"},
                         "limit": {"type": "integer", "description": "条数上限，默认 100"}}}},
    {"name": "search_messages",
     "description": "按关键词搜索消息；指定 chat 只搜该会话（快），不指定则全库扫描",
     "inputSchema": {"type": "object", "required": ["account", "keyword"],
                     "properties": {
                         "account": {"type": "string"},
                         "keyword": {"type": "string", "description": "搜索关键词"},
                         "chat": {"type": "string", "description": "可选，限定会话"},
                         "limit": {"type": "integer", "description": "命中条数上限，默认 30"}}}},
    {"name": "export_chat",
     "description": "导出某会话聊天记录到文件（json/html/txt/csv/markdown/toml/sqlite/xlsx），返回路径",
     "inputSchema": {"type": "object", "required": ["account", "chat"],
                     "properties": {
                         "account": {"type": "string"},
                         "chat": {"type": "string"},
                         "format": {"type": "string", "description": "默认 json"},
                         "media": {"type": "boolean", "description": "是否解密图片，默认 false"},
                         "voice": {"type": "boolean", "description": "是否导出语音，默认 false"},
                         "avatars": {"type": "boolean", "description": "是否提取头像，默认 false"}}}},
    {"name": "list_sns_accounts",
     "description": "列出有朋友圈数据的账号（动态总数、最新动态时间）",
     "inputSchema": {"type": "object", "properties": {}}},
    {"name": "get_sns_timeline",
     "description": "读取朋友圈时间线（最新 N 条，支持关键词/发布者/时间范围过滤与游标分页）",
     "inputSchema": {"type": "object", "required": ["account"],
                     "properties": {
                         "account": {"type": "string", "description": "账号 wxid"},
                         "limit": {"type": "integer", "description": "条数上限，默认 20，最大 100"},
                         "before_tid": {"type": "integer", "description": "上一页最后一条的 tid，加载更早的动态"},
                         "keyword": {"type": "string", "description": "关键词（匹配正文/卡片标题/歌手/视频号昵称/位置/媒体描述）"},
                         "username": {"type": "string", "description": "只看某位好友的动态（username 来自 get_sns_friends）"},
                         "start": {"type": "string", "description": "开始时间（unix 秒或 YYYY-MM-DD）"},
                         "end": {"type": "string", "description": "结束时间（unix 秒或 YYYY-MM-DD，纯日期补到当日末）"}}}},
    {"name": "get_sns_detail",
     "description": "读取单条朋友圈动态的完整详情（不截断评论；点赞/评论/表情齐全）",
     "inputSchema": {"type": "object", "required": ["account", "tid"],
                     "properties": {
                         "account": {"type": "string", "description": "账号 wxid"},
                         "tid": {"type": "integer", "description": "动态 tid，来自 get_sns_timeline"}}}},
    {"name": "get_sns_friends",
     "description": "按发布者聚合朋友圈动态（谁发了多少条，按数量降序，含备注昵称）",
     "inputSchema": {"type": "object", "required": ["account"],
                     "properties": {
                         "account": {"type": "string", "description": "账号 wxid"},
                         "limit": {"type": "integer", "description": "条数上限，默认 200，最大 2000"}}}},
    {"name": "export_sns",
     "description": "导出朋友圈到文件（json/markdown/txt/html），可选一并从 CDN 下载媒体，返回路径",
     "inputSchema": {"type": "object", "required": ["account"],
                     "properties": {
                         "account": {"type": "string", "description": "账号 wxid"},
                         "format": {"type": "string", "description": "json/markdown/txt/html，默认 json"},
                         "keyword": {"type": "string", "description": "可选过滤关键词"},
                         "username": {"type": "string", "description": "可选过滤发布者"},
                         "start": {"type": "string", "description": "开始时间（unix 秒或 YYYY-MM-DD）"},
                         "end": {"type": "string", "description": "结束时间（unix 秒或 YYYY-MM-DD）"},
                         "limit": {"type": "integer", "description": "最多导出条数"},
                         "media": {"type": "boolean", "description": "是否从 CDN 下载媒体（较慢），默认 false"}}}},
]

_HANDLERS = {
    "get_status": tool_get_status,
    "list_accounts": tool_list_accounts,
    "list_sessions": tool_list_sessions,
    "get_messages": tool_get_messages,
    "search_messages": tool_search_messages,
    "export_chat": tool_export_chat,
    "list_sns_accounts": tool_list_sns_accounts,
    "get_sns_timeline": tool_get_sns_timeline,
    "get_sns_detail": tool_get_sns_detail,
    "get_sns_friends": tool_get_sns_friends,
    "export_sns": tool_export_sns,
}


# ── 插件工具（内置优先；同名插件的跳过）─────────────────────────

def _plugin_tools() -> list:
    """插件贡献的 MCP 工具声明（已过滤与内置重名者）。"""
    try:
        from siwx.plugins import ensure_loaded, registry
        ensure_loaded()
    except Exception:
        return []
    builtin = {t["name"] for t in TOOLS}
    out = []
    for _i, t in registry.mcp_tools.sorted_items():
        if t.name in builtin:
            continue
        out.append({
            "name": t.name,
            "description": t.description or f"[插件 {t.meta.name if t.meta else '?'}]",
            "inputSchema": t.input_schema or {"type": "object", "properties": {}},
        })
    return out


def _plugin_tool_handler(name: str):
    """按名字取插件 MCP 工具处理函数；不存在返回 None。"""
    try:
        from siwx.plugins import ensure_loaded, registry
        ensure_loaded()
    except Exception:
        return None
    for _i, t in registry.mcp_tools.sorted_items():
        if t.name == name:
            return t.handler
    return None


def _all_tools() -> list:
    """内置 + 插件工具（插件追加在内置之后）。"""
    return TOOLS + _plugin_tools()


# ── JSON-RPC 主循环 ─────────────────────────────────────────────

def _tool_call(name: str, args: dict) -> str:
    if not tool_enabled(name):
        raise ValueError(f"工具 {name} 已在 MCP 配置页禁用")
    h = _HANDLERS.get(name)
    is_plugin = False
    if not h:
        h = _plugin_tool_handler(name)
        is_plugin = h is not None
    if not h:
        raise ValueError(f"未知工具: {name}")
    mcp_log.info("调用 %s args=%s", name, json.dumps(args, ensure_ascii=False)[:500])
    t0 = time.time()
    try:
        result = h(args or {})
        # 插件工具可以返回 dict（MCP content 结构）或 str，统一成字符串
        if isinstance(result, dict):
            content = result.get("content")
            if isinstance(content, list):
                parts = [c.get("text", "") for c in content
                         if isinstance(c, dict) and c.get("type") == "text"]
                result = "\n".join(parts) if parts else json.dumps(
                    result, ensure_ascii=False)
            else:
                result = json.dumps(result, ensure_ascii=False)
        result = str(result)
        dt = time.time() - t0
        mcp_log.info("完成 %s 耗时 %.2fs 返回 %d 字符", name, dt, len(result))
        return result
    except Exception as e:
        dt = time.time() - t0
        tag = "插件工具 " if is_plugin else ""
        mcp_log.error("失败 %s%s 耗时 %.2fs: %s", tag, name, dt, e)
        raise


def run_mcp_server() -> None:
    # Windows 管道默认 GBK，MCP 协议要求 UTF-8
    try:
        sys.stdin.reconfigure(encoding="utf-8")
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass

    def _send(obj) -> None:
        sys.stdout.write(json.dumps(obj, ensure_ascii=False) + "\n")
        sys.stdout.flush()

    def _reply(msg, result=None, error=None) -> None:
        if "id" not in msg:      # notification，不回复
            return
        resp = {"jsonrpc": "2.0", "id": msg["id"]}
        if error is not None:
            resp["error"] = error
        else:
            resp["result"] = result
        _send(resp)

    print(f"[mcp] {SERVER_NAME} {SERVER_VERSION} ready (stdio)",
          file=sys.stderr, flush=True)

    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(msg, dict):
            continue
        method = msg.get("method", "")
        try:
            if method == "initialize":
                pv = (msg.get("params") or {}).get("protocolVersion")
                _reply(msg, result={
                    "protocolVersion": pv if isinstance(pv, str) else PROTOCOL_VERSION,
                    "capabilities": {"tools": {}},
                    "serverInfo": {"name": SERVER_NAME, "version": SERVER_VERSION},
                })
            elif method == "ping":
                _reply(msg, result={})
            elif method == "tools/list":
                _reply(msg, result={"tools": [t for t in _all_tools()
                                              if tool_enabled(t["name"])]})
            elif method == "tools/call":
                params = msg.get("params") or {}
                name = params.get("name", "")
                args = params.get("arguments") or {}
                try:
                    out = _tool_call(name, args)
                    _reply(msg, result={"content": [{"type": "text", "text": out}]})
                except Exception as e:
                    _reply(msg, result={"content": [{"type": "text",
                                                     "text": f"错误: {e}"}],
                                        "isError": True})
            elif method.startswith("notifications/"):
                pass                       # 客户端通知，忽略
            else:
                _reply(msg, error={"code": -32601,
                                   "message": f"未知方法: {method}"})
        except Exception as e:
            _reply(msg, error={"code": -32603, "message": str(e)})


if __name__ == "__main__":
    run_mcp_server()
