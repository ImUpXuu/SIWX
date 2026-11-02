"""Explicit-current-user adapter over unchanged wexport Windows 4.x code.

No changes to extraction algorithms, injection, privilege adjustment, key cache,
process termination, whole-disk search, image-key scan, or network access.
"""
from __future__ import annotations

from datetime import datetime, time
from collections import deque
import os
from pathlib import Path
import sys

from .documents import MAX_MESSAGES
from .vendor.wexport.sources.win4 import Win4Source
from .vendor.wexport.parse import parse_message


def _local_path(value):
    if not isinstance(value, (str, Path)):
        return None
    text = str(value).strip().strip('"')
    if not text or text.casefold() == "mydocument:" or text.startswith(("\\\\", "//")) or "://" in text:
        return None
    return Path(os.path.expandvars(os.path.expanduser(text)))


def _windows_roots():
    """Current-user settings and fixed/removable local drive roots; no other users."""
    import winreg
    roots = []
    for key, value_name in (
        (r"Software\Microsoft\Windows\CurrentVersion\Explorer\User Shell Folders", "Personal"),
        (r"Software\Microsoft\Windows\CurrentVersion\Explorer\Shell Folders", "Personal"),
        (r"Software\Tencent\Weixin", "FileSavePath"),
        (r"Software\Tencent\WeChat", "FileSavePath"),
        (r"Software\Tencent\Weixin", "InstallPath"),
        (r"Software\Tencent\WeChat", "InstallPath"),
    ):
        try:
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, key) as handle:
                value, _ = winreg.QueryValueEx(handle, value_name)
            path = _local_path(value)
            if path is not None:
                roots.append(path)
                if value_name == "InstallPath":
                    roots.append(path.parent / "xwechat_files")
        except OSError:
            continue
    try:
        import ctypes
        mask = ctypes.windll.kernel32.GetLogicalDrives()
        for index in range(26):
            if mask & (1 << index):
                drive = chr(65 + index) + ":\\"
                if ctypes.windll.kernel32.GetDriveTypeW(drive) in (2, 3):
                    roots.append(Path(drive) / "xwechat_files")
                    roots.append(Path(drive) / "WeChat Files")
    except (OSError, AttributeError):
        pass
    return roots


def _accounts_at(value):
    """Normalize explicit account/db_storage paths and known one-level containers."""
    base = _local_path(value)
    if base is None:
        return []
    try:
        if base.is_file():
            base = base.parent
        for parent in (base, *list(base.parents)[:6]):
            if parent.name.casefold() == "db_storage":
                base = parent.parent
                break
        containers = (base, base / "xwechat_files", base / "WeChat Files",
                      base / "WeChat Files" / "xwechat_files", base / "Documents" / "xwechat_files",
                      base / "Data" / "Documents" / "xwechat_files")
        accounts, checked = [], set()
        for directory in containers:
            if directory in checked or directory.is_symlink() or not directory.is_dir():
                continue
            checked.add(directory)
            if (directory / "db_storage").is_dir():
                account = Win4Source.discover(directory)
                if account:
                    accounts.append(account)
            elif directory.name.casefold() in ("xwechat_files", "wechat files"):
                # Never recurse through the drive, user tree, or arbitrary containers.
                try:
                    children = list(directory.iterdir())[:500]
                except OSError:
                    continue
                for child in children:
                    if child.is_dir() and not child.is_symlink() and (child / "db_storage").is_dir():
                        account = Win4Source.discover(child)
                        if account:
                            accounts.append(account)
        return accounts
    except OSError:
        return []


def discover_accounts(explicit: Path | None = None):
    """Discover bounded current-user locations, redirected settings and local drive roots."""
    bases = [explicit] if explicit is not None else [Path.home(), Path.home() / "Documents"]
    if explicit is None and sys.platform == "win32":
        bases.extend(_windows_roots())
    accounts, seen = [], set()
    for base in bases:
        for account in _accounts_at(base):
            identity = os.path.normcase(str(account.root.resolve()))
            if identity not in seen:
                accounts.append(account)
                seen.add(identity)
    if explicit is not None and not accounts:
        raise ValueError("未找到微信4.x账号，请选择 xwechat_files、账号目录或 db_storage。")
    return accounts


