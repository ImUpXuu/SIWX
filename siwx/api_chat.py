"""聊天查看 API —— 读取解密产物（message_*.db / contact.db / session.db）。

模块化设计：Blueprint 独立于 server，可整体挪走或替换。
v1 能力：会话列表（session.db ∪ Msg_ 表统计）、消息分页、
zstd 解压、类型映射、发送者解析（前缀 / real_sender_id / origin_source）。
后续富文本（图片/语音实体）在此扩展。
"""
import hashlib
import html
import os
import re
import sqlite3
import threading
from pathlib import Path

from flask import Blueprint, Response, jsonify, request, current_app as _current_app

from siwx import logger as _logger
from siwx import media, voice
from siwx.plugins import chat_bridge as _plugins

# ── 分片索引（表名 → 分片路径）──────────────────────────────────
# message/ 下有十几个 *.db，而一个会话的 Msg_ 表通常只落在 1~2 个分片里；每个会话
# 却要把全部库打开查一遍 sqlite_master。实测这一步占导出总耗时的 99.5%
# （12k 条会话：close 3.1s + master 1.2s，真正的数据读取只有 78ms）。
# 这里按目录 mtime_ns 缓存索引，扫一次后：两遍导出、多会话批量、聊天页、MCP 共用。
#
# 同时修正一处不一致：聊天页原先只扫 message_*.db，而导出扫全部 *.db，
# 导致 biz_message_*.db 里的 68 个会话在聊天页完全看不到（实测本机 1.07 万条消息）。
# 索引覆盖全部 *.db，两边口径就此统一，且不会漏消息。
_SHARD_INDEX: dict = {}
_CONTACT_CACHE: dict = {}
_SESSION_CACHE: dict = {}
_SHARD_LOCK = threading.Lock()
_CACHE_LOCK = threading.Lock()
_HOLDER_SESSIONS = {"brandsessionholder", "brandservicesessionholder", "@placeholder_foldgroup"}


def _dir_signature(msg_dir: Path):
    """基于目录内 *.db 的 (文件名, 大小, mtime_ns) 生成签名；失败返回 None。

    注意：不能用目录自身的 mtime 做缓存键 —— 实测本机 G: 盘的目录 mtime 会随
    墙钟时间自行推进（比目录内最新文件还新），导致索引每次都被判为失效并重建，
    反而比不做缓存更慢。文件的时间戳是稳定的，因此以文件签名为准。
    18 个分片一次 scandir 约 1ms，且不需要打开数据库。
    """
    try:
        with os.scandir(msg_dir) as it:
            items = []
            for e in it:
                if e.name.endswith(".db"):
                    st = e.stat()
                    items.append((e.name, st.st_size, st.st_mtime_ns))
    except OSError:
        return None
    items.sort()
    return tuple(items)


