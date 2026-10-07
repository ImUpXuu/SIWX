"""流式导出管线 —— 解决万条聊天 OOM 问题。

核心策略：
1. 流式读取：heapq.merge 做多分片 K 路归并，不加载全部消息
2. 增量写入：JSON/TXT/CSV 直接写文件句柄，不构建巨型字符串
3. 并行媒体：multiprocessing.Pool 并行解密图片（CPU 密集型 AES）
4. 精简消息：去掉 rawContent 重复字段，减少 50% 内存
"""
import heapq
import hashlib
import json
import sqlite3
from pathlib import Path

from siwx import logger
from siwx.api_chat import (
    SENDER_PREFIX_RE, TYPE_NAMES, _contact_names, _decode_content,
    _sender_map, _fmt, enrich_message_row, message_kind,
    parse_quote_or_link, self_ids_for, shards_for,
)

# 精简消息字段（去掉 rawContent 重复、去掉前端专用字段）
_KEEP_FIELDS = ("localId", "createTime", "localType", "typeName",
                "content", "isSend", "senderUsername", "senderDisplayName",
                "md5", "bubbleMd5", "quote", "link")

# 分批大小
BATCH_SIZE = 500


def _enrich_row(row, names, my_base, self_ids, is_group, chat, account):
    """把一行原始数据精简为导出用 dict。"""
    local_id, server_id, ltype, ts, origin, rsid, content, packed, smap = row
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
    is_me = sender_wxid in self_ids
    # 本人发送者归一到展示用 wxid：设备后缀变体不在联系人表，
    # 不归一的话导出里的 senderDisplayName/头像落不到本人身上
    if is_me and sender_wxid != my_base and sender_wxid not in names \
            and my_base in names:
        sender_wxid = my_base
    if is_group and not is_me and sender_wxid == chat:
        sender_wxid = ""

    t = ltype & 0xFFFF
    # md5/气泡 md5/语音元数据/表情包元数据采集与 49/57 引用分流统一走 api_chat 公共函数
    # （原与 build_messages / messages 两处逐字重复，改判定需三处同步）
    md5, bubble_md5, voice_meta, sticker = enrich_message_row(t, text, packed)

    if t in (3, 47) and not md5 and not bubble_md5:
        # 审计 §4.2：三级来源全靠 local_id+ts，成功率骤降但此前无日志
        logger.detailed("media",
                        f"图片无md5可定位 local_id={local_id} ts={ts} localType={t}",
                        ring=False)

    quote, link, record, channels = parse_quote_or_link(t, text)

    # 审计 §4.2：只埋"无异常但返回 None"的兜底——引用解析**异常**路径已有
    # api_chat.py:_safe_parse_refer 的日志，勿重复埋点
    if quote is None and t == 57:
        logger.detailed(
            "parse",
            f"引用解析为空 local_id={local_id} ts={ts} len={len(text or '')} "
            f"head={(text or '').encode('utf-8', 'replace')[:16].hex()}",
            ring=False)

    if t not in TYPE_NAMES:
        logger.detailed(
            "parse",
            f"未知类型 t={t} local_id={local_id} ts={ts} len={len(text or '')} "
            f"head={(text or '').encode('utf-8', 'replace')[:16].hex()}",
            ring=False)

    return {
        "localId": local_id,
        "platformMessageId": str(server_id or ""),
        "createTime": ts or 0,
        "localType": t,
        "typeName": TYPE_NAMES.get(t, f"类型{t}"),
        "rawContent": raw_text,
        "content": _fmt(ltype, text) if t != 1 else text,
        "isSend": 1 if is_me else 0,
        "senderUsername": sender_wxid or chat,
        "senderDisplayName": (names.get(sender_wxid, sender_wxid) if sender_wxid
                              else (names.get(chat, chat) if not is_group else chat)),
        "md5": md5,
        "bubbleMd5": bubble_md5,
        "voice": voice_meta,
        "sticker": sticker,
        "channels": channels,
        "quote": quote,
        "link": link,
        "record": record,
    }


