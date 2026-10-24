"""恢复引擎：.siwxpack → SIWX 缓存库（output/<wxid>/）。

安全模型（「最安全的恢复」）：
  1. 分三阶段：stage（解包到独立暂存目录 + 逐文件 SHA-256 对账）→
     analyze（**只读** diff 报告，不写 output 一个字节）→ 用户确认后 apply；
  2. apply 永不触碰微信源库（db_storage）——只写 SIWX 自己的解密产物目录；
  3. 消息写入**专用恢复分片** message/message_restore_0.db：它不是任何微信
     源库的镜像名，后续「重新解密」只按源文件名写 message_0..N.db，永远不会
     覆盖它——恢复的数据一次写入、永久存活；
  4. session/contact 属增强信息（会话摘要/联系人名），按 username 合并进
     对应库；这两个文件名与源库同名，源库变化触发重解密时合并内容会回退
     （消息不受影响，会话仍会经「session.db ∪ Msg 表」出现在列表里），
     重跑一次恢复即可补回——报告会明示；
  5. apply 前被改写的目标文件先冷备到 data_dir/restore_backup/<stamp>/。

diff/去重口径：server_id（消息服务端 id，收发双方与各分片一致）；
local_id 在恢复分片内统一重编号；real_sender_id 经 Name2Id 按 username
重映射（分片内 rowid 不通用，实测同一人跨分片 rowid 1/6/46 各不相同）。
"""
import json
import shutil
import sqlite3
import time
import uuid
from pathlib import Path

from siwx import paths as _paths
from siwx import logger as _slog
from siwx.pack import read_pack

RESTORE_SHARD = "message_restore_0.db"
STAGE_TTL = 24 * 3600          # 暂存目录保留时长（未 apply 的分析产物）

MSG_COLUMNS = [
    "local_id", "server_id", "local_type", "sort_seq", "real_sender_id",
    "create_time", "status", "upload_status", "download_status", "server_seq",
    "origin_source", "source", "message_content", "compress_content",
    "packed_info_data", "WCDB_CT_message_content", "WCDB_CT_source",
]


class RestoreError(Exception):
    pass


def _stage_root(root: Path = None) -> Path:
    return Path(root) if root else _paths.data_dir() / "restore_stage"


def _out_root(root: Path = None) -> Path:
    return Path(root) if root else _paths.out_root()


def _conn(p: Path):
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


def cleanup_sidecars(p: Path):
    for suffix in ("-wal", "-shm", "-journal"):
        Path(str(p) + suffix).unlink(missing_ok=True)


def msg_table_of(chat: str) -> str:
    import hashlib
    return "Msg_" + hashlib.md5(chat.encode()).hexdigest()


# ── 阶段 1：stage（解包 + 对账）────────────────────────────────


def stage_pack(pack_path, password: str, stage_root: Path = None,
               log=print, progress=None) -> dict:
    """解包到暂存目录并读取 manifest。返回 {id, dir, manifest}。"""
    root = _stage_root(stage_root)
    root.mkdir(parents=True, exist_ok=True)
    # 顺带清理过期暂存（>24h 且非本次）
    now = time.time()
    for d in root.iterdir():
        try:
            if d.is_dir() and now - d.stat().st_mtime > STAGE_TTL:
                shutil.rmtree(d, ignore_errors=True)
        except OSError:
            continue
    sid = uuid.uuid4().hex[:12]
    work = root / sid
    work.mkdir(parents=True, exist_ok=True)
    try:
        manifest = read_pack(pack_path, password, work / "payload",
                             progress=progress)
    except Exception:
        shutil.rmtree(work, ignore_errors=True)
        raise
    (work / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=1), encoding="utf-8")
    log(f"[restore] 已暂存包 {Path(pack_path).name} → 阶段 {sid}")
    return {"id": sid, "dir": work, "manifest": manifest}


def load_stage(stage_id: str, stage_root: Path = None) -> dict:
    work = _stage_root(stage_root) / stage_id
    if not (work / "manifest.json").is_file():
        raise RestoreError(f"暂存 {stage_id} 不存在或已过期（重新分析一次即可）")
    manifest = json.loads((work / "manifest.json").read_text(encoding="utf-8"))
    return {"id": stage_id, "dir": work, "manifest": manifest}