def shard_index(msg_dir: Path) -> dict:
    """建立 {Msg_ 表名: [分片路径]}，按分片文件签名自动失效。"""
    key = str(msg_dir)
    stamp = _dir_signature(msg_dir)
    if stamp is None:
        return {}
    with _SHARD_LOCK:
        hit = _SHARD_INDEX.get(key)
        if hit is not None and hit[0] == stamp:
            return hit[1]

    index: dict = {}
    for db in sorted(msg_dir.glob("*.db")):
        try:
            conn = sqlite3.connect(db)
            try:
                names = [r[0] for r in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'")]
            finally:
                conn.close()
        except sqlite3.Error as e:
            # 分片打不开即被剔除索引 → 该库里的会话在聊天页/MCP 里"消失"
            _logger.detailed("api", f"[shard] 分片读取失败 db={db.name}: {e}")
            continue
        for t in names:
            if t.startswith("Msg_"):
                index.setdefault(t, []).append(db)

    with _SHARD_LOCK:
        _SHARD_INDEX[key] = (stamp, index)
    return index


def shards_for(acc: Path, chat: str) -> list:
    """包含该会话 Msg_ 表的分片路径；不存在时返回空列表。"""
    table = "Msg_" + hashlib.md5(chat.encode()).hexdigest()
    return shard_index(Path(acc) / "message").get(table, [])


def message_tables_by_shard(acc: Path) -> dict:
    """{分片路径: [Msg_ 表名]}，供全库搜索按分片遍历（已排序）。"""
    out: dict = {}
    for table, dbs in shard_index(Path(acc) / "message").items():
        for db in dbs:
            out.setdefault(db, []).append(table)
    return {db: sorted(t) for db, t in sorted(out.items())}

def _log(msg: str) -> None:
    """api_chat 模块的轻量日志（同步 API 端点用，不写任务缓冲）。"""
    try:
        import logging
        logging.getLogger("siwx").info(msg)
    except Exception:
        pass

try:
    import zstandard as _zstd
    _ZCTX = _zstd.ZstdDecompressor()
except ImportError:
    _ZCTX = None

bp = Blueprint("chat_api", __name__, url_prefix="/api/chat")

ZSTD_MAGIC = b"\x28\xb5\x2f\xfd"
KIND_MAP = {3: "image", 6: "file", 34: "voice", 42: "card", 43: "video",
            47: "sticker", 48: "location", 50: "call", 19: "record"}
FALLBACK_LABEL = {3: "[图片]", 6: "[文件]", 34: "[语音]", 42: "[名片]",
                  43: "[视频]", 47: "[动画表情]", 48: "[位置]", 50: "[通话]",
                  19: "[聊天记录]"}
SENDER_PREFIX_RE = re.compile(r"^([a-zA-Z0-9_\-]+):\n")

# ── 引用消息解析（审计 D1/D2/D3/D7/D8 修复）─────────────────────
# D1：refermsg 标签可能带属性（微信服务端形态不受本控），"<refermsg>"
# 包含判断对带属性形态全盲 → 统一用容忍属性的正则判定。
REFER_MSG_RE = re.compile(r"<refermsg[\s>]")

# 被引用消息类型 → 固定文案（精确类型开关：按 refermsg 的 type 字段全等
# 判定，绝不猜类型、每类有兜底；替换原 type="?3"? 宽松正则，D2/D3）
REFER_TYPE_LABELS = {
    3: "[图片]", 6: "[文件]", 34: "[语音]", 42: "[名片]", 43: "[视频]",
    47: "[表情包]", 48: "[位置]", 49: "[链接]", 50: "[通话]", 19: "[聊天记录]",
}

# 微信 4.x/5.0 packed_info 复合 localType：完整值 = 子类型 << 16 | 基本类型(49)
# （已知形态：244813135921=引用、266287972401=拍一拍、8594229559345=红包、
# 8589934592049=转账）。低 16 位折叠后都是 49，需按完整值区分子类型文案。
COMPOSITE_49_LABELS = {
    244813135921 >> 16: "[引用]",
    266287972401 >> 16: "[拍一拍]",
    8594229559345 >> 16: "[红包]",
    8589934592049 >> 16: "[转账]",
}

# 合并转发 recordinfo 的 dataitem datatype → 文案（无法识别的给 [消息] 兜底）
RECORD_DATATYPE_LABELS = {
    "1": "", "2": "[图片]", "3": "[图片]", "4": "[视频]", "5": "[链接]",
    "6": "[文件]", "8": "[动画表情]", "17": "[文件]",
}


def has_refermsg(text: str) -> bool:
    """refermsg 存在性判定：容忍标签属性（D1）。

    原实现 `"<refermsg>" in text` 对 `<refermsg type="3">` 等带属性形态
    全盲，引用关系静默丢失；服务端形态不受本控，判定必须容忍属性。
    """
    return bool(text) and bool(REFER_MSG_RE.search(text))


def composite_49_subtype(ltype) -> int:
    """packed_info 复合 localType 的子类型段（非复合或基本类型非 49 返回 0）。"""
    if not ltype or ltype <= 0xFFFF or (ltype & 0xFFFF) != 49:
        return 0
    return ltype >> 16


def _tag_text(src: str, tag: str) -> str:
    """提取 <tag>…</tag> 的原文（未命中返回空串，永不为 None）。"""
    m = re.search(rf"<{tag}>(.*?)</{tag}>", src or "", re.S)
    return m.group(1) if m else ""


from siwx import paths as _paths


def _out_root() -> Path:
    return _paths.out_root()


def _accounts() -> list:
    root = _out_root()
    out = []
    if root.is_dir():
        for d in sorted(root.iterdir()):
            if (d / "message").is_dir():
                out.append(d.name)
    return out


def _file_signature(p: Path):
    """轻量缓存签名：文件不存在返回 None。"""
    try:
        st = p.stat()
        return (st.st_size, st.st_mtime_ns)
    except OSError:
        return None


def _is_official_account(username: str) -> bool:
    """微信 4.x 公众号 username 通常以 gh_ 开头。"""
    return (username or "").startswith("gh_")


def owner_base(account: str) -> str:
    """从账号目录名推断本人原始 wxid：剥掉末尾的纯数字 uin 段。

    目录名形如 wxid_xxx_<uin>。不能用 `split("_6")`（uin 不以 6 开头、或
    wxid 本体含 6 开头段时会把 is_me 判定整体切错），也不能用
    media.clean_wxid()（它取第一段下划线前缀，对 wxid_a_b_1234 会切错，
    且其语义被 MMKV 密钥派生与图片路径查找依赖，不能改动）。
    与前端 chat.js::ownerUsername() 的"只剥尾部纯数字段"规则保持一致。
    """
    s = (account or "").strip()
    if re.fullmatch(r"wxid_.+_\d+", s):
        return re.sub(r"_\d+$", "", s)
    return s


def _is_ghost_session(username: str, summary: str, ts: int) -> bool:
    """过滤 SessionTable 中的折叠占位/空壳会话。

    这些行通常来自微信自己的折叠入口或已注销/从未实际打开的公众号：
    没有时间、没有摘要，点开后也没有可读消息，用户会感知为“幽灵会话”。
    """
    username = (username or "").strip()
    if not username or username in _HOLDER_SESSIONS:
        return True
    if not int(ts or 0) and not (summary or "").strip():
        return True
    return False


def _contact_names(acc_dir: Path) -> dict:
    p = acc_dir / "contact" / "contact.db"
    names = {}
    sig = _file_signature(p)
    if sig is None:
        return names
    key = (str(p), "all")
    with _CACHE_LOCK:
        hit = _CONTACT_CACHE.get(key)
        if hit is not None and hit[0] == sig:
            return hit[1]
    try:
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
        except sqlite3.Error as e:
            # schema 不匹配 → 降级到仅 username
            _logger.detailed("api", f"[contact] contact.db schema 降级（仅 username）"
                                    f"db={p.name}: {e}")
            try:
                for (un,) in conn.execute("SELECT username FROM contact"):
                    if (un or "").strip():
                        names[(un or "").strip()] = (un or "").strip()
            except sqlite3.Error as e2:
                _logger.detailed("api", f"[contact] contact.db 降级查询也失败"
                                        f"db={p.name}: {e2}")
        finally:
            conn.close()
    except Exception as e:
        # 数据库损坏 / 无法打开 → 返回空（不阻塞会话列表）；空结果会进缓存
        _logger.warn("api", f"[contact] contact.db 打不开，联系人名降级为空 db={p.name}: "
                            f"{type(e).__name__}: {e}")
    with _CACHE_LOCK:
        _CONTACT_CACHE[key] = (sig, names)
    return names


def _contact_names_for(acc_dir: Path, usernames) -> dict:
    """只读取指定 username 的联系人名，避免会话列表首次加载全表扫描 contact.db。"""
    wanted = sorted({(u or "").strip() for u in usernames if (u or "").strip()})
    if not wanted:
        return {}
    p = acc_dir / "contact" / "contact.db"
    sig = _file_signature(p)
    if sig is None:
        return {}
    # 若全量缓存已存在，直接从中取子集。
    with _CACHE_LOCK:
        full = _CONTACT_CACHE.get((str(p), "all"))
        if full is not None and full[0] == sig:
            all_names = full[1]
            return {u: all_names.get(u, u) for u in wanted}
        key = (str(p), tuple(wanted))
        hit = _CONTACT_CACHE.get(key)
        if hit is not None and hit[0] == sig:
            return hit[1]

    names = {u: u for u in wanted}
    try:
        conn = sqlite3.connect(p)
        try:
            for i in range(0, len(wanted), 400):
                chunk = wanted[i:i + 400]
                marks = ",".join("?" for _ in chunk)
                sql = ("SELECT username, remark, nick_name, alias FROM contact "
                       f"WHERE username IN ({marks})")
                for un, remark, nick, alias in conn.execute(sql, chunk):
                    un = (un or "").strip()
                    best = un
                    for v in (remark, nick, alias):
                        v = (v or "").strip()
                        if v and v != un and "\ufffd" not in v:
                            best = v
                            break
                    if un:
                        names[un] = best
        except sqlite3.Error as e:
            # schema 不匹配时退回全量函数，保持兼容性。
            _logger.detailed("api", f"[contact] contact.db schema 降级（回退全量读取）"
                                    f"db={p.name}: {e}")
            all_names = _contact_names(acc_dir)
            names = {u: all_names.get(u, u) for u in wanted}
        finally:
            conn.close()
    except Exception as e:
        _logger.warn("api", f"[contact] contact.db 打不开，联系人名降级 db={p.name}: "
                            f"{type(e).__name__}: {e}")
    with _CACHE_LOCK:
        _CONTACT_CACHE[(str(p), tuple(wanted))] = (sig, names)
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
        except Exception as e:
            # 只记 hex 头 + 长度，绝不透传正文（_FILE_LOG 存未脱敏原文）
            _logger.detailed("parse", f"[content] zstd 解压失败 len={len(raw)} "
                                      f"head={raw[:16].hex()}: {type(e).__name__}")
            return ""
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError:
        _logger.detailed("parse", f"[content] UTF-8 解码失败 len={len(raw)} "
                                  f"head={raw[:16].hex()}")
        return ""


def _fmt(ltype: int, text: str) -> str:
    t = (ltype or 0) & 0xFFFF
    if t == 1:
        return text
    if t == 57:
        # 引用消息：用户回复文本在 <title>，被引用部分由 quote 字段单独承载
        m = re.search(r"<title>(.*?)</title>", text, re.S)
        if m:
            return _xml_text(m.group(1)) or "[引用]"
        return text or "[引用]"
    if t in (10000, 10002):
        return text or "[系统消息]"
    if t == 49:
        if has_refermsg(text):
            # 引用消息（外层 49、内层 57）：与 t==57 同口径，只取回复文本，
            # 不带 "[链接]" 前缀；CDATA 由 _xml_text 一并剥离。
            # has_refermsg 容忍带属性形态（D1）。
            m = re.search(r"<title>(.*?)</title>", text, re.S)
            return (_xml_text(m.group(1)) if m else None) or "[引用]"
        # D5：appmsg type=19 合并转发（recordinfo）→ 逐条热解析，
        # 不再把内部消息整包丢弃、只显示 "[链接] 标题"
        if _is_recordinfo(text):
            rec = _parse_recordinfo(text)
            head = f"[聊天记录] {rec['title']}".rstrip()
            if rec["items"]:
                preview = "；".join(
                    f"{it['sender'] or '未知'}: {it['text']}"
                    for it in rec["items"][:3])
                tail = "…" if rec["count"] > 3 else ""
                return f"{head}（{rec['count']} 条：{preview}{tail}）"
            return head
        m = re.search(r"<title>(.*?)</title>", text, re.S)
        # packed_info 复合类型（转账/红包/拍一拍）按完整 localType 细分文案
        label = COMPOSITE_49_LABELS.get(composite_49_subtype(ltype), "[链接]")
        if m:
            # 剥离 CDATA 包装，避免 content 出现 <![CDATA[...]]> 原文
            return f"{label} {_xml_text(m.group(1))}".rstrip()
        return label
    return FALLBACK_LABEL.get(t, f"[类型{t}]")


def _is_recordinfo(text: str) -> bool:
    """appmsg type=19 合并转发（聊天记录）识别。

    只认明确标记：<recordinfo> 元素或 <appmsg type="19">；
    不做 "type=19" 子串匹配（会误中 datatype="19" 等无关属性）。"""
    if not text:
        return False
    return "<recordinfo>" in text or bool(
        re.search(r'<appmsg\s[^>]*type="19"', text))


def _sender_map(conn) -> dict:
    """real_sender_id(rowid) → wxid（分片内有效）。"""
    try:
        return {rid: un for rid, un in conn.execute("SELECT rowid, user_name FROM Name2Id")}
    except sqlite3.Error as e:
        # 静默归零 → 群消息发送者只能显示为空/会话名
        _logger.detailed("api", f"[sender] Name2Id 读取失败，发送者映射降级为空: {e}")
        return {}


@bp.get("/accounts")
def accounts():
    _logger.detailed("api", "[accounts] 请求")
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
            except sqlite3.Error as e:
                _logger.detailed("api", f"[accounts] session.db 读取失败 "
                                        f"account={acc}: {e}")
        out.append({"wxid": acc, "sessions": sessions})
    return jsonify({"accounts": out})


@bp.get("/sessions")
def sessions():
    """轻量会话列表：只读 session.db（最新预览+排序时间），带进程内缓存。"""
    account = request.args.get("account", "")
    acc = _out_root() / account
    if not (acc / "message").is_dir():
        return jsonify({"error": "账号不存在或未解密"}), 404

    sdb = acc / "session" / "session.db"
    contact_db = acc / "contact" / "contact.db"
    cache_key = str(acc)
    cache_sig = (_file_signature(sdb), _file_signature(contact_db))
    with _CACHE_LOCK:
        hit = _SESSION_CACHE.get(cache_key)
        if hit is not None and hit[0] == cache_sig:
            return jsonify({"account": account, "sessions": hit[1]})

    items = {}

    if sdb.is_file():
        conn = sqlite3.connect(sdb)
        try:
            for un, summary, ts in conn.execute(
                    "SELECT username, summary, sort_timestamp FROM SessionTable"):
                un = (un or "").strip()
                summary = (summary or "").strip()
                ts = ts or 0
                if un and not _is_ghost_session(un, summary, ts):
                    items[un] = {"username": un, "summary": summary,
                                 "last_time": ts}
        except sqlite3.Error as e:
            # SessionTable 损坏 → 尝试 Name2Id。这里没有摘要/时间，按幽灵会话规则不
            # 做空会话过滤，否则损坏库下会完全没有列表。
            _logger.warn("api", f"[sessions] SessionTable 读取失败，降级 Name2Id "
                                f"account={account}: {e}")
            try:
                for (un,) in conn.execute("SELECT user_name FROM Name2Id"):
                    if un and un not in items:
                        items[un] = {"username": un, "summary": "", "last_time": 0}
            except sqlite3.Error as e2:
                _logger.warn("api", f"[sessions] Name2Id 降级也失败 account={account}: {e2}")
        conn.close()

    if not items:
        _logger.warn("api", f"[sessions] session.db 无会话，降级扫描 message 分片 "
                            f"account={account}")
        for db in sorted((acc / "message").glob("message_*.db")):
            conn = sqlite3.connect(db)
            try:
                for (un,) in conn.execute("SELECT user_name FROM Name2Id"):
                    if un and un not in items:
                        items[un] = {"username": un, "summary": "", "last_time": 0}
            except sqlite3.Error as e:
                _logger.warn("api", f"[sessions] 分片 Name2Id 读取失败 account={account} "
                                    f"db={db.name}: {e}")
            finally:
                conn.close()

    try:
        # 会话列表只需要当前 items 的显示名，按需查询比全表读取 contact.db 快得多。
        names = _contact_names_for(acc, items.keys())
    except Exception as e:
        _log(f"[sessions] _contact_names_for 失败: {e}")
        names = {}

    out = []
    for it in items.values():
        un = (it.get("username") or "").strip()
        if not un:
            continue
        display = (names.get(un) or un).strip()
        if not display:
            continue
        is_official = _is_official_account(un)
        out.append({
            "username": un, "display": display,
            "is_group": un.endswith("@chatroom"),
            "is_official": is_official,
            "kind": "official" if is_official else ("group" if un.endswith("@chatroom") else "chat"),
            "preview": (it.get("summary") or "")[:60],
            "last_time": it.get("last_time", 0),
        })
    out.sort(key=lambda x: x["last_time"], reverse=True)
    out = _apply_session_plugins(out, account)
    with _CACHE_LOCK:
        _SESSION_CACHE[cache_key] = (cache_sig, out)
    _log(f"[sessions] 账号={account}, 返回 {len(out)} 个会话")
    return jsonify({"account": account, "sessions": out})


def _apply_session_plugins(sessions: list, account: str) -> list:
    """把插件层应用到会话列表（装饰器 + 过滤器）。无插件时原样返回。"""
    if not sessions or not _plugins.has_session_hooks():
        return sessions
    ctx = _plugins.chat_ctx(account, "", False)
    for s in sessions:
        _plugins.decorate_session(s, ctx, hot=False)
    return _plugins.filter_sessions(sessions, ctx)


TYPE_NAMES = {1: "文本消息", 3: "图片消息", 34: "语音消息", 42: "名片消息",
              43: "视频消息", 47: "动画表情", 48: "位置消息", 49: "链接/文件",
              50: "通话消息", 51: "状态消息", 57: "引用消息", 10000: "系统消息",
              10002: "系统消息"}


def _xml_text(s):
    """剥掉 CDATA 包装并清理转义。"""
    if s is None:
        return None
    # 修复：原替换串是控制字符 0x01，而不是捕获组引用 \1，导致所有 CDATA
    # 字段（链接标题、引用正文等）被一个不可见字符替换而丢失内容。
    s = re.sub(r"<!\[CDATA\[(.*?)\]\]>", r"\1", s, flags=re.S).strip()
    return s or None


def _parse_appmsg(text: str):
    """type 49/57 公共字段：title/url/des。"""
    def g(tag):
        return _xml_text(_tag_text(text, tag)) if _tag_text(text, tag) else None
    return g("title"), g("url"), g("des")


def _parse_refer(text: str):
    """type 57 引用：refermsg → {displayname, content, ts}。

    修复（对照 docs/audit-siwx-issues-2026-10-03.md）：
    - D1：refermsg 定位容忍标签属性（<refermsg type="3"> 不再全盲）；
    - D7：refermsg 无 <content>/content 为空时置空串兜底，不再对 None 做
      re.search（真实库 182 条命中：此前 TypeError 使全量导出丢整会话、
      聊天页 500）；
    - D8：微信把嵌套 XML 以 HTML 实体形态存储，提取后必须 html.unescape，
      否则 quote.content 是 "&lt;title&gt;…" 字面量（真实库 7358 条 / 23%）；
    - D2/D3：删除 type="?3"? 宽松正则；被引用类型改按 refermsg 的 type
      字段全等给固定文案，未识别类型剥标签兜底，绝不把嵌套 XML 原文直出。
    """
    m = re.search(r"<refermsg(\s[^>]*)?>(.*?)</refermsg>", text or "", re.S)
    if not m:
        return None
    attrs, blk = m.group(1) or "", m.group(2)
    dn = _xml_text(html.unescape(_tag_text(blk, "displayname")))
    # D7：content 缺失/为空 → 空串（原实现对 None 做 re.search 直接崩溃）
    content = _xml_text(html.unescape(_tag_text(blk, "content"))) or ""
    # 被引用内容本身可能是 XML（图片/链接卡片）→ 优先取其 <title> 作可读文本
    inner = re.search(r"<title>(.*?)</title>", content, re.S)
    if inner:
        content = _xml_text(inner.group(1)) or ""
    if not content.strip():
        content = _refer_type_label(attrs, blk) or ""
    elif re.match(r"<[a-zA-Z/?!]", content.lstrip()):
        # 无 <title> 可提取的嵌套 XML（视频/表情/未识别卡片）→ 不透传原文
        content = _refer_type_label(attrs, blk) or (
            re.sub(r"<[^>]+>", " ", content).strip() or "[引用]")
    try:
        ts = int((_tag_text(blk, "createtime") or "").strip() or 0)
    except ValueError:
        ts = 0
    return {"displayname": dn, "content": content[:500], "ts": ts}


def _refer_type_label(attrs: str, blk: str):
    """按 refermsg 的 type 字段全等判定被引用类型 → 固定文案（D2/D3）。

    先取标签属性 type="N"，缺失时回退内层 <type>N</type>；均缺失返回 None。"""
    tm = re.search(r'type\s*=\s*"(\d+)"', attrs)
    if not tm:
        tm = re.search(r"<type>\s*(\d+)\s*</type>", blk)
    return REFER_TYPE_LABELS.get(int(tm.group(1))) if tm else None


def _safe_parse_refer(text: str):
    """_parse_refer 的兜底包装：解析异常只丢弃该条引用并打详细日志，
    绝不让单条坏消息把整个会话的导出/聊天页拖垮（D7 调用点加固）。"""
    try:
        return _parse_refer(text)
    except Exception as e:
        _logger.detailed("parse",
                         f"[refer] 引用解析异常已兜底: {type(e).__name__}: {e}; "
                         f"raw={str(text)[:300]!r}")
        return None


def _parse_recordinfo(text: str) -> dict:
    """appmsg type=19 合并转发热解析（D5，参照 forwardRecordParser 思路）。

    按 <dataitem datatype="N"> 逐条提取 sourcename/sourcetime/datadesc/
    datatitle，按 (datatype, sender, ts, desc, title) 组合键去重。
    此前整条消息只显示 "[链接] 标题"，内部逐条消息全部丢弃
    （真实库 2252 条受影响，其中 1206 条 datadesc 整体丢弃）。
    返回 {"title": str, "count": int, "items": [{"sender","ts","text"}]}。
    """
    title = _xml_text(html.unescape(_tag_text(text, "title"))) or "聊天记录"
    items, seen = [], set()
    for m in re.finditer(r"<dataitem\s[^>]*>(.*?)</dataitem>", text or "", re.S):
        blk = m.group(1)
        dtm = re.search(r'datatype="(\d+)"', m.group(0))
        dtype = dtm.group(1) if dtm else ""
        sender = _xml_text(html.unescape(_tag_text(blk, "sourcename"))) or ""
        try:
            ts = int((_tag_text(blk, "sourcetime") or "").strip() or 0)
        except ValueError:
            ts = 0
        desc = _xml_text(html.unescape(_tag_text(blk, "datadesc"))) or ""
        dtitle = _xml_text(html.unescape(_tag_text(blk, "datatitle"))) or ""
        key = (dtype, sender, ts, desc, dtitle)
        if key in seen:
            continue
        seen.add(key)
        body = desc or dtitle or RECORD_DATATYPE_LABELS.get(dtype, "[消息]")
        items.append({"sender": sender, "ts": ts, "text": body[:200]})
    return {"title": title, "count": len(items), "items": items}


def parse_quote_or_link(t: int, text: str, local_id=None, ts=None):
    """type 57 / 49 的引用与链接卡统一分流（公共函数）。

    build_messages / messages / export_stream._enrich_row 三处原先逐字重复
    同一段 md5/49-57 门控与引用分流逻辑，改类型判定必须三处同步——现收敛
    到这里。返回 (quote, link, record)。

    local_id / ts 为可选定位上下文（审计 §4.3）：解析"无异常但返回 None"
    的兜底分支（即前端出现 [引用]/[链接] 文案的场景）会带这两个字段记
    detailed 日志；解析**异常**路径已由 _safe_parse_refer 记录，不重复埋点。
    """
    def _raw_head(s: str) -> str:
        # 正文只记 hex 头 + 长度（_FILE_LOG 存未脱敏原文，绝不透传正文）
        return (s or "").encode("utf-8", "replace")[:16].hex()

    quote = link = record = None
    if t == 57:
        quote = _safe_parse_refer(text)
        if quote is None:
            # ring=False：逐条埋点只进文件轨，防大导出冲掉日志页环形缓冲
            _logger.detailed("parse", f"[quote] t=57 未匹配到 refermsg（将显示 [引用]）"
                                      f"local_id={local_id} ts={ts} "
                                      f"raw_len={len(text or '')} raw_head={_raw_head(text)}",
                             ring=False)
    elif t == 49:
        if has_refermsg(text):
            quote = _safe_parse_refer(text)
            if quote is None:
                _logger.detailed("parse", f"[quote] t=49 refermsg 解析为空（将显示 [引用]）"
                                          f"local_id={local_id} ts={ts} "
                                          f"raw_len={len(text or '')} raw_head={_raw_head(text)}",
                                 ring=False)
        elif _is_recordinfo(text):
            record = _parse_recordinfo(text)
        else:
            title, url, des = _parse_appmsg(text)
            if title or url:
                link = {"title": title or "链接", "url": url, "desc": des}
            else:
                _logger.detailed("parse", f"[link] t=49 无 title/url（将显示 [链接]）"
                                          f"local_id={local_id} ts={ts} "
                                          f"raw_len={len(text or '')} raw_head={_raw_head(text)}",
                                 ring=False)
    return quote, link, record


def enrich_message_row(t: int, text: str, packed):
    """md5 / 气泡 md5 / 语音元数据采集（原三处逐字重复逻辑收敛于此）。"""
    md5 = media.extract_md5_from_xml(text) if t in (3, 47) else None
    voice_meta = voice.parse_voice_meta(text) if t == 34 else None
    bubble_md5 = None
    if t in (3, 47) and packed:
        m2 = re.search(rb"[0-9a-f]{32}", bytes(packed))
        bubble_md5 = m2.group().decode() if m2 else None
    return md5, bubble_md5, voice_meta


def message_kind(t: int, quote, link) -> str:
    """按类型与解析结果定 kind（原三处重复逻辑收敛于此）。"""
    if t == 57 or (t == 49 and quote):
        return "quote"
    if t == 49 and link and link.get("url"):
        return "link"
    return KIND_MAP.get(t, "text")


def build_messages(acc: Path, chat: str, start_ts=None, end_ts=None,
                   account: str = None):
    """读取一个会话的全部消息并富化。

    生产调用方只有 MCP（mcp_server.tool_get_messages / tool_search_messages）；
    导出走 export_stream.message_stream，API /messages 是端点内联查询
    （原 docstring 的"导出与 API 共用"是过时描述，审计核查 B1 已更正）。

    account: 数据库归属账号（wxid），用于判定 is_me；None 时从 acc 推断。
    返回 dict 列表，字段同时服务前端（id/ts/kind/...）与导出（CipherTalk 风格）。
    """
    account = account or acc.name
    table = "Msg_" + hashlib.md5(chat.encode()).hexdigest()
    names = _contact_names(acc)
    my_base = owner_base(account)
    is_group = chat.endswith("@chatroom")

    rows = []
    # 用分片索引直接定位分片，避免每个会话都把十几个库全打开查一遍
    for db in shards_for(acc, chat):
        conn = sqlite3.connect(db)
        try:
            smap = _sender_map(conn)
            for r in conn.execute(
                    f"SELECT local_id, server_id, local_type, create_time, "
                    f"origin_source, real_sender_id, message_content, "
                    f"packed_info_data FROM [{table}] ORDER BY create_time"):
                rows.append(r + (smap,))
        except sqlite3.Error as e:
            # 生产调用方只有 MCP：该分片 0 条消息 → MCP 工具结果缺段（数据完整性）
            _logger.error("api", f"[build] 表查询失败 chat={chat} db={db.name}: {e}")
        finally:
            conn.close()

    rows.sort(key=lambda r: (r[3] or 0, r[0] or 0))
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
        md5, bubble_md5, voice_meta = enrich_message_row(t, text, packed)

        quote, link, record = parse_quote_or_link(t, text, local_id=local_id, ts=ts)
        kind = message_kind(t, quote, link)

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
            "voice": voice_meta,
            "quote": quote,
            "link": link,
            "record": record,
            "text": _fmt(ltype, text) if t != 1 else text,
            # 导出形状（CipherTalk 风格）
            "localId": local_id,
            "platformMessageId": str(server_id or ""),
            "createTime": ts or 0,
            "localType": t,
            "typeName": TYPE_NAMES.get(t, f"类型{t}"),
            "rawContent": raw_text,
            "content": _fmt(ltype, text) if t != 1 else text,
            "isSend": 1 if is_me else 0,
            "senderUsername": sender_wxid or chat,
            "senderDisplayName": names.get(sender_wxid, sender_wxid) if sender_wxid
            else (names.get(chat, chat) if not is_group else chat),
        })
    return msgs


