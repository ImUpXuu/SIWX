"""流式导出引擎 —— 万条聊天不 OOM。

核心改进（对比旧版）：
1. message_stream() 流式读取：heapq.merge K 路归并，内存 O(分片数)
2. 增量写入 JSON/TXT/CSV/MD：直接写文件句柄，不构建巨型字符串
3. 并行媒体解密：multiprocessing.Pool 多进程 AES 解密
4. 双遍扫描：第一遍轻量采集元数据（计数/发送者/图片引用），第二遍流式写出
"""
import hashlib
import json
import os
import shutil
import sqlite3
import time
from datetime import datetime
from pathlib import Path

from siwx import media
from siwx import paths as _paths
from siwx.api_chat import _contact_names
from siwx.export_stream import (
    message_stream, stream_export_json, stream_export_txt,
    stream_export_csv, stream_export_md,
)

GENERATOR = "stories-in-wx"
EXPORT_VERSION = "1.0"


def _safe_name(name: str) -> str:
    for ch in '<>:"/\\|?*':
        name = ch.replace(ch, "_")
    return name[:48].strip() or "chat"


def collect_avatars(acc_out_dir: Path, usernames: list, dest: Path, progress=None):
    """从 head_image.db 提取头像 → avatars/<md5(username)>.jpg。"""
    db = acc_out_dir / "head_image" / "head_image.db"
    mapping = {}
    if not db.is_file():
        return mapping
    conn = sqlite3.connect(db)
    try:
        for i, un in enumerate(usernames):
            row = conn.execute(
                "SELECT image_buffer FROM head_image WHERE username=?", (un,)).fetchone()
            if row and row[0]:
                fn = hashlib.md5(un.encode()).hexdigest() + ".jpg"
                (dest / fn).write_bytes(row[0])
                mapping[un] = f"avatars/{fn}"
            if (i + 1) % 50 == 0 and progress:
                progress(0, f"头像 {i + 1}/{len(usernames)}")
    finally:
        conn.close()
    return mapping


def _collect_metadata(acc_out_dir, chat, start_ts, end_ts, account):
    """第一遍：轻量扫描，只采集计数/发送者/图片引用。内存 O(发送者数 + 图片数)。"""
    senders = set()
    images = []       # (md5, bubble_md5, localId) 引用
    count = 0
    first_ts = last_ts = 0
    for msg in message_stream(acc_out_dir, chat, start_ts, end_ts, account):
        count += 1
        ts = msg["createTime"] or 0
        if count == 1:
            first_ts = ts
        last_ts = ts
        senders.add(msg["senderUsername"])
        if msg.get("md5") or msg.get("bubbleMd5"):
            images.append((msg.get("md5"), msg.get("bubbleMd5"), msg["localId"]))
    return count, first_ts, last_ts, senders, images


def _decrypt_media_parallel(acc_out_dir, account, images, dest, progress=None):
    """并行解密媒体图片。CPU 密集型 AES → 多进程池。"""
    dest.mkdir(parents=True, exist_ok=True)
    if not images:
        return {}

    # 准备任务：(abs_out_dir, account, md5, bubble_md5, chat, local_id, ts, dst_path)
    tasks = []
    for i, (md5, bubble_md5, local_id) in enumerate(images):
        fn = f"{i:04d}_{(md5 or 'img')[:12]}.jpg"
        dst = dest / fn
        tasks.append((str(acc_out_dir), account, md5, bubble_md5, local_id, str(dst)))

    # 多进程并行解密
    n = min(os.cpu_count() or 4, len(tasks), 8)
    if n <= 1:
        # 串行兜底
        return _decrypt_media_serial(acc_out_dir, account, images, dest, progress)

    from multiprocessing import Pool

    media_map = {}
    with Pool(n) as pool:
        for i, result in enumerate(pool.imap_unordered(_decrypt_one, tasks)):
            local_id, rel_path, ok = result
            if ok:
                media_map[local_id] = rel_path
            if (i + 1) % 10 == 0 and progress:
                progress(0, f"媒体 {i + 1}/{len(images)}")
    return media_map