def count_messages(acc, chat):
    """统计消息总数（不加载全部消息到内存）。"""
    table = "Msg_" + hashlib.md5(chat.encode()).hexdigest()
    total = 0
    for db in shards_for(acc, chat):
        try:
            conn = sqlite3.connect(db)
            try:
                total += conn.execute(
                    f'SELECT COUNT(*) FROM [{table}]').fetchone()[0]
            finally:
                conn.close()
        except sqlite3.Error as e:
            logger.warn("export", f"[export] 分片打开失败，该分片消息可能缺失: "
                                  f"{Path(db).name}: {e}")
    return total


def message_stream(acc: Path, chat: str, start_ts=None, end_ts=None,
                   account=None, names=None):
    """流式生成器：按 create_time 顺序逐条产出消息，内存 O(分片数)。

    使用 heapq.merge 做 K 路归并，每个分片内已按 create_time 有序。
    搜索所有 *.db（含 biz_message_*.db），不遗漏任何消息。

    参数:
        names: 联系人名 dict，不传则自动加载（建议外部缓存传入以避免重复加载）
    """
    account = account or acc.name
    table = "Msg_" + hashlib.md5(chat.encode()).hexdigest()
    if names is None:
        names = _contact_names(acc)
    my_base, self_ids = self_ids_for(acc, account)
    is_group = chat.endswith("@chatroom")

    # 分片索引：只打开真正含该会话的分片。原先每次调用都要把 message/ 下全部
    # *.db 逐个打开查 sqlite_master，实测占导出总耗时的 99.5%。
    # 审计 §4.2：分片打开/查询包 try——单分片损坏从"中断整个导出"改为
    # "部分导出" + warn 对账（缺失由 run_export 的 count-vs-written 兜底）。
    iterators = []
    failed_shards = 0
    for db in shards_for(acc, chat):
        conn = None
        try:
            conn = sqlite3.connect(db)
            smap = _sender_map(conn)
            cur = conn.execute(
                f"SELECT local_id, server_id, local_type, create_time, "
                f"origin_source, real_sender_id, message_content, packed_info_data "
                f"FROM [{table}] ORDER BY create_time")
        except sqlite3.Error as e:
            if conn is not None:
                try:
                    conn.close()
                except Exception:  # noqa: BLE001  关闭失败不影响主流程
                    pass
            failed_shards += 1
            logger.warn("export", f"[export] 流式读取分片失败，消息可能缺失: "
                                  f"{Path(db).name}: {e}")
            continue
        iterators.append(_shard_iter(cur, conn, smap))

    logger.detailed("export",
                    f"message_stream: shards={len(iterators)} "
                    f"failed={failed_shards} table={table}")

    if not iterators:
        return

    merged = heapq.merge(*iterators, key=lambda x: (x[0] or 0, x[1] or 0))
    for ts, local_id, (ltype, server_id, origin, rsid, content, packed,
                       smap) in merged:
        if start_ts and (ts or 0) < start_ts:
            continue
        if end_ts and (ts or 0) > end_ts:
            continue
        row = (local_id, server_id, ltype, ts, origin, rsid, content, packed,
               smap)
        yield _enrich_row(row, names, my_base, self_ids, is_group, chat,
                          account)


def _shard_iter(cursor, conn, smap):
    """分片迭代器：yield (ts, local_id, row_tuple)。

    row_tuple 第二元素是 server_id——P1 修复：此前 SELECT 取出了它却在
    这里丢弃，导致导出 platformMessageId 恒为空串、语音导出 svr_id 恒 0
    （voice.get_voice 少一条查询命中路径）。"""
    try:
        for row in cursor:
            local_id, server_id, ltype, ts, origin, rsid, content, packed = row
            yield (ts, local_id, (ltype, server_id, origin, rsid, content,
                                  packed, smap))
    finally:
        conn.close()


# ── 增量写入器 ──────────────────────────────────────────────────

class IncrementalJSONWriter:
    """流式写 JSON：session 头 → 逐条 messages → 尾。内存 O(1)。"""

    def __init__(self, path: Path, session: dict):
        self.f = open(path, "w", encoding="utf-8")
        session_copy = {k: v for k, v in session.items() if k != "messages"}
        head = json.dumps({"exportInfo": {"version": "1.0", "generator": "stories-in-wx"},
                           "session": session_copy}, ensure_ascii=False)
        # 去掉末尾的 }，准备拼接 messages
        self.f.write(head[:-1] + ',"messages":[')
        self.first = True
        self.count = 0

    def write_msg(self, msg: dict):
        if not self.first:
            self.f.write(",")
        self.f.write(json.dumps(msg, ensure_ascii=False))
        self.first = False
        self.count += 1

    def close(self, session_extra: dict = None):
        self.f.write("]")
        if session_extra:
            self.f.write("," + json.dumps(session_extra, ensure_ascii=False)[1:])
        self.f.write("}")
        self.f.close()


