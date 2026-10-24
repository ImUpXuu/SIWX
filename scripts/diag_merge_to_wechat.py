#!/usr/bin/env python3
"""diag_merge_to_wechat — 真库合并实战：.siwxpack → 微信源库（限 text，单会话）。

安全模型（与恢复引擎同源，目标是微信真库）：
  - 门禁：微信进程必须退出；密钥必须覆盖目标库；
  - 每次写路径 = 冷备份（SHA-256 + manifest）→ 副本上手术（解密→单事务→
    保 salt 重加密→解密回读校验→integrity_check）→ 原子替换；
  - 失败点全程原库未动；--rollback <stamp> 一键还原。

子命令：
  backup   只备份指定会话的 text 消息为 .siwxpack（复用 backup.run_backup）
  delete   【模拟缺失】在副本上删掉目标会话 N 条 text（记录到冷备 manifest，
           供随后 merge 补回——模拟"另一设备有、本机已删"的场景）
  analyze  包 vs 微信真库的只读 diff（将恢复/已存在）
  merge    把包内缺失的 text 合并进微信真库（Name2Id 重映射/重编号/seq 校准）
  rollback 从冷备还原指定会话涉及的库

用法：
  python scripts/diag_merge_to_wechat.py backup --chat <username> --password-stdin
  python scripts/diag_merge_to_wechat.py delete --chat <username> --count 3
  python scripts/diag_merge_to_wechat.py analyze --pack <file>
  python scripts/diag_merge_to_wechat.py merge  --pack <file> --chat <username>
  python scripts/diag_merge_to_wechat.py rollback --stamp <dir>
"""
import argparse
import hashlib
import json
import re
import shutil
import sqlite3
import sys
import time
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from siwx import keystore  # noqa: E402
from siwx import paths as _paths  # noqa: E402
from siwx.discover import find_wechat_data_dirs, find_wechat_pids  # noqa: E402
from siwx.pack import read_pack  # noqa: E402
from siwx.sqlcipher import (collect_db_files, decrypt_database,  # noqa: E402
                            encrypt_database, verify_enc_key)

MSG_COLUMNS = [
    "local_id", "server_id", "local_type", "sort_seq", "real_sender_id",
    "create_time", "status", "upload_status", "download_status", "server_seq",
    "origin_source", "source", "message_content", "compress_content",
    "packed_info_data", "WCDB_CT_message_content", "WCDB_CT_source",
]
INSERT_COLS = [c for c in MSG_COLUMNS if c != "local_id"]


def mask(u):
    return (str(u)[:3] + "***") if len(str(u or "")) > 4 else "***"


def msg_table_of(chat):
    return "Msg_" + hashlib.md5(chat.encode()).hexdigest()


def die(msg):
    print(f"✗ {msg}")
    raise SystemExit(1)


def pick_account(store):
    for w, d in find_wechat_data_dirs():
        es = collect_db_files(d)
        if es and all(e.salt_hex in store for e in es):
            return w, d, es
    die("没有密钥全覆盖的账号")


def require_wechat_closed():
    pids = find_wechat_pids()
    if pids:
        die(f"微信正在运行 (pids={pids})——真库操作要求完全退出微信")


def _conn(p):
    c = sqlite3.connect(str(p))
    c.isolation_level = None
    c.execute("PRAGMA busy_timeout=5000")
    return c


def _finish(c):
    try:
        c.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    except sqlite3.Error:
        pass
    c.close()


def cleanup_sidecars(p):
    for sfx in ("-wal", "-shm", "-journal"):
        Path(str(p) + sfx).unlink(missing_ok=True)


def backup_root():
    return _paths.data_dir() / "wechat_backup"