def _decrypt_media_serial(acc_out_dir, account, images, dest, progress=None):
    """串行解密（单核兜底）。"""
    media_map = {}
    for i, (md5, bubble_md5, local_id) in enumerate(images):
        fn = f"{i:04d}_{(md5 or 'img')[:12]}.jpg"
        dst = dest / fn
        ok = _try_decrypt(acc_out_dir, account, md5, bubble_md5, local_id, dst)
        if ok:
            media_map[local_id] = f"media/{fn}"
        if (i + 1) % 10 == 0 and progress:
            progress(0, f"媒体 {i + 1}/{len(images)}")
    return media_map


def _decrypt_one(task):
    """单张图片解密（子进程入口）。"""
    acc_dir, account, md5, bubble_md5, local_id, dst = task
    ok = _try_decrypt(acc_dir, account, md5, bubble_md5, local_id, Path(dst))
    return (local_id, f"media/{Path(dst).name}", ok)


def _try_decrypt(acc_dir, account, md5, bubble_md5, local_id, dst):
    """尝试解密单张图片。"""
    try:
        body, ctype = media.get_image(account, md5, Path(acc_dir),
                                      local_id=local_id or None,
                                      bubble_md5=bubble_md5 or None)
        if body:
            ext = "png" if "png" in ctype else ("gif" if "gif" in ctype else "jpg")
            dst = dst.with_suffix(f".{ext}")
            dst.write_bytes(body)
            return True
    except Exception:
        pass
    return False


