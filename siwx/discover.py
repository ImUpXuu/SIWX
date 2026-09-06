"""全自动目录发现：跨平台扫描 xwechat_files/*/db_storage。"""
import os
import platform
import string
from pathlib import Path

import psutil

WECHAT_PROCESSES_WIN = ("weixin.exe", "wechat.exe")
WECHAT_PROCESSES_MAC = ("WeChat",)


def wxid_of(db_dir) -> str:
    p = Path(db_dir)
    return p.parent.name or "unknown"


def find_wechat_pids():
    """运行中的微信进程（按内存占用降序，主进程优先）。"""
    system = platform.system()
    if system == "Windows":
        target_names = WECHAT_PROCESSES_WIN
    elif system == "Darwin":
        target_names = WECHAT_PROCESSES_MAC
    else:
        return []

    out = []
    for proc in psutil.process_iter(["pid", "name", "memory_info"]):
        try:
            name = (proc.info["name"] or "").lower()
            if name in [n.lower() for n in target_names]:
                rss = proc.info["memory_info"].rss if proc.info["memory_info"] else 0
                out.append((rss, proc.info["pid"]))
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue
    out.sort(reverse=True)
    return [pid for _rss, pid in out]


def find_wechat_data_dirs():
    """跨平台自动扫描，返回 [(wxid, db_storage 路径)]。"""
    system = platform.system()
    roots = []

    if system == "Windows":
        # Windows: 搜索所有盘符
        up = os.environ.get("USERPROFILE", "")
        if up:
            roots.append(Path(up) / "Documents" / "xwechat_files")
            roots.append(Path(up) / "xwechat_files")
        for letter in string.ascii_uppercase:
            drive = Path(f"{letter}:\\")
            if drive.is_dir():
                roots.append(drive / "xwechat_files")
    elif system == "Darwin":
        # macOS: 标准路径
        home = Path.home()
        containers = home / "Library" / "Containers"
        # 微信数据可能在 Containers 下
        if containers.is_dir():
            for entry in containers.iterdir():
                if entry.is_dir() and "wechat" in entry.name.lower():
                    wx_dir = entry / "Data" / "Documents" / "xwechat_files"
                    if wx_dir.is_dir():
                        roots.append(wx_dir)
        # 也检查用户目录
        roots.append(home / "Documents" / "xwechat_files")
        roots.append(home / "xwechat_files")
    else:
        return []

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