def own_wechat_pids(process_api=None):
    """Restrict the existing read-only extractor to the invoking Windows user."""
    if process_api is None:
        import psutil as process_api
    owner = process_api.Process().username()
    if not owner or owner.rsplit("\\", 1)[-1].casefold() in ("system", "local service", "network service"):
        raise ValueError("请在本人已登录的Windows桌面直接运行，不使用SYSTEM任务或服务。")
    found = []
    for process in process_api.process_iter(["pid", "name", "username"]):
        info = process.info
        if ((info.get("name") or "").casefold() in {"weixin.exe", "wechat.exe"}
                and (info.get("username") or "").casefold() == owner.casefold()):
            found.append(info["pid"])
    return found


def open_account(account):
    if sys.platform != "win32":
        raise ValueError("本人已登录微信自动提取目前仅支持Windows微信4.x。")
    pids = own_wechat_pids()
    if not pids:
        raise ValueError("未找到本人运行的微信，请登录Windows微信4.x后重试。")
    # Use the original extractor unchanged, with an explicit current-user PID set.
    from .vendor.wexport.win.wcdb_keys import extract_keys
    keys = extract_keys(account.root, pids=pids)
    if not keys:
        raise ValueError("未提取到当前账号密钥。请在本人微信中打开一段聊天后重试；若被安全软件阻止，请保留检测信息。")
    source = Win4Source(account, keys)
    try:
        source.open()
    except BaseException:
        source.close()
        source._keys.clear()
        raise
    finally:
        keys.clear()
    return source


def _date(text, end=False):
    if not text:
        return None
    day = datetime.strptime(str(text), "%Y-%m-%d").date()
    return int(datetime.combine(day, time(23, 59, 59) if end else time.min).timestamp())