@bp.get("/messages")
def messages():
    """分页加载聊天消息：SQL LIMIT/OFFSET，不载入全部消息。"""
    account = request.args.get("account", "")
    chat = request.args.get("chat", "")
    before = int(request.args.get("before", "0") or 0)
    # 游标升级为 (create_time, local_id) 组合：只用时间戳做 `<` 条件时，
    # 翻页边界落在同一秒的一批消息中间（连发消息、消息与系统提示同秒很
    # 常见），剩余同秒消息会被永久跳过。before_id 与 before 配套使用；
    # 只传 before 时保持旧语义（向后兼容旧前端）。
    before_id = int(request.args.get("before_id", "0") or 0)
    # 修复：limit 只做了上限、没做下限。负数会被直接拼进 SQL，而 SQLite 的
    # LIMIT -2 等同「无限制」，一次请求就能把整个会话读进内存。
    limit = max(1, min(int(request.args.get("limit", "100") or 100), 300))
    acc = _out_root() / account
    if not (acc / "message").is_dir():
        return jsonify({"error": "账号不存在或未解密"}), 404

    table = "Msg_" + hashlib.md5(chat.encode()).hexdigest()
    names = _contact_names(acc)
    my_base = owner_base(account)
    is_group = chat.endswith("@chatroom")
    _log(f"[msg] 查询消息: account={account}, chat={chat}, table={table}, before={before}, before_id={before_id}, limit={limit}")

    # 用分片索引只打开真正含该会话的分片（原来是把十几个库全扫一遍）。
    # 分片内按 create_time 有序，合并后取最新的 limit 条。
    candidates = []
    shard_idx = 0
    for db in reversed(shards_for(acc, chat)):
        shard_idx += 1
        conn = sqlite3.connect(db)
        try:
            smap = _sender_map(conn)
            sql = (f"SELECT local_id, server_id, local_type, create_time, "
                   f"origin_source, real_sender_id, message_content, "
                   f"packed_info_data FROM [{table}]")
            params = []
            if before:
                if before_id:
                    sql += (" WHERE (create_time < ?) "
                            "OR (create_time = ? AND local_id < ?)")
                    params = [before, before, before_id]
                else:
                    sql += " WHERE create_time < ?"
                    params.append(before)
            sql += f" ORDER BY create_time DESC LIMIT {limit * 2}"
            rows = list(conn.execute(sql, params))
            if rows:
                _log(f"[msg] 分片{shard_idx} {db.name}: 取 {len(rows)} 条候选")
            for r in rows:
                candidates.append((r, smap))
        except sqlite3.Error as e:
            _logger.detailed("api", f"[msg] 分片查询失败 account={account} chat={chat} "
                                    f"db={db.name}: {e}")
        finally:
            conn.close()

    _log(f"[msg] 候选总数: {len(candidates)}, 分片数: {shard_idx}")

    # 合并排序（ASC 旧→新），取最新的 limit 条
    candidates.sort(key=lambda x: (x[0][3] or 0, x[0][0] or 0))
    page = candidates[-limit:]  # 取最后 limit 条（最新的）
    has_more = len(candidates) > limit

    msgs = []
    for (local_id, server_id, ltype, ts, origin, rsid, content, packed), smap in page:
        text = _decode_content(content)
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
        md5, bubble_md5, voice_meta = enrich_message_row(t, text, packed)

        quote, link, record = parse_quote_or_link(t, text, local_id=local_id, ts=ts)
        kind = message_kind(t, quote, link)

        msgs.append({
            "id": local_id, "platformMessageId": str(server_id or ""),
            "ts": ts or 0, "type": t, "kind": kind,
            "sender_wxid": sender_wxid,
            "sender_name": names.get(sender_wxid, sender_wxid) if sender_wxid
            else (names.get(chat, chat) if not is_group else ""),
            "is_me": bool(is_me), "md5": md5, "bubble_md5": bubble_md5,
            "voice": voice_meta,
            "quote": quote, "link": link, "record": record,
            "text": _fmt(ltype, text) if t != 1 else text,
        })

    # ── 插件层（无插件时零开销）──────────────────────────────
    if msgs and _plugins.has_message_hooks():
        pctx = _plugins.chat_ctx(account, chat, is_group, names=names)
        for m in msgs:
            # 装饰器默认不进热路径：仅 hot=True 的插件生效（带超时熔断）
            _plugins.decorate_message(m, pctx, hot=False)
            # 渲染器可替换 kind / 追加结构化 render 节点树
            _plugins.apply_renderer(m, pctx)
            if m.get("text"):
                m["text"] = _plugins.transform_content(m["text"], m, pctx)

    display = names.get(chat, chat)
    _log(f"[msg] 返回 {len(msgs)} 条消息, has_more={has_more}")
    return jsonify({"account": account, "chat": chat, "display": display,
                    "is_group": is_group, "messages": msgs, "has_more": has_more})


