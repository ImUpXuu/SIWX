"""全自动目录发现：跨平台扫描 xwechat_files/*/db_storage。"""
import json
import os
import platform
import string
from pathlib import Path

try:
    import psutil
except ImportError:  # 进程发现降级（见 find_wechat_pids）
    psutil = None

from siwx import logger as _slog
from siwx import paths as _paths

WECHAT_PROCESSES_WIN = ("weixin.exe", "wechat.exe")
WECHAT_PROCESSES_MAC = ("WeChat",)
WECHAT_PROCESSES_LINUX = ("wechat", "weixin", "wechat-bin")

# 缺 psutil 的告警只打一次：find_wechat_pids 会被策略链/轮询高频调用，
# 每次打会刷满日志（同 `_MIGRATION_WARNED` 的套路）。
_PSUTIL_WARNED = False


def manual_paths_file() -> Path:
    """手动指定的微信 db_storage 路径配置文件。

    放在 `data_dir()` 而非 `app_root()`：文件内容是真实绝对路径与账号目录名，
    历史上写在仓库根/安装目录，既会污染仓库（曾未进 .gitignore），也会在
    升级/卸载时丢失或泄露。旧位置由 `load_manual_data_dirs()` 首次读取时迁移。
    """
    return _paths.data_dir() / "wechat_paths.json"


def _legacy_manual_paths_file() -> Path:
    """历史位置：仓库根 / 安装目录（app_root）。只用于迁移与兜底读取。"""
    return _paths.app_root() / "wechat_paths.json"


def _same_file(a: Path, b: Path) -> bool:
    try:
        return a.resolve() == b.resolve()
    except OSError:
        return str(a) == str(b)


_MIGRATION_WARNED = set()     # load_manual_data_dirs 在媒体热路径上高频调用


def _warn_once(key: str, msg: str) -> None:
    """迁移类告警每进程每条只记一次，避免每张图解密都刷一行 WARN。"""
    if key in _MIGRATION_WARNED:
        return
    _MIGRATION_WARNED.add(key)
    _slog.warn("discover", msg)


def _migrate_legacy_manual_paths(dest: Path) -> None:
    """把旧位置的 wechat_paths.json 原子迁移到数据目录。

    - 目标已存在：以数据目录为准，旧文件不动（只告警提示残留，可手动删除）；
    - 目标不存在而旧文件存在：同目录 `.tmp` + replace 复制（权限尽量 0600），
      成功后才删除旧文件；
    - 复制失败（权限/跨盘/占用）：保留旧文件并告警，调用方回退读旧文件；
    - 删除旧文件失败：只告警（Windows 上被杀软/索引占用常见）。
    """
    legacy = _legacy_manual_paths_file()
    if _same_file(legacy, dest):      # SIWX_DATA_DIR 恰好指到安装目录
        return
    try:
        if not legacy.is_file():
            return
        if dest.exists():
            _warn_once(f"residual:{dest}",
                       f"数据目录已有 {dest.name}，旧位置仍有同名残留文件，请手动删除")
            return
        dest.parent.mkdir(parents=True, exist_ok=True)
        tmp = dest.with_suffix(dest.suffix + ".tmp")
        tmp.write_bytes(legacy.read_bytes())
        try:
            tmp.chmod(0o600)          # 含真实绝对路径/账号目录名
        except OSError:
            pass
        tmp.replace(dest)
    except OSError as e:
        _warn_once(f"migrate:{dest}",
                   f"迁移旧 {dest.name} 失败，继续读取旧位置: "
                   f"{type(e).__name__}: {e}")
        try:
            dest.with_suffix(dest.suffix + ".tmp").unlink(missing_ok=True)
        except OSError:
            pass
        return
    try:
        legacy.unlink()
        _slog.info("discover", f"{dest.name} 已迁移到应用数据目录")
    except OSError as e:
        _warn_once(f"unlink:{legacy}",
                   f"旧 {dest.name} 删除失败，请手动删除: {type(e).__name__}: {e}")


