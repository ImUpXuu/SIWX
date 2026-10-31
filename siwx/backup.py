"""备份导出编排：微信进程终止 → 载荷构建 → .siwxpack 加密落盘。

两条硬规则：
  1. 备份前微信必须完全退出——活库被写一半的文件、未 checkpoint 的 -wal
     都会污染快照。终止动作必须经用户显式确认（confirm_kill 回调返回
     True 才动手；优雅退出优先，超时才强杀）。
  2. 杀进程后若残留非空 -wal（强杀导致微信没机会 checkpoint），必须明示
     用户"最新消息可能未包含"，并把 -wal 原样入包（full 模式）留待后解。

载荷两种模式：
  full      —— 整账号：db_storage 全部 .db 解密入包 + 非空 -wal 原样附带。
               恢复力最强（含朋友圈/语音 blob/收藏），体积也最大。
  selective —— 按联系人(会话) × 消息类别 × 时间窗，合成 WeChat 同构库：
               message_0.db（选中的 Msg 表，real_sender_id 经 Name2Id 重映射）
               + session.db + contact.db。导回后既可在 SIWX 里当档案账号浏览，
               也可走恢复引擎合回微信（与 diag_restore_probe 同构）。
"""
import hashlib
import json
import os
import platform
import re
import shutil
import sqlite3
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

import psutil

from siwx import keystore
from siwx import logger as _slog
from siwx import paths as _paths
from siwx.discover import find_wechat_data_dirs, find_wechat_pids
from siwx.pack import write_pack
from siwx.sqlcipher import collect_db_files, decrypt_database

MSG_COLUMNS = [
    "local_id", "server_id", "local_type", "sort_seq", "real_sender_id",
    "create_time", "status", "upload_status", "download_status", "server_seq",
    "origin_source", "source", "message_content", "compress_content",
    "packed_info_data", "WCDB_CT_message_content", "WCDB_CT_source",
]


def _dedup_sid(row, sid_i):
    """消息行 server_id 去重键（与 siwx/restore.py 同一口径）：
    仅 server_id>0 参与去重；0/NULL（系统类/未同步消息）返回 None，
    调用方必须放行——0 值在库中大量并存，按裸值去重会把同会话
    除首条外的 0 值消息全部静默塌缩。"""
    sid = row[sid_i]
    return sid if sid is not None and sid > 0 else None


class BackupAborted(Exception):
    """用户未授权/前置条件不满足，备份主动中止。"""


# ── 微信进程终止 ───────────────────────────────────────────────


def stop_wechat(confirm_kill, log=print, grace_timeout=20) -> dict:
    """要求备份前微信完全退出。

    confirm_kill: 回调 (pids) -> bool。None 或返回 False 都视为未授权 →
    BackupAborted（提示用户手动退出或显式允许）。
    优雅退出优先：Windows taskkill（无 /F，发 WM_CLOSE）、macOS SIGTERM；
    grace_timeout 秒后仍存活才升级强杀，并在返回值里标记 forced。
    """
    pids = find_wechat_pids()
    if not pids:
        return {"was_running": False, "killed": False, "forced": False,
                "pids": []}
    if confirm_kill is None or not confirm_kill(list(pids)):
        raise BackupAborted(
            "微信正在运行，备份要求先完全退出微信。"
            "请手动退出后重试，或显式允许 SIWX 终止它。")
    log(f"[backup] 请求微信优雅退出 (pids={pids})")
    if platform.system() == "Windows":
        for pid in pids:
            subprocess.run(["taskkill", "/PID", str(pid)],
                           capture_output=True)
    else:
        import signal
        for pid in pids:
            try:
                os.kill(pid, signal.SIGTERM)
            except (ProcessLookupError, PermissionError):
                pass

    def alive():
        return [p for p in pids if psutil.pid_exists(p)]

    forced = False
    t0 = time.time()
    while alive():
        if time.time() - t0 > grace_timeout:
            log("[backup] 优雅退出超时，升级强制终止")
            forced = True
            if platform.system() == "Windows":
                for pid in alive():
                    subprocess.run(["taskkill", "/F", "/PID", str(pid)],
                                   capture_output=True)
            else:
                import signal
                for pid in alive():
                    try:
                        os.kill(pid, signal.SIGKILL)
                    except (ProcessLookupError, PermissionError):
                        pass
            t1 = time.time()
            while alive() and time.time() - t1 < 5:
                time.sleep(0.3)
            break
        time.sleep(0.5)
    left = alive()
    if left:
        raise BackupAborted(f"无法终止微信进程 (pids={left})，备份中止")
    log("[backup] 微信已退出" + ("（强制）" if forced else "（优雅）"))
    return {"was_running": True, "killed": True, "forced": forced, "pids": pids}