@bp.get("/timeline")
def timeline():
    """单个会话时间轴：默认只聚合月份，展开时才查询指定月的日期。"""
    account = request.args.get("account", "")
    chat = request.args.get("chat", "")
    month = (request.args.get("month") or "").strip()
    _logger.detailed("api", f"[timeline] account={account} chat={chat} month={month or '全部'}")
    if month and not re.fullmatch(r"\d{4}-(0[1-9]|1[0-2])", month):
        return jsonify({"error": "month 参数格式应为 YYYY-MM"}), 400
    acc = _out_root() / account
    if not (acc / "message").is_dir():
        return jsonify({"error": "账号不存在或未解密"}), 404
    table = "Msg_" + hashlib.md5(chat.encode()).hexdigest()
    key_name = "day" if month else "month"
    fmt = "%Y-%m-%d" if month else "%Y-%m"
    buckets = {}
    total = 0
    ts_min = ts_max = 0
    for db in shards_for(acc, chat):
        conn = sqlite3.connect(db)
        try:
            where = "create_time > 0"
            params = []
            if month:
                where += " AND strftime('%Y-%m', create_time, 'unixepoch', 'localtime') = ?"
                params.append(month)
            sql = (f"SELECT strftime('{fmt}', create_time, 'unixepoch', 'localtime') AS k, "
                   f"COUNT(*), MIN(create_time), MAX(create_time) FROM [{table}] "
                   f"WHERE {where} GROUP BY k")
            for key, cnt, mn, mx in conn.execute(sql, params):
                if not key:
                    continue
                item = buckets.setdefault(key, {key_name: key, "count": 0,
                                                "first_ts": 0, "last_ts": 0})
                item["count"] += int(cnt or 0)
                if mn and (not item["first_ts"] or mn < item["first_ts"]):
                    item["first_ts"] = int(mn)
                if mx and mx > item["last_ts"]:
                    item["last_ts"] = int(mx)
                total += int(cnt or 0)
                if mn and (not ts_min or mn < ts_min):
                    ts_min = int(mn)
                if mx and mx > ts_max:
                    ts_max = int(mx)
        except sqlite3.Error as e:
            _logger.detailed("api", f"[timeline] 分片查询失败 account={account} "
                                    f"chat={chat} db={db.name}: {e}")
        finally:
            conn.close()
    result = {"account": account, "chat": chat, "total": total,
              "ts_min": ts_min, "ts_max": ts_max}
    result["days" if month else "months"] = [buckets[k] for k in sorted(buckets, reverse=True)]
    if month:
        result["month"] = month
    return jsonify(result)


