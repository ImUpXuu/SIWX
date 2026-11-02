"""PC 微信 4.x 数据源（xwechat_files）。"""

from __future__ import annotations

import hashlib
import re
import sqlite3
from pathlib import Path
from typing import Iterator, Any

from ..crypto.sqlcipher import WIN4, decrypt_database, file_salt, open_sqlite, verify_key
from ..crypto.zstd import decompress as zstd_decompress
from ..crypto.zstd import is_zstd
from ..errors import DecryptError, DiscoveryError, KeyError_, UsageError
from ..model import Account, Contact, Conversation, Message
from .base import Source

WXID_RE = re.compile(r"^(wxid_[0-9a-zA-Z]+)")
VERSION_RE = re.compile(r"db_storage")
_SHARD_RE = re.compile(r"^message_\d+$")


def _decode_text(data: bytes) -> str:
    for encoding in ("utf-8", "gb18030"):
        try:
            return data.decode(encoding)
        except UnicodeDecodeError:
            continue
    return data.decode("utf-8", "replace")


def decode_content(value: Any) -> str:
    """4.x 的 message_content 可能是 TEXT，也可能是 zstd BLOB。"""
    if isinstance(value, (bytes, bytearray, memoryview)):
        data = bytes(value)
        if not data:
            return ""
        if is_zstd(data):
            plain = zstd_decompress(data)
            if plain is not None:
                return _decode_text(plain)
            return data.decode("utf-8", "replace")
        return _decode_text(data)
    return str(value or "")


_FILE_ID_RE = re.compile(rb"[0-9a-f]{32}")


def file_id_of(packed: Any) -> str:
    """packed_info_data 里带资源文件 id（attach 下的 .dat 文件名前缀）。"""
    if not packed:
        return ""
    match = _FILE_ID_RE.findall(bytes(packed))
    return match[-1].decode("ascii") if match else ""


def _columns(conn: sqlite3.Connection, table: str) -> set[str]:
    try:
        rows = conn.execute(f'PRAGMA table_info("{table}")').fetchall()
    except sqlite3.DatabaseError:
        return set()
    return {str(r[1]).lower() for r in rows}


def _pick(cols: set[str], *candidates: str) -> str | None:
    for c in candidates:
        if c.lower() in cols:
            return c.lower()
    return None


def _table_names(conn: sqlite3.Connection) -> list[str]:
    try:
        rows = conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
    except sqlite3.DatabaseError:
        return []
    return [str(r[0]) for r in rows]


def _md5_hex(text: str) -> str:
    return hashlib.md5(text.encode("utf-8")).hexdigest()


