"""聊天统计 —— 跨分片聚合，为统计面板提供图表数据。

设计要点
--------
1. **纯 SQL 聚合，不逐条读消息**：统计只需 `COUNT` / `GROUP BY`，直接在
   `message_*.db` 的 `Msg_*` 表上做 `UNION ALL` 子查询聚合，避免把几十万条
   消息读进 Python（实测 8 万条约 2.4s，而逐条 Python 遍历要 9s+）。
2. **按文件签名缓存**：统计结果缓存到 `<account>/.siwx_stats.json`，键为
   所有分片文件的 (名, 大小, mtime_ns) 签名。源库未变直接命中，秒回。
   与 `_dir_signature` 的思路一致 —— 目录 mtime 在部分盘符上不可靠。
3. **一次扫描产出全部维度**：总量、类型分布、月度趋势、小时活跃度、星期
   分布、会话排行、发送者排行、时间跨度都在同一趟里算完，避免多次全表扫描。
4. 只读取解密产物目录，不接触微信原始数据。

对外主入口：`compute_stats(account, force=False) -> dict`
"""
import hashlib
import json
import os
import sqlite3
import threading
import time
from pathlib import Path

from siwx import paths as _paths


# SQLite 的 `local_type` 是带标志位的整数，低 16 位才是真实类型。
_TYPE_MASK = 0xFFFF

# 类型中文名（与 api_chat.TYPE_NAMES 保持一致，此处独立定义避免循环导入）
TYPE_LABELS = {
    1: "文本",
    3: "图片",
    34: "语音",
    42: "名片",
    43: "视频",
    47: "表情",
    48: "位置",
    49: "链接/文件",
    50: "通话",
    51: "状态",
    57: "引用",
    66: "其他",
    10000: "系统",
    10002: "系统",
}

# 发送者排行的口径：只统计私聊。群聊（@chatroom）里 real_sender_id 是群成员，
# 公众号（gh_）/ openim / 服务号是单向推送，都不算"和谁聊得多"。
_GROUP_SUFFIX = "@chatroom"
_OFFICIAL_PREFIX = "gh_"
# 系统会话：不是真实联系人，排除在排行之外
_SYSTEM_CHATS = {"filehelper", "newsapp", "fmessage", "weibo", "qqmail",
                 "floatbottle", "medianote", "tmessage", "qmessage",
                 "officialaccounts", "notification_messages", "exmail_tool",
                 "notifymessage", "masssendapp", "blogapp", "helper_entry"}

# 图表用的类型分组（把长尾类型归并，避免图例过长）
TYPE_GROUPS = [
    ("text", "文本", lambda t: t == 1),
    ("image", "图片", lambda t: t == 3),
    ("sticker", "表情", lambda t: t == 47),
    ("link", "链接/文件", lambda t: t in (49, 57)),
    ("voice", "语音", lambda t: t == 34),
    ("video", "视频", lambda t: t == 43),
    ("system", "系统", lambda t: t in (10000, 10002)),
]
# 其余类型统一归入「其他」

_CACHE_LOCK = threading.Lock()
# 进程内缓存：{account: (signature, stats_dict)}，避免同进程反复读 json
_MEM_CACHE: dict = {}
_CONTACT_NAME_CACHE: dict = {}   # {(path, mode, key): (file_sig, value)}

_CACHE_VERSION = 3      # 统计口径变更时递增，使旧缓存自动失效
                        # v3: 增加 by_day，使自定义时间范围真正过滤全页指标


def _out_root() -> Path:
    return _paths.out_root()


def _msg_dir(account: str) -> Path:
    return _out_root() / account / "message"


def signature(account: str):
    """基于 message/*.db 的 (文件名, 大小, mtime_ns) 生成签名；不可用返回 None。

    目录自身的 mtime 在部分盘符上会随墙钟自行推进（实测 G: 盘），会导致缓存
    永远失效，因此只认文件时间戳。
    """
    d = _msg_dir(account)
    try:
        with os.scandir(d) as it:
            items = []
            for e in it:
                if e.name.endswith(".db"):
                    st = e.stat()
                    items.append((e.name, st.st_size, st.st_mtime_ns))
    except OSError:
        return None
    items.sort()
    return tuple(items)