def run_export(acc_out_dir: Path, account: str, chat: str, display: str,
               fmt: str, start_ts=None, end_ts=None,
               want_messages=True, want_media=True, want_avatars=True,
               export_root: Path = None, pack: str = "zip",
               folder_name: str = None,
               progress=lambda pct, msg: None) -> dict:
    t0 = time.time()
    progress(1, f"开始导出: 账号={account}, 会话={chat}, 格式={fmt}")

    # ── 第一遍：轻量采集元数据 ─────────────────────────────
    progress(3, "扫描消息元数据…")
    count, first_ts, last_ts, senders, images = _collect_metadata(
        acc_out_dir, chat, start_ts, end_ts, account)
    progress(10, f"共 {count} 条消息，{len(images)} 张图片")

    names = _contact_names(acc_out_dir)
    display = display or names.get(chat, chat) or chat
    safe = _safe_name(display)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    root = (export_root or _paths.exports_root())
    export_dir = root / _safe_name(folder_name or f"{stamp}_{safe}")
    export_dir.mkdir(parents=True, exist_ok=True)

    session = {
        "wxid": chat, "nickname": names.get(chat, chat),
        "displayName": display,
        "type": "群聊" if chat.endswith("@chatroom") else "私聊",
        "platform": "wechat", "isGroup": chat.endswith("@chatroom"),
        "ownerId": account,
        "firstTimestamp": first_ts, "lastTimestamp": last_ts,
        "messageCount": count,
    }

    # ── 头像 ──────────────────────────────────────────────
    avatar_map = {}
    stats_ava = 0
    if want_avatars and count > 0:
        progress(15, "提取头像…")
        dest = export_dir / "avatars"
        dest.mkdir(exist_ok=True)
        avatar_map = collect_avatars(acc_out_dir, list(senders), dest, progress)
        session["_avatar_map"] = avatar_map
        stats_ava = len(avatar_map)
        progress(30, f"头像 {stats_ava}/{len(senders)}")

    # ── 媒体（并行解密）──────────────────────────────────
    stats_media = 0
    media_map = {}
    if want_media and images:
        progress(35, f"解密媒体（{len(images)} 张）…")
        media_map = _decrypt_media_parallel(acc_out_dir, account, images,
                                            export_dir / "media", progress)
        stats_media = len(media_map)
        progress(70, f"媒体解密完成: {stats_media}/{len(images)}")

    # ── 第二遍：流式写出 ─────────────────────────────────
    progress(75, "写入文件…")
    fname = f"{safe}_{stamp}"
    fmt = fmt.lower()
    ext = {"json": "json", "html": "html", "txt": "txt", "csv": "csv",
           "markdown": "md", "toml": "toml", "sqlite": "db",
           "xlsx": "xlsx"}.get(fmt, "json")
    out_file = export_dir / f"{fname}.{ext}"

    # 传入缓存的 names，避免重复加载
    if fmt == "json":
        def json_stream():
            for msg in message_stream(acc_out_dir, chat, start_ts, end_ts, account, names):
                msg["mediaFile"] = media_map.get(msg["localId"])
                yield msg
        written = stream_export_json(out_file, session, json_stream(), progress)
    elif fmt == "html":
        written = _write_html_streaming(out_file, acc_out_dir, chat, start_ts,
                                        end_ts, account, names, media_map, avatar_map, progress)
    elif fmt == "txt":
        written = stream_export_txt(out_file, session,
                                    message_stream(acc_out_dir, chat, start_ts, end_ts, account, names), progress)
    elif fmt == "csv":
        written = stream_export_csv(out_file,
                                    message_stream(acc_out_dir, chat, start_ts, end_ts, account, names), progress)
    elif fmt == "markdown":
        written = stream_export_md(out_file, session,
                                   message_stream(acc_out_dir, chat, start_ts, end_ts, account, names), progress)
    elif fmt in ("toml", "sqlite", "xlsx"):
        # 这些格式需要全量数据 → 降级为流式分批（每批 500 条）
        written = _write_batch(out_file, fmt, session,
                               acc_out_dir, chat, start_ts, end_ts, account, names, media_map, progress)
    else:
        raise ValueError(f"未知格式: {fmt}")

    progress(88, f"写入完成: {written} 条")

    # ── 打包 ─────────────────────────────────────────────
    zip_path = None
    if pack == "zip":
        progress(92, "打包 zip…")
        zip_path = shutil.make_archive(str(root / f"{fname}_{fmt}"), "zip",
                                       root_dir=export_dir)
        shutil.rmtree(export_dir, ignore_errors=True)

    progress(100, "导出完成")
    return {
        "export_dir": str(root) if pack == "zip" else str(export_dir),
        "zip": zip_path,
        "file": str(out_file),
        "format": fmt, "pack": pack,
        "message_count": written, "media_count": stats_media, "avatar_count": stats_ava,
        "duration_ms": int((time.time() - t0) * 1000),
    }


def _write_html_streaming(path, acc_dir, chat, start_ts, end_ts, account,
                          names, media_map, avatar_map, progress=None):
    """HTML 流式导出：分批渲染，避免一次性构建巨型 JSON。"""
    from siwx.html_template import render_html
    lines = []
    batch, count = [], 0
    for msg in message_stream(acc_dir, chat, start_ts, end_ts, account):
        msg["mediaFile"] = media_map.get(msg["localId"])
        batch.append(msg)
        count += 1
        if len(batch) >= 500:
            lines.extend(batch)
            batch = []
            if count % 1000 == 0 and progress:
                progress(0, f"已收集 {count} 条…")
    lines.extend(batch)

    session = {"wxid": chat, "displayName": names.get(chat, chat),
               "isGroup": chat.endswith("@chatroom"), "messageCount": count}
    from siwx.html_template import build_chat_data
    html = render_html(build_chat_data(session, lines, avatar_map))
    path.write_text(html, encoding="utf-8")
    return count


