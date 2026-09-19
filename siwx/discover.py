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
    """解析、验证并持久化手动路径；根目录可一次加入多个账号。"""
    r = validate_db_path(path_str)
    if not r.get("ok"):
        return r
    accounts = r.get("accounts") or [{"wxid": r["wxid"], "db_dir": r["db_dir"]}]
    existing = [db for _wxid, db in load_manual_data_dirs()]
    existing.extend(a["db_dir"] for a in accounts)
    save_manual_data_dirs(existing)
    r["saved"] = True
    return r


def wxid_of(db_dir) -> str:
    p = Path(db_dir)
    return p.parent.name or "unknown"


def find_account_conflicts(dirs=None) -> list:
    """检测同名账号（多个 db_dir 映射到同一输出目录）。

    `wxid_of()` 只取 db_dir 的父目录名，而 `find_wechat_data_dirs()` 会扫描
    所有盘符与所有用户目录 —— 同一个微信号在 C 盘和 D 盘各留一份
    xwechat_files 时（换过数据盘、迁移残留、备份副本），会产生两条 wxid
    相同、db_dir 不同的记录，但它们共用 `output/<wxid>/`：
    后跑的那份会覆盖先跑的产物。

    本函数只做**检测与告警**，不改变目录解析行为（零破坏性）。

    返回 [{"wxid": str, "dirs": [db_dir, ...]}]，无冲突时返回 []。
    """
    if dirs is None:
        dirs = find_wechat_data_dirs()
    grouped: dict = {}
    for wxid, db in dirs:
        grouped.setdefault(wxid, []).append(db)
    return [{"wxid": w, "dirs": d} for w, d in sorted(grouped.items())
            if len(d) > 1]


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


def _normalize_input_path(path_str: str) -> Path:
    """清理复制来的引号、环境变量和数据库文件路径。"""
    raw = str(path_str or "").strip().strip('"').strip("'").strip()
    raw = os.path.expandvars(os.path.expanduser(raw))
    p = Path(raw)
    if p.is_file():
        p = p.parent
    try:
        return p.resolve()
    except OSError:
        return p


def _accounts_under(root: Path) -> list:
    """识别 xwechat_files 根目录下的账号；只查一层，不做昂贵递归。"""
    out = []
    if not root.is_dir():
        return out
    try:
        children = list(root.iterdir())
    except OSError:
        return out
    for child in children:
        db = child / "db_storage"
        if child.is_dir() and db.is_dir():
            try:
                db = db.resolve()
            except OSError:
                pass
            out.append({"wxid": child.name, "db_dir": str(db)})
    return sorted(out, key=lambda x: (x["wxid"], x["db_dir"]))


def resolve_db_paths(path_str: str) -> list:
    """把常见微信路径形态统一解析成账号级 db_storage 目录。

    支持：db_storage 本身、账号目录、db_storage 内任意子目录或 .db 文件、
    xwechat_files 根目录，以及其上一级（直接包含 xwechat_files）。
    """
    p = _normalize_input_path(path_str)
    if not p.is_dir():
        return []

    # 输入位于 db_storage 内部（message 子目录、具体数据库文件等）。
    for node in (p, *p.parents):
        if node.name.casefold() == "db_storage" and node.is_dir():
            return [{"wxid": node.parent.name, "db_dir": str(node)}]

    # 输入账号目录，或账号目录内与 db_storage 同级的其他目录。
    for node in (p, *list(p.parents)[:3]):
        db = node / "db_storage"
        if db.is_dir():
            try:
                db = db.resolve()
            except OSError:
                pass
            return [{"wxid": node.name, "db_dir": str(db)}]

    # 输入 xwechat_files 根目录，或它的上一级/常见容器目录。
    candidates = [p, p / "xwechat_files", p / "Documents" / "xwechat_files",
                  p / "Data" / "Documents" / "xwechat_files"]
    seen = set()
    accounts = []
    for root in candidates:
        try:
            key = str(root.resolve()).casefold()
        except OSError:
            key = str(root).casefold()
        if key in seen:
            continue
        seen.add(key)
        accounts.extend(_accounts_under(root))
    unique = {}
    for a in accounts:
        unique[a["db_dir"].casefold()] = a
    return list(unique.values())


def validate_db_path(path_str: str) -> dict:
    """验证并解析手动路径；根目录可返回多个账号。"""
    p = _normalize_input_path(path_str)
    if not p.is_dir():
        return {"ok": False, "error": f"目录不存在: {p}"}
    accounts = resolve_db_paths(path_str)
    if not accounts:
        return {"ok": False, "error":
                "未识别到微信数据目录。可填写 xwechat_files、账号目录、db_storage、其内部子目录或 .db 文件"}
    first = accounts[0]
    return {"ok": True, "wxid": first["wxid"], "db_dir": first["db_dir"],
            "accounts": accounts, "account_count": len(accounts)}


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