def _cache_file(account: str) -> Path:
    return _out_root() / account / ".siwx_stats.json"


def _shard_tables(conn: sqlite3.Connection) -> list:
    """该分片内的 Msg_ 表名列表。"""
    return [r[0] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name LIKE 'Msg\\_%' ESCAPE '\\'")]


def _day_key(ts: int, offset: int = 0) -> str:
    return time.strftime("%Y-%m-%d", time.localtime(ts + offset))


def _merge_type_counts(agg: dict, raw: dict) -> None:
    for t, cnt in raw.items():
        agg[t] = agg.get(t, 0) + cnt


def _scan_shard(db: Path, flags: dict = None) -> dict:
    """单个分片的统计（供线程池并行调用）。

    关键优化：把该分片所有 `Msg_*` 表 UNION ALL 后物化到一张 TEMP 表，
    并在物化时一次性算好日/月/小时/星期列。随后只用两趟 GROUP BY：
    一趟产出总量、类型、月/日、小时、星期；一趟产出私聊排行。
    这样既保留日级时间范围能力，又避免每个维度反复扫表、反复 strftime。

    会话归属的处理：`Msg_<md5(chat)>` 的表名反向 hash 不出 username，但
    Name2Id 里有 rowid→username，可以正向 hash 后与表名比对，得到
    「表名 → username」。这个映射在建 UNION 子查询时**以字面量写进 SQL**
    （`'wxid_xxx' AS cid`），所以私聊排行的会话归属跟着主扫描一起出来了，
    不需要事后再对每张表跑一次 COUNT(*) —— 后者实测会从 2s 退化到 36s。
    """
    out = {"total": 0, "type_counts": {}, "by_month": {}, "by_hour": [0] * 24,
           "by_weekday": [0] * 7, "by_sender": {}, "by_chat": {},
           "by_day": {}, "ts_min": 0, "ts_max": 0}
    try:
        conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    except sqlite3.Error:
        return out
    try:
        tables = _shard_tables(conn)
        if not tables:
            return out

        # 表名 → 会话 username（正向 hash 比对，比反解 md5 可靠）
        own: dict = {}
        try:
            for (un,) in conn.execute("SELECT user_name FROM Name2Id"):
                un = (un or "").strip()
                if un:
                    own.setdefault("Msg_" + hashlib.md5(un.encode()).hexdigest(), un)
        except sqlite3.Error:
            pass

        # 会话归属：私聊表写入 username，其余（群聊/公众号/系统）写入空串，
        # 这样后面的 `WHERE cid <> ''` 就天然只捞出私聊消息。
        flags = flags or {}
        parts = []
        for t in tables:
            un = own.get(t)
            vf, al = flags.get(un, (0, "")) if un else (0, "")
            cid = un if (un and _is_private_chat(un, vf, al)) else ""
            parts.append(f"SELECT create_time, local_type, real_sender_id, "
                         f"{_sql_lit(cid)} AS cid FROM [{t}]")
        union = " UNION ALL ".join(parts)
        try:
            conn.execute("PRAGMA temp_store=MEMORY")
            # 一次性算出日/月/小时/星期，后续 GROUP BY 直接用列，避免重复 strftime。
            conn.execute(
                f"CREATE TEMP TABLE _siwx_s AS "
                f"SELECT create_time, (local_type & {_TYPE_MASK}) AS k, "
                f"real_sender_id AS sid, cid, "
                f"strftime('%Y-%m-%d', create_time, 'unixepoch', 'localtime') AS d, "
                f"strftime('%Y-%m', create_time, 'unixepoch', 'localtime') AS m, "
                f"CAST(strftime('%H', create_time, 'unixepoch', 'localtime') AS INTEGER) AS h, "
                f"((CAST(strftime('%w', create_time, 'unixepoch', 'localtime') AS INTEGER) + 6) % 7) AS w "
                f"FROM ({union}) WHERE create_time > 0")
        except sqlite3.Error:
            return out

        # 主聚合：一趟 GROUP BY 同时喂给全局类型/月/小时/星期和日级桶。
        for day, mon, hour, weekday, k, cnt, mn, mx in conn.execute(
                "SELECT d, m, h, w, k, COUNT(*), MIN(create_time), MAX(create_time) "
                "FROM _siwx_s GROUP BY d, m, h, w, k"):
            if not day:
                continue
            out["total"] += cnt
            out["type_counts"][k] = out["type_counts"].get(k, 0) + cnt
            if mon:
                out["by_month"][mon] = out["by_month"].get(mon, 0) + cnt
            if hour is not None:
                out["by_hour"][int(hour)] += cnt
            if weekday is not None:
                out["by_weekday"][int(weekday)] += cnt
            if mn and (not out["ts_min"] or mn < out["ts_min"]):
                out["ts_min"] = mn
            if mx and mx > out["ts_max"]:
                out["ts_max"] = mx

            b = _day_bucket(out["by_day"], day)
            b["total"] += cnt
            b["type_counts"][k] = b["type_counts"].get(k, 0) + cnt
            if hour is not None:
                b["by_hour"][int(hour)] += cnt
            if weekday is not None:
                b["by_weekday"][int(weekday)] += cnt
            if mn and (not b["ts_min"] or mn < b["ts_min"]):
                b["ts_min"] = mn
            if mx and mx > b["ts_max"]:
                b["ts_max"] = mx

        if not out["total"]:
            return out

        # 私聊排行：只需按 day + cid 聚合，一趟扫完全量和日级排行。
        for day, un, cnt in conn.execute(
                "SELECT d, cid, COUNT(*) FROM _siwx_s WHERE cid <> '' GROUP BY d, cid"):
            if day and un:
                out["by_chat"][un] = out["by_chat"].get(un, 0) + cnt
                b = _day_bucket(out["by_day"], day)
                b["by_chat"][un] = b["by_chat"].get(un, 0) + cnt

        conn.execute("DROP TABLE _siwx_s")
    except sqlite3.Error:
        pass
    finally:
        conn.close()
    return out


def _sql_lit(s: str) -> str:
    """把字符串安全地写成 SQL 字面量（仅用于会话 username）。"""
    return "'" + str(s).replace("'", "''") + "'"


def _day_bucket(by_day: dict, day: str) -> dict:
    """取得/创建单日统计桶。"""
    b = by_day.get(day)
    if b is None:
        b = {"total": 0, "type_counts": {}, "by_hour": [0] * 24,
             "by_weekday": [0] * 7, "by_chat": {}, "ts_min": 0, "ts_max": 0}
        by_day[day] = b
    return b


def _merge_scans(results: list) -> dict:
    """合并各分片结果。"""
    total = 0
    type_counts: dict = {}
    by_month: dict = {}
    by_hour = [0] * 24
    by_weekday = [0] * 7
    by_sender: dict = {}
    by_chat: dict = {}
    by_day: dict = {}
    ts_min = ts_max = 0
    for r in results:
        total += r["total"]
        _merge_type_counts(type_counts, r["type_counts"])
        for k, v in r["by_month"].items():
            by_month[k] = by_month.get(k, 0) + v
        for i, v in enumerate(r["by_hour"]):
            by_hour[i] += v
        for i, v in enumerate(r["by_weekday"]):
            by_weekday[i] += v
        for sid, v in r["by_sender"].items():
            by_sender[sid] = by_sender.get(sid, 0) + v
        for un, v in r.get("by_chat", {}).items():
            by_chat[un] = by_chat.get(un, 0) + v
        for day, src in r.get("by_day", {}).items():
            dst = _day_bucket(by_day, day)
            dst["total"] += src.get("total", 0)
            _merge_type_counts(dst["type_counts"], src.get("type_counts") or {})
            for i, v in enumerate(src.get("by_hour") or [0] * 24):
                dst["by_hour"][i] += v
            for i, v in enumerate(src.get("by_weekday") or [0] * 7):
                dst["by_weekday"][i] += v
            for un, v in (src.get("by_chat") or {}).items():
                dst["by_chat"][un] = dst["by_chat"].get(un, 0) + v
            mn, mx = src.get("ts_min") or 0, src.get("ts_max") or 0
            if mn and (not dst["ts_min"] or mn < dst["ts_min"]):
                dst["ts_min"] = mn
            if mx > dst["ts_max"]:
                dst["ts_max"] = mx
        if r["ts_min"] and (not ts_min or r["ts_min"] < ts_min):
            ts_min = r["ts_min"]
        if r["ts_max"] > ts_max:
            ts_max = r["ts_max"]
    return {"total": total, "type_counts": type_counts, "by_month": by_month,
            "by_hour": by_hour, "by_weekday": by_weekday, "by_sender": by_sender,
            "by_chat": by_chat, "by_day": dict(sorted(by_day.items())),
            "ts_min": ts_min, "ts_max": ts_max}


def _scan(account: str, log=None) -> dict:
    """并行扫描全部分片，产出全部统计维度。"""
    def _l(msg):
        if log:
            try:
                log(msg)
            except Exception:
                pass

    msg_dir = _msg_dir(account)
    shards = sorted(msg_dir.glob("*.db"))
    if not shards:
        return _empty_stats(account, 0)

    agg = None
    flags = _contact_flags(account)
    try:
        from concurrent.futures import ThreadPoolExecutor
        workers = min(8, max(2, (os.cpu_count() or 4)))
        with ThreadPoolExecutor(max_workers=workers) as ex:
            agg = _merge_scans(list(ex.map(lambda d: _scan_shard(d, flags), shards)))
    except Exception as e:
        _l(f"[stats] 并行扫描失败，回退串行: {e}")
        agg = _merge_scans([_scan_shard(db, flags) for db in shards])

    total = agg["total"]
    _l(f"[stats] {account}: {len(shards)} 分片, {total} 条消息")

    # 发送者排行：只统计私聊会话，并解析为昵称。
    # 注意不能直接用 agg["by_sender"] —— 那是 real_sender_id 的全局聚合，
    # 群成员的发言也会混进来，无法区分会话归属。
    top_senders = _private_sender_ranking(account, agg.get("by_chat") or {})
    _l(f"[stats] {account}: 私聊发送者 {len(top_senders)} 位")

    return {
        "version": _CACHE_VERSION,
        "account": account,
        "generated_at": int(time.time()),
        "shards": len(shards),
        "total": total,
        "ts_min": agg["ts_min"],
        "ts_max": agg["ts_max"],
        "type_counts": {str(k): v for k, v in sorted(agg["type_counts"].items(),
                                                    key=lambda x: -x[1])},
        "by_month": dict(sorted(agg["by_month"].items())),
        "by_day": agg.get("by_day") or {},
        "by_hour": agg["by_hour"],
        "by_weekday": agg["by_weekday"],
        "by_chat": agg.get("by_chat") or {},
        "top_senders": top_senders,
        # 会话数：统计里有消息的 Msg_ 表数量
        "chat_count": len(_chat_tables(account)),
    }


def _chat_tables(account: str) -> list:
    """有消息的会话表名列表（用于会话数统计）。"""
    out = []
    for db in sorted(_msg_dir(account).glob("*.db")):
        try:
            conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
        except sqlite3.Error:
            continue
        try:
            out.extend(_shard_tables(conn))
        except sqlite3.Error:
            pass
        finally:
            conn.close()
    return out


def _empty_stats(account: str, shards: int) -> dict:
    return {"version": _CACHE_VERSION, "account": account,
            "generated_at": int(time.time()), "shards": shards, "total": 0,
            "ts_min": 0, "ts_max": 0, "type_counts": {}, "by_month": {},
            "by_day": {}, "by_hour": [0] * 24, "by_weekday": [0] * 7,
            "by_chat": {}, "top_senders": [], "chat_count": 0}


def _is_private_chat(username: str, verify_flag: int = 0, alias: str = "") -> bool:
    """判断某个会话是否为「私聊」。

    排除几类非私聊（全部基于会话 username 与少量联系人字段）：

    - 群聊：`xxx@chatroom`
    - 官方通道：username 带 `@`（`@openim` 企业微信 / `@weclaw` 机器人 /
      `@hardcode` 内部账号），它们不是真人
    - 公众号：`gh_` 前缀；或 `verify_flag` 非 0 且带 alias 的（微信 4.x 里
      未走 `gh_` 前缀的新闻/媒体号，例如「中央广电总台中国之声」，
      username 是 `zhongguozhisheng5538`，靠这一条兜住）
    - 系统会话：文件传输助手、微信团队等
    """
    un = (username or "").strip()
    if not un or "@" in un:
        return False
    if un.startswith(_OFFICIAL_PREFIX):
        return False
    if un.lower() in _SYSTEM_CHATS:
        return False
    # 未带 gh_ 前缀的公众号：有认证标记 + 有对外 alias
    if verify_flag and (alias or "").strip():
        return False
    return True


def _file_sig(p: Path):
    """单文件签名，用于联系人辅助缓存。"""
    try:
        st = p.stat()
        return (st.st_size, st.st_mtime_ns)
    except OSError:
        return None


def _contact_flags(account: str) -> dict:
    """username → (verify_flag, alias)，用于识别没走 gh_ 前缀的公众号。"""
    p = _out_root() / account / "contact" / "contact.db"
    sig = _file_sig(p)
    if sig is None:
        return {}
    key = (str(p), "flags")
    with _CACHE_LOCK:
        hit = _CONTACT_NAME_CACHE.get(key)
        if hit is not None and hit[0] == sig:
            return hit[1]

    out: dict = {}
    try:
        conn = sqlite3.connect(f"file:{p}?mode=ro", uri=True)
    except sqlite3.Error:
        return {}
    try:
        for un, vf, al in conn.execute(
                "SELECT username, verify_flag, alias FROM contact"):
            un = (un or "").strip()
            if un:
                out[un] = (int(vf or 0), (al or "").strip())
    except sqlite3.Error:
        # 老测试/旧库可能没有 verify_flag；没有认证信息时只靠 username 规则过滤。
        pass
    finally:
        conn.close()
    with _CACHE_LOCK:
        _CONTACT_NAME_CACHE[key] = (sig, out)
    return out


def _best_contact_name(un, remark, nick, alias) -> str:
    """按聊天页同款优先级取联系人显示名。"""
    un = (un or "").strip()
    best = un
    for v in (remark, nick, alias):
        v = (v or "").strip()
        if v and v != un and "\ufffd" not in v:
            best = v
            break
    return best


def _contact_name_map(account: str) -> dict:
    """全量 username → 显示名缓存。

    日期范围切换时 top 20 可能不断变化，如果每次都按 IN 查询 contact.db，
    用户连续调范围会感觉卡。这里第一次读全表（真实样本约 3800 行，成本很低），
    后续所有范围直接内存取子集。
    """
    p = _out_root() / account / "contact" / "contact.db"
    sig = _file_sig(p)
    if sig is None:
        return {}
    key = (str(p), "names_all")
    with _CACHE_LOCK:
        hit = _CONTACT_NAME_CACHE.get(key)
        if hit is not None and hit[0] == sig:
            return hit[1]

    names: dict = {}
    try:
        conn = sqlite3.connect(f"file:{p}?mode=ro", uri=True)
    except sqlite3.Error:
        return names
    try:
        for un, remark, nick, alias in conn.execute(
                "SELECT username, remark, nick_name, alias FROM contact"):
            un = (un or "").strip()
            if un:
                names[un] = _best_contact_name(un, remark, nick, alias)
    except sqlite3.Error:
        # schema 不匹配时退化为无昵称，不影响统计主流程。
        names = {}
    finally:
        conn.close()
    with _CACHE_LOCK:
        _CONTACT_NAME_CACHE[key] = (sig, names)
    return names


def _contact_names(account: str, usernames) -> dict:
    """username → 显示名（备注 > 昵称 > 微信号）。"""
    wanted = sorted({(u or "").strip() for u in usernames if (u or "").strip()})
    if not wanted:
        return {}
    all_names = _contact_name_map(account)
    return {u: all_names.get(u, u) for u in wanted}


def _private_sender_ranking(account: str, by_chat: dict, limit: int = 20) -> list:
    """私聊发送者排行（带昵称）。

    `by_chat` 是主扫描顺带产出的「私聊会话 → 消息数」（见 `_scan_shard`），
    因此这里只做排序 + 查昵称，不额外扫库。
    """
    if not by_chat:
        return []
    top = sorted(by_chat.items(), key=lambda x: -x[1])[:limit]
    names = _contact_names(account, [u for u, _ in top])
    return [{"wxid": u, "name": names.get(u) or u, "count": n} for u, n in top]


def _sig_equal(a, b) -> bool:
    """比较两个签名。

    JSON 会把元组序列化成列表（`("a", 1)` → `["a", 1]`），因此不能直接用
    `tuple(a) != tuple(b)` —— 那会得到「元组里装着列表」与「元组里装着元组」
    的比较，永远不相等，磁盘缓存将永不命中。这里做一次显式归一化。
    """
    if a is None or b is None:
        return False
    try:
        na = [(str(x[0]), int(x[1]), int(x[2])) for x in a]
        nb = [(str(x[0]), int(x[1]), int(x[2])) for x in b]
    except (TypeError, IndexError, ValueError):
        return False
    return na == nb


def _load_disk_cache(account: str, sig):
    if sig is None:
        return None
    p = _cache_file(account)
    if not p.is_file():
        return None
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if data.get("version") != _CACHE_VERSION:
        return None
    if not _sig_equal(data.get("sig"), sig):
        return None
    stats = data.get("stats")
    return stats if isinstance(stats, dict) else None


def _save_disk_cache(account: str, sig, stats: dict) -> None:
    if sig is None:
        return
    p = _cache_file(account)
    try:
        p.write_text(json.dumps({"version": _CACHE_VERSION, "sig": list(sig),
                                 "stats": stats}, ensure_ascii=False),
                     encoding="utf-8")
    except OSError:
        pass


def compute_stats(account: str, force: bool = False, log=None) -> dict:
    """计算（或命中缓存返回）某账号的聊天统计。"""
    if not _msg_dir(account).is_dir():
        raise FileNotFoundError("账号不存在或未解密")

    sig = signature(account)
    if not force and sig is not None:
        with _CACHE_LOCK:
            hit = _MEM_CACHE.get(account)
            if hit is not None and _sig_equal(hit[0], sig):
                return hit[1]
        disk = _load_disk_cache(account, sig)
        if disk is not None:
            with _CACHE_LOCK:
                _MEM_CACHE[account] = (sig, disk)
            return disk

    stats = _scan(account, log=log)
    if sig is not None:
        _save_disk_cache(account, sig, stats)
        with _CACHE_LOCK:
            _MEM_CACHE[account] = (sig, stats)
    return stats


def _group_type_counts(type_counts: dict) -> list:
    """把原始类型分布归并为图表用的分组。"""
    raw = {int(k): v for k, v in (type_counts or {}).items()}
    used: set = set()
    out = []
    for key, label, fn in TYPE_GROUPS:
        cnt = sum(v for t, v in raw.items() if fn(t))
        if not cnt:
            continue
        for t in raw:
            if fn(t):
                used.add(t)
        out.append({"key": key, "label": label, "count": cnt})
    other = sum(v for t, v in raw.items() if t not in used)
    if other:
        out.append({"key": "other", "label": "其他", "count": other})
    out.sort(key=lambda x: -x["count"])
    return out


def summarize(stats: dict, start: str = None, end: str = None) -> dict:
    """把原始统计裁剪成面板要的形状，可选按日期区间过滤。

    start / end 接受 `YYYY-MM-DD` 或 `YYYY-MM`；过滤会影响整页指标：总量、
    类型分布、月度趋势、小时/星期活跃度、私聊发送者排行和时间跨度。
    """
    use_range = bool(start or end)
    if use_range and stats.get("by_day"):
        view = _range_stats(stats, start, end)
    else:
        view = stats

    months = view.get("by_month") or {}
    hours = view.get("by_hour") or [0] * 24
    weekdays = view.get("by_weekday") or [0] * 7
    total = view.get("total") or 0

    # 平均每条消息的活跃度参考：按当前范围内有消息的天数算日均
    days_active = len(view.get("by_day") or {}) or (_active_days(view) if total else 0)
    per_day = round(total / days_active, 1) if days_active else 0

    top_senders = view.get("top_senders")
    if top_senders is None:
        top_senders = _private_sender_ranking(stats.get("account") or "", view.get("by_chat") or {})

    return {
        "total": total,
        "chat_count": stats.get("chat_count") or 0,
        "shards": stats.get("shards") or 0,
        "span": {"min": view.get("ts_min") or 0, "max": view.get("ts_max") or 0},
        "generated_at": stats.get("generated_at") or 0,
        "peak_hour": hours.index(max(hours)) if any(hours) else 0,
        "peak_weekday": weekdays.index(max(weekdays)) if any(weekdays) else 0,
        "per_day": per_day,
        "type_groups": _group_type_counts(view.get("type_counts") or {}),
        "type_raw": [{"type": int(k), "label": TYPE_LABELS.get(int(k), f"类型{k}"),
                      "count": v}
                     for k, v in sorted((view.get("type_counts") or {}).items(),
                                        key=lambda x: -x[1])],
        "by_month": [{"month": k, "count": v} for k, v in sorted(months.items())],
        "by_hour": hours,
        "by_weekday": weekdays,
        "top_senders": top_senders or [],
        "top_senders_note": "仅统计私聊（已排除群聊与公众号）",
        "range": {"start": _norm_date_start(start) if start else None,
                  "end": _norm_date_end(end) if end else None},
    }


def _norm_date_start(v: str = None) -> str:
    """把 YYYY-MM / YYYY-MM-DD 归一化为区间起始日。"""
    if not v:
        return "0000-00-00"
    s = str(v).strip()
    if len(s) >= 10:
        return s[:10]
    if len(s) >= 7:
        return s[:7] + "-01"
    return s


def _norm_date_end(v: str = None) -> str:
    """把 YYYY-MM / YYYY-MM-DD 归一化为区间结束日（字符串比较安全）。"""
    if not v:
        return "9999-99-99"
    s = str(v).strip()
    if len(s) >= 10:
        return s[:10]
    if len(s) >= 7:
        return s[:7] + "-31"
    return s


def _range_stats(stats: dict, start: str = None, end: str = None) -> dict:
    """从 by_day 汇总出指定日期范围内的一套统计。"""
    s = _norm_date_start(start)
    e = _norm_date_end(end)
    out = {"total": 0, "type_counts": {}, "by_month": {}, "by_hour": [0] * 24,
           "by_weekday": [0] * 7, "by_chat": {}, "by_day": {},
           "ts_min": 0, "ts_max": 0}
    for day, d in sorted((stats.get("by_day") or {}).items()):
        if day < s or day > e:
            continue
        out["by_day"][day] = d
        out["total"] += d.get("total", 0)
        out["by_month"][day[:7]] = out["by_month"].get(day[:7], 0) + d.get("total", 0)
        _merge_type_counts(out["type_counts"], d.get("type_counts") or {})
        for i, v in enumerate(d.get("by_hour") or [0] * 24):
            out["by_hour"][i] += v
        for i, v in enumerate(d.get("by_weekday") or [0] * 7):
            out["by_weekday"][i] += v
        for un, v in (d.get("by_chat") or {}).items():
            out["by_chat"][un] = out["by_chat"].get(un, 0) + v
        mn, mx = d.get("ts_min") or 0, d.get("ts_max") or 0
        if mn and (not out["ts_min"] or mn < out["ts_min"]):
            out["ts_min"] = mn
        if mx > out["ts_max"]:
            out["ts_max"] = mx
    return out


def _active_days(stats: dict) -> int:
    """粗估活跃天数：用时间跨度，避免为统计再扫一遍库。"""
    span = (stats.get("ts_max") or 0) - (stats.get("ts_min") or 0)
    if span <= 0:
        return 0
    return max(1, int(span / 86400) + 1)


def clear_cache(account: str = None) -> None:
    """清除统计缓存（account 为 None 时清全部）。"""
    with _CACHE_LOCK:
        if account:
            _MEM_CACHE.pop(account, None)
            acc_contact = str(_out_root() / account / "contact" / "contact.db")
            for k in list(_CONTACT_NAME_CACHE):
                if k and k[0] == acc_contact:
                    _CONTACT_NAME_CACHE.pop(k, None)
        else:
            _MEM_CACHE.clear()
            _CONTACT_NAME_CACHE.clear()
    root = _out_root()
    if not root.is_dir():
        return
    targets = [root / account] if account else [d for d in root.iterdir() if d.is_dir()]
    for d in targets:
        try:
            (d / ".siwx_stats.json").unlink(missing_ok=True)
        except OSError:
            pass