@bp.get("/stats")
def conversation_stats():
    """单个会话统计：供聊天页右上角弹窗使用。"""
    account = request.args.get("account", "")
    chat = request.args.get("chat", "")
    acc = _out_root() / account
    if not (acc / "message").is_dir():
        return jsonify({"error": "账号不存在或未解密"}), 404
    table = "Msg_" + hashlib.md5(chat.encode()).hexdigest()
    names = _contact_names(acc)
    type_counts = {}
    by_day = {}
    by_hour = [0] * 24
    total = sent = 0
    ts_min = ts_max = 0
    _logger.detailed("api", f"[stats] account={account} chat={chat}")
    for db in shards_for(acc, chat):
        conn = sqlite3.connect(db)
        try:
            for cnt, mn, mx, sent_cnt in conn.execute(
                    f"SELECT COUNT(*), MIN(create_time), MAX(create_time), "
                    f"SUM(CASE WHEN origin_source = 1 THEN 1 ELSE 0 END) "
                    f"FROM [{table}] WHERE create_time > 0"):
                total += int(cnt or 0)
                sent += int(sent_cnt or 0)
                if mn and (not ts_min or mn < ts_min):
                    ts_min = int(mn)
                if mx and mx > ts_max:
                    ts_max = int(mx)
            for t, cnt in conn.execute(
                    f"SELECT (local_type & 65535), COUNT(*) FROM [{table}] "
                    f"WHERE create_time > 0 GROUP BY (local_type & 65535)"):
                type_counts[int(t or 0)] = type_counts.get(int(t or 0), 0) + int(cnt or 0)
            for day, cnt in conn.execute(
                    f"SELECT strftime('%Y-%m-%d', create_time, 'unixepoch', 'localtime'), "
                    f"COUNT(*) FROM [{table}] WHERE create_time > 0 GROUP BY 1"):
                if day:
                    by_day[day] = by_day.get(day, 0) + int(cnt or 0)
            for hour, cnt in conn.execute(
                    f"SELECT CAST(strftime('%H', create_time, 'unixepoch', 'localtime') AS INTEGER), "
                    f"COUNT(*) FROM [{table}] WHERE create_time > 0 GROUP BY 1"):
                if hour is not None:
                    by_hour[int(hour)] += int(cnt or 0)
        except sqlite3.Error as e:
            _logger.detailed("api", f"[stats] 分片查询失败 account={account} "
                                    f"chat={chat} db={db.name}: {e}")
        finally:
            conn.close()
    received = max(0, total - sent)
    busiest_day = {"day": "", "count": 0}
    if by_day:
        day, cnt = max(by_day.items(), key=lambda x: x[1])
        busiest_day = {"day": day, "count": cnt}
    busiest_hour = max(range(24), key=lambda h: by_hour[h]) if any(by_hour) else None
    types = [{"type": t, "label": TYPE_NAMES.get(t, f"类型{t}"), "count": c}
             for t, c in sorted(type_counts.items(), key=lambda x: -x[1])]
    return jsonify({"account": account, "chat": chat,
                    "display": names.get(chat, chat), "total": total,
                    "sent": sent, "received": received,
                    "first_ts": ts_min, "last_ts": ts_max,
                    "active_days": len(by_day), "busiest_day": busiest_day,
                    "busiest_hour": busiest_hour, "types": types})


