"""导出引擎 —— 聊天记录多格式导出（JSON/HTML/TXT/CSV/Markdown/TOML/SQLite/XLSX）。

支持勾选：消息 / 媒体（解密图片落盘到导出目录）/ 头像（head_image 提取）。
进度通过 progress(pct, msg) 回调上报。
"""
import csv
import hashlib
import json
import shutil
import sqlite3
import time
from datetime import datetime
from pathlib import Path

from siwx import media
from siwx import paths as _paths
from siwx.api_chat import build_messages, _contact_names
from siwx.html_template import build_chat_data, render_html

GENERATOR = "stories-in-wx"
EXPORT_VERSION = "1.0"


def _safe_name(name: str) -> str:
    for ch in '<>:"/\\|?*':
        name = name.replace(ch, "_")
    return name[:48].strip() or "chat"


def _fmt_time(ts) -> str:
    return datetime.fromtimestamp(ts or 0).strftime("%Y-%m-%d %H:%M:%S")


def collect_avatars(acc_out_dir: Path, usernames: list, dest: Path, progress):
    """从 head_image.db 提取头像 → avatars/<md5(username)>.jpg。返回 {username: 相对路径}。"""
    db = acc_out_dir / "head_image" / "head_image.db"
    mapping = {}
    if not db.is_file():
        return mapping
    conn = sqlite3.connect(db)
    try:
        done = 0
        for i, un in enumerate(usernames):
            row = conn.execute(
                "SELECT image_buffer FROM head_image WHERE username=?", (un,)).fetchone()
            if row and row[0]:
                fn = hashlib.md5(un.encode()).hexdigest() + ".jpg"
                (dest / fn).write_bytes(row[0])
                mapping[un] = f"avatars/{fn}"
            done += 1
            if done % 50 == 0:
                progress(60 + int(done / max(len(usernames), 1) * 15),
                         f"提取头像 {done}/{len(usernames)}")
    finally:
        conn.close()
    return mapping


def export_media_files(acc_out_dir: Path, account: str, msgs: list, dest: Path,
                       progress):
    """解密消息图片 → media/0001_<md5>.jpg。返回 {localId: 相对路径}。"""
    out = {}
    imgs = [m for m in msgs if m.get("md5") or m.get("bubble_md5")]
    dest.mkdir(parents=True, exist_ok=True)
    for i, m in enumerate(imgs):
        try:
            body, ctype = media.get_image(
                account, m.get("md5") or "", acc_out_dir,
                chat=m.get("_chat"), local_id=m["localId"], ts=m["createTime"],
                bubble_md5=m.get("bubbleMd5"))
        except Exception:
            body = None
        if body:
            ext = "png" if "png" in ctype else ("gif" if "gif" in ctype else "jpg")
            fn = f"{i:04d}_{(m.get('md5') or 'img')[:12]}.{ext}"
            (dest / fn).write_bytes(body)
            out[m["localId"]] = f"media/{fn}"
        if i % 3 == 0:
            progress(45 + int(i / max(len(imgs), 1) * 40),
                     f"解密媒体 {i + 1}/{len(imgs)}")
    return out


# ── 各格式写入器 ────────────────────────────────────────────────────

def _write_json(path: Path, session: dict, msgs: list, export_dir: Path):
    payload = {
        "exportInfo": {"version": EXPORT_VERSION,
                       "exportedAt": int(time.time()),
                       "generator": GENERATOR, "format": "detailed-json"},
        "session": session,
        "messages": [{
            "localId": m["localId"],
            "platformMessageId": m["platformMessageId"],
            "createTime": m["createTime"],
            "formattedTime": _fmt_time(m["createTime"]),
            "type": m["typeName"],
            "localType": m["localType"],
            "content": m["content"],
            "rawContent": m["rawContent"],
            "isSend": m["isSend"],
            "senderUsername": m["senderUsername"],
            "senderDisplayName": m["senderDisplayName"],
            **({"mediaFile": m["mediaFile"]} if m.get("mediaFile") else {}),
            **({"quote": m["quote"]} if m.get("quote") else {}),
            **({"link": m["link"]} if m.get("link") else {}),
        } for m in msgs],
    }
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def _write_txt(path: Path, session: dict, msgs: list):
    lines = [f"聊天记录：{session['displayName']}（{session['type']}）",
             f"消息数：{len(msgs)}    导出时间：{_fmt_time(int(time.time()))}",
             "=" * 60, ""]
    for m in msgs:
        lines.append(f"[{_fmt_time(m['createTime'])}] "
                     f"{'我' if m['isSend'] else m['senderDisplayName']}: {m['content']}")
    path.write_text("\n".join(lines), encoding="utf-8")


