"""全自动目录发现：全盘符 + 用户目录扫描 xwechat_files/*/db_storage。"""
import os
import string
from pathlib import Path

import psutil

WECHAT_PROCESSES = ("weixin.exe", "wechat.exe")


def wxid_of(db_dir) -> str:
    p = Path(db_dir)
    return p.parent.name or "unknown"


def find_wechat_pids():
    """运行中的微信进程（按内存占用降序，主进程优先）。"""
    out = []
    for proc in psutil.process_iter(["pid", "name", "memory_info"]):
        try:
            name = (proc.info["name"] or "").lower()
            if name in WECHAT_PROCESSES:
                rss = proc.info["memory_info"].rss if proc.info["memory_info"] else 0
                out.append((rss, proc.info["pid"]))
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue
    out.sort(reverse=True)
    return [pid for _rss, pid in out]


def find_wechat_data_dirs():
    """全盘自动扫描，返回 [(wxid, db_storage 路径)]。"""
    roots = []
    up = os.environ.get("USERPROFILE", "")
    if up:
        roots.append(Path(up) / "Documents" / "xwechat_files")
        roots.append(Path(up) / "xwechat_files")
    for letter in string.ascii_uppercase:
        drive = Path(f"{letter}:\\")
        if drive.is_dir():
            roots.append(drive / "xwechat_files")

    out, seen = [], set()
    for root in roots:
        if not root.is_dir():
            continue
        try:
            for entry in root.iterdir():
                db = entry / "db_storage"
                if db.is_dir() and db not in seen:
                    seen.add(db)
                    out.append((entry.name, str(db)))
        except OSError:
            continue
    out.sort()
    return out