# ── 公共：包内结构解析 ────────────────────────────────────────


def _pack_msg_shards(payload: Path, manifest: dict) -> list:
    files = [f["rel"] for f in manifest.get("files", [])]
    shards = [payload / Path(*r.split("/")) for r in files
              if r.startswith("message/") and r.endswith(".db")
              and "-wal" not in r]
    return [p for p in shards if p.is_file()]


def _target_shards(acc_dir: Path) -> list:
    d = acc_dir / "message"
    if not d.is_dir():
        return []
    out = []
    for p in sorted(d.glob("*.db")):
        if p.name.endswith("-wal") or p.name.endswith("-shm"):
            continue
        out.append(p)
    return out


def _reverse_usernames(payload: Path, manifest: dict,
                       shards: list) -> dict:
    """表名 md5 → username（包内各 Name2Id / contact name2id 汇总）。"""
    rev = {}
    sources = list(shards)
    for rel in ("session/session.db", "contact/contact.db"):
        p = payload / Path(*rel.split("/"))
        if p.is_file():
            sources.append(p)
    for p in sources:
        try:
            c = sqlite3.connect(str(p))
        except sqlite3.Error:
            continue
        try:
            tables = {r[0] for r in c.execute(
                "SELECT name FROM sqlite_master WHERE type='table'")}
            queries = []
            if "Name2Id" in tables:
                queries.append("SELECT user_name FROM Name2Id")
            if "name2id" in tables:
                queries.append("SELECT username FROM name2id")
            if "SessionTable" in tables:
                queries.append("SELECT username FROM SessionTable")
            for q in queries:
                for (u,) in c.execute(q):
                    u = (u or "").strip()
                    if u:
                        rev.setdefault(msg_table_of(u), u)
        except sqlite3.Error:
            continue
        finally:
            c.close()
    return rev


def _display_names(payload: Path, acc_dir: Path) -> dict:
    """username → 展示名：包 contact 优先，目标库 contact 兜底。"""
    names = {}

    def load(p, sql, fallback_sql):
        if not p.is_file():
            return
        try:
            c = sqlite3.connect(str(p))
            try:
                cols = {r[1] for r in c.execute("PRAGMA table_info(contact)")}
                use = sql if {"remark", "nick_name"} <= cols else fallback_sql
                for u, *cand in c.execute(use):
                    if u and not names.get(u):
                        names[u] = next((x for x in cand if x), u)
            finally:
                c.close()
        except sqlite3.Error:
            pass

    load(payload / "contact" / "contact.db",
         "SELECT username, remark, nick_name FROM contact",
         "SELECT username, nick_name FROM contact")
    if acc_dir.is_dir():
        load(acc_dir / "contact" / "contact.db",
             "SELECT username, remark, nick_name FROM contact",
             "SELECT username, nick_name FROM contact")
    return names


def _table_rows(conn, table) -> int:
    try:
        return conn.execute(f"SELECT COUNT(*) FROM [{table}]").fetchone()[0]
    except sqlite3.Error:
        return 0


def _server_ids(conn, table) -> set:
    try:
        return {r[0] for r in conn.execute(
            f"SELECT server_id FROM [{table}]")}
    except sqlite3.Error:
        return set()


# ── 阶段 2：analyze（只读 diff 报告）──────────────────────────