def cold_backup(db_path: Path, note: dict) -> Path:
    """文件级冷备份 + manifest（sha256/salt/note）。返回会话目录。"""
    stamp = time.strftime("%Y%m%d_%H%M%S")
    bdir = backup_root() / stamp
    bdir.mkdir(parents=True, exist_ok=True)
    dest = bdir / db_path.name
    shutil.copy2(db_path, dest)
    h = hashlib.sha256()
    h.update(dest.read_bytes())
    raw = db_path.read_bytes()
    meta = {"original": str(db_path), "sha256": h.hexdigest(),
            "salt": raw[:16].hex(), "note": note,
            "time": datetime.now().isoformat(timespec="seconds")}
    (bdir / "manifest.json").write_text(
        json.dumps(meta, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"[cold-backup] {db_path.name} → {bdir.name} "
          f"({dest.stat().st_size / 1048576:.1f} MB)")
    return bdir


def rewrite_db(db_path: Path, key: bytes, mutate, log=print):
    """安全写路径：副本解密 → mutate(明文连接) → 校验 → 保 salt 重加密 →
    解密回读比对 → 原子替换。任何失败原库未动。"""
    raw = db_path.read_bytes()
    salt = raw[:16]
    work = _paths.data_dir() / "merge_work"
    work.mkdir(parents=True, exist_ok=True)
    plain = work / (db_path.stem + "_plain.db")
    enc = work / (db_path.stem + "_enc.db")
    decrypt_database(db_path, plain, key)
    c = _conn(plain)
    stats = {}
    try:
        c.execute("BEGIN IMMEDIATE")
        mutate(c, stats)
        c.execute("COMMIT")
    except Exception:
        try:
            c.execute("ROLLBACK")
        except sqlite3.Error:
            pass
        raise
    finally:
        _finish(c)
    cleanup_sidecars(plain)
    log(f"[rewrite] 明文修改完成: {stats}")
    ic = sqlite3.connect(str(plain))
    try:
        chk = ic.execute("PRAGMA integrity_check").fetchone()[0]
    finally:
        ic.close()
    if chk != "ok":
        die(f"明文 integrity_check 失败: {chk}")
    # 加密产物必须落在目标同目录：os.replace 的原子性只在同一卷成立
    # （实测 C 盘 work → D 盘微信库 WinError 17）
    enc = db_path.with_name(db_path.name + ".siwx.part")
    encrypt_database(plain, enc, key, salt=salt)
    if not verify_enc_key(key, enc.read_bytes()[:4096]):
        die("重加密产物 page-1 HMAC 校验失败")
    back = work / (db_path.stem + "_back.db")
    decrypt_database(enc, back, key)
    c1 = sqlite3.connect(str(plain))
    c2 = sqlite3.connect(str(back))
    h1 = hashlib.sha256(plain.read_bytes()).hexdigest()
    h2 = hashlib.sha256(back.read_bytes()).hexdigest()
    c1.close()
    c2.close()
    if h1 != h2:
        die("解密回读与明文不一致（往返哈希不等）")
    log("[rewrite] 往返校验通过 → 原子替换")
    os_replace(enc, db_path)
    for p in (plain, back):
        p.unlink(missing_ok=True)
        cleanup_sidecars(p)
    return stats


def os_replace(src: Path, dst: Path):
    import os
    os.replace(src, dst)


def find_shards(rel_map, chat):
    tbl = msg_table_of(chat)
    out = {}
    for rel, e in rel_map.items():
        if not re.fullmatch(r"message/message_\d+\.db", rel):
            continue
        p = _paths.data_dir() / "merge_probe" / rel.replace("/", "_")
        p.parent.mkdir(parents=True, exist_ok=True)
        decrypt_database(e.path, p, bytes.fromhex(
            _CTX["store"][e.salt_hex]["key"]))
        c = sqlite3.connect(str(p))
        try:
            have = c.execute(
                "SELECT COUNT(*) FROM sqlite_master WHERE name=?",
                (tbl,)).fetchone()[0]
            if have:
                out[rel] = (p, c.execute(
                    f"SELECT MAX(create_time) FROM [{tbl}]").fetchone()[0] or 0)
        finally:
            c.close()
    return tbl, out


_CTX = {}


# ── 子命令 ────────────────────────────────────────────────────


def cmd_backup(args):
    from siwx import backup as bk

    def confirm(pids):
        return True        # 脚本模式：微信必须已退出，stop_wechat 直通

    pw = sys.stdin.readline().rstrip("\r\n")
    rep = bk.run_backup(
        pw, mode="selective", chats=[args.chat],
        types=[int(t) for t in args.types.split(",")] if args.types else None,
        start=None, end=None, confirm_kill=confirm, keep_work=False)
    print(f"✓ 包: {rep['path']} ({rep['size'] / 1048576:.1f} MB)")


def cmd_delete(args):
    store = keystore.load()
    require_wechat_closed()
    wxid, db_dir, entries = pick_account(store)[:3]
    rel_map = {e.rel.replace("\\", "/"): e for e in entries}
    _CTX["store"] = store
    tbl, shards = find_shards(rel_map, args.chat)
    if not shards:
        die("目标会话在微信库中没有消息表")
    # 活跃分片 = 该表 create_time 最大的分片
    active_rel = max(shards, key=lambda r: shards[r][1])
    print(f"表 {tbl[:14]}… 分布在 {list(shards)}；活跃分片 {active_rel}")
    target = Path(rel_map[active_rel].path)
    key = bytes.fromhex(store[rel_map[active_rel].salt_hex]["key"])
    bdir = cold_backup(target, {"action": "simulate-delete",
                                "chat": mask(args.chat), "count": args.count})

    removed = {}

    def mutate(c, stats):
        if args.sids:
            # 指定消息删除：按 server_id 后 6 位匹配（跨库唯一，需校验唯一性）
            wanted = [s.strip() for s in args.sids.split(",") if s.strip()]
            allrows = c.execute(
                f"SELECT local_id, server_id, create_time, message_content, "
                f"local_type FROM [{tbl}]").fetchall()
            picks = []
            for w in wanted:
                hit = [r for r in allrows if str(r[1]).endswith(w)]
                if len(hit) != 1:
                    die(f"server_id 后缀 {w} 命中 {len(hit)} 条（需唯一）")
                picks.append(hit[0])
        else:
            rows = c.execute(
                f"SELECT local_id, server_id, create_time, message_content, "
                f"local_type FROM [{tbl}] WHERE (local_type & 0xFFFF)=1 "
                f"ORDER BY create_time").fetchall()
            if len(rows) < args.count:
                die(f"text 行只有 {len(rows)} 条，不够删 {args.count} 条")
            step = max(1, len(rows) // args.count)
            picks = [rows[i * step] for i in range(args.count)]
        for lid, sid, ctime, content, ltype in picks:
            c.execute(f"DELETE FROM [{tbl}] WHERE local_id=?", (lid,))
            removed[str(sid)] = {
                "create_time": ctime, "local_type": ltype,
                "preview": (content or b"").decode("utf-8", "replace")[:24]}
        stats["deleted"] = len(picks)

    rewrite_db(target, key, mutate)
    (bdir / "removed.json").write_text(
        json.dumps(removed, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"✓ 已在真库删除 {len(removed)} 条 text（模拟缺失），"
          f"server_id 记录于 {bdir / 'removed.json'}")
    print("  现在可以开微信确认消息消失，然后跑 merge 用包补回来")


def cmd_analyze(args):
    store = keystore.load()
    wxid, db_dir, entries = pick_account(store)[:3]
    rel_map = {e.rel.replace("\\", "/"): e for e in entries}
    _CTX["store"] = store
    tmp = _paths.data_dir() / "merge_probe"
    manifest = None
    # 找包：优先 exports 下最新的
    pack = Path(args.pack)
    if not pack.is_file():
        die(f"包不存在: {pack}")
    stage = _paths.data_dir() / "merge_stage"
    shutil.rmtree(stage, ignore_errors=True)
    manifest = read_pack(pack, args.password, stage / "payload")
    print(f"包: {manifest.get('mode')} · {manifest.get('created_at')}")
    chats = manifest.get("stats", {}).get("chats", {})
    print(f"包内会话 {len(chats)} 个: "
          + ", ".join(f"{mask(k)}:{v}条" for k, v in list(chats.items())[:5]))
    # 包内消息 vs 真库
    tbl, shards = find_shards(rel_map, args.chat)
    live_sids = set()
    for rel, (p, _mt) in shards.items():
        c = sqlite3.connect(str(p))
        live_sids |= {r[0] for r in c.execute(
            f"SELECT server_id FROM [{tbl}]")}
        c.close()
    pack_sids = set()
    pack_rows = []
    for p in sorted((stage / "payload" / "message").glob("message_*.db")):
        if "restore" in p.name:
            continue
        c = sqlite3.connect(str(p))
        try:
            pack_sids |= {r[0] for r in c.execute(
                f"SELECT server_id FROM [{tbl}]")}
            pack_rows += c.execute(
                f"SELECT {', '.join(MSG_COLUMNS)} FROM [{tbl}]").fetchall()
        except sqlite3.Error:
            pass
        c.close()
    missing = pack_sids - live_sids
    print(f"包内 {len(pack_sids)} 条 / 真库 {len(live_sids)} 条 / "
          f"缺失 {len(missing)} 条（将恢复）")
    (stage / "analyze.json").write_text(json.dumps({
        "missing": sorted(missing)}), encoding="utf-8")
    print("（analyze.json 已写入暂存目录，merge 将复用）")


def cmd_merge(args):
    store = keystore.load()
    require_wechat_closed()
    wxid, db_dir, entries = pick_account(store)[:3]
    rel_map = {e.rel.replace("\\", "/"): e for e in entries}
    _CTX["store"] = store
    tbl, shards = find_shards(rel_map, args.chat)
    if not shards:
        die("目标会话在微信库中没有消息表——被删会话请走恢复分片路径")
    pack = Path(args.pack)
    stage = _paths.data_dir() / "merge_stage"
    if not (stage / "payload").is_dir():
        read_pack(pack, args.password, stage / "payload")
    # 包内目标会话行
    pack_rows, pack_senders = [], {}
    for p in sorted((stage / "payload" / "message").glob("message_*.db")):
        c = sqlite3.connect(str(p))
        try:
            pack_rows += c.execute(
                f"SELECT {', '.join(MSG_COLUMNS)} FROM [{tbl}]").fetchall()
            pack_senders.update(dict(c.execute(
                "SELECT rowid, user_name FROM Name2Id").fetchall()))
        except sqlite3.Error:
            pass
        c.close()
    live_sids = set()
    for rel, (p, _mt) in shards.items():
        c = sqlite3.connect(str(p))
        live_sids |= {r[0] for r in c.execute(
            f"SELECT server_id FROM [{tbl}]")}
        c.close()
    todo = [r for r in pack_rows if r[MSG_COLUMNS.index("server_id")]
            not in live_sids]
    print(f"待合并 {len(todo)} 条（包 {len(pack_rows)} / 真库 {len(live_sids)}）")
    if not todo:
        print("没有缺失，无事可做（幂等）")
        return
    active_rel = max(shards, key=lambda r: shards[r][1])
    target = Path(rel_map[active_rel].path)
    key = bytes.fromhex(store[rel_map[active_rel].salt_hex]["key"])
    bdir = cold_backup(target, {"action": "merge", "chat": mask(args.chat),
                                "rows": len(todo)})

    # Name2Id 重映射准备：真库分片自身 Name2Id（读活跃分片明文）
    plain_probe = shards[active_rel][0]

    def mutate(c, stats):
        # 目标分片的 Name2Id：username → rowid（缺则补）
        n2i = {u: rid for rid, u in c.execute(
            "SELECT rowid, user_name FROM Name2Id").fetchall()}
        meta = dict(c.execute(
            "SELECT user_name, is_session FROM Name2Id").fetchall())
        src_si = MSG_COLUMNS.index("real_sender_id")
        dst_si = INSERT_COLS.index("real_sender_id")
        sid_i = MSG_COLUMNS.index("server_id")
        for row in todo:
            r = dict(zip(MSG_COLUMNS, row))
            if r["server_id"] in live_sids:
                continue
            vals = [v for k, v in zip(MSG_COLUMNS, row) if k != "local_id"]
            u = pack_senders.get(row[src_si])
            if u and u not in n2i:
                cur = c.execute(
                    "INSERT INTO Name2Id(user_name, is_session) VALUES(?, ?)",
                    (u, 0))
                n2i[u] = cur.lastrowid
            if u:
                vals[dst_si] = n2i[u]
            c.execute(
                f"INSERT INTO [{tbl}] ({colspec_join()}) VALUES ({qm(len(INSERT_COLS))})",
                vals)
            live_sids.add(r["server_id"])
        # AUTOINCREMENT 校准
        mx = c.execute(f"SELECT MAX(local_id) FROM [{tbl}]").fetchone()[0] or 0
        cur = c.execute("UPDATE sqlite_sequence SET seq=? WHERE name=?",
                        (mx, tbl))
        if cur.rowcount == 0:
            c.execute("INSERT INTO sqlite_sequence(name, seq) VALUES(?, ?)",
                      (tbl, mx))
        stats["merged"] = len(todo)

    rewrite_db(target, key, mutate)
    print(f"✓ 合并完成 → {active_rel}；冷备 {bdir.name}（--rollback {bdir.name} 可还原）")
    print("  打开微信 → 会话'用户131'查看被删的 text 是否回来；")
    print("  重启微信看是否存活；发一条新消息触发同步再确认。")


def colspec_join():
    return ", ".join(INSERT_COLS)


def qm(n):
    return ", ".join("?" * n)


def cmd_rollback(args):
    bdir = backup_root() / args.stamp
    meta = json.loads((bdir / "manifest.json").read_text(encoding="utf-8"))
    target = Path(meta["original"])
    require_wechat_closed()
    dest = bdir / target.name
    if not dest.is_file():
        die(f"冷备文件缺失: {dest}")
    shutil.copy2(dest, target)
    cleanup_sidecars(target)
    print(f"✓ 已还原 {target.name} ← {bdir.name}（sha256 见 manifest）")


def main():
    ap = argparse.ArgumentParser(description="真库合并实战脚本")
    sub = ap.add_subparsers(dest="cmd", required=True)
    b = sub.add_parser("backup")
    b.add_argument("--chat", required=True)
    b.add_argument("--types", default="1")
    b.set_defaults(fn=cmd_backup)
    d = sub.add_parser("delete")
    d.add_argument("--chat", required=True)
    d.add_argument("--count", type=int, default=3)
    d.add_argument("--sids", default=None,
                   help="逗号分隔的 server_id 后 6 位（指定删除；与 --count 二选一）")
    d.set_defaults(fn=cmd_delete)
    a = sub.add_parser("analyze")
    a.add_argument("--pack", required=True)
    a.add_argument("--password", required=True)
    a.set_defaults(fn=cmd_analyze)
    m = sub.add_parser("merge")
    m.add_argument("--pack", required=True)
    m.add_argument("--chat", required=True)
    m.add_argument("--password", required=True)
    m.set_defaults(fn=cmd_merge)
    r = sub.add_parser("rollback")
    r.add_argument("--stamp", required=True)
    r.set_defaults(fn=cmd_rollback)
    args = ap.parse_args()
    args.fn(args)


if __name__ == "__main__":
    main()