def wal_residuals(db_dir) -> list:
    """非空 -wal 清单：强杀后微信没机会 checkpoint 的写入残留。"""
    out = []
    root = Path(db_dir)
    if not root.is_dir():
        return out
    for p in root.rglob("*-wal"):
        try:
            if p.stat().st_size > 0:
                out.append(str(p.relative_to(root)).replace("\\", "/"))
        except OSError:
            continue
    return sorted(out)


# ── 选择性载荷构建（纯函数，可测）──────────────────────────────


def _clone_ddl(src: sqlite3.Connection, table: str, dst: sqlite3.Connection):
    """把源库某表的建表 + 建索引语句原样克隆到目标库（表先于索引）。"""
    rows = src.execute(
        "SELECT type, sql FROM sqlite_master WHERE tbl_name=? AND sql IS NOT NULL "
        "AND type IN ('table','index') "
        "ORDER BY CASE type WHEN 'table' THEN 0 ELSE 1 END", (table,)).fetchall()
    if not rows:
        raise ValueError(f"源库没有表 {table}")
    for _typ, sql in rows:
        dst.execute(sql)


def msg_table_of(chat: str) -> str:
    return "Msg_" + hashlib.md5(chat.encode()).hexdigest()


def _filter_sql(types, start, end) -> tuple:
    """选择性过滤 → (WHERE 片段, 参数)。types 按 local_type 低 16 位匹配。"""
    conds, params = [], []
    if types:
        conds.append("(" + " OR ".join(["(local_type & 0xFFFF) = ?"] * len(types)) + ")")
        params.extend(int(t) & 0xFFFF for t in types)
    if start is not None:
        conds.append("create_time >= ?")
        params.append(int(start))
    if end is not None:
        conds.append("create_time <= ?")
        params.append(int(end))
    return (" WHERE " + " AND ".join(conds) if conds else ""), params