def analyze(stage: dict, out_root: Path = None) -> dict:
    """只读对比：包 vs 目标缓存库。不写任何文件。"""
    payload = stage["dir"] / "payload"
    manifest = stage["manifest"]
    account = manifest.get("account") or ""
    acc_dir = _out_root(out_root) / account
    fresh = not acc_dir.is_dir()

    pack_shards = _pack_msg_shards(payload, manifest)
    if not pack_shards:
        raise RestoreError("包里没有可恢复的消息库（manifest.files 缺 message/*.db）")
    rev = _reverse_usernames(payload, manifest, pack_shards)
    names = _display_names(payload, acc_dir)
    target_shards = [] if fresh else _target_shards(acc_dir)

    # 目标侧各表的 server_id 全集（含恢复分片自身 → 幂等）
    tgt_sids = {}
    for sp in target_shards:
        try:
            c = sqlite3.connect(str(sp))
        except sqlite3.Error:
            continue
        try:
            for (tbl,) in c.execute(
                    "SELECT name FROM sqlite_master WHERE type='table' "
                    "AND name LIKE 'Msg_%'"):
                tgt_sids.setdefault(tbl, set()).update(_server_ids(c, tbl))
        finally:
            c.close()

    chats, total_pack, total_dup, total_new = [], 0, 0, 0
    pack_tables = {}                     # tbl → server_id 并集（同表跨包内分片）
    for sp in pack_shards:
        c = sqlite3.connect(str(sp))
        try:
            tables = [r[0] for r in c.execute(
                "SELECT name FROM sqlite_master WHERE type='table' "
                "AND name LIKE 'Msg_%'")]
            for tbl in tables:
                pack_tables.setdefault(tbl, set()).update(_server_ids(c, tbl))
        finally:
            c.close()

    for tbl, sids in pack_tables.items():
        existing = tgt_sids.get(tbl, set())
        dup = len(sids & existing)
        insert = len(sids) - dup
        username = rev.get(tbl, "")
        chats.append({
            "table": tbl[:10] + "…",
            "username": username,
            "display": names.get(username, username or tbl[:12] + "…"),
            "pack_rows": len(sids),
            "existing": len(existing),
            "dup": dup,
            "to_insert": insert,
        })
        total_pack += len(sids)
        total_dup += dup
        total_new += insert
    chats.sort(key=lambda e: (-e["to_insert"], e["display"]))

    # session / contact 增量
    sessions_new = contacts_new = 0
    if not fresh:
        ps = payload / "session" / "session.db"
        ts = acc_dir / "session" / "session.db"
        if ps.is_file() and ts.is_file():
            try:
                c1, c2 = sqlite3.connect(str(ps)), sqlite3.connect(str(ts))
                have = {r[0] for r in c2.execute(
                    "SELECT username FROM SessionTable")}
                sessions_new = sum(
                    1 for (u,) in c1.execute(
                        "SELECT username FROM SessionTable") if u not in have)
                c1.close()
                c2.close()
            except sqlite3.Error:
                pass
        pc = payload / "contact" / "contact.db"
        tc = acc_dir / "contact" / "contact.db"
        if pc.is_file() and tc.is_file():
            try:
                c1, c2 = sqlite3.connect(str(pc)), sqlite3.connect(str(tc))
                have = {r[0] for r in c2.execute(
                    "SELECT username FROM contact")}
                contacts_new = sum(
                    1 for (u,) in c1.execute(
                        "SELECT username FROM contact") if u and u not in have)
                c1.close()
                c2.close()
            except sqlite3.Error:
                pass

    return {
        "stage_id": stage["id"],
        "account": account,
        "fresh": fresh,
        "pack_mode": manifest.get("mode"),
        "pack_created": manifest.get("created_at"),
        "chats": chats[:200],
        "chats_count": len(chats),
        "total_pack": total_pack,
        "total_dup": total_dup,
        "total_insert": total_new,
        "sessions_new": sessions_new,
        "contacts_new": contacts_new,
    }


# ── 阶段 3：apply（写目标缓存库）─────────────────────────────


def _clone_ddl(src: sqlite3.Connection, table: str, dst: sqlite3.Connection):
    rows = src.execute(
        "SELECT sql FROM sqlite_master WHERE tbl_name=? AND sql IS NOT NULL "
        "AND type IN ('table','index') "
        "ORDER BY CASE type WHEN 'table' THEN 0 ELSE 1 END", (table,)).fetchall()
    if not rows:
        raise ValueError(f"源库没有表 {table}")
    for (sql,) in rows:
        dst.execute(sql)