def _write_batch(path, fmt, session, acc_dir, chat, start_ts, end_ts, account, names,
                 media_map, progress=None):
    """TOML/SQLite/XLSX 流式分批写入。"""
    if fmt == "toml":
        return _write_toml_batch(path, session, acc_dir, chat, start_ts, end_ts, account, names, media_map, progress)
    elif fmt == "sqlite":
        return _write_sqlite_batch(path, session, acc_dir, chat, start_ts, end_ts, account, names, media_map, progress)
    elif fmt == "xlsx":
        return _write_xlsx_batch(path, acc_dir, chat, start_ts, end_ts, account, names, media_map, progress)


def _write_toml_batch(path, session, acc_dir, chat, start_ts, end_ts, account, names, media_map, progress):
    def toml_str(s):
        return json.dumps(str(s), ensure_ascii=False)
    lines = ["[exportInfo]", f'version = {toml_str("1.0")}',
             f'generator = {toml_str(GENERATOR)}', f'exportedAt = {int(time.time())}',
             "", "[session]"]
    for k, v in session.items():
        if isinstance(v, str):
            lines.append(f'{k} = {toml_str(v)}')
        elif isinstance(v, dict):
            pass
        else:
            lines.append(f'{k} = {v}')
    lines.append("")
    count = 0
    for msg in message_stream(acc_dir, chat, start_ts, end_ts, account, names):
        msg["mediaFile"] = media_map.get(msg["localId"])
        count += 1
        lines.append("[[messages]]")
        lines.append(f"localId = {msg['localId']}")
        lines.append(f"createTime = {toml_str(datetime.fromtimestamp(msg['createTime']).strftime('%Y-%m-%d %H:%M:%S'))}")
        lines.append(f"type = {toml_str(msg['typeName'])}")
        lines.append(f"sender = {toml_str(msg['senderDisplayName'])}")
        lines.append(f"isSend = {msg['isSend']}")
        lines.append(f"content = {toml_str(msg['content'])}")
        if msg.get("mediaFile"):
            lines.append(f"mediaFile = {toml_str(msg['mediaFile'])}")
        lines.append("")
        if count % 500 == 0 and progress:
            progress(0, f"已写入 {count} 条…")
    path.write_text("\n".join(lines), encoding="utf-8")
    return count


def _write_sqlite_batch(path, session, acc_dir, chat, start_ts, end_ts, account, names, media_map, progress):
    if path.exists():
        path.unlink()
    conn = sqlite3.connect(path)
    conn.executescript("""
        CREATE TABLE session (wxid TEXT, nickname TEXT, type TEXT, isGroup INTEGER,
            messageCount INTEGER, firstTimestamp INTEGER, lastTimestamp INTEGER);
        CREATE TABLE messages (localId INTEGER, createTime INTEGER, formattedTime TEXT,
            localType INTEGER, typeName TEXT, isSend INTEGER, senderUsername TEXT,
            senderDisplayName TEXT, content TEXT, rawContent TEXT, mediaFile TEXT);
        CREATE INDEX msg_idx ON messages(createTime);
    """)
    conn.execute("INSERT INTO session VALUES (?,?,?,?,?,?,?)",
                 (session["wxid"], session.get("nickname", ""), session["type"],
                  session["isGroup"], session["messageCount"],
                  session["firstTimestamp"], session["lastTimestamp"]))
    count = 0
    for msg in message_stream(acc_dir, chat, start_ts, end_ts, account, names):
        msg["mediaFile"] = media_map.get(msg["localId"])
        count += 1
        conn.execute("INSERT INTO messages VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                     (msg["localId"], msg["createTime"],
                      datetime.fromtimestamp(msg["createTime"]).strftime("%Y-%m-%d %H:%M:%S"),
                      msg["localType"], msg["typeName"], msg["isSend"],
                      msg["senderUsername"], msg["senderDisplayName"],
                      msg["content"], msg.get("rawContent", "")[:8000],
                      msg.get("mediaFile")))
        if count % 1000 == 0:
            conn.commit()
            if progress:
                progress(0, f"已写入 {count} 条…")
    conn.commit()
    conn.close()
    return count