@bp.get("/avatar")
def avatar():
    """联系人头像：head_image.db 的 image_buffer 为明文 JPEG。

    内置实现优先；查不到时询问插件头像解析器（插件可按需给出缓存/远程头像）。
    """
    account = request.args.get("account", "")
    username = request.args.get("username", "")
    _logger.detailed("api", f"[avatar] account={account} username={username}")

    def _plugin_avatar():
        if not username:
            return None
        return _plugins.resolve_avatar(username, account,
                                       {"out_root": str(_out_root())})

    db = _out_root() / account / "head_image" / "head_image.db"
    if not username:
        return jsonify({"error": "无头像"}), 404
    if not db.is_file():
        data = _plugin_avatar()
        if data:
            return Response(data, mimetype="image/jpeg",
                            headers={"Cache-Control": "private, max-age=86400"})
        return jsonify({"error": "无头像"}), 404
    try:
        conn = sqlite3.connect(db)
        candidates = [username]
        # 输出目录名通常是 wxid_xxx_6409 这类带后缀的账号目录，而头像库里的
        # 本人 username 是原始 wxid_xxx。给“自己的头像”做一次兼容回退。
        if username == account:
            clean = media.clean_wxid(account)
            if clean not in candidates:
                candidates.append(clean)
            if "_" in account:
                short = account.rsplit("_", 1)[0]
                if short not in candidates:
                    candidates.append(short)
        row = None
        for u in candidates:
            row = conn.execute(
                "SELECT image_buffer FROM head_image WHERE username=?",
                (u,)).fetchone()
            if row and row[0]:
                break
        conn.close()
    except sqlite3.Error as e:
        # 非 pass：降级到插件头像解析器再 404，缺的是日志而非处理
        _logger.detailed("api", f"[avatar] head_image.db 读取失败，降级插件解析器 "
                                f"account={account} username={username}: {e}")
        data = _plugin_avatar()
        if data:
            return Response(data, mimetype="image/jpeg",
                            headers={"Cache-Control": "private, max-age=86400"})
        return jsonify({"error": "无头像"}), 404
    if not row or not row[0]:
        data = _plugin_avatar()
        if data:
            return Response(data, mimetype="image/jpeg",
                            headers={"Cache-Control": "private, max-age=86400"})
        return jsonify({"error": "无头像"}), 404
    return Response(row[0], mimetype="image/jpeg",
                    headers={"Cache-Control": "private, max-age=86400"})