def _write_csv(path: Path, msgs: list):
    import codecs
    with open(path, "w", encoding="utf-8-sig", newline="") as f:
        w = csv.writer(f)
        w.writerow(["localId", "时间", "类型", "发送者", "是否自己", "内容", "rawContent"])
        for m in msgs:
            w.writerow([m["localId"], _fmt_time(m["createTime"]), m["typeName"],
                        m["senderDisplayName"], m["isSend"], m["content"],
                        m["rawContent"][:2000]])


def _write_md(path: Path, session: dict, msgs: list):
    lines = [f"# 聊天记录：{session['displayName']}", "",
             f"- 消息数：**{len(msgs)}**", f"- 导出时间：{_fmt_time(int(time.time()))}",
             "", "---", ""]
    last_day = ""
    for m in msgs:
        day = _fmt_time(m["createTime"])[:10]
        if day != last_day:
            lines.append(f"## {day}")
            lines.append("")
            last_day = day
        who = "我" if m["isSend"] else m["senderDisplayName"]
        body = m["content"].replace("\n", "  \n")
        if m.get("mediaFile"):
            body = f"![图片]({m['mediaFile']})"
        lines.append(f"**{who}** `{_fmt_time(m['createTime'])[11:]}`：{body}")
        lines.append("")
    path.write_text("\n".join(lines), encoding="utf-8")


def _write_toml(path: Path, session: dict, msgs: list):
    def toml_str(s: str) -> str:
        import json
        return json.dumps(str(s), ensure_ascii=False)   # JSON 字符串转义与 TOML 基本串兼容

    lines = ["[exportInfo]",
             f'version = {toml_str(EXPORT_VERSION)}',
             f'generator = {toml_str(GENERATOR)}',
             f'exportedAt = {int(time.time())}',
             "", "[session]"]
    for k, v in session.items():
        lines.append(f"{k} = {toml_str(v) if isinstance(v, str) else v}")
    lines.append("")
    for m in msgs:
        lines.append("[[messages]]")
        lines.append(f"localId = {m['localId']}")
        lines.append(f"createTime = {toml_str(_fmt_time(m['createTime']))}")
        lines.append(f"type = {toml_str(m['typeName'])}")
        lines.append(f"sender = {toml_str(m['senderDisplayName'])}")
        lines.append(f"isSend = {m['isSend']}")
        lines.append(f"content = {toml_str(m['content'])}")
        if m.get("mediaFile"):
            lines.append(f"mediaFile = {toml_str(m['mediaFile'])}")
        lines.append("")
    path.write_text("\n".join(lines), encoding="utf-8")


def _write_sqlite(path: Path, session: dict, msgs: list):
    if path.exists():
        path.unlink()
    conn = sqlite3.connect(path)
    conn.executescript("""
        CREATE TABLE session (
            wxid TEXT, nickname TEXT, type TEXT, isGroup INTEGER,
            messageCount INTEGER, firstTimestamp INTEGER, lastTimestamp INTEGER);
        CREATE TABLE messages (
            localId INTEGER, createTime INTEGER, formattedTime TEXT,
            localType INTEGER, typeName TEXT, isSend INTEGER,
            senderUsername TEXT, senderDisplayName TEXT,
            content TEXT, rawContent TEXT, mediaFile TEXT);
        CREATE UNIQUE INDEX msg_uniq ON messages(createTime, localId);
    """)
    conn.execute("INSERT INTO session VALUES (?,?,?,?,?,?,?)",
                 (session["wxid"], session["displayName"], session["type"],
                  session["isGroup"], len(msgs),
                  session["firstTimestamp"], session["lastTimestamp"]))
    conn.executemany("INSERT INTO messages VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                     [(m["localId"], m["createTime"], _fmt_time(m["createTime"]),
                       m["localType"], m["typeName"], m["isSend"],
                       m["senderUsername"], m["senderDisplayName"],
                       m["content"], m["rawContent"][:8000],
                       m.get("mediaFile")) for m in msgs])
    conn.commit()
    conn.close()


def _write_xlsx(path: Path, msgs: list):
    from openpyxl import Workbook
    wb = Workbook()
    ws = wb.active
    ws.title = "聊天记录"
    ws.append(["localId", "时间", "类型", "发送者", "是否自己", "内容"])
    for m in msgs:
        ws.append([m["localId"], _fmt_time(m["createTime"]), m["typeName"],
                   m["senderDisplayName"], "是" if m["isSend"] else "否",
                   m["content"][:30000]])
    wb.save(path)