def build_selective_plain(msg_shards, session_db, contact_db, chats,
                          types=None, start=None, end=None,
                          payload_dir: Path = None, log=print) -> dict:
    """从**已解密**的源库合成选择性载荷（WeChat 同构，可直接浏览/恢复）。

    msg_shards: [Path]（message_*.db 明文）；chats: [username] 或 None=全部会话。
    返回 {"files": [(rel, path)], "stats": {...}}。
    """
    payload_dir = Path(payload_dir)
    (payload_dir / "message").mkdir(parents=True, exist_ok=True)
    (payload_dir / "session").mkdir(parents=True, exist_ok=True)
    (payload_dir / "contact").mkdir(parents=True, exist_ok=True)

    # 1) 定位每个会话的表分布在哪些分片（无 Name2Id 的库直接跳过）
    table_loc = {}          # chat -> [(shard_path, table)]
    all_chats = []
    for sp in msg_shards:
        c = sqlite3.connect(str(sp))
        try:
            try:
                n2i = {u: rid for rid, u in c.execute(
                    "SELECT rowid, user_name FROM Name2Id")}
            except sqlite3.Error:
                continue
            tabs = [r[0] for r in c.execute(
                "SELECT name FROM sqlite_master "
                "WHERE type='table' AND name LIKE 'Msg_%'")]
            for tbl in tabs:
                # 反查 username：Name2Id 里 md5 命中的就是该表会话
                owner = next((u for u in n2i
                              if msg_table_of(u) == tbl), None)
                if owner is None:
                    continue
                all_chats.append(owner)
                if chats is None or owner in chats:
                    table_loc.setdefault(owner, []).append((sp, tbl))
        finally:
            c.close()
    if chats:
        missing = [c for c in chats if c not in table_loc]
        if missing:
            log(f"[backup] ▲ {len(missing)} 个会话在本机没有消息表，已跳过")
    if not table_loc:
        raise BackupAborted("没有任何可备份的会话（检查联系人勾选/时间窗）")

    # 2) 合成 message_0.db（先克隆 Name2Id 本体，行按需重映射插入）
    out_msg = payload_dir / "message" / "message_0.db"
    om = sqlite3.connect(str(out_msg))
    om.isolation_level = None
    sc0 = sqlite3.connect(str(msg_shards[0]))
    try:
        _clone_ddl(sc0, "Name2Id", om)
    finally:
        sc0.close()
    sender_new = {}         # username -> 新 rowid（全合成库统一映射）
    stats = {"chats": {}, "messages": 0, "skipped_no_sender": 0}

    def _sender_id(src_conn, shard_cache, src_rid):
        if src_rid in shard_cache:
            u = shard_cache[src_rid]
        else:
            row = src_conn.execute(
                "SELECT user_name, is_session FROM Name2Id WHERE rowid=?",
                (src_rid,)).fetchone()
            u = row[0] if row else None
            shard_cache[src_rid] = u
        if u is None:
            stats["skipped_no_sender"] += 1
            return None, None
        if u not in sender_new:
            cur = om.execute("INSERT INTO Name2Id(user_name, is_session) "
                             "VALUES(?, ?)", (u, _sender_meta.get(u, 1)))
            sender_new[u] = cur.lastrowid
        return sender_new[u], u

    _sender_meta = {}       # username -> is_session（首个来源分片的值）
    for sp in msg_shards:
        c = sqlite3.connect(str(sp))
        try:
            for u, s in c.execute("SELECT user_name, is_session FROM Name2Id"):
                _sender_meta.setdefault(u, s)
        except sqlite3.Error:
            pass
        finally:
            c.close()

    try:
        om.execute("BEGIN IMMEDIATE")
        # 合成库统一**重编号 local_id**（INSERT 不带该列，AUTOINCREMENT 分配）：
        # 同一会话的表跨多个分片且各分片 local_id 独立编号（实测同一 Msg 表
        # 分布在 6 个分片、各自都有 local_id=1），照抄合并必撞主键。包内编号
        # 自洽即可——排序键是 sort_seq(=create_time*1000)、去重键是 server_id，
        # 恢复合回微信时本就要按目标库重新编号。
        insert_cols = [c for c in MSG_COLUMNS if c != "local_id"]
        colspec = ", ".join(insert_cols)
        qmarks = ", ".join("?" * len(insert_cols))
        # SELECT 行按全列 MSG_COLUMNS 取索引；INSERT vals 按去 local_id 后的
        # insert_cols 取索引——两套索引不能混用（实测混用后把 sort_seq 大数
        # 当 real_sender_id 写进包，sender 映射全断）
        src_si = MSG_COLUMNS.index("real_sender_id")
        dst_si = insert_cols.index("real_sender_id")
        sid = MSG_COLUMNS.index("server_id")
        for chat, locs in table_loc.items():
            total_rows = 0
            seen_sids = set()   # 跨分片 server_id 去重兜底（仅 >0 参与）
            for sp, tbl in locs:
                sc = sqlite3.connect(str(sp))
                try:
                    try:
                        _clone_ddl(sc, tbl, om)  # 首个分片建表+索引，后续同表已存在则跳过
                    except sqlite3.OperationalError:
                        pass
                    where, params = _filter_sql(types, start, end)
                    shard_cache = {}
                    for row in sc.execute(
                            f"SELECT {', '.join(MSG_COLUMNS)} "
                            f"FROM [{tbl}]{where}", params):
                        row_sid = _dedup_sid(row, sid)
                        if row_sid is not None:
                            if row_sid in seen_sids:
                                stats["dup_skipped"] = \
                                    stats.get("dup_skipped", 0) + 1
                                continue
                            seen_sids.add(row_sid)
                        # server_id 0/NULL（系统/未同步消息）不去重：
                        # 同会话多条 0 值全量保留，防止塌缩成一条
                        vals = [v for k, v in zip(MSG_COLUMNS, row)
                                if k != "local_id"]
                        new_id, _u = _sender_id(sc, shard_cache, row[src_si])
                        if new_id is not None:
                            vals[dst_si] = new_id
                        om.execute(
                            f"INSERT INTO [{msg_table_of(chat)}] "
                            f"({colspec}) VALUES ({qmarks})", vals)
                        total_rows += 1
                finally:
                    sc.close()
            tblname = msg_table_of(chat)
            max_lid = om.execute(
                f"SELECT MAX(local_id) FROM [{tblname}]").fetchone()[0] or 0
            cur = om.execute("UPDATE sqlite_sequence SET seq=? WHERE name=?",
                             (max_lid, tblname))
            if cur.rowcount == 0:
                om.execute("INSERT INTO sqlite_sequence(name, seq) VALUES(?, ?)",
                           (tblname, max_lid))
            stats["chats"][chat] = total_rows
            stats["messages"] += total_rows
        om.execute("COMMIT")
    except Exception:
        try:
            om.execute("ROLLBACK")
        except sqlite3.Error:
            pass
        raise
    finally:
        om.close()

    # 3) 合成 session.db（SessionTable + Name2Id）
    out_sess = payload_dir / "session" / "session.db"
    oss = sqlite3.connect(str(out_sess))
    oss.isolation_level = None
    try:
        oss.execute("BEGIN IMMEDIATE")   # 单事务：逐行自动提交会每行一次 journal 刷盘
        src = sqlite3.connect(str(session_db))
        try:
            _clone_ddl(src, "SessionTable", oss)
            _clone_ddl(src, "Name2Id", oss)
            for chat in table_loc:
                row = src.execute(
                    "SELECT * FROM SessionTable WHERE username=?",
                    (chat,)).fetchone()
                if row:
                    cols = [d[0] for d in src.execute(
                        "SELECT * FROM SessionTable WHERE username=?",
                        (chat,)).description]
                    colspec = ", ".join(f"[{c}]" for c in cols)
                    oss.execute(
                        f"INSERT INTO SessionTable ({colspec}) "
                        f"VALUES ({', '.join('?' * len(cols))})", row)
                oss.execute("INSERT INTO Name2Id(user_name) VALUES(?)", (chat,))
        finally:
            src.close()
        oss.execute("COMMIT")
    finally:
        oss.close()

    # 4) 合成 contact.db（联系人 + 群 + 群成员）
    out_contact = payload_dir / "contact" / "contact.db"
    oc = sqlite3.connect(str(out_contact))
    oc.isolation_level = None
    try:
        oc.execute("BEGIN IMMEDIATE")    # 同上：几千群成员逐行提交会慢到分钟级
        src = sqlite3.connect(str(contact_db))
        try:
            for t in ("contact", "chat_room", "chatroom_member", "name2id"):
                try:
                    _clone_ddl(src, t, oc)
                except (ValueError, sqlite3.Error):
                    pass

            def copy_where(table, where, params):
                try:
                    cur = src.execute(f"SELECT * FROM [{table}] {where}", params)
                    cols = [d[0] for d in cur.description]
                    colspec = ", ".join(f"[{c}]" for c in cols)
                    for row in cur.fetchall():
                        oc.execute(
                            f"INSERT OR IGNORE INTO [{table}] ({colspec}) "
                            f"VALUES ({', '.join('?' * len(cols))})", row)
                except sqlite3.Error as e:
                    _slog.detailed("backup", f"contact {table} 拷贝失败: {e}")

            for chat in table_loc:
                copy_where("contact", "WHERE username=?", (chat,))
                copy_where("name2id", "WHERE username=?", (chat,))
                if chat.endswith("@chatroom"):
                    room = src.execute(
                        "SELECT id FROM chat_room WHERE username=?",
                        (chat,)).fetchone()
                    if room:
                        copy_where("chat_room", "WHERE id=?", (room[0],))
                        copy_where("chatroom_member", "WHERE room_id=?",
                                   (room[0],))
        finally:
            src.close()
        oc.execute("COMMIT")
    finally:
        oc.close()

    files = [
        ("message/message_0.db", out_msg),
        ("session/session.db", out_sess),
        ("contact/contact.db", out_contact),
    ]
    stats["chats_count"] = len(table_loc)
    return {"files": files, "stats": stats}