@bp.get("/media/voice")
def media_voice():
    """读取或转码解密后的语音数据。

    默认保持向后兼容，返回原始 SILK；传 format=wav 时尝试用本机可选解码器
    转成浏览器可播放的 WAV。项目不新增强制外部依赖，缺少解码器时返回 415。
    """
    account = request.args.get("account", "")
    chat = request.args.get("chat", "")
    fmt = (request.args.get("format") or request.args.get("fmt") or "silk").lower()
    try:
        local_id = int(request.args.get("local_id", "0") or 0)
        ts = int(request.args.get("ts", "0") or 0)
        svr_id = int(request.args.get("svr_id", "0") or request.args.get("server_id", "0") or 0)
    except ValueError:
        local_id = ts = svr_id = 0
        # 参数非法静默归零 → 后续按 local_id=0 查询，大概率误导性 404
        _logger.detailed("api", f"[voice] local_id/ts/svr_id 参数非法，已归零 "
                                f"account={account} chat={chat}")
    if not account:
        return jsonify({"error": "参数缺失"}), 400
    _logger.detailed("api", f"[voice] account={account} chat={chat} "
                            f"local_id={local_id} ts={ts} fmt={fmt}")
    body, info = voice.get_voice(_out_root() / account, chat=chat,
                                 local_id=local_id, svr_id=svr_id, ts=ts)
    if body is None:
        return jsonify({"error": info}), 404

    if fmt in ("wav", "wave"):
        wav, meta = voice.transcode_voice(body, "wav")
        if wav is None:
            return jsonify({"error": meta, "fallback": "silk"}), 415
        filename = f"voice_{local_id or info.get('localId') or 'msg'}.wav"
        return Response(wav, mimetype=meta["mimetype"], headers={
            "Cache-Control": "private, max-age=86400",
            "Content-Disposition": f'inline; filename="{filename}"',
            "X-SIWX-Voice-Format": meta["format"],
            "X-SIWX-Voice-Transcoder": meta.get("engine", ""),
            "X-SIWX-Voice-Source": str(info.get("db", "")),
        })

    if fmt not in ("silk", "raw", "original"):
        return jsonify({"error": f"暂不支持的语音格式: {fmt}"}), 400
    filename = f"voice_{local_id or info.get('localId') or 'msg'}.silk"
    return Response(body, mimetype=info.get("mimetype", "audio/silk"), headers={
        "Cache-Control": "private, max-age=86400",
        "Content-Disposition": f'inline; filename="{filename}"',
        "X-SIWX-Voice-Format": str(info.get("format", "unknown")),
        "X-SIWX-Voice-Source": str(info.get("db", "")),
    })


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
        # 参数非法静默归零 → 后续按 local_id=0 查询，大概率误导性 404
        _logger.detailed("api", f"[image] local_id/ts 参数非法，已归零 "
                                f"account={account} chat={chat} md5={(md5 or '')[:8]}")
    if not account:
        return jsonify({"error": "参数缺失"}), 400
    # 高频端点：detailed 天然节流（仅详细模式可见）
    _logger.detailed("api", f"[image] account={account} chat={chat} "
                            f"local_id={local_id} ts={ts} md5={(md5 or '')[:8]} hq={hq}")
    body, info = media.get_image(account, md5, _out_root() / account,
                                 chat=chat or None, local_id=local_id or None,
                                 ts=ts or None, bubble_md5=bubble_md5 or None,
                                 hq=hq)
    if body is None:
        return jsonify({"error": info}), 404
    return Response(body, mimetype=info,
                    headers={"Cache-Control": "private, max-age=86400"})