def _merge_session(payload: Path, acc_dir: Path, backup_dir: Path,
                   rev: dict, log) -> dict:
    ps = payload / "session" / "session.db"
    ts = acc_dir / "session" / "session.db"
    if not ps.is_file() or not ts.is_file():
        return {"merged": 0}
    backup_dir.mkdir(parents=True, exist_ok=True)
    shutil.copy2(ts, backup_dir / ts.name)
    src = sqlite3.connect(str(ps))
    dst = _conn(ts)
    n = 0
    try:
        cur = src.execute("SELECT * FROM SessionTable")
        cols = [d[0] for d in cur.description]
        rows = cur.fetchall()
        colspec = ", ".join(f"[{c}]" for c in cols)
        qmarks = ", ".join("?" * len(cols))
        lts = cols.index("last_timestamp") if "last_timestamp" in cols else None
        dst.execute("BEGIN IMMEDIATE")
        for row in rows:
            u = row[cols.index("username")] if "username" in cols else None
            if not u:
                continue
            # 只在包内行更新（last_timestamp 更大）或目标无行时覆盖——
            # 无脑 REPLACE 会把缓存里较新的会话摘要回退成包里的旧状态
            if lts is not None:
                old = dst.execute(
                    "SELECT last_timestamp FROM SessionTable WHERE username=?",
                    (u,)).fetchone()
                if old and (row[lts] or 0) <= (old[0] or 0):
                    continue
            dst.execute("DELETE FROM SessionTable WHERE username=?", (u,))
            dst.execute(
                f"INSERT INTO SessionTable ({colspec}) VALUES ({qmarks})", row)
            try:
                dst.execute("INSERT OR IGNORE INTO Name2Id(user_name) VALUES(?)",
                            (u,))
            except sqlite3.Error:
                pass
            n += 1
        dst.execute("COMMIT")
    except Exception:
        try:
            dst.execute("ROLLBACK")
        except sqlite3.Error:
            pass
        raise
    finally:
        src.close()
        _finish(dst)
    cleanup_sidecars(ts)
    return {"merged": n}


def _merge_contact(payload: Path, acc_dir: Path, backup_dir: Path,
                   log) -> dict:
    pc = payload / "contact" / "contact.db"
    tc = acc_dir / "contact" / "contact.db"
    if not pc.is_file() or not tc.is_file():
        return {"added": 0, "rooms": 0}
    backup_dir.mkdir(parents=True, exist_ok=True)
    shutil.copy2(tc, backup_dir / tc.name)
    src = sqlite3.connect(str(pc))
    dst = _conn(tc)
    added = rooms = 0
    try:
        dst.execute("BEGIN IMMEDIATE")
        have = {r[0] for r in dst.execute("SELECT username FROM contact")}
        # username → 目标 id 映射（新联系人插入后捕获自增 id）
        idmap = {}
        cur = src.execute("SELECT * FROM contact")
        cols = [d[0] for d in cur.description]
        for row in cur.fetchall():
            u = row[cols.index("username")]
            if not u:
                continue
            hit = dst.execute(
                "SELECT id FROM contact WHERE username=?", (u,)).fetchone()
            if hit:
                idmap[u] = hit[0]
                continue
            colspec = ", ".join(f"[{c}]" for c in cols if c != "id")
            qmarks = ", ".join("?" * (len(cols) - 1))
            vals = [v for c, v in zip(cols, row) if c != "id"]
            c2 = dst.execute(
                f"INSERT INTO contact ({colspec}) VALUES ({qmarks})", vals)
            idmap[u] = c2.lastrowid
            added += 1
        # chat_room / chatroom_member 按 username 重建映射
        try:
            room_cur = src.execute("SELECT * FROM chat_room")
            rcols = [d[0] for d in room_cur.description]
            for row in room_cur.fetchall():
                u = row[rcols.index("username")]
                if not u:
                    continue
                hit = dst.execute(
                    "SELECT id FROM chat_room WHERE username=?", (u,)).fetchone()
                if hit:
                    room_id = hit[0]
                else:
                    colspec = ", ".join(f"[{c}]" for c in rcols if c != "id")
                    qmarks = ", ".join("?" * (len(rcols) - 1))
                    vals = [v for c, v in zip(rcols, row) if c != "id"]
                    c3 = dst.execute(
                        f"INSERT INTO chat_room ({colspec}) VALUES ({qmarks})",
                        vals)
                    room_id = c3.lastrowid
                rooms += 1
                # 成员：包内 member_id → 包内 username → 目标 id
                src_room = src.execute(
                    "SELECT id FROM chat_room WHERE username=?", (u,)).fetchone()
                if not src_room:
                    continue
                for (mid,) in src.execute(
                        "SELECT member_id FROM chatroom_member WHERE room_id=?",
                        (src_room[0],)):
                    mu = src.execute(
                        "SELECT username FROM contact WHERE id=?", (mid,)).fetchone()
                    if mu and mu[0] in idmap:
                        dst.execute(
                            "INSERT OR IGNORE INTO chatroom_member"
                            "(room_id, member_id) VALUES(?, ?)",
                            (room_id, idmap[mu[0]]))
        except sqlite3.Error as e:
            _slog.detailed("restore", f"chat_room 合并跳过: {e}")
        dst.execute("COMMIT")
    except Exception:
        try:
            dst.execute("ROLLBACK")
        except sqlite3.Error:
            pass
        raise
    finally:
        src.close()
        _finish(dst)
    cleanup_sidecars(tc)
    return {"added": added, "rooms": rooms}