def load_manual_data_dirs() -> list:
    """读取用户手动保存的微信数据目录，返回 [(wxid, db_storage 路径)]。"""
    p = manual_paths_file()
    _migrate_legacy_manual_paths(p)
    if not p.is_file():
        # 迁移失败时继续读旧文件兜底，避免用户已配置的目录凭空消失。
        legacy = _legacy_manual_paths_file()
        if not _same_file(legacy, p) and legacy.is_file():
            p = legacy
        else:
            return []
    try:
        raw = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as e:
        # 审计 §3.6：配置损坏静默返回空 → 引导页直接回空，无从排查
        _slog.warn("discover", f"手动路径配置损坏 {p.name}: {e}")
        return []
    paths = raw.get("paths", raw if isinstance(raw, list) else [])
    out, seen = [], set()
    for item in paths:
        r = validate_db_path(str(item or ""))
        if not r.get("ok"):
            _slog.detailed("discover", f"手动路径被拒: {item} ({r.get('error', '')})")
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
    """运行中的微信进程（按内存占用降序，主进程优先）。

    psutil 缺失时返回空列表并告警一次——语义等同于「微信未运行」，下游
    （strategies/memscan、config_cipher）本就有「未检测到微信进程，跳过」
    分支，不会崩；但密钥提取会因此静默失效，必须把真实原因留在日志里，
    否则用户只会看到「请先启动并登录微信」的误导提示。
    """
    global _PSUTIL_WARNED
    if psutil is None:
        if not _PSUTIL_WARNED:
            _PSUTIL_WARNED = True
            _slog.warn("discover", "未安装 psutil，无法检测微信进程（密钥提取/"
                                   "自动退出会失效）；请执行 "
                                   "python -m pip install -r requirements.txt")
        return []
    system = platform.system()
    if system == "Windows":
        target_names = WECHAT_PROCESSES_WIN
    elif system == "Darwin":
        target_names = WECHAT_PROCESSES_MAC
    elif system == "Linux":
        target_names = WECHAT_PROCESSES_LINUX
    else:
        return []

    out = []
    for proc in psutil.process_iter(["pid", "name", "memory_info"]):
        try:
            name = (proc.info["name"] or "").lower()
            if name in [n.lower() for n in target_names]:
                rss = proc.info["memory_info"].rss if proc.info["memory_info"] else 0
                out.append((rss, proc.info["pid"]))
        except psutil.NoSuchProcess:
            continue  # 正常竞态，不记（审计 §7.6 不埋点清单）
        except psutil.AccessDenied:
            _slog.detailed("discover", f"进程枚举被拒 pid={proc.info.get('pid')}")
            continue
    out.sort(reverse=True)
    pids = [pid for _rss, pid in out]
    _slog.detailed("discover", f"微信进程: {pids}")
    return pids


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
                            try:
                                if user_dir.is_dir():
                                    roots.append(user_dir / "Documents" / "xwechat_files")
                                    roots.append(user_dir / "xwechat_files")
                            except OSError:
                                continue
                    except OSError as e:
                        _slog.detailed("discover", f"枚举 {users_dir} 失败: {e}")
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
                    # 其他用户的 ~/Documents 受系统保护，is_dir() 会抛
                    # PermissionError（EACCES 不在 pathlib 忽略的错误码内），
                    # 必须逐个兜住，否则一个不可读用户就中断其余扫描。
                    try:
                        if user_dir.is_dir() and user_dir != home:
                            roots.append(user_dir / "Documents" / "xwechat_files")
                            roots.append(user_dir / "xwechat_files")
                    except OSError:
                        continue
            except OSError as e:
                _slog.detailed("discover", f"枚举 {users_dir} 失败: {e}")
    elif system == "Linux":
        home = Path.home()
        roots.append(home / "Documents" / "xwechat_files")
        roots.append(home / "xwechat_files")
        # 桌面版把文档目录本地化（中文系统是 ~/文档），读 XDG 配置兜住
        xdg = home / ".config" / "user-dirs.dirs"
        try:
            for line in xdg.read_text(errors="replace").splitlines():
                if line.startswith("XDG_DOCUMENTS_DIR="):
                    val = line.split("=", 1)[1].strip().strip('"')
                    roots.append(Path(val.replace("$HOME", str(home))) / "xwechat_files")
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
        # macOS 上其他用户的 ~/Documents 受系统保护，is_dir() 会抛 PermissionError
        # (EACCES 不在 pathlib 忽略的错误码内)，必须一并捕获，否则整个扫描中断。
        try:
            if not root.is_dir():
                continue
            for entry in root.iterdir():
                db = entry / "db_storage"
                if db.is_dir():
                    add(entry.name, db)
        except OSError as e:
            # 枚举失败静默削减候选集不可见（审计 §3.6:195-196）
            _slog.detailed("discover", f"枚举根目录 {root} 失败: {e}")
            continue

    # 这行在每次扫描时都写：媒体解密每张图都会调 find_wechat_data_dirs()，
    # 实测 logs/siwx.log 13760 行里 5800 行（42%）都是这条扫描噪音，把日志页
    # 那个 5000 行的环形缓冲反复冲干净。降级为不写 UI 环的详细轨。
    _slog.detailed("discover", f"扫描 {len(roots)} 个根, 命中 {len(out)} 账号",
                   ring=False)

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
        except Exception as e:
            _slog.detailed("discover", f"注册表读取失败: {type(e).__name__}: {e}")
    return ""


# 向后兼容旧命名。
def find_wechat_storage_in_wechat() -> str:
    return find_wechat_storage_in_registry()