# ── 解密包装 + 编排 ──────────────────────────────────────────


def _resolve_account(account, store, log=print) -> tuple:
    dirs = find_wechat_data_dirs()
    if not dirs:
        raise BackupAborted("未找到微信数据目录")
    if account:
        hits = [(w, d) for w, d in dirs if w == account]
        if not hits:
            raise BackupAborted(f"账号 {account[:3]}*** 不在本机账号列表中")
        if len(hits) == 1:
            wxid, db_dir = hits[0]
        else:
            # 同名多副本（换盘迁移残留）：必须按密钥覆盖选优，不能取排序第一个。
            # 实测踩坑：C 盘残留副本 16 库 0 密钥排在 D 盘正身（32 库 32/32）之前，
            # hits[0] 命中残留 → 报"密钥缺失 16/16"，而用户选的账号明明密钥齐全。
            ranked = []
            for _w, d in hits:
                try:
                    es = collect_db_files(d)
                except Exception:
                    es = []
                covered = sum(1 for e in es if e.salt_hex in store)
                try:
                    latest = max((e.path.stat().st_mtime for e in es), default=0)
                except OSError:
                    latest = 0
                ranked.append((covered, len(es), latest, d))
            # 覆盖优先、库数次之、写入时间兜底——实测残留副本（老 salt、老 mtime）
            # 与正身（当前微信在写、密钥齐全）并存时三者都指向正身
            ranked.sort(key=lambda t: (t[0], t[1], t[2]), reverse=True)
            wxid, db_dir = account, ranked[0][3]
            log(f"[backup] ▲ 账号有 {len(hits)} 个数据目录（同名多副本），"
                f"已选密钥覆盖最高的: {db_dir}")
    else:
        best = None
        for wxid, db_dir in dirs:
            try:
                entries = collect_db_files(db_dir)
            except Exception:
                continue
            covered = sum(1 for e in entries if e.salt_hex in store)
            if entries and covered == len(entries):
                best = (wxid, db_dir)
                break
            if best is None or covered > best[2]:
                best = (wxid, db_dir, covered)
        if best is None:
            raise BackupAborted("没有可用账号")
        wxid, db_dir = best[0], best[1]
        # 自动挑选落到 0 覆盖账号时，与其静默进入"自动提取"（内存里只有当前
        # 登录账号的密钥，多半提取不到这个账号），不如让用户显式选账号
        if len(best) == 3 and best[2] == 0 and len(dirs) > 1:
            raise BackupAborted(
                f"本机 {len(dirs)} 个账号都没有密钥缓存。请在账号卡选择要备份"
                "的账号后重试（开始备份时会自动提取当前登录账号的密钥）。")
    return wxid, db_dir