class Win4Source(Source):
    kind = "win4"

    def __init__(self, account: Account, key: bytes | dict[str, bytes]) -> None:
        super().__init__(account)
        # 4.x 每个库有独立密钥：支持单个 bytes（对所有库生效）或
        # {db_storage 下相对路径: 32 字节密钥} 的映射。
        self._key = key if isinstance(key, bytes) else b""
        self._keys: dict[str, bytes] = {}
        if isinstance(key, dict):
            for rel, value in key.items():
                if len(value) != 32:
                    raise UsageError(f"{rel} 的密钥必须是 32 字节")
                self._keys[rel.replace("\\", "/")] = value
        elif len(key) != 32:
            raise UsageError("PC 微信 4.x 需要 32 字节（64 位十六进制）密钥")
        self._conns: dict[str, sqlite3.Connection] = {}
        self._contacts: dict[str, Contact] | None = None
        # 每个 message_N.db 分片有独立的 Name2Id 表，rowid 不能跨库使用
        self._name2id: dict[str, dict[int, str]] = {}
        self._msg_tables: dict[str, list[str]] = {}  # 小写 md5 -> [库键, 表名...]
        self._db_sizes: dict[str, int] = {}

    def _key_for(self, path: Path) -> bytes | None:
        if self._keys:
            storage = self.root / "db_storage"
            try:
                rel = path.relative_to(storage).as_posix()
            except ValueError:
                rel = path.name
            return self._keys.get(rel)
        return self._key or None

    # ------------------------------------------------------------------ 发现
    @classmethod
    def discover(cls, root: Path) -> Account | None:
        root = Path(root)
        storage = root / "db_storage"
        if not storage.is_dir():
            return None
        wxid = ""
        m = WXID_RE.match(root.name)
        if m:
            wxid = m.group(1)
        return Account(
            key=f"win4:{wxid or root.name}",
            kind="win4",
            display_name=wxid or root.name,
            wxid=wxid,
            root=root,
            detail={"db_storage": str(storage)},
        )

    @classmethod
    def scan_roots(cls, base: Path) -> list[Path]:
        base = Path(base)
        if not base.is_dir():
            return []
        out = []
        for child in base.iterdir():
            if child.is_dir() and (child / "db_storage").is_dir():
                out.append(child)
        return sorted(out)

    @classmethod
    def is_encrypted(cls, root: Path) -> bool:
        msg_dir = Path(root) / "db_storage" / "message"
        if not msg_dir.is_dir():
            return False
        for db in sorted(msg_dir.glob("message_*.db")):
            try:
                with db.open("rb") as fh:
                    head = fh.read(16)
            except OSError:
                continue
            return not head.startswith(b"SQLite format 3\x00")
        return False

    # ------------------------------------------------------------------ 打开
    def open(self, progress=None, checkpoint=None) -> None:
        storage = self.root / "db_storage"
        if not storage.is_dir():
            raise DiscoveryError(f"{self.root} 下没有 db_storage，可能不是 PC 微信 4.x 的数据目录")
        # 先用 message_0.db 校验密钥，避免把 40 秒花在错误的密钥上
        first = storage / "message" / "message_0.db"
        first_key = self._key_for(first) if first.exists() else None
        if first.exists():
            if first_key is None:
                raise KeyError_("缺少 message_0.db 的密钥")
            if not verify_key(first, first_key, WIN4):
                raise DecryptError("密钥校验失败：无法解密 message_0.db 第一页，请确认密钥属于该账号")
        paths = self._db_paths()
        for number, path in enumerate(paths, 1):
            if checkpoint:
                checkpoint()
            if progress:
                progress("database", number, len(paths))
            key = self._key_for(path)
            if key is None:
                self.load_errors.append(f"{path.name}: 没有对应密钥，已跳过")
                continue
            try:
                def pages(done, total):
                    if done % 256 == 0 or done == total:
                        if checkpoint:
                            checkpoint()
                        if progress:
                            progress("pages", done, total)
                raw = decrypt_database(path, key, WIN4, progress=pages if checkpoint or progress else None)
            except Exception as exc:  # 单个库损坏不影响其他库
                if checkpoint:
                    checkpoint()
                if path.name == "message_0.db":
                    raise
                self.load_errors.append(f"{path.name}: {exc}")
                continue
            try:
                conn = open_sqlite(raw)
            except Exception as exc:
                self.load_errors.append(f"{path.name}: {exc}")
                continue
            self._conns[self._db_key(path)] = conn
            self._db_sizes[path.name] = len(raw)
        if checkpoint:
            checkpoint()
        if progress:
            progress("index")
        self._index_messages()
        self._load_name2id()
        if checkpoint:
            checkpoint()
        if progress:
            progress("contacts")
        self._load_contacts()
        if checkpoint:
            checkpoint()

    def close(self) -> None:
        for conn in self._conns.values():
            conn.close()
        self._conns.clear()

    def _db_paths(self) -> list[Path]:
        storage = self.root / "db_storage"
        paths: list[Path] = []
        msg_dir = storage / "message"
        if msg_dir.is_dir():
            paths.extend(sorted(msg_dir.glob("message_*.db")))
        for rel in (
            "contact/contact.db",
            "session/session.db",
            "media/media_0.db",
            "media/media_1.db",
        ):
            p = storage / rel
            if p.exists():
                paths.append(p)
        # 其他目录里的数据库（bizchat、emoticon、message_resource 等）
        for p in sorted(storage.rglob("*.db")):
            if p in paths:
                continue
            if not p.is_file():
                continue
            if "-wal" in p.name or "-shm" in p.name:
                continue
            paths.append(p)
        return paths

    @staticmethod
    def _db_key(path: Path) -> str:
        return path.stem

    def _conn(self, key: str) -> sqlite3.Connection | None:
        return self._conns.get(key)

    # ------------------------------------------------------------------ 索引
    def _index_messages(self) -> None:
        self._msg_tables.clear()
        for key, conn in self._conns.items():
            if not _SHARD_RE.match(key):
                continue
            for name in _table_names(conn):
                if not name.lower().startswith("msg_"):
                    continue
                md5 = name[4:].lower()
                self._msg_tables.setdefault(md5, []).append(key)

    def _load_name2id(self) -> None:
        self._name2id.clear()
        for key, conn in self._conns.items():
            if not _SHARD_RE.match(key):
                continue
            per_db: dict[int, str] = {}
            for table in ("Name2Id", "name2id", "Name2ID"):
                cols = _columns(conn, table)
                if not cols:
                    continue
                user_col = _pick(cols, "username", "user_name", "usr_name")
                if not user_col:
                    continue
                try:
                    rows = conn.execute(f'SELECT rowid, "{user_col}" FROM "{table}"').fetchall()
                except sqlite3.DatabaseError:
                    continue
                for rowid, username in rows:
                    if username:
                        per_db[int(rowid)] = str(username)
                break
            self._name2id[key] = per_db

    def _load_contacts(self) -> None:
        contacts: dict[str, Contact] = {}
        for key, conn in self._conns.items():
            if "contact" not in key:
                continue
            tables = [t for t in _table_names(conn) if t.lower() == "contact"]
            if not tables:
                tables = [t for t in _table_names(conn) if "contact" in t.lower()]
            for table in tables:
                cols = _columns(conn, table)
                if not cols:
                    continue
                user_col = _pick(cols, "username", "user_name", "usr_name")
                if not user_col:
                    continue
                name_col = _pick(cols, "remark", "remark_name", "con_remark")
                nick_col = _pick(cols, "nick_name", "nickname", "nick")
                alias_col = _pick(cols, "alias", "con_alias")
                type_col = _pick(cols, "local_type", "localtype", "type")
                select = [f'"{user_col}"']
                for col in (name_col, nick_col, alias_col, type_col):
                    select.append(f'"{col}"' if col else "NULL")
                try:
                    rows = conn.execute(f'SELECT {", ".join(select)} FROM "{table}"').fetchall()
                except sqlite3.DatabaseError:
                    continue
                for row in rows:
                    username = str(row[0] or "")
                    if not username:
                        continue
                    remark, nick, alias, local_type = row[1], row[2], row[3], row[4]
                    is_group = username.endswith("@chatroom") or username.startswith("gh_") and False
                    display = (remark or nick or alias or username)
                    contacts[username] = Contact(
                        username=username,
                        display_name=str(display),
                        alias=str(alias or ""),
                        remark=str(remark or ""),
                        nick_name=str(nick or ""),
                        is_group=bool(is_group),
                    )
        self._contacts = contacts

    @property
    def media_root(self) -> Path | None:
        msg = self.root / "msg"
        return msg if msg.is_dir() else None

    # ------------------------------------------------------------------ 查询
    def contacts(self) -> dict[str, Contact]:
        if self._contacts is None:
            self._load_contacts()
        return self._contacts or {}

    def conversations(self) -> list[Conversation]:
        contacts = self.contacts()
        sessions: dict[str, dict[str, Any]] = {}
        for key, conn in self._conns.items():
            if "session" not in key:
                continue
            tables = [t for t in _table_names(conn) if t.lower() in ("session", "sessiontable", "sessions")]
            if not tables:
                tables = [t for t in _table_names(conn) if "session" in t.lower()]
            for table in tables:
                cols = _columns(conn, table)
                user_col = _pick(cols, "username", "user_name", "str_username")
                time_col = _pick(cols, "last_timestamp", "last_time", "timestamp", "time")
                if not user_col:
                    continue
                select = [f'"{user_col}"', f'"{time_col}"' if time_col else "0"]
                try:
                    rows = conn.execute(f'SELECT {", ".join(select)} FROM "{table}"').fetchall()
                except sqlite3.DatabaseError:
                    continue
                for username, ts in rows:
                    username = str(username or "")
                    if not username:
                        continue
                    sessions[username] = {"last_time": int(ts or 0), "table": table}

        out: list[Conversation] = []
        seen: set[str] = set()
        for username, info in sessions.items():
            contact = contacts.get(username)
            out.append(
                Conversation(
                    username=username,
                    display_name=contact.display_name if contact else username,
                    is_group=bool(contact and contact.is_group) or username.endswith("@chatroom"),
                    last_time=info["last_time"],
                    message_count=self._count_messages(username),
                )
            )
            seen.add(username)
        # session 表缺失的会话（只在消息表里出现）
        for md5, _keys in self._msg_tables.items():
            username = self._username_for_md5(md5)
            if username and username not in seen:
                contact = contacts.get(username)
                out.append(
                    Conversation(
                        username=username,
                        display_name=contact.display_name if contact else username,
                        is_group=bool(contact and contact.is_group) or username.endswith("@chatroom"),
                        last_time=0,
                        message_count=self._count_messages(username),
                    )
                )
                seen.add(username)
        out.sort(key=lambda c: (-c.last_time, c.username))
        return out

    def _count_messages(self, username: str) -> int:
        md5 = _md5_hex(username)
        total = 0
        for key in self._msg_tables.get(md5, []):
            conn = self._conn(key)
            if conn is None:
                continue
            try:
                total += int(conn.execute(f'SELECT COUNT(*) FROM "Msg_{md5}"').fetchone()[0])
            except sqlite3.DatabaseError:
                continue
        return total

    def _username_for_md5(self, md5: str) -> str | None:
        for username in self.contacts():
            if _md5_hex(username) == md5:
                return username
        if self.account.wxid and _md5_hex(self.account.wxid) == md5:
            return self.account.wxid
        return None

    # ------------------------------------------------------------------ 消息
    def iter_messages(
        self,
        username: str,
        since: int | None = None,
        until: int | None = None,
    ) -> Iterator[Message]:
        md5 = _md5_hex(username)
        keys = self._msg_tables.get(md5, [])
        if not keys:
            return
        table = f"Msg_{md5}"
        for key in keys:
            conn = self._conn(key)
            if conn is None:
                continue
            cols = _columns(conn, table)
            if not cols:
                continue
            id_col = _pick(cols, "local_id", "localid", "msg_local_id")
            server_col = _pick(cols, "server_id", "serverid", "msg_server_id")
            type_col = _pick(cols, "local_type", "localtype", "msg_type", "type")
            time_col = _pick(cols, "create_time", "createtime", "msg_create_time")
            content_col = _pick(cols, "message_content", "content", "msg_content", "str_content")
            sender_col = _pick(cols, "real_sender_id", "realsenderid", "sender_id")
            sort_col = _pick(cols, "sort_seq", "sortseq")
            packed_col = _pick(cols, "packed_info_data", "packedinfo", "packed_info")
            if not id_col or not time_col or not content_col:
                continue
            where = []
            params: list[Any] = []
            if since:
                where.append(f'"{time_col}" >= ?')
                params.append(int(since))
            if until:
                where.append(f'"{time_col}" <= ?')
                params.append(int(until))
            sql = f'SELECT "{id_col}", "{time_col}", "{content_col}"'
            sql += f', "{type_col}"' if type_col else ", 0"
            sql += f', "{server_col}"' if server_col else ", 0"
            sql += f', "{sender_col}"' if sender_col else ", 0"
            sql += f', "{sort_col}"' if sort_col else ", 0"
            sql += f', "{packed_col}"' if packed_col else ", NULL"
            sql += f' FROM "{table}"'
            if where:
                sql += " WHERE " + " AND ".join(where)
            order = []
            if sort_col:
                order.append(f'"{sort_col}"')
            order.append(f'"{time_col}"')
            order.append(f'"{id_col}"')
            sql += " ORDER BY " + ", ".join(order)
            try:
                rows = conn.execute(sql, params).fetchall()
            except sqlite3.DatabaseError:
                continue
            for row in rows:
                local_id, ts, content, mtype, server_id, sender_id, sort_seq, packed = row[:8]
                sender = self._name2id.get(key, {}).get(int(sender_id or 0), "")
                yield Message(
                    local_id=int(local_id or 0),
                    server_id=int(server_id or 0),
                    timestamp=int(ts or 0),
                    sender=sender,
                    sender_name=self._sender_name(username, sender),
                    is_sender=bool(sender and self.account.wxid and sender == self.account.wxid),
                    type=int(mtype or 0),
                    raw=decode_content(content),
                    extra={
                        "db": key,
                        "sort_seq": sort_seq,
                        "file_id": file_id_of(packed),
                        "chat_md5": md5,
                    },
                )

    def _sender_name(self, conversation: str, sender: str) -> str:
        if not sender:
            return ""
        if conversation.endswith("@chatroom"):
            contact = self.contacts().get(conversation)
            if contact and sender in contact.members:
                return contact.members[sender]
        contact = self.contacts().get(sender)
        if contact:
            return contact.display_name
        if sender == self.account.wxid:
            return self.account.display_name or "我"
        return sender
