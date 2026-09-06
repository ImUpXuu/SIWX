"""聊天查看 API —— 读取解密产物（message_*.db / contact.db / session.db）。

模块化设计：Blueprint 独立于 server，可整体挪走或替换。
v1 能力：会话列表（session.db ∪ Msg_ 表统计）、消息分页、
zstd 解压、类型映射、发送者解析（前缀 / real_sender_id / origin_source）。
后续富文本（图片/语音实体）在此扩展。
"""
import hashlib
import re
import sqlite3
from pathlib import Path

from flask import Blueprint, Response, jsonify, request

from siwx import media

try:
    import zstandard as _zstd
    _ZCTX = _zstd.ZstdDecompressor()
except ImportError:
    _ZCTX = None

bp = Blueprint("chat_api", __name__, url_prefix="/api/chat")

ZSTD_MAGIC = b"\x28\xb5\x2f\xfd"
KIND_MAP = {3: "image", 6: "file", 34: "voice", 42: "card", 43: "video",
            47: "sticker", 48: "location", 50: "call"}
FALLBACK_LABEL = {3: "[图片]", 6: "[文件]", 34: "[语音]", 42: "[名片]",
                  43: "[视频]", 47: "[动画表情]", 48: "[位置]", 50: "[通话]"}
SENDER_PREFIX_RE = re.compile(r"^([a-zA-Z0-9_\-]+):\n")


def _out_root() -> Path:
    return Path.cwd() / "output"


def _accounts() -> list:
    root = _out_root()
    out = []
    if root.is_dir():
        for d in sorted(root.iterdir()):
            if (d / "message").is_dir():
                out.append(d.name)
    return out


def _contact_names(acc_dir: Path) -> dict:
    p = acc_dir / "contact" / "contact.db"
    names = {}
    if not p.is_file():
        return names
    conn = sqlite3.connect(p)
    try:
        for un, remark, nick, alias in conn.execute(
                "SELECT username, remark, nick_name, alias FROM contact"):
            un = (un or "").strip()
            if not un:
                continue
            best = un
            for v in (remark, nick, alias):
                v = (v or "").strip()
                if v and v != un and "\ufffd" not in v:
                    best = v
                    break
            names[un] = best
    finally:
        conn.close()
    return names


def _decode_content(content) -> str:
    """zstd 魔数解压 + UTF-8 解码；失败返回空串。"""
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    raw = bytes(content)
    if raw[:4] == ZSTD_MAGIC and _ZCTX is not None:
        try:
            raw = _ZCTX.decompressobj().decompress(raw)
        except Exception:
            return ""
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError:
        return ""


def _fmt(ltype: int, text: str) -> str:
    t = ltype & 0xFFFF
    if t == 1:
        return text
    if t == 57:
        # 引用消息：用户回复文本在 <title>，被引用部分由 quote 字段单独承载
        m = re.search(r"<title>(.*?)</title>", text, re.S)
        return m.group(1).strip() if m else (text or "[引用]")
    if t in (10000, 10002):
        return text or "[系统消息]"
    if t == 49:
        m = re.search(r"<title>(.*?)</title>", text, re.S)
        return f"[链接] {m.group(1).strip()}" if m else "[链接]"
    return FALLBACK_LABEL.get(t, f"[类型{t}]")


def _sender_map(conn) -> dict:
    """real_sender_id(rowid) → wxid（分片内有效）。"""
    try:
        return {rid: un for rid, un in conn.execute("SELECT rowid, user_name FROM Name2Id")}
    except sqlite3.Error:
        return {}


@bp.get("/accounts")
def accounts():
    out = []
    for acc in _accounts():
        d = _out_root() / acc
        sessions = 0
        sdb = d / "session" / "session.db"
        if sdb.is_file():
            try:
                conn = sqlite3.connect(sdb)
                sessions = conn.execute("SELECT COUNT(*) FROM SessionTable").fetchone()[0]
                conn.close()
            except sqlite3.Error:
                pass
        out.append({"wxid": acc, "sessions": sessions})
    return jsonify({"accounts": out})


