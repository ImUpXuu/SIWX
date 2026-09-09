"""自动更新 —— 版本检查 + 平台检测 + 增量更新。

流程:
1. 启动时异步拉取远程 version.json（raw.gh.1s.fan 代理）
2. 比对本地版本 → 有新版本则提示
3. 用户确认 → 下载对应平台的更新脚本并执行
4. 脚本负责：杀旧进程 → 下载新产物 → 校验 SHA256 → 替换 → 重启

源码运行（非 frozen）→ 跳过检查，提示用户 git pull。
"""
import json
import os
import platform
import subprocess
import sys
import threading
import time
from pathlib import Path

from siwx import paths

RAW_BASE = "https://raw.gh.1s.fan/ImUpXuu/SIWX/main"
VERSION_URL = f"{RAW_BASE}/version.json"
TIMEOUT = 10


def current_version() -> str:
    """获取当前版本号。唯一来源: version.json (CI/CD 自动生成)。"""
    # 打包产物中 version.json 在 app_root()
    # 源码运行中 version.json 在项目根目录
    for base in (paths.app_root(), Path(__file__).resolve().parent.parent):
        p = base / "version.json"
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
            v = data.get("version", "")
            if v:
                return v
        except Exception:
            continue
    return "0.0.0"


def is_frozen() -> bool:
    """是否 PyInstaller 打包产物。"""
    return getattr(sys, "frozen", False)


def system_platform() -> str:
    """返回 'windows' / 'macos' / 'other'。"""
    s = platform.system().lower()
    if s == "windows":
        return "windows"
    if s == "darwin":
        return "macos"
    return "other"


def fetch_remote_version() -> dict | None:
    """拉取远程 version.json。失败返回 None。"""
    try:
        import urllib.request
        req = urllib.request.Request(VERSION_URL, headers={"User-Agent": "stories-in-wx"})
        with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
            return json.loads(r.read().decode("utf-8"))
    except Exception:
        return None


def has_update() -> tuple[bool, dict | None, str]:
    """检查是否有新版本。

    返回: (是否有更新, 远程 version.json, 当前版本)
    """
    if not is_frozen():
        return False, None, current_version()

    remote = fetch_remote_version()
    if not remote:
        return False, None, current_version()

    cur = current_version()
    new = remote.get("version", "0.0.0")

    try:
        cur_parts = [int(x) for x in cur.split(".")]
        new_parts = [int(x) for x in new.split(".")]
        newer = new_parts > cur_parts
    except (ValueError, AttributeError):
        newer = new != cur

    return newer, remote, cur


def _download_file(url: str, dest: Path) -> bool:
    """下载文件到 dest。"""
    try:
        import urllib.request
        req = urllib.request.Request(url, headers={"User-Agent": "stories-in-wx"})
        with urllib.request.urlopen(req, timeout=120) as r, open(dest, "wb") as f:
            while True:
                chunk = r.read(65536)
                if not chunk:
                    break
                f.write(chunk)
        return True
    except Exception:
        return False


def _verify_sha256(file_path: Path, expected_sha: str) -> bool:
    """校验文件 SHA-256。"""
    if not expected_sha:
        return True
    try:
        import hashlib
        h = hashlib.sha256()
        with open(file_path, "rb") as f:
            while True:
                chunk = f.read(65536)
                if not chunk:
                    break
                h.update(chunk)
        return h.hexdigest().lower() == expected_sha.strip().lower()
    except Exception:
        return False


def _get_asset_sha(remote: dict, platform_key: str) -> str:
    """从远程 version.json 获取指定平台产物的 SHA-256。"""
    sha_url = remote.get("sha256", "")
    asset_name = {
        "windows": f"stories-in-wx-v{remote.get('version','')}-windows-x64.exe",
        "macos": f"stories-in-wx-v{remote.get('version','')}-macos.dmg",
    }.get(platform_key, "")
    if not sha_url or not asset_name:
        return ""
    try:
        import urllib.request
        req = urllib.request.Request(sha_url, headers={"User-Agent": "stories-in-wx"})
        with urllib.request.urlopen(req, timeout=30) as r:
            for line in r.read().decode("utf-8").splitlines():
                if asset_name in line:
                    return line.split()[0]
    except Exception:
        pass
    return ""


def run_update(remote: dict, progress=None) -> dict:
    """执行更新。返回 {"ok": bool, "message": str}。"""
    if not is_frozen():
        return {"ok": False, "message": "源码运行模式，请手动 git pull 更新"}

    plat = system_platform()
    if plat not in ("windows", "macos"):
        return {"ok": False, "message": f"不支持的平台: {plat}"}

    ver = remote.get("version", "")
    asset_url = remote.get("assets", {}).get(f"{plat}_dmg" if plat == "macos" else "windows", "")
    if not asset_url:
        return {"ok": False, "message": f"未找到 {plat} 平台的下载链接"}

    # 下载到临时目录
    tmp_dir = Path(os.environ.get("TEMP", "/tmp")) / "siwx_update"
    tmp_dir.mkdir(parents=True, exist_ok=True)
    fname = asset_url.split("/")[-1]
    dest = tmp_dir / fname

    if progress:
        progress(10, f"下载 v{ver}...")
    if not _download_file(asset_url, dest):
        return {"ok": False, "message": "下载失败，请检查网络"}

    # 校验
    if progress:
        progress(50, "校验 SHA-256...")
    expected_sha = _get_asset_sha(remote, plat)
    if expected_sha and not _verify_sha256(dest, expected_sha):
        dest.unlink(missing_ok=True)
        return {"ok": False, "message": "SHA-256 校验失败，文件可能损坏"}

    # 执行平台更新脚本
    if progress:
            progress(80, "执行更新脚本...")
    script_dir = paths.app_root() / "scripts"
    if plat == "windows":
        script = script_dir / "update_win.bat"
        if script.exists():
            subprocess.Popen(["cmd", "/c", str(str(script))], cwd=str(paths.app_root()))
            return {"ok": True, "message": "更新脚本已启动，应用将自动重启"}
        # 无脚本：直接替换 exe
        return _replace_windows_exe(dest, ver)
    else:
        script = script_dir / "update_mac.sh"
        if script.exists():
            script.chmod(0o755)
            subprocess.Popen(["bash", str(script)], cwd=str(paths.app_root()))
            return {"ok": True, "message": "更新脚本已启动，应用将自动重启"}
        return {"ok": False, "message": "未找到更新脚本"}


def _replace_windows_exe(new_exe: Path, ver: str) -> dict:
    """直接替换 Windows exe（无脚本兜底）。"""
    app_dir = paths.app_root()
    old_exe = app_dir / "stories-in-wx.exe"
    backup = app_dir / "stories-in-wx.backup.exe"
    try:
        # 杀旧进程
        subprocess.run(["taskkill", "/f", "/im", "stories-in-wx*.exe", "/t"],
                        capture_output=True, timeout=10)
        time.sleep(2)
        # 备份
        if old_exe.exists():
            old_exe.replace(backup)
        # 替换
        new_exe.replace(old_exe)
        # 启动
        subprocess.Popen([str(old_exe), "serve"], cwd=str(app_dir))
        return {"ok": True, "message": f"已更新到 v{ver} 并重启"}
    except Exception as e:
        return {"ok": False, "message": f"替换失败: {e}"}


# ── 后台检查 ───────────────────────────────────────────────────

def check_in_background(callback):
    """后台线程检查更新。callback(has_update, remote, current)。"""

    def _check():
        has, remote, cur = has_update()
        callback(has, remote, cur)

    t = threading.Thread(target=_check, daemon=True)
    t.start()
