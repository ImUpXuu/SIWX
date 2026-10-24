#!/usr/bin/env python3
"""diag_restore_probe — 「联系人级聊天记录恢复」可行性自测（端到端，全程不碰线上库）。

背景（真机研究结论，2026-10）：
  - 微信 4.x 删除会话 = DROP 该会话的 Msg_<md5> 表 + 删 SessionTable 行
    + 写 SessionDeleteTable 一笔；语音 blob 仍留在 media_*.db。
  - Msg 表无唯一约束；server_id 跨分片全局唯一（去重键）；
    sort_seq == create_time*1000（排序键）；local_id 为表内 AUTOINCREMENT。
  - real_sender_id 指向**分片内** Name2Id.rowid，同一 username 在不同分片
    的 rowid 不同 → 跨库搬运必须按 username 重映射。
  - general.db.revokemessage 按 svr_id 记录撤回 → 恢复时须跳过。

流程（simulate 模式）：
  1. 从密钥全覆盖的真实账号自动挑一个会话（默认挑行数最多的 Msg_ 表）；
  2. 解密该分片 + session.db + general.db 到工作目录（pristine 副本）；
  3. 在另一份副本上模拟微信「删除会话」（与真机行为一致）；
  4. 恢复引擎从 pristine 合并回去（server_id 去重 / sender 重映射 /
     整行照抄 / session 修复 / 撤回过滤）；
  5. encrypt_database（同 salt）重加密回微信格式；
  6. 逐项校验（行级字段一致 / server_id 集合一致 / sqlite_sequence /
     session 修复 / HMAC / integrity_check / 线上原库字节未动）。

用法：
  python scripts/diag_restore_probe.py simulate
  python scripts/diag_restore_probe.py simulate --pick chatroom   # 挑群聊
  python scripts/diag_restore_probe.py simulate --chat <username>

输出中的用户名一律打码；工作目录默认 data_dir/restore_probe（**不能放
output/ 下**——那里的子目录会被 api_chat 当作解密账号扫出来）。
"""
import argparse
import hashlib
import shutil
import sqlite3
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from siwx import keystore  # noqa: E402
from siwx.discover import find_wechat_data_dirs  # noqa: E402
from siwx.sqlcipher import (collect_db_files, decrypt_database,  # noqa: E402
                            encrypt_database, verify_enc_key)


def mask(u) -> str:
    u = str(u or "")
    return (u[:3] + "***") if len(u) > 4 else "***"