def run_export(acc_out_dir: Path, account: str, chat: str, display: str,
               fmt: str, start_ts=None, end_ts=None,
               want_messages=True, want_media=True, want_avatars=True,
               export_root: Path = None, pack: str = "zip",
               progress=lambda pct, msg: None) -> dict:
    t0 = time.time()
    progress(3, "读取消息…")
    msgs = build_messages(acc_out_dir, chat, start_ts, end_ts, account=account)
    names = _contact_names(acc_out_dir)
    display = display or names.get(chat, chat) or chat
    safe = _safe_name(display)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    root = (export_root or _paths.exports_root())
    export_dir = root / f"{stamp}_{safe}"
    export_dir.mkdir(parents=True, exist_ok=True)

    _ok = False
    try:
        session = {
            "wxid": chat, "nickname": names.get(chat, chat), "remark": names.get(chat, ""),
            "displayName": display,
            "type": "群聊" if chat.endswith("@chatroom") else "私聊",
            "platform": "wechat", "isGroup": chat.endswith("@chatroom"),
            "ownerId": account,
            "firstTimestamp": msgs[0]["createTime"] if msgs else 0,
            "lastTimestamp": msgs[-1]["createTime"] if msgs else 0,
            "messageCount": len(msgs),
        }

        # 头像
        avatar_map = {}
        if want_avatars:
            progress(55, "提取头像…")
            users = list({m["senderUsername"] for m in msgs} | {chat})
            dest = export_dir / "avatars"
            dest.mkdir(exist_ok=True)
            avatar_map = collect_avatars(acc_out_dir, users, dest, progress)
            session["_avatar_map"] = avatar_map
        stats_ava = len(avatar_map)

        # 媒体（解密图片落盘到导出目录）
        stats_media = 0
        if want_media and any(m.get("md5") or m.get("bubbleMd5") for m in msgs):
            progress(40, "解密媒体图片…")
            for m in msgs:
                m["_chat"] = chat
                m["bubbleMd5"] = m.get("bubble_md5")
            media_map = export_media_files(acc_out_dir, account, msgs,
                                           export_dir / "media", progress)
            for m in msgs:
                m["mediaFile"] = media_map.get(m["localId"])
            stats_media = len(media_map)

        progress(88, f"写入 {fmt.upper()} …")
        content_msgs = msgs if want_messages else []
        fname = f"{safe}_{stamp}"
        fmt = fmt.lower()
        ext = {"json": "json", "html": "html", "txt": "txt", "csv": "csv",
               "markdown": "md", "toml": "toml", "sqlite": "db",
               "xlsx": "xlsx"}.get(fmt, "json")
        out_file = export_dir / f"{fname}.{ext}"
        if fmt == "json":
            _write_json(out_file, session, content_msgs, export_dir)
        elif fmt == "html":
            chat_data = build_chat_data(session, content_msgs, avatar_map)
            out_file.write_text(render_html(chat_data), encoding="utf-8")
        elif fmt == "txt":
            _write_txt(out_file, session, content_msgs)
        elif fmt == "csv":
            _write_csv(out_file, content_msgs)
        elif fmt == "markdown":
            _write_md(out_file, session, content_msgs)
        elif fmt == "toml":
            _write_toml(out_file, session, content_msgs)
        elif fmt == "sqlite":
            _write_sqlite(out_file, session, content_msgs)
        elif fmt == "xlsx":
            _write_xlsx(out_file, content_msgs)
        else:
            raise ValueError(f"未知格式: {fmt}")

        # 打包
        zip_path = None
        if pack == "zip":
            progress(94, "打包 zip…")
            zip_path = shutil.make_archive(str(root / f"{fname}_{fmt}"), "zip",
                                           root_dir=export_dir)
            shutil.rmtree(export_dir, ignore_errors=True)

        progress(100, "导出完成")
        result = {
            "export_dir": str(root) if pack == "zip" else str(export_dir),
            "zip": zip_path,
            "file": str(out_file),
            "format": fmt,
            "pack": pack,
            "message_count": len(content_msgs),
            "media_count": stats_media,
            "avatar_count": stats_ava,
            "duration_ms": int((time.time() - t0) * 1000),
        }
        _ok = True
        return result
    finally:
        if not _ok:
            shutil.rmtree(export_dir, ignore_errors=True)