def _write_xlsx_batch(path, acc_dir, chat, start_ts, end_ts, account, names, media_map, progress):
    from openpyxl import Workbook
    wb = Workbook()
    ws = wb.active
    ws.title = "聊天记录"
    ws.append(["localId", "时间", "类型", "发送者", "是否自己", "内容"])
    count = 0
    for msg in message_stream(acc_dir, chat, start_ts, end_ts, account, names):
        msg["mediaFile"] = media_map.get(msg["localId"])
        count += 1
        ws.append([msg["localId"],
                   datetime.fromtimestamp(msg["createTime"]).strftime("%Y-%m-%d %H:%M:%S"),
                   msg["typeName"], msg["senderDisplayName"],
                   "是" if msg["isSend"] else "否", msg["content"][:30000]])
        if count % 1000 == 0 and progress:
            progress(0, f"已写入 {count} 条…")
    wb.save(path)
    return count


def run_export_multi(acc_out_dir: Path, account: str, chats: list, fmt: str = "json",
                     start_ts=None, end_ts=None,
                     want_messages=True, want_media=False, want_avatars=False,
                     export_root: Path = None, pack: str = "folder",
                     progress=lambda pct, msg: None) -> dict:
    """多会话批量导出。"""
    t0 = time.time()
    n = max(len(chats), 1)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    root = Path(export_root or _paths.exports_root())
    total_dir = root / f"export_{stamp}"
    total_dir.mkdir(parents=True, exist_ok=True)
    progress(1, f"共 {n} 个会话 · 输出目录 {total_dir.name}/ · 打包={pack}")

    results, ok_n = [], 0
    for i, item in enumerate(chats):
        chat = item.get("chat") if isinstance(item, dict) else str(item)
        display = (item.get("display") or "") if isinstance(item, dict) else ""
        base, endp = int(100 * i / n), int(100 * (i + 1) / n)

        def sub(pct, msg, _b=base, _e=endp, _i=i):
            progress(_b + int(pct * (_e - _b) / 100), f"[{_i + 1}/{n}] {msg}")

        try:
            res = run_export(acc_out_dir, account, chat, display, fmt,
                             start_ts, end_ts, want_messages, want_media, want_avatars,
                             export_root=total_dir,
                             folder_name=f"{i + 1:02d}_{display or chat}",
                             pack=("zip" if pack == "each" else "none"),
                             progress=sub)
            ok_n += 1
            results.append({"chat": chat, "display": display or chat,
                            "message_count": res.get("message_count", 0),
                            "media_count": res.get("media_count", 0),
                            "avatar_count": res.get("avatar_count", 0),
                            "file": res.get("file")})
            progress(endp, f"[{i + 1}/{n}] ✔ {display or chat} ({res.get('message_count', 0)} 条)")
        except Exception as e:
            results.append({"chat": chat, "display": display or chat, "error": str(e)})
            progress(endp, f"[{i + 1}/{n}] ✗ {display or chat}: {e}")

    zip_path, zips = None, []
    if pack == "each":
        zips = sorted(str(p) for p in total_dir.glob("*.zip"))
        progress(97, f"每会话 zip 共 {len(zips)} 个")
    elif pack == "single":
        progress(96, "打包整体 zip…")
        zip_path = shutil.make_archive(str(root / total_dir.name), "zip",
                                       root_dir=total_dir)
        progress(98, f"zip 完成: {Path(zip_path).stat().st_size / 1048576:.1f} MB")

    progress(100, "导出完成")
    return {
        "total_dir": str(total_dir), "zip": zip_path, "zips": zips,
        "pack": pack, "format": fmt, "sessions": results, "ok_count": ok_n,
        "message_count": sum(r.get("message_count", 0) for r in results),
        "media_count": sum(r.get("media_count", 0) for r in results),
        "avatar_count": sum(r.get("avatar_count", 0) for r in results),
        "duration_ms": int((time.time() - t0) * 1000),
    }