def apply(stage: dict, out_root: Path = None, log=print,
          progress=None) -> dict:
    """把暂存的包合入目标缓存库。消息 → 专用恢复分片；session/contact 按
    username 合并；被改写文件先冷备。"""
    prog = progress or (lambda pct, msg: None)
    payload = stage["dir"] / "payload"
    manifest = stage["manifest"]
    account = manifest.get("account") or ""
    if not account:
        raise RestoreError("包 manifest 缺少 account（旧版包？重新备份一次）")
    acc_dir = _out_root(out_root) / account

    # ── 全新安装：目标账号目录不存在 → 直接落盘，无需合并 ──
    if not acc_dir.is_dir():
        acc_dir.mkdir(parents=True, exist_ok=True)
        n = 0
        for ent in manifest.get("files", []):
            rel = ent["rel"]
            src = payload / Path(*rel.split("/"))
            if not src.is_file() or rel.endswith(("-wal", "-shm")):
                continue
            dst = acc_dir / Path(*rel.split("/"))
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src, dst)
            n += 1
        log(f"[restore] ✓ 全新安装：{n} 个文件 → {acc_dir.name[:3]}***")
        return {"mode": "install", "files": n, "inserted": 0, "dup": 0,
                "sessions": 0, "contacts": 0, "chats": {}}

    pack_shards = _pack_msg_shards(payload, manifest)
    rev = _reverse_usernames(payload, manifest, pack_shards)

    backup_dir = _paths.data_dir() / "restore_backup" / time.strftime("%Y%m%d_%H%M%S")
    msg_dir = acc_dir / "message"
    msg_dir.mkdir(parents=True, exist_ok=True)
    rshard = msg_dir / RESTORE_SHARD

    prog(5, "合并消息 → 恢复分片…")
    inserted = dup = 0
    per_chat = {}
    if pack_shards:
        if rshard.is_file():
            _ensure_backup(backup_dir, rshard)   # 恢复分片也冷备（追加合并前）
        rd = _conn(rshard)
        try:
            # 恢复分片自身 DDL 基底：Name2Id（首个包分片克隆）
            sc0 = sqlite3.connect(str(pack_shards[0]))
            try:
                _clone_ddl(sc0, "Name2Id", rd)
            except (ValueError, sqlite3.OperationalError):
                pass
            finally:
                sc0.close()

            target_sids = {}
            for sp in _target_shards(acc_dir):
                try:
                    c = sqlite3.connect(str(sp))
                except sqlite3.Error:
                    continue
                try:
                    for (tbl,) in c.execute(
                            "SELECT name FROM sqlite_master WHERE type='table' "
                            "AND name LIKE 'Msg_%'"):
                        target_sids.setdefault(tbl, set()).update(
                            _server_ids(c, tbl))
                finally:
                    c.close()

            sender_map = {u: rid for rid, u in rd.execute(
                "SELECT rowid, user_name FROM Name2Id").fetchall()}
            insert_cols = [c for c in MSG_COLUMNS if c != "local_id"]
            colspec = ", ".join(insert_cols)
            qmarks = ", ".join("?" * len(insert_cols))
            src_si = MSG_COLUMNS.index("real_sender_id")
            dst_si = insert_cols.index("real_sender_id")
            sid_i = MSG_COLUMNS.index("server_id")

            rd.execute("BEGIN IMMEDIATE")
            tbl_created = set()
            try:
                for sp in pack_shards:
                    sc = sqlite3.connect(str(sp))
                    try:
                        tables = [r[0] for r in sc.execute(
                            "SELECT name FROM sqlite_master WHERE type='table' "
                            "AND name LIKE 'Msg_%'")]
                        for tbl in tables:
                            username = rev.get(tbl, "")
                            have = target_sids.setdefault(tbl, set())
                            shard_senders = dict(sc.execute(
                                "SELECT rowid, user_name FROM Name2Id"
                            ).fetchall())
                            skip_tbl = False
                            for row in sc.execute(
                                    f"SELECT {', '.join(MSG_COLUMNS)} FROM [{tbl}]"):
                                s = row[sid_i]
                                if s in have:
                                    dup += 1
                                    continue
                                if tbl not in tbl_created:
                                    # 惰性建表：全部重复（同机重放）时不留空表壳
                                    try:
                                        _clone_ddl(sc, tbl, rd)
                                    except sqlite3.OperationalError:
                                        pass        # 恢复分片里已存在（幂等二跑）
                                    except ValueError:
                                        skip_tbl = True   # 源缺 DDL
                                        break
                                    tbl_created.add(tbl)
                                if skip_tbl:
                                    break
                                have.add(s)
                                vals = [v for k, v in zip(MSG_COLUMNS, row)
                                        if k != "local_id"]
                                u = shard_senders.get(row[src_si])
                                if u is not None and u not in sender_map:
                                    cur = rd.execute(
                                        "INSERT INTO Name2Id(user_name, is_session) "
                                        "VALUES(?, 0)", (u,))
                                    sender_map[u] = cur.lastrowid
                                if u is not None:
                                    vals[dst_si] = sender_map[u]
                                rd.execute(
                                    f"INSERT INTO [{tbl}] ({colspec}) "
                                    f"VALUES ({qmarks})", vals)
                                inserted += 1
                                ent = per_chat.setdefault(
                                    username or tbl[:12], {"inserted": 0, "dup": 0})
                                ent["inserted"] += 1
                            if not skip_tbl and tbl in tbl_created:
                                # 恢复分片 AUTOINCREMENT 校准
                                mx = rd.execute(
                                    f"SELECT MAX(local_id) FROM [{tbl}]").fetchone()[0] or 0
                                cur = rd.execute(
                                    "UPDATE sqlite_sequence SET seq=? WHERE name=?",
                                    (mx, tbl))
                                if cur.rowcount == 0:
                                    rd.execute(
                                        "INSERT INTO sqlite_sequence(name, seq) "
                                        "VALUES(?, ?)", (tbl, mx))
                    finally:
                        sc.close()
                rd.execute("COMMIT")
            except Exception:
                rd.execute("ROLLBACK")
                raise
        finally:
            _finish(rd)
        cleanup_sidecars(rshard)

    prog(70, "合并 session / contact…")
    sess = _merge_session(payload, acc_dir, backup_dir / "session", rev, log)
    cont = _merge_contact(payload, acc_dir, backup_dir / "contact", log)
    log(f"[restore] ✓ 完成：消息 +{inserted}（重复 {dup}），"
        f"session {sess['merged']}，contact +{cont['added']}")
    return {"mode": "merge", "inserted": inserted, "dup": dup,
            "sessions": sess["merged"], "contacts": cont["added"],
            "chats": per_chat, "shard": str(rshard),
            "backup_dir": str(backup_dir)}


def _ensure_backup(backup_dir: Path, target: Path) -> Path:
    backup_dir.mkdir(parents=True, exist_ok=True)
    dest = backup_dir / target.name
    shutil.copy2(target, dest)
    return dest
