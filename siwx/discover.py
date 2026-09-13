"""全自动目录发现：跨平台扫描 xwechat_files/*/db_storage。"""
import json
import os
import platform
import string
from pathlib import Path

import psutil

from siwx import paths as _paths

WECHAT_PROCESSES_WIN = ("weixin.exe", "wechat.exe")
WECHAT_PROCESSES_MAC = ("WeChat",)


def manual_paths_file() -> Path:
    """手动指定的微信 db_storage 路径配置文件。"""
    return _paths.app_root() / "wechat_paths.json"


def load_manual_data_dirs() -> list:
    """读取用户手动保存的微信数据目录，返回 [(wxid, db_storage 路径)]。"""
    p = manual_paths_file()
    if not p.is_file():
        return []
    try:
        raw = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []
    paths = raw.get("paths", raw if isinstance(raw, list) else [])
    out, seen = [], set()
    for item in paths:
        r = validate_db_path(str(item or ""))
        if not r.get("ok"):
            continue
        key = str(Path(r["db_dir"]).resolve()).casefold()
        if key in seen:
            continue
        seen.add(key)
        out.append((r["wxid"], r["db_dir"]))
    return out


def save_manual_data_dirs(db_dirs: list) -> None:
    """保存已验证的手动目录，自动去重并保留可用项。"""
    valid, seen = [], set()
    for item in db_dirs or []:
        r = validate_db_path(str(item or ""))
        if not r.get("ok"):
            continue
        db = str(Path(r["db_dir"]).resolve())
        key = db.casefold()
        if key in seen:
            continue
        seen.add(key)
        valid.append(db)
    p = manual_paths_file()
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(p.suffix + ".tmp")
    tmp.write_text(json.dumps({"paths": valid}, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(p)


def add_manual_data_dir(path_str: str) -> dict:
    """验证并持久化一个手动微信目录。"""
    r = validate_db_path(path_str)
    if not r.get("ok"):
        return r
    existing = [db for _wxid, db in load_manual_data_dirs()]
    existing.append(r["db_dir"])
    save_manual_data_dirs(existing)
    r["saved"] = True
    return r


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
        # 搜索所有盘符与多用户目录
        for letter in string.ascii_uppercase:
            drive = Path(f"{letter}:\\")
            if drive.is_dir():
                roots.append(drive / "xwechat_files")
                users_dir = drive / "Users"
                if users_dir.is_dir():
                    try:
                        for user_dir in users_dir.iterdir():
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
        # 搜索 /Users 下其他用户
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
        roots = []

    out, seen = [], set()

    def add(wxid, db):
        try:
            key = str(Path(db).resolve()).casefold()
        except OSError:
            key = str(db).casefold()
        if key in seen:
            return
        seen.add(key)
        out.append((wxid, str(db)))

    for root in roots:
        if not root.is_dir():
            continue
        try:
            for entry in root.iterdir():
                db = entry / "db_storage"
                if db.is_dir():
                    add(entry.name, db)
        except OSError:
            continue

    # 用户手动指定的目录必须参与后续状态页、解密任务与媒体查找；否则自动
    # 扫描没命中时，引导页永远无法进入自定义路径流程。
    for wxid, db in load_manual_data_dirs():
        add(wxid, db)

    out.sort()
    return out


def validate_db_path(path_str: str) -> dict:
    """验证手动输入的路径是否有效。

    返回 {"ok": bool, "wxid": str, "db_dir": str, "error": str}
    """
    p = Path(path_str.strip().strip('"').strip("'"))
    if not p.is_dir():
        return {"ok": False, "error": f"目录不存在: {p}"}
    try:
        p = p.resolve()
    except OSError:
        pass
    # 检查是否是 db_storage 目录
    if p.name == "db_storage":
        return {"ok": True, "wxid": p.parent.name, "db_dir": str(p)}
    # 检查目录下是否有 db_storage
    db_storage = p / "db_storage"
    if db_storage.is_dir():
        return {"ok": True, "wxid": p.name, "db_dir": str(db_storage.resolve())}
    # 检查目录下是否有 .db 文件（可能是 message 等子目录）
    dbs = list(p.glob("*.db"))
    if dbs:
        return {"ok": True, "wxid": p.parent.name, "db_dir": str(p)}
    return {"ok": False, "error": "未找到 db_storage 子目录或 .db 文件，请确认路径"}


def find_wechat_storage_in_registry() -> str:
    """尝试从 Windows 注册表获取微信安装路径。"""
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


# 向后兼容旧命名。
def find_wechat_storage_in_wechat() -> str:
    return find_wechat_storage_in_registry()
