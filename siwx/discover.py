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
        up = os.environ.get("USERPROFILE", "")
        if up:
            roots.append(Path(up) / "Documents" / "xwechat_files")
            roots.append(Path(up) / "xwechat_files")
        # 搜索所有盘符
        for letter in string.ascii_uppercase:
            drive = Path(f"{letter}:\\")
            if drive.is_dir():
                roots.append(drive / "xwechat_files")
                # 也搜索 Documents 目录
                docs = drive / "Users"
                if docs.is_dir():
                    try:
                        for user_dir in docs.iterdir():
                            if user_dir.is_dir():
                                roots.append(user_dir / "Documents" / "xwechat_files")
                                roots.append(user_dir / "xwechat_files")
                    except OSError:
                        pass
    elif system == "Darwin":
        home = Path.home()
        containers = home / "Library" / "Containers"
        if containers.is_dir():
            for entry in containers.iterdir():
                if entry.is_dir() and "wechat" in entry.name.lower():
                    wx_dir = entry / "Data" / "Documents" / "xwechat_files"
                    if wx_dir.is_dir():
                        roots.append(wx_dir)
        roots.append(home / "Documents" / "xwechat_files")
        roots.append(home / "xwechat_files")
        # 也搜索 /Users 下其他用户
        users_dir = Path("/Users")
        if users_dir.is_dir():
            try:
                for user_dir in users_dir.iterdir():
                    if user_dir.is_dir() and user_dir != home:
                        roots.append(user_dir / "Documents" / "xwechat_files")
                        roots.append(user_dir / "xwechat_files")
            except OSError:
                pass
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


def validate_db_path(path_str: str) -> dict:
    """验证手动输入的路径是否有效。

    返回 {"ok": bool, "wxid": str, "db_dir": str, "error": str}
    """
    p = Path(path_str.strip().strip('"').strip("'"))
    if not p.is_dir():
        return {"ok": False, "error": f"目录不存在: {p}"}
    # 检查是否是 db_storage 目录
    if p.name == "db_storage":
        return {"ok": True, "wxid": p.parent.name, "db_dir": str(p)}
    # 检查目录下是否有 db_storage
    db_storage = p / "db_storage"
    if db_storage.is_dir():
        return {"ok": True, "wxid": p.name, "db_dir": str(db_storage)}
    # 检查目录下是否有 .db 文件（可能是 message 等子目录）
    dbs = list(p.glob("*.db"))
    if dbs:
        return {"ok": True, "wxid": p.parent.name, "db_dir": str(p)}
    return {"ok": False, "error": "未找到 db_storage 子目录或 .db 文件，请确认路径"}


def find_wechat_storage_in_wechat() -> str:
    """尝试从微信进程中获取存储路径（通过进程命令行或内存）。"""
    # 微信 4.x 默认路径通常在注册表中
    # Windows: HKEY_CURRENT_USER\Software\Tencent\WeChat
    system = platform.system()
    if system == "Windows":
        try:
            import winreg
            key = winreg.OpenKey(winreg.HKEY_CURRENT_USER,
                                 r"Software\Tencent\WeChat")
            install_path, _ = winreg.QueryValueEx(key, "InstallPath")
            winreg.CloseKey(key)
            if install_path:
                return str(Path(install_path).parent / "xwechat_files")
        except Exception:
            pass
    return ""