class Engine:
    def __init__(self, accounts, opener=open_account, discoverer=None):
        self.accounts, self.opener = list(accounts), opener
        self.discoverer = discoverer or discover_accounts
        self.source = None
        self.account_index = None
        self.moments = None
        self.events = deque(maxlen=500)
        self.event("info", "程序已启动，等待选择账号。")

    def event(self, level, message):
        self.events.append({"time": datetime.now().isoformat(sep=" ", timespec="seconds"),
                            "level": level, "message": message})

    def account_list(self):
        return [{"id": i, "name": a.label} for i, a in enumerate(self.accounts)]

    def refresh_accounts(self):
        discovered = self.discoverer()
        seen = {os.path.normcase(str(a.root.resolve())) for a in self.accounts if a.root is not None}
        added = 0
        for account in discovered:
            identity = os.path.normcase(str(account.root.resolve()))
            if identity not in seen:
                self.accounts.append(account)
                seen.add(identity)
                added += 1
        self.event("info", "账号检测完成，发现可用账号。" if self.accounts else "账号检测完成，未发现账号，可手动指定微信数据目录。")
        return {"accounts": self.account_list(), "added": added, "detected": len(discovered)}

    def add_account_directory(self, path):
        if not isinstance(path, str) or not path.strip() or len(path) > 4096:
            raise ValueError("请输入微信数据目录。")
        root = _local_path(path)
        if root is None or not root.is_absolute():
            raise ValueError("请填写本地磁盘上的完整微信数据目录，不使用网络共享地址。")
        candidates = _accounts_at(root)
        if any(p.is_symlink() for p in (root, *root.parents)):
            raise ValueError("请使用实际微信数据目录，不使用目录链接。")
        added = 0
        for account in candidates:
            if all(a.root is None or a.root.resolve() != account.root.resolve() for a in self.accounts):
                self.accounts.append(account)
                added += 1
        if not added:
            raise ValueError("未发现新的微信4.x账号，请选择 xwechat_files、账号目录或 db_storage。")
        self.event("info", "已添加微信数据目录。")
        return {"added": added, "accounts": self.account_list()}

    def open(self, index, consent):
        if consent is not True:
            raise ValueError("请先确认仅处理本人有权访问的微信数据。")
        if type(index) is not int or not 0 <= index < len(self.accounts):
            raise ValueError("请选择列表中的账号。")
        # Close old decrypted data before beginning a new extraction attempt.
        self.close()
        self.event("info", "开始提取并读取账号数据库。")
        try:
            self.source = self.opener(self.accounts[index])
        except Exception:
            self.event("error", "提取未完成，请检查微信登录状态与数据目录后重试。")
            raise
        self.account_index = index
        from .sns_live import Moments
        self.moments = Moments(self.source)
        self.event("warn" if self.source.load_errors else "info", "部分数据库未能读取，已打开可用数据。" if self.source.load_errors else "账号数据已打开。")
        return {"opened": True, "warnings": len(self.source.load_errors)}

    def close(self):
        if self.moments is not None:
            self.moments.close()
            self.moments = None
        if self.source is not None:
            try:
                self.source.close()
            finally:
                if hasattr(self.source, "_keys"):
                    self.source._keys.clear()
                self.source = None
                self.account_index = None
                self.event("info", "当前账号数据已关闭。")

    def chats(self):
        if self.source is None:
            raise ValueError("请先选择账号并提取。")
        return [{"id": c.username, "name": c.display_name or c.username,
                 "kind": "group" if c.is_group else ("official" if c.username.startswith("gh_") else "chat"),
                 "lastTime": c.last_time, "count": c.message_count}
                for c in self.source.conversations()]

    def document(self, chat, query="", since="", until="", kind=""):
        chats = {c["id"]: c["name"] for c in self.chats()}
        if not isinstance(chat, str) or chat not in chats:
            raise ValueError("请选择已提取账号中的会话。")
        if not isinstance(query, str) or len(query) > 500:
            raise ValueError("搜索关键词过长。")
        if kind not in ("", "text", "image", "video", "voice", "sticker", "link", "record", "system", "other"):
            raise ValueError("消息类型不正确。")
        start, end = _date(since), _date(until, True)
        if start is not None and end is not None and start > end:
            raise ValueError("开始日期不能晚于结束日期。")
        rows = []
        for m in self.source.iter_messages(chat, start, end):
            if "<!DOCTYPE" not in m.raw.upper() and "<!ENTITY" not in m.raw.upper():
                parse_message(m, media_root=None)
            else:
                m.text = m.raw
            text = m.text or ""
            if m.quoted:
                text += "\n引用 " + m.quoted_sender + "：" + m.quoted
            for item in m.extra.get("merge_items", []):
                text += "\n" + " · ".join(str(item.get(k) or "") for k in ("sender", "title", "desc"))
            try:
                timestamp = datetime.fromtimestamp(m.timestamp).isoformat(sep=" ")
            except (OSError, OverflowError, ValueError):
                timestamp = str(m.timestamp)
            record = {"createTime": timestamp, "senderDisplayName": m.sender_name or m.sender or "未知",
                      "typeName": m.subtype or "other", "content": text}
            category = {"file": "link", "app": "link", "emoji": "sticker", "merge": "record"}.get(record["typeName"], record["typeName"])
            if kind and category != kind:
                continue
            if query.casefold() not in " ".join(record.values()).casefold():
                continue
            record["isSender"] = bool(m.is_sender)
            rows.append(record)
            if len(rows) > MAX_MESSAGES:
                raise ValueError("匹配消息超过十万条，请选择更小的日期范围；本次未导出。")
        rows.sort(key=lambda m: m["createTime"])
        for index, record in enumerate(rows):
            record["id"] = index
        return {"session": {"displayName": chats[chat]}, "messages": rows}