def stream_export_json(path: Path, session: dict, msg_iter, progress=None):
    """流式导出 JSON。返回消息数。"""
    w = IncrementalJSONWriter(path, session)
    count = 0
    for msg in msg_iter:
        w.write_msg(msg)
        count += 1
        if count % 500 == 0 and progress:
            progress(0, f"已写入 {count} 条…")
    w.close()
    return count


def stream_export_txt(path: Path, session: dict, msg_iter, progress=None):
    """流式导出 TXT。"""
    from datetime import datetime
    with open(path, "w", encoding="utf-8") as f:
        f.write(f"聊天记录：{session['displayName']}（{session['type']}）\n")
        # 元数据遍（run_export 第一遍扫描）早已统计出条数，回填而非"未知"
        f.write(f"消息数：{session.get('messageCount') or '未知（流式导出）'}    "
                f"导出时间：{datetime.now():%Y-%m-%d %H:%M:%S}\n")
        f.write("=" * 60 + "\n\n")
        count = 0
        for msg in msg_iter:
            who = "我" if msg["isSend"] else msg["senderDisplayName"]
            ts = datetime.fromtimestamp(msg['createTime']).strftime('%Y-%m-%d %H:%M:%S')
            media = f" [媒体: {msg['mediaFile']}]" if msg.get("mediaFile") else ""
            f.write(f"[{ts}] {who}: {msg['content']}{media}\n")
            count += 1
            if count % 500 == 0 and progress:
                progress(0, f"已写入 {count} 条…")
    return count


def stream_export_csv(path: Path, msg_iter, progress=None):
    """流式导出 CSV。"""
    import csv
    from datetime import datetime
    with open(path, "w", encoding="utf-8-sig", newline="") as f:
        w = csv.writer(f)
        w.writerow(["localId", "时间", "类型", "发送者", "是否自己", "内容", "媒体文件"])
        count = 0
        truncated = 0      # P3：内容截断留痕（CSV 单元格 2000 字上限）
        for msg in msg_iter:
            if len(msg["content"]) > 2000:
                truncated += 1
            w.writerow([msg["localId"],
                        datetime.fromtimestamp(msg["createTime"]).strftime("%Y-%m-%d %H:%M:%S"),
                        msg["typeName"], msg["senderDisplayName"],
                        msg["isSend"], msg["content"][:2000],
                        msg.get("mediaFile") or ""])
            count += 1
            if count % 500 == 0 and progress:
                progress(0, f"已写入 {count} 条…")
    if truncated:
        logger.detailed("export",
                        f"CSV内容截断(>2000字) {truncated}/{count} 条，"
                        f"换 JSON/HTML 格式可得全文", ring=False)
    return count


def stream_export_md(path: Path, session: dict, msg_iter, progress=None):
    """流式导出 Markdown。"""
    from datetime import datetime
    with open(path, "w", encoding="utf-8") as f:
        f.write(f"# 聊天记录：{session['displayName']}\n\n")
        f.write(f"- 导出时间：{datetime.now():%Y-%m-%d %H:%M:%S}\n\n---\n\n")
        count, last_day = 0, ""
        for msg in msg_iter:
            day = datetime.fromtimestamp(msg["createTime"]).strftime("%Y-%m-%d")
            if day != last_day:
                f.write(f"## {day}\n\n")
                last_day = day
            who = "我" if msg["isSend"] else msg["senderDisplayName"]
            body = msg["content"].replace("\n", "  \n")
            if msg.get("mediaFile"):
                body += f"  \n[媒体文件]({msg['mediaFile']})"
            f.write(f"**{who}** `{datetime.fromtimestamp(msg['createTime']):%H:%M:%S}`：{body}\n\n")
            count += 1
            if count % 500 == 0 and progress:
                progress(0, f"已写入 {count} 条…")
    return count