def sha256_file(p: Path) -> str:
    h = hashlib.sha256()
    with open(p, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def msg_table_of(chat: str) -> str:
    return "Msg_" + hashlib.md5(chat.encode()).hexdigest()


def _conn(p: Path):
    c = sqlite3.connect(str(p))
    c.isolation_level = None          # 显式控制事务
    c.execute("PRAGMA busy_timeout=5000")
    return c


def _finish(c):
    """收尾：WAL 落盘后关连接。残留 wal/shm 由 cleanup_sidecars 处理。"""
    try:
        c.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    except sqlite3.Error:
        pass
    c.close()


def cleanup_sidecars(p: Path):
    for suffix in ("-wal", "-shm", "-journal"):
        Path(str(p) + suffix).unlink(missing_ok=True)


# ── 账号与目标会话选择 ─────────────────────────────────────────


def pick_covered_account():
    """挑密钥全覆盖的账号（自测要求能解密全部涉及的库）。"""
    store = keystore.load()
    for wxid, db_dir in find_wechat_data_dirs():
        entries = collect_db_files(db_dir)
        if entries and all(e.salt_hex in store for e in entries):
            return wxid, db_dir, entries, store
    raise SystemExit("✗ 没有密钥全覆盖的账号（请先跑一次完整提取）")


def ensure_decrypted(entry, store, dest: Path) -> Path:
    if not dest.is_file():
        dest.parent.mkdir(parents=True, exist_ok=True)
        decrypt_database(entry.path, dest,
                         bytes.fromhex(store[entry.salt_hex]["key"]))
    return dest


def pick_chat(entries, store, work: Path, prefer="largest", want_chat=None):
    """挑测试会话：默认全分片中行数最多的 Msg_ 表；要求能反查 username，
    且该会话在 SessionTable 里有行（保证 session 修复环节可测）。"""
    by_rel = {e.rel.replace("\\", "/"): e for e in entries}
    shard_rels = sorted(r for r in by_rel
                        if r.startswith("message/message_") and r.endswith(".db"))

    # 反查表名 → username：各分片自身 Name2Id（含会话名）+ session.db
    rev = {}
    for rel in shard_rels:
        p = ensure_decrypted(by_rel[rel], store, work / "idx" / rel.replace("/", "_"))
        try:
            c = sqlite3.connect(str(p))
            try:
                for (u,) in c.execute("SELECT user_name FROM Name2Id"):
                    u = (u or "").strip()
                    if u:
                        rev.setdefault(msg_table_of(u), u)
            finally:
                c.close()
        except sqlite3.Error:
            continue
    sess_plain = ensure_decrypted(by_rel["session/session.db"], store,
                                  work / "idx" / "session.db")
    sess_chats, sess_tbl_owner = set(), {}
    c = sqlite3.connect(str(sess_plain))
    try:
        for (u,) in c.execute("SELECT username FROM SessionTable"):
            sess_tbl_owner.setdefault(u, None)
            sess_chats.add(u)
        for (u,) in c.execute("SELECT user_name FROM Name2Id"):
            sess_chats.add(u)
    finally:
        c.close()

    def table_rows(p, tbl):
        cc = sqlite3.connect(str(p))
        try:
            return cc.execute(f"SELECT COUNT(*) FROM [{tbl}]").fetchone()[0]
        finally:
            cc.close()

    if want_chat:
        tbl = msg_table_of(want_chat)
        for rel in shard_rels:
            p = work / "idx" / rel.replace("/", "_")
            try:
                have = sqlite3.connect(str(p)).execute(
                    "SELECT COUNT(*) FROM sqlite_master WHERE name=?",
                    (tbl,)).fetchone()[0]
            except sqlite3.Error:
                continue
            if have:
                return want_chat, tbl, rel, table_rows(p, tbl), by_rel[rel]
        raise SystemExit(f"✗ 会话 {mask(want_chat)} 在任何分片都没有消息表")

    cands = []
    for rel in shard_rels:
        p = work / "idx" / rel.replace("/", "_")
        try:
            c = sqlite3.connect(str(p))
            try:
                for (tbl,) in c.execute(
                        "SELECT name FROM sqlite_master "
                        "WHERE type='table' AND name LIKE 'Msg_%'"):
                    chat = rev.get(tbl)
                    if not chat or chat not in sess_chats:
                        continue
                    cands.append((table_rows(p, tbl), tbl, chat, rel))
            finally:
                c.close()
        except sqlite3.Error:
            continue
    if not cands:
        raise SystemExit("✗ 没有可测试的会话（需能反查 username 且 SessionTable 有行）")
    if prefer == "chatroom":
        cands = [x for x in cands if x[2].endswith("@chatroom")] or cands
    cands.sort(reverse=True)
    n, tbl, chat, rel = cands[0]
    return chat, tbl, rel, n, by_rel[rel]


# ── 模拟删除 / 恢复引擎 ───────────────────────────────────────


def simulate_deletion(msg_db: Path, session_db: Path, table: str, chat: str,
                      partial: bool = False):
    """在副本上复刻删除：整删（DROP 表 + 删 session 行 + 记删除）或
    部分删（只删最近一半行，会话还在）——后者验证增量合并的去重路径。"""
    c = _conn(msg_db)
    try:
        c.execute("BEGIN IMMEDIATE")
        if partial:
            half = c.execute(
                f"SELECT MAX(local_id) FROM [{table}]").fetchone()[0] // 2
            c.execute(f"DELETE FROM [{table}] WHERE local_id > ?", (half,))
        else:
            c.execute(f'DROP TABLE IF EXISTS [{table}]')
        c.execute("COMMIT")
    finally:
        _finish(c)
    cleanup_sidecars(msg_db)

    if partial:
        return
    s = _conn(session_db)
    try:
        s.execute("BEGIN IMMEDIATE")
        s.execute("DELETE FROM SessionTable WHERE username=?", (chat,))
        s.execute(
            "INSERT OR REPLACE INTO SessionDeleteTable(username, delete_time) "
            "VALUES(?, ?)", (chat, int(time.time())))
        s.execute("COMMIT")
    finally:
        _finish(s)
    cleanup_sidecars(session_db)


MSG_COLUMNS = [
    "local_id", "server_id", "local_type", "sort_seq", "real_sender_id",
    "create_time", "status", "upload_status", "download_status", "server_seq",
    "origin_source", "source", "message_content", "compress_content",
    "packed_info_data", "WCDB_CT_message_content", "WCDB_CT_source",
]


def restore_chat(msg_pristine: Path, session_pristine: Path,
                 msg_victim: Path, session_victim: Path,
                 table: str, chat: str, revoked: set) -> dict:
    """恢复引擎：pristine → victim。

    规则（真机研究结论）：
      - 目标表不存在 → 用 pristine 的 DDL + 4 个索引原样克隆；
      - server_id 去重（victim 已有的行不覆盖）；
      - real_sender_id 按 username 重映射（分片内 Name2Id.rowid 不通用）；
      - local_id / sort_seq / server_seq 等字段整行照抄；
      - 已撤回（revoked svr_id）的消息跳过；
      - 插入后校准 sqlite_sequence（AUTOINCREMENT 状态）；
      - session：SessionTable 行照抄、SessionDeleteTable 清除该会话记录。
    """
    stats = {"source_rows": 0, "inserted": 0, "dup_skipped": 0,
             "revoked_skipped": 0, "senders_added": 0, "table_created": False,
             "session_restored": False, "delete_row_cleared": False}

    src = _conn(msg_pristine)
    try:
        cols = ", ".join(MSG_COLUMNS)
        rows = src.execute(
            f"SELECT {cols} FROM [{table}] ORDER BY local_id").fetchall()
        stats["source_rows"] = len(rows)
        ddl = src.execute(
            "SELECT sql FROM sqlite_master WHERE tbl_name=? AND sql IS NOT NULL "
            "ORDER BY CASE type WHEN 'table' THEN 0 ELSE 1 END", (table,)).fetchall()
        src_senders = dict(src.execute(
            "SELECT rowid, user_name FROM Name2Id").fetchall())
        pmeta = dict(src.execute(
            "SELECT user_name, is_session FROM Name2Id").fetchall())
    finally:
        _finish(src)

    dst = _conn(msg_victim)
    try:
        have_table = dst.execute(
            "SELECT COUNT(*) FROM sqlite_master WHERE type='table' AND name=?",
            (table,)).fetchone()[0]
        if not have_table:
            for (sql,) in ddl:
                dst.execute(sql)
            stats["table_created"] = True

        # victim 侧 sender 映射：username → rowid（缺则补插，沿用 pristine 的 is_session）
        dst_senders = {u: rid for rid, u in dst.execute(
            "SELECT rowid, user_name FROM Name2Id").fetchall()}

        def sender_rowid(src_rid):
            u = src_senders.get(src_rid)
            if u is None:
                return src_rid  # 反查不到：原样保留（对账报告里兜底）
            if u not in dst_senders:
                cur = dst.execute(
                    "INSERT INTO Name2Id(user_name, is_session) VALUES(?, ?)",
                    (u, pmeta.get(u, 1)))
                dst_senders[u] = cur.lastrowid
                stats["senders_added"] += 1
            return dst_senders[u]

        existing = {r[0] for r in dst.execute(
            f"SELECT server_id FROM [{table}]").fetchall()}
        colspec = ", ".join(MSG_COLUMNS)
        qmarks = ", ".join("?" * len(MSG_COLUMNS))
        si = MSG_COLUMNS.index("real_sender_id")
        dst.execute("BEGIN IMMEDIATE")
        try:
            for row in rows:
                sid = row[MSG_COLUMNS.index("server_id")]
                if sid in revoked:
                    stats["revoked_skipped"] += 1
                    continue
                if sid in existing:
                    stats["dup_skipped"] += 1
                    continue
                vals = list(row)
                vals[si] = sender_rowid(row[si])
                dst.execute(
                    f"INSERT INTO [{table}] ({colspec}) VALUES ({qmarks})", vals)
                existing.add(sid)
                stats["inserted"] += 1
            # AUTOINCREMENT 状态校准：显式插入 local_id 后 seq 必须追平 max，
            # 否则微信后续新消息分配 local_id 会撞主键。DROP TABLE 可能已把
            # 该表的 seq 行删掉，UPDATE 不中就 INSERT。
            max_lid = dst.execute(
                f"SELECT MAX(local_id) FROM [{table}]").fetchone()[0] or 0
            cur = dst.execute(
                "UPDATE sqlite_sequence SET seq=? WHERE name=?", (max_lid, table))
            if cur.rowcount == 0:
                dst.execute(
                    "INSERT INTO sqlite_sequence(name, seq) VALUES(?, ?)",
                    (table, max_lid))
            dst.execute("COMMIT")
        except Exception:
            dst.execute("ROLLBACK")
            raise
    finally:
        _finish(dst)
    cleanup_sidecars(msg_victim)

    # session 修复：行从 pristine 照抄，删除记录清除
    ps = _conn(session_pristine)
    try:
        cur2 = ps.execute(
            "SELECT * FROM SessionTable WHERE username=?", (chat,))
        sess_row = cur2.fetchone()
        sess_cols = [d[0] for d in cur2.description]
    finally:
        _finish(ps)
    if sess_row:
        vs = _conn(session_victim)
        try:
            vs.execute("BEGIN IMMEDIATE")
            vs.execute("DELETE FROM SessionTable WHERE username=?", (chat,))
            colspec = ", ".join(f"[{c}]" for c in sess_cols)
            qmarks = ", ".join("?" * len(sess_cols))
            vs.execute(
                f"INSERT INTO SessionTable ({colspec}) VALUES ({qmarks})", sess_row)
            cur = vs.execute(
                "DELETE FROM SessionDeleteTable WHERE username=?", (chat,))
            stats["delete_row_cleared"] = cur.rowcount > 0
            vs.execute("COMMIT")
            stats["session_restored"] = True
        finally:
            _finish(vs)
        cleanup_sidecars(session_victim)
    return stats


def load_revoked(general_plain: Path, chat: str) -> set:
    if not general_plain.is_file():
        return set()
    try:
        c = sqlite3.connect(str(general_plain))
        try:
            return {r[0] for r in c.execute(
                "SELECT svr_id FROM revokemessage WHERE to_user_name=?", (chat,))}
        finally:
            c.close()
    except sqlite3.Error:
        return set()


# ── 校验 ──────────────────────────────────────────────────────


def verify(msg_pristine, session_pristine, msg_restored_plain,
           session_restored_plain, table, chat,
           enc_msg, enc_sess, key_msg, salt_msg, key_sess) -> list:
    checks = []

    def chk(name, ok, detail=""):
        checks.append((name, bool(ok), detail))

    src = sqlite3.connect(str(msg_pristine))
    dst = sqlite3.connect(str(msg_restored_plain))
    try:
        cols = ", ".join(MSG_COLUMNS)
        src_rows = src.execute(
            f"SELECT {cols} FROM [{table}] ORDER BY local_id").fetchall()
        got_rows = dst.execute(
            f"SELECT {cols} FROM [{table}] ORDER BY local_id").fetchall()
        chk("消息行数一致", len(src_rows) == len(got_rows),
            f"pristine={len(src_rows)} restored={len(got_rows)}")

        mismatch, first_bad = 0, None
        for a, b in zip(src_rows, got_rows):
            if a != b:
                mismatch += 1
                if first_bad is None:
                    bad_col = next(i for i, (x, y) in enumerate(zip(a, b))
                                   if x != y)
                    first_bad = f"local_id={a[0]} 列 {MSG_COLUMNS[bad_col]}"
        chk("行级字段逐列一致", mismatch == 0 and len(src_rows) == len(got_rows),
            f"不一致 {mismatch} 行" + (f"，首例 {first_bad}" if first_bad else ""))

        sset = {r[1] for r in src_rows}
        gset = {r[1] for r in got_rows}
        chk("server_id 集合一致", sset == gset,
            f"src={len(sset)} dst={len(gset)} 差={len(sset ^ gset)}")

        seq = dst.execute(
            "SELECT seq FROM sqlite_sequence WHERE name=?", (table,)).fetchone()
        max_lid = dst.execute(
            f"SELECT MAX(local_id) FROM [{table}]").fetchone()[0]
        chk("sqlite_sequence == max(local_id)",
            seq is not None and seq[0] == max_lid,
            f"seq={seq[0] if seq else None} max={max_lid}")

        integ = dst.execute("PRAGMA integrity_check").fetchone()[0]
        chk("integrity_check ok", integ == "ok", integ)

        idx = dst.execute(
            "SELECT COUNT(*) FROM sqlite_master WHERE type='index' "
            "AND tbl_name=? AND sql IS NOT NULL", (table,)).fetchone()[0]
        src_idx = src.execute(
            "SELECT COUNT(*) FROM sqlite_master WHERE type='index' "
            "AND tbl_name=? AND sql IS NOT NULL", (table,)).fetchone()[0]
        chk("索引数一致", idx == src_idx, f"{idx}/{src_idx}")
    finally:
        src.close()
        dst.close()

    ps = sqlite3.connect(str(session_pristine))
    vs = sqlite3.connect(str(session_restored_plain))
    try:
        prow = ps.execute(
            "SELECT * FROM SessionTable WHERE username=?", (chat,)).fetchone()
        vrow = vs.execute(
            "SELECT * FROM SessionTable WHERE username=?", (chat,)).fetchone()
        chk("SessionTable 行已恢复", prow is not None and prow == vrow)
        n_del = vs.execute(
            "SELECT COUNT(*) FROM SessionDeleteTable WHERE username=?",
            (chat,)).fetchone()[0]
        chk("SessionDeleteTable 已清除", n_del == 0, f"残留 {n_del} 行")
    finally:
        ps.close()
        vs.close()

    chk("消息库重加密 HMAC 通过", verify_enc_key(key_msg, enc_msg.read_bytes()[:4096]))
    chk("消息库保留原 salt", enc_msg.read_bytes()[:16] == salt_msg)
    chk("session 库重加密 HMAC 通过",
        verify_enc_key(key_sess, enc_sess.read_bytes()[:4096]))
    return checks


# ── 主流程 ────────────────────────────────────────────────────


def cmd_simulate(args):
    work = Path(args.work)
    if args.clean and work.exists():
        shutil.rmtree(work)
    work.mkdir(parents=True, exist_ok=True)

    wxid, db_dir, entries, store = pick_covered_account()
    print(f"账号: {mask(wxid)}  库数: {len(entries)}")

    chat, table, shard_rel, nrows, entry = pick_chat(
        entries, store, work, prefer=args.pick, want_chat=args.chat)
    print(f"目标会话: {mask(chat)}  表 {table[:12]}…  分片 {shard_rel}  {nrows} 行")

    rel_map = {e.rel.replace("\\", "/"): e for e in entries}
    sess_entry = rel_map["session/session.db"]
    key_msg = bytes.fromhex(store[entry.salt_hex]["key"])
    salt_msg = entry.page1[:16]
    key_sess = bytes.fromhex(store[sess_entry.salt_hex]["key"])
    salt_sess = bytes.fromhex(sess_entry.salt_hex)

    run = work / "run"
    run.mkdir(parents=True, exist_ok=True)
    pristine_msg, victim_msg = run / "pristine_msg.db", run / "victim_msg.db"
    pristine_sess, victim_sess = (run / "pristine_session.db",
                                  run / "victim_session.db")
    general = run / "general.db"

    print("解密 pristine 副本…")
    decrypt_database(entry.path, pristine_msg, key_msg)
    decrypt_database(sess_entry.path, pristine_sess, key_sess)
    decrypt_database(rel_map["general/general.db"].path, general,
                     bytes.fromhex(store[rel_map["general/general.db"].salt_hex]["key"]))
    shutil.copy2(pristine_msg, victim_msg)
    shutil.copy2(pristine_sess, victim_sess)

    # 线上原库指纹（证明全程只读）
    live_msg, live_sess = Path(entry.path), Path(sess_entry.path)
    hash_before = (sha256_file(live_msg), sha256_file(live_sess))

    revoked = load_revoked(general, chat)
    print(f"撤回记录: {len(revoked)} 条 svr_id")

    print(f"{'部分删除（增量合并场景）' if args.partial else '模拟删除会话'}"
          "（DROP/DELETE + 删 session 行 + 记 SessionDeleteTable）…")
    simulate_deletion(victim_msg, victim_sess, table, chat, partial=args.partial)
    c = sqlite3.connect(str(victim_msg))
    if args.partial:
        left = c.execute(f"SELECT COUNT(*) FROM [{table}]").fetchone()[0]
        c.close()
        print(f"  ✓ 部分删除态就绪（剩 {left} 行）")
    else:
        gone = c.execute("SELECT COUNT(*) FROM sqlite_master WHERE name=?",
                         (table,)).fetchone()[0] == 0
        c.close()
        assert gone, "DROP 后表应不存在"
        print("  ✓ 删除态就绪（表已不存在）")

    print("恢复引擎合并…")
    stats = restore_chat(pristine_msg, pristine_sess, victim_msg, victim_sess,
                         table, chat, revoked)
    print(f"  源 {stats['source_rows']} 行 → 插入 {stats['inserted']}, "
          f"重复跳过 {stats['dup_skipped']}, 撤回跳过 {stats['revoked_skipped']}, "
          f"补 sender {stats['senders_added']}, 建表 {stats['table_created']}, "
          f"session 恢复 {stats['session_restored']}, "
          f"清删除记录 {stats['delete_row_cleared']}")

    print("重加密（同 salt）…")
    enc_msg, enc_sess = run / "victim_msg.enc.db", run / "victim_session.enc.db"
    encrypt_database(victim_msg, enc_msg, key_msg, salt=salt_msg)
    encrypt_database(victim_sess, enc_sess, key_sess, salt=salt_sess)

    print("解密回读校验…")
    back_msg, back_sess = run / "back_msg.db", run / "back_session.db"
    decrypt_database(enc_msg, back_msg, key_msg)
    decrypt_database(enc_sess, back_sess, key_sess)

    checks = verify(pristine_msg, pristine_sess, back_msg, back_sess,
                    table, chat, enc_msg, enc_sess,
                    key_msg, salt_msg, key_sess)
    checks.append(("线上原库字节未动", hash_before == (sha256_file(live_msg),
                                                      sha256_file(live_sess)),
                   "sha256 前后一致"))

    print("\n──────── 校验结果 ────────")
    all_ok = True
    for name, ok, detail in checks:
        print(f"  {'✓' if ok else '✗'} {name}" + (f"  ({detail})" if detail else ""))
        all_ok &= ok
    print("────────────────────────")
    print("结论: PASS ✅" if all_ok else "结论: FAIL ❌")
    print(f"工作目录: {work}")
    return 0 if all_ok else 1


def main():
    ap = argparse.ArgumentParser(description="联系人级恢复可行性自测")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sim = sub.add_parser("simulate", help="端到端自测（不碰线上库）")
    sim.add_argument("--chat", help="指定会话 username（默认自动挑行数最多的表）")
    sim.add_argument("--pick", default="largest",
                     choices=["largest", "chatroom"], help="自动挑选偏好")
    sim.add_argument("--partial", action="store_true",
                     help="只删最近一半行（验证增量合并的去重路径），不动 session")
    sim.add_argument("--work", default=None,
                     help="工作目录（默认 data_dir/restore_probe，勿放 output/ 下）")
    sim.add_argument("--clean", action="store_true", help="先清空工作目录")
    sim.set_defaults(func=cmd_simulate)
    args = ap.parse_args()
    raise SystemExit(args.func(args))


if __name__ == "__main__":
    main()