@bp.get("/sessions")
def sessions():
    account = request.args.get("account", "")
    acc = _out_root() / account
    if not (acc / "message").is_dir():
        return jsonify({"error": "账号不存在或未解密"}), 404

    names = _contact_names(acc)
    items = {}

    # session.db：会话摘要与排序时间
    sdb = acc / "session" / "session.db"
    if sdb.is_file():
        conn = sqlite3.connect(sdb)
        try:
            for un, summary, ts in conn.execute(
                    "SELECT username, summary, sort_timestamp FROM SessionTable"):
                un = (un or "").strip()
                if un:
                    items[un] = {"username": un, "summary": (summary or "").strip(),
                                 "last_time": ts or 0, "msg_count": 0}
        except sqlite3.Error:
            pass
        conn.close()

    # message_*.db：聚合 Msg_ 表（count / last_time），Name2Id 反查 username
    for db in sorted((acc / "message").glob("message_*.db")):
        conn = sqlite3.connect(db)
        try:
            id_map = {}
            for (un,) in conn.execute("SELECT user_name FROM Name2Id"):
                if un:
                    id_map[hashlib.md5(un.encode()).hexdigest()] = un
            tables = [r[0] for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name LIKE 'Msg_%'")]
            for t in tables:
                un = id_map.get(t[4:])
                if not un:
                    continue
                row = conn.execute(
                    f"SELECT COUNT(*), MAX(create_time) FROM [{t}]").fetchone()
                cnt, last = row[0] or 0, row[1] or 0
                it = items.setdefault(un, {"username": un, "summary": "",
                                           "last_time": 0, "msg_count": 0})
                it["msg_count"] += cnt
                it["last_time"] = max(it["last_time"], last or 0)
        except sqlite3.Error:
            pass
        finally:
            conn.close()

    out = []
    for it in items.values():
        if it["msg_count"] == 0 and not it["summary"]:
            continue
        un = it["username"]
        display = names.get(un) or (it["summary"].split(":")[0].strip()
                                    if ":" in it["summary"] else "") or un
        out.append({
            "username": un,
            "display": display,
            "is_group": un.endswith("@chatroom"),
            "preview": (it["summary"] or "")[:60],
            "last_time": it["last_time"],
            "msg_count": it["msg_count"],
        })
    out.sort(key=lambda x: x["last_time"], reverse=True)
    return jsonify({"account": account, "sessions": out})


TYPE_NAMES = {1: "文本消息", 3: "图片消息", 34: "语音消息", 42: "名片消息",
              43: "视频消息", 47: "动画表情", 48: "位置消息", 49: "链接/文件",
              50: "通话消息", 51: "状态消息", 57: "引用消息", 10000: "系统消息",
              10002: "系统消息"}


def _xml_text(s):
    """剥掉 CDATA 包装并清理转义。"""
    if s is None:
        return None
    s = re.sub(r"<!\[CDATA\[(.*?)\]\]>", r"", s, flags=re.S).strip()
    return s or None


def _parse_appmsg(text: str):
    """type 49/57 公共字段：title/url/des。"""
    def g(tag):
        m = re.search(rf"<{tag}>(.*?)</{tag}>", text, re.S)
        return _xml_text(m.group(1)) if m else None
    return g("title"), g("url"), g("des")


def _parse_refer(text: str):
    """type 57 引用：refermsg → {displayname, content, ts}。"""
    m = re.search(r"<refermsg>(.*?)</refermsg>", text, re.S)
    if not m:
        return None
    blk = m.group(1)
    def g(tag):
        mm = re.search(rf"<{tag}>(.*?)</{tag}>", blk, re.S)
        return mm.group(1).strip() if mm else ""
    dn = _xml_text(g("displayname"))
    content = _xml_text(g("content"))
    # 被引用内容本身可能是 XML（图片/链接）→ 归纳为可读文本
    inner = re.search(r"<title>(.*?)</title>", content, re.S)
    if inner:
        content = inner.group(1)
    if re.search(r"type=\"?3\"?", blk) or "<img" in content:
        content = content if content and not content.startswith("<?xml") else "[图片]"
    try:
        ts = int(g("createtime") or 0)
    except ValueError:
        ts = 0
    return {"displayname": dn, "content": content[:500], "ts": ts}


def build_messages(acc: Path, chat: str, start_ts=None, end_ts=None,
                   account: str = None):
    """读取一个会话的全部消息并富化（导出与 API 共用）。

    account: 数据库归属账号（wxid），用于判定 is_me；None 时从 acc 推断。
    返回 dict 列表，字段同时服务前端（id/ts/kind/...）与导出（CipherTalk 风格）。
    """
    account = account or acc.name
    table = "Msg_" + hashlib.md5(chat.encode()).hexdigest()
    names = _contact_names(acc)
    my_base = account.split("_6")[0] if "_6" in account else account
    is_group = chat.endswith("@chatroom")

    rows = []
    for db in sorted((acc / "message").glob("message_*.db")):
        conn = sqlite3.connect(db)
        try:
            exists = conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name=?",
                (table,)).fetchone()
            if not exists:
                continue
            smap = _sender_map(conn)
            for r in conn.execute(
                    f"SELECT local_id, server_id, local_type, create_time, "
                    f"origin_source, real_sender_id, message_content, "
                    f"packed_info_data FROM [{table}] ORDER BY create_time"):
                rows.append(r + (smap,))
        except sqlite3.Error:
            pass
        finally:
            conn.close()

    rows.sort(key=lambda r: r[3] or 0)
    msgs = []
    for local_id, server_id, ltype, ts, origin, rsid, content, packed, smap in rows:
        if start_ts and (ts or 0) < start_ts:
            continue
        if end_ts and (ts or 0) > end_ts:
            continue
        text = _decode_content(content)
        raw_text = text
        sender_wxid = ""
        m = SENDER_PREFIX_RE.match(text[:100]) if text else None
        if m and (m.group(1).startswith("wxid_") or m.group(1).startswith("gh_")
                  or m.group(1).endswith("@chatroom")):
            sender_wxid = m.group(1)
            text = text[m.end():]
        if not sender_wxid and rsid:
            sender_wxid = smap.get(int(rsid), "")
        if not is_group:
            if sender_wxid and sender_wxid != chat:
                pass
            elif origin == 1:
                sender_wxid = my_base
            else:
                sender_wxid = chat
        if is_group and not sender_wxid and origin == 1:
            sender_wxid = my_base
        is_me = (sender_wxid == my_base or sender_wxid == account
                 or (not is_group and sender_wxid == my_base))
        if is_group and not is_me and sender_wxid == chat:
            sender_wxid = ""

        t = ltype & 0xFFFF
        md5 = media.extract_md5_from_xml(text) if t in (3, 47) else None
        bubble_md5 = None
        if t in (3, 47) and packed:
            m2 = re.search(rb"[0-9a-f]{32}", bytes(packed))
            bubble_md5 = m2.group().decode() if m2 else None

        quote = link = None
        if t == 57:
            quote = _parse_refer(text)
        if t == 49:
            title, url, des = _parse_appmsg(text)
            if title or url:
                link = {"title": title or "链接", "url": url, "desc": des}

        kind = KIND_MAP.get(t, "text")
        if t == 57:
            kind = "quote"
        elif t == 49 and link and link.get("url"):
            kind = "link"

        msgs.append({
            # 前端形状
            "id": local_id,
            "ts": ts or 0,
            "type": t,
            "kind": kind,
            "sender_wxid": sender_wxid,
            "sender_name": names.get(sender_wxid, sender_wxid) if sender_wxid
            else (names.get(chat, chat) if not is_group else ""),
            "is_me": bool(is_me),
            "md5": md5,
            "bubble_md5": bubble_md5,
            "quote": quote,
            "link": link,
            "text": _fmt(t, text) if t != 1 else text,
            # 导出形状（CipherTalk 风格）
            "localId": local_id,
            "platformMessageId": str(server_id or ""),
            "createTime": ts or 0,
            "localType": t,
            "typeName": TYPE_NAMES.get(t, f"类型{t}"),
            "rawContent": raw_text,
            "content": _fmt(t, text) if t != 1 else text,
            "isSend": 1 if is_me else 0,
            "senderUsername": sender_wxid or chat,
            "senderDisplayName": names.get(sender_wxid, sender_wxid) if sender_wxid
            else (names.get(chat, chat) if not is_group else chat),
        })
    return msgs


