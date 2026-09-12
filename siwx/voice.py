"""微信语音消息读取。

微信 4.x 解密后的语音数据位于 message/media_*.db 的 VoiceInfo 表：
  - Name2Id.rowid -> chat_name_id
  - VoiceInfo.local_id / svr_id / create_time 与 Msg_* 表中的消息对应
  - VoiceInfo.voice_data 已是明文 SILK 数据；有些记录在 #!SILK_V3 前带 1 个控制字节，
    需要剥掉前缀后再作为 .silk 导出/下载。

注意：浏览器通常不能直接播放 SILK。当前模块负责把明文语音安全取出，前端提供
下载入口；若后续内置 SILK 解码器，可在本模块上层增加 wav/mp3 转码。
"""
import re
import sqlite3
from pathlib import Path


SILK_MAGIC = b"#!SILK_V3"


def parse_voice_meta(text: str) -> dict | None:
    """从 <voicemsg .../> XML 中提取展示元数据。"""
    if not text or "<voicemsg" not in text:
        return None
    m = re.search(r"<voicemsg\b([^>]*)/?>", text, re.S)
    if not m:
        return None
    attrs = dict(re.findall(r'([a-zA-Z_][\w:-]*)="([^"]*)"', m.group(1)))

    def as_int(name, default=0):
        try:
            return int(attrs.get(name) or default)
        except (TypeError, ValueError):
            return default

    return {
        "durationMs": as_int("voicelength"),
        "size": as_int("length"),
        "voiceFormat": as_int("voiceformat"),
        "clientMsgId": attrs.get("clientmsgid", ""),
        "fromUsername": attrs.get("fromusername", ""),
        "voiceMd5": attrs.get("voicemd5", ""),
        "silkLength": as_int("silklength"),
    }


def _clean_voice_data(data: bytes) -> tuple[bytes, int]:
    """返回可写出的语音数据与 SILK magic 的偏移。"""
    if not data:
        return b"", -1
    pos = data.find(SILK_MAGIC)
    if pos >= 0:
        return data[pos:], pos
    return data, -1


def _chat_id(conn, chat: str):
    if not chat:
        return None
    try:
        row = conn.execute("SELECT rowid FROM Name2Id WHERE user_name=?", (chat,)).fetchone()
        return int(row[0]) if row else None
    except (sqlite3.Error, TypeError, ValueError):
        return None


def _candidate_queries(chat_id, local_id, svr_id, ts):
    """按可信度生成 VoiceInfo 查询条件。"""
    if chat_id is not None and svr_id:
        yield "chat_name_id=? AND svr_id=?", (chat_id, svr_id)
    if chat_id is not None and local_id and ts:
        yield "chat_name_id=? AND local_id=? AND create_time=?", (chat_id, local_id, ts)
    if chat_id is not None and local_id:
        yield "chat_name_id=? AND local_id=?", (chat_id, local_id)
    if svr_id:
        yield "svr_id=?", (svr_id,)
    if local_id and ts:
        yield "local_id=? AND create_time=?", (local_id, ts)
    if local_id:
        yield "local_id=?", (local_id,)


def get_voice(acc_dir: Path, chat: str = "", local_id: int = 0,
              svr_id: int = 0, ts: int = 0) -> tuple[bytes | None, dict | str]:
    """读取一条语音消息。

    返回 (data, info)。data 为清理过前缀的 SILK/原始语音数据；找不到时
    返回 (None, reason)。
    """
    msg_dir = Path(acc_dir) / "message"
    if not msg_dir.is_dir():
        return None, "message 目录不存在"

    for db in sorted(msg_dir.glob("media_*.db"), reverse=True):
        try:
            conn = sqlite3.connect(db)
            try:
                if not conn.execute(
                        "SELECT name FROM sqlite_master WHERE type='table' AND name='VoiceInfo'"
                ).fetchone():
                    continue
                cid = _chat_id(conn, chat)
                for where, params in _candidate_queries(cid, local_id, svr_id, ts):
                    row = conn.execute(
                        "SELECT chat_name_id, create_time, local_id, svr_id, voice_data, data_index "
                        f"FROM VoiceInfo WHERE {where} ORDER BY create_time DESC LIMIT 1",
                        params).fetchone()
                    if not row or not row[4]:
                        continue
                    raw, offset = _clean_voice_data(bytes(row[4]))
                    if not raw:
                        continue
                    return raw, {
                        "db": db.name,
                        "chatNameId": row[0],
                        "createTime": row[1],
                        "localId": row[2],
                        "serverId": str(row[3] or ""),
                        "dataIndex": row[5] or "",
                        "size": len(raw),
                        "rawSize": len(row[4]),
                        "silkOffset": offset,
                        "format": "silk" if raw.startswith(SILK_MAGIC) else "unknown",
                    }
            finally:
                conn.close()
        except sqlite3.Error:
            continue
    return None, "语音数据不存在或尚未同步"