def run_backup(password: str, mode: str = "full",
               chats=None, types=None, start=None, end=None,
               account=None, out=None,
               confirm_kill=None, log=print, progress=None,
               keep_work=False) -> dict:
    """备份主入口。mode: full | selective。

    返回 {"path", "size", "files", "mode", "account", "stats", "stop", "wal"}。
    """
    prog = progress or (lambda pct, msg: None)
    store = keystore.load()
    wxid, db_dir = _resolve_account(account, store, log=log)
    log(f"[backup] 账号 {wxid[:3]}***  模式 {mode}")

    # 密钥门禁必须在杀微信**之前**：密钥提取依赖微信进程（Windows 内存扫描），
    # 先杀再查会让用户白杀一次微信才发现缺密钥（实测踩坑）。
    entries = collect_db_files(db_dir)
    if not entries:
        raise BackupAborted(f"账号 {wxid[:3]}*** 没有可备份的数据库文件")
    missing = [e for e in entries if e.salt_hex not in store]
    if missing:
        pids = find_wechat_pids()
        if platform.system() != "Windows":
            raise BackupAborted(
                f"密钥缺失 {len(missing)}/{len(entries)} 个库。macOS 请先在控制台"
                "完成一次密钥提取（需微信处于登录期间），再来备份。")
        if not pids:
            raise BackupAborted(
                f"密钥缺失 {len(missing)}/{len(entries)} 个库且微信未运行——"
                "自动提取需要微信在线。请先启动并登录微信，再点一次开始备份。")
        log(f"[backup] 密钥缺失 {len(missing)}/{len(entries)} 个库，"
            "微信运行中 → 自动提取（内存扫描，只对当前登录账号有效）")
        prog(2, "自动提取密钥…")
        from siwx.extract import extract_keys_for_dir
        try:
            rep = extract_keys_for_dir(db_dir, log=log, use_memory=True)
        except Exception as e:  # noqa: BLE001 — 提取失败要落到备份错误里而不是炸任务
            raise BackupAborted(
                f"自动提取异常 {type(e).__name__}: {e}——请在控制台「提取并解密」"
                "看详细日志") from e
        if rep.get("verified", 0) == 0:
            raise BackupAborted(
                "自动提取 0 命中——密钥只存在于**当前登录账号**的微信内存里。"
                "请确认微信登录的就是要备份的账号后重试；仍失败先在控制台"
                "「提取并解密」看详细日志。")
        store = keystore.load()
        entries = collect_db_files(db_dir)
        missing = [e for e in entries if e.salt_hex not in store]
        if missing:
            raise BackupAborted(
                f"提取后仍缺 {len(missing)}/{len(entries)} 个库——缺少的库可能"
                "是新建/未落库，稍后重试；详情见上方提取日志。")
        log(f"[backup] √ 密钥已补齐（+{rep['verified']}）")

    prog(4, "检查微信进程…")
    stop = stop_wechat(confirm_kill, log)
    wals = wal_residuals(db_dir)
    if wals:
        log(f"[backup] ▲ 残留 {len(wals)} 个非空 -wal（强制退出未 checkpoint），"
            "最新消息可能未包含：" + ", ".join(wals[:3]) +
            ("…" if len(wals) > 3 else ""))

    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    # 工作目录必须避开 out_root()：output/ 下的任何含 message/ 子目录的
    # 文件夹都会被 api_chat 当作"解密账号"扫出来（实测 _dblab/backup_work
    # 曾污染账号选择器）。data_dir 不参与账号发现。
    work = _paths.data_dir() / "backup_work" / stamp
    if work.exists():
        shutil.rmtree(work)
    work.mkdir(parents=True, exist_ok=True)

    try:
        rel_map = {e.rel.replace("\\", "/"): e for e in entries}

        def dec(rel, dest):
            e = rel_map[rel]
            dest.parent.mkdir(parents=True, exist_ok=True)
            decrypt_database(e.path, dest,
                             bytes.fromhex(store[e.salt_hex]["key"]))

        if mode == "full":
            prog(8, f"解密全部 {len(entries)} 个库…")
            payload = []
            for i, (rel, e) in enumerate(sorted(rel_map.items())):
                dest = work / "payload" / Path(*rel.split("/"))
                dec(rel, dest)
                payload.append((rel, dest))
                prog(8 + int(i * 30 / len(rel_map)), f"已解密 {rel}")
            for rel in wals:     # 非空 -wal 原样附带，留待未来解析
                src = Path(db_dir) / Path(*rel.split("/"))
                dest = work / "payload" / Path(*rel.split("/"))
                dest.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(src, dest)
                payload.append((rel, dest))
            stats = {"dbs": len(payload), "chats_count": None,
                     "messages": None}
        elif mode == "selective":
            prog(8, "解密消息分片…")
            # 严格匹配 message/message_<N>.db：message_fts（Name2Id 列结构不同）、
            # message_resource（无 Name2Id）、biz_message_*（公众号单独体系）都不算
            shard_rels = sorted(r for r in rel_map
                                if re.fullmatch(r"message/message_\d+\.db", r))
            shards, i = [], 0
            for rel in shard_rels:
                dest = work / "src" / Path(*rel.split("/"))
                dec(rel, dest)
                shards.append(dest)
                i += 1
                prog(8 + int(i * 20 / len(shard_rels)), f"已解密 {rel}")
            sess_p = work / "src" / "session.db"
            dec("session/session.db", sess_p)
            contact_p = work / "src" / "contact.db"
            dec("contact/contact.db", contact_p)
            prog(35, "合成选择性载荷…")
            built = build_selective_plain(
                shards, sess_p, contact_p, chats, types, start, end,
                payload_dir=work / "payload", log=log)
            payload = built["files"]
            stats = built["stats"]
        else:
            raise BackupAborted(f"未知模式: {mode}")

        meta = {
            "generator": _generator(),
            "created_at": datetime.now().isoformat(timespec="seconds"),
            "mode": mode,
            "account": wxid,
            "selection": {
                "chats": "all" if mode == "full" else
                         (sorted(chats) if chats is not None else "all"),
                "types": sorted(int(t) for t in types) if types else "all",
                "start": int(start) if start is not None else None,
                "end": int(end) if end is not None else None,
            },
            "stats": stats,
            "wal_residuals": wals,
        }
        out_path = Path(out) if out else (
            _paths.exports_root() / f"siwx_backup_{mode}_{stamp}.siwxpack")
        prog(70, "写入加密包…")
        rep = write_pack(out_path, password, payload, meta, progress=prog)
        log(f"[backup] √ 完成: {rep['path']} "
            f"({rep['size'] / 1048576:.1f} MB, {rep['files']} 个文件)")
        return {**rep, "mode": mode, "account": wxid, "stats": stats,
                "stop": stop, "wal": wals}
    finally:
        if not keep_work:
            shutil.rmtree(work, ignore_errors=True)


def _generator() -> str:
    try:
        from siwx import __version__
        return f"stories-in-wx {__version__}"
    except Exception:
        return "stories-in-wx"