@bp.get("/messages")
def messages():
    account = request.args.get("account", "")
    chat = request.args.get("chat", "")
    before = int(request.args.get("before", "0") or 0)
    limit = min(int(request.args.get("limit", "100") or 100), 300)
    acc = _out_root() / account
    if not (acc / "message").is_dir():
        return jsonify({"error": "账号不存在或未解密"}), 404

    all_msgs = build_messages(acc, chat, account=account)
    all_msgs.sort(key=lambda m: m["ts"] or 0)
    if before:
        all_msgs = [m for m in all_msgs if (m["ts"] or 0) < before]
    has_more = len(all_msgs) > limit
    msgs = all_msgs[-limit:]

    display = next((m["sender_name"] for m in msgs if m["sender_name"]), "") \
        or (all_msgs[0]["sender_name"] if all_msgs else "")
    names = _contact_names(acc)
    display = names.get(chat, chat) or display
    return jsonify({"account": account, "chat": chat, "display": display,
                    "is_group": chat.endswith("@chatroom"),
                    "messages": msgs, "has_more": has_more,
                    "total": len(all_msgs)})


@bp.get("/avatar")
def avatar():
    """联系人头像：head_image.db 的 image_buffer 为明文 JPEG。"""
    account = request.args.get("account", "")
    username = request.args.get("username", "")
    db = _out_root() / account / "head_image" / "head_image.db"
    if not db.is_file() or not username:
        return jsonify({"error": "无头像"}), 404
    try:
        conn = sqlite3.connect(db)
        row = conn.execute(
            "SELECT image_buffer FROM head_image WHERE username=?",
            (username,)).fetchone()
        conn.close()
    except sqlite3.Error:
        return jsonify({"error": "无头像"}), 404
    if not row or not row[0]:
        return jsonify({"error": "无头像"}), 404
    return Response(row[0], mimetype="image/jpeg",
                    headers={"Cache-Control": "private, max-age=86400"})


@bp.get("/media/image")
def media_image():
    """按需解密单张图片：三级来源（hardlink 原图 → Bubble 气泡缓存 → Thumb 缩略图）。"""
    account = request.args.get("account", "")
    md5 = request.args.get("md5", "")
    chat = request.args.get("chat", "")
    bubble_md5 = request.args.get("bubble_md5", "")
    hq = request.args.get("hq", "") in ("1", "true")
    try:
        local_id = int(request.args.get("local_id", "0") or 0)
        ts = int(request.args.get("ts", "0") or 0)
    except ValueError:
        local_id = ts = 0
    if not account:
        return jsonify({"error": "参数缺失"}), 400
    body, info = media.get_image(account, md5, _out_root() / account,
                                 chat=chat or None, local_id=local_id or None,
                                 ts=ts or None, bubble_md5=bubble_md5 or None,
                                 hq=hq)
    if body is None:
        return jsonify({"error": info}), 404
    return Response(body, mimetype=info,
                    headers={"Cache-Control": "private, max-age=86400"})
