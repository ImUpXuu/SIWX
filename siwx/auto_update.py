"""自动更新 —— 版本检查 + 平台检测 + 安装替换。

流程:
1. 启动时异步拉取远程 version.json（raw.gh.1s.fan 代理）
2. 比对本地版本 → 有新版本则提示
3. 用户确认 → 下载对应平台产物到系统临时目录 → 校验 SHA-256 → 安装
4. 安装（纯 Python，双平台统一，不依赖打包进产物的外部脚本）:
   - Windows: shutil.copyfile 跨盘复制（os.replace 不能跨卷，WinError 17）
              → 覆盖旧文件（运行中的 exe 只能改名不能覆盖）→ 拉起新 exe
              → 旧进程延迟退出释放端口
   - macOS:   挂载 DMG → 整包换血 .app（旧包改名，失败回滚）→ open 重启

文件名约定: 产物带版本号（stories-in-wx-v5.0.5-windows-x64.exe），用户目录里
通常是按版本号一字排开的多个 exe。因此 Windows 的安装目标是「当前运行的
exe 换上新版本号」，而不是硬编码的 stories-in-wx.exe——后者在用户机器上
往往根本不存在。

源码运行（非 frozen）→ 跳过检查，提示用户 git pull。
"""
import json
import os
import platform
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

from siwx import logger as log
from siwx import paths
from siwx import validate

RAW_BASE = "https://raw.gh.1s.fan/ImUpXuu/SIWX/main"
GITHUB_RAW_BASE = "https://raw.githubusercontent.com/ImUpXuu/SIWX/main"
GITHUB_REPO = "ImUpXuu/SIWX"
VERSION_URLS = [f"{RAW_BASE}/version.json", f"{GITHUB_RAW_BASE}/version.json"]
TIMEOUT = 10
DOWNLOAD_TIMEOUT = 300

# 更新链域名白名单（审计 S1/S5）：version.json 提供的下载/哈希 URL 必须落在
# 这些域名内，否则跳过该候选（GitHub Release 直链兜底不受影响），防止
# manifest 把下载源指到任意域名。
_ALLOWED_UPDATE_HOSTS = {
    "github.com", "objects.githubusercontent.com",
    "raw.githubusercontent.com", "raw.gh.1s.fan",
}

# 只认域名时 github.com/attacker/repo 一样放行——白名单形同虚设。产物与哈希
# URL 还必须落在本仓库路径下，这样即使代理被投毒也无法把下载源指到别处。
_REPO_PATH_PREFIX = f"/{GITHUB_REPO}/releases/"

# version 字段会被拼进文件名 / 安装目标路径 / 正则替换模板，必须是严格语义
# 版本；否则 "9.9.9/../../.." 能逃出 tmp_dir/app_dir 并最终被 Popen 执行。
_VERSION_RE = re.compile(r"^\d+\.\d+\.\d+(?:[-.][0-9A-Za-z.]+){0,2}$")


def valid_version(v) -> bool:
    """version 字段是否为安全可用的语义版本号。"""
    return isinstance(v, str) and bool(_VERSION_RE.fullmatch(v))


def _host_allowed(url: str) -> bool:
    """URL 的 host 是否在更新链白名单内。"""
    try:
        from urllib.parse import urlsplit
        host = (urlsplit(url).hostname or "").lower()
    except ValueError:
        return False
    return host in _ALLOWED_UPDATE_HOSTS


def _manifest_url_allowed(url: str) -> bool:
    """manifest 提供的产物/哈希 URL：域名白名单 + 必须落在本仓库路径下。"""
    from urllib.parse import urlsplit
    try:
        u = urlsplit(url)
    except ValueError:
        return False
    host = (u.hostname or "").lower()
    if host not in _ALLOWED_UPDATE_HOSTS:
        return False
    if host == "github.com":
        return u.path.startswith(_REPO_PATH_PREFIX)
    if host in ("raw.githubusercontent.com", "raw.gh.1s.fan"):
        return u.path.startswith(f"/{GITHUB_REPO}/")
    # objects.githubusercontent.com 的路径由 GitHub 生成（Release 资源重定向），
    # 攻击者无法往该域上传内容，放行。
    return True


def current_version() -> str:
    """获取当前版本号；唯一源头是 siwx.__version__。"""
    from siwx import __version__
    return __version__


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
    """拉取远程 version.json，返回所有可用源中的最高版本。

    更新代理可能短时间缓存旧的 version.json，因此不能在第一个有效响应处
    直接返回。每个请求同时带上防缓存参数和请求头，并在可用结果中选择最高
    版本；这样代理仍为旧版时，也能采用 GitHub Raw 上已经发布的新版本。
    """
    import urllib.parse
    import urllib.request

    candidates = []
    cache_key = str(time.time_ns())
    for base_url in VERSION_URLS:
        try:
            parts = urllib.parse.urlsplit(base_url)
            query = urllib.parse.parse_qsl(parts.query, keep_blank_values=True)
            query.append(("_siwx_update", cache_key))
            url = urllib.parse.urlunsplit(parts._replace(
                query=urllib.parse.urlencode(query),
            ))
            req = urllib.request.Request(url, headers={
                "User-Agent": "stories-in-wx",
                "Cache-Control": "no-cache",
                "Pragma": "no-cache",
            })
            with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
                data = json.loads(r.read().decode("utf-8"))
                if isinstance(data, dict) and data.get("version"):
                    if not valid_version(data.get("version")):
                        log.warn("update", f"[check] {base_url} 的 version 字段格式非法，"
                                           f"已忽略该来源")
                        continue
                    if not _manifest_download_urls_ok(data):
                        log.warn("update", f"[check] {base_url} 的下载/哈希地址不在本仓库"
                                           f"路径下，已忽略该来源")
                        continue
                    candidates.append(data)
        except Exception as e:
            # 网络错误全部丢弃会让"无更新"与"检查失败"对前端不可区分
            log.detailed("update", f"[check] {base_url} 拉取失败: "
                                   f"{type(e).__name__}: {e}")
            continue

    if not candidates:
        return None
    return max(candidates, key=lambda item: _version_tuple(item.get("version", "")))


def _version_tuple(v: str) -> tuple:
    """宽松语义版本比较：v5.0.0 / 5.0.0-rc1 都能比较。"""
    nums = [int(x) for x in re.findall(r"\d+", str(v or ""))[:3]]
    while len(nums) < 3:
        nums.append(0)
    return tuple(nums)


def _manifest_download_urls_ok(remote: dict) -> bool:
    """manifest 声明的产物/哈希地址是否全部落在本仓库路径下。

    代理源（raw.gh.1s.fan）可能被投毒，且 fetch_remote_version 取的是「版本号
    最大者」——只投毒代理就能压过权威源。这里要求它的下载地址只能指向本仓库
    releases，投毒最多退化成下载失败（DoS），无法指向攻击者产物。
    """
    urls = [remote.get("sha256") or ""] + list((remote.get("assets") or {}).values())
    return all(_manifest_url_allowed(u) for u in urls if u)


def has_update() -> tuple[bool, dict | None, str]:
    """检查是否有新版本。

    返回: (是否有更新, 远程 version.json, 当前版本)
    """
    cur = current_version()
    remote = fetch_remote_version()
    if not remote:
        return False, None, cur

    new = remote.get("version", "0.0.0")
    newer = _version_tuple(new) > _version_tuple(cur)
    return newer, remote, cur


def _asset_name(remote: dict, plat: str) -> str:
    """指定平台的产物文件名（与 _get_asset_sha、SHA256SUMS.txt 保持一致）。"""
    ver = remote.get("version", "")
    if plat == "macos":
        return f"stories-in-wx-v{ver}-macos.dmg"
    return f"stories-in-wx-v{ver}-windows-x64.exe"


def _asset_urls(remote: dict, plat: str) -> list:
    """候选下载地址：version.json 提供的在前（域名白名单校验），GitHub Release
    直链兜底在后。"""
    ver = remote.get("version", "")
    urls = []
    key = "macos_dmg" if plat == "macos" else "windows"
    primary = (remote.get("assets") or {}).get(key, "")
    if primary and _manifest_url_allowed(primary):
        urls.append(primary)
    fallback = (f"https://github.com/{GITHUB_REPO}/releases/download/"
                f"v{ver}/{_asset_name(remote, plat)}")
    if fallback not in urls:
        urls.append(fallback)
    return urls


def _download_file(url: str, dest: Path, attempts: int = 2) -> bool:
    """下载文件到 dest，失败重试。"""
    import urllib.request
    for i in range(attempts):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "stories-in-wx"})
            with urllib.request.urlopen(req, timeout=DOWNLOAD_TIMEOUT) as r, \
                    open(dest, "wb") as f:
                while True:
                    chunk = r.read(1 << 16)
                    if not chunk:
                        break
                    f.write(chunk)
            return True
        except Exception as e:
            log.warn("update", f"[download] 第 {i + 1}/{attempts} 次下载失败: "
                               f"{type(e).__name__}: {e}")
            if dest.exists():
                dest.unlink(missing_ok=True)
            if i + 1 < attempts:
                time.sleep(1)
    return False


def _download_asset(remote: dict, plat: str, dest: Path) -> bool:
    """按候选地址逐个尝试下载产物。"""
    for url in _asset_urls(remote, plat):
        if _download_file(url, dest):
            return True
    return False


def _verify_sha256(file_path: Path, expected_sha: str) -> bool:
    """校验文件 SHA-256。

    fail-closed（审计 S1）：expected_sha 为空返回 False 而不是 True——
    静默放行等于校验形同虚设；调用方必须在拿到期望哈希后才允许安装。"""
    if not expected_sha:
        return False
    try:
        import hashlib
        h = hashlib.sha256()
        with open(file_path, "rb") as f:
            while True:
                chunk = f.read(1 << 16)
                if not chunk:
                    break
                h.update(chunk)
        return h.hexdigest().lower() == expected_sha.strip().lower()
    except Exception as e:
        # IO 错误（权限/磁盘/占用）≠ 文件损坏，勿让上层文案误导
        log.warn("update", f"[verify] 校验过程异常（IO 错误，非文件损坏）: "
                           f"{type(e).__name__}: {e}")
        return False


def _get_asset_sha(remote: dict, platform_key: str) -> str:
    """从远程 version.json 获取指定平台产物的 SHA-256。

    哈希 URL 同样受域名白名单约束（审计 S1/S5）：哈希源不在白名单内时
    返回空串，由 run_update 拒绝安装，而不是静默跳过校验。"""
    sha_url = remote.get("sha256", "")
    asset_name = _asset_name(remote, platform_key)
    if not sha_url or not asset_name:
        return ""
    if not _manifest_url_allowed(sha_url):
        return ""
    try:
        import urllib.request
        req = urllib.request.Request(sha_url, headers={"User-Agent": "stories-in-wx"})
        with urllib.request.urlopen(req, timeout=30) as r:
            for line in r.read().decode("utf-8").splitlines():
                if asset_name in line:
                    return line.split()[0]
    except Exception as e:
        # sha 拉取失败会让 run_update 拒绝安装（fail-closed），必须留痕
        log.warn("update", f"[sha] SHA-256 清单拉取失败: {type(e).__name__}: {e}")
    return ""


def run_update(remote: dict, progress=None) -> dict:
    """执行更新。返回 {"ok": bool, "message": str}。"""
    if not is_frozen():
        return {"ok": False, "message": "源码运行模式，请手动 git pull 更新"}

    plat = system_platform()
    if plat not in ("windows", "macos"):
        return {"ok": False, "message": f"不支持的平台: {plat}"}

    ver = remote.get("version", "")
    if not ver:
        return {"ok": False, "message": "远程版本信息缺少 version 字段"}
    # version 会被拼进文件名 / 安装目标路径 / re.sub 替换模板：非法格式既能让
    # dest/target 逃出 tmp_dir/app_dir（"../.." 穿越），也会让 re.sub 抛错。
    if not valid_version(ver):
        log.warn("update", f"[run] version 字段格式非法，拒绝安装: {ver!r}")
        return {"ok": False, "message": f"远程版本号格式非法，已拒绝安装: {ver}"}

    log.info("update", f"[run] 开始更新: v{current_version()} → v{ver} "
                       f"平台={plat}")
    # 下载到系统临时目录；装到哪个盘由安装阶段决定（支持跨盘复制）
    tmp_dir = Path(tempfile.gettempdir()) / "siwx_update"
    tmp_dir.mkdir(parents=True, exist_ok=True)
    dest = tmp_dir / _asset_name(remote, plat)
    if not validate.within(dest, tmp_dir):
        return {"ok": False, "message": "产物落地路径越界，已拒绝安装"}

    if progress:
        progress(10, f"下载 v{ver}...")
    if not _download_asset(remote, plat, dest):
        return {"ok": False, "message": "下载失败，请检查网络"}
    log.info("update", f"[run] 下载完成: {dest.name} ({dest.stat().st_size} 字节)")

    # 校验（审计 S1：哈希缺失时拒绝安装，不再静默跳过校验继续安装）
    if progress:
        progress(50, "校验 SHA-256...")
    expected_sha = _get_asset_sha(remote, plat)
    if not expected_sha:
        dest.unlink(missing_ok=True)
        return {"ok": False,
                "message": "无法获取官方 SHA-256 校验值，已拒绝安装（防更新链投毒）"}
    if not _verify_sha256(dest, expected_sha):
        dest.unlink(missing_ok=True)
        return {"ok": False, "message": "SHA-256 校验失败，文件可能损坏"}
    log.info("update", f"[run] SHA-256 校验通过，进入安装: {plat}")

    # 安装
    if progress:
        progress(80, "安装新版本...")
    if plat == "windows":
        return _replace_windows_exe(dest, ver, expected_sha)
    return _replace_macos_app(dest, ver)


# ── Windows 安装 ───────────────────────────────────────────────

def _target_exe_name(running_name: str, cur_ver: str, new_ver: str) -> str:
    """计算更新后的文件名。

    产物通常带版本号（stories-in-wx-v5.0.4-windows-x64.exe），用户也习惯
    直接运行它——更新目标应是同格式换上新版本号；文件名里没有当前版本号
    （如 stories-in-wx.exe）则沿用原名，就地替换。
    """
    if cur_ver and new_ver:
        new_name = re.sub(rf"v{re.escape(cur_ver)}(?![\d.])", f"v{new_ver}",
                          running_name, count=1)
        if new_name != running_name:
            return new_name
    return running_name


def _install_copy(src: Path, dst: Path, expected_sha: str = "") -> None:
    """把新产物复制到 dst。统一走 copyfile：os.replace 不能跨盘（WinError 17）。

    复制后复核完整性（优先 SHA-256，否则比对大小），失败删除残留并抛出。
    """
    shutil.copyfile(src, dst)
    try:
        if expected_sha:
            if not _verify_sha256(dst, expected_sha):
                raise RuntimeError("复制后 SHA-256 校验失败")
        elif src.stat().st_size != dst.stat().st_size:
            raise RuntimeError("复制后文件大小不一致")
    except Exception:
        dst.unlink(missing_ok=True)
        raise


def _replace_windows_exe(new_exe: Path, ver: str, expected_sha: str = "") -> dict:
    """安装 Windows 更新：跨盘安全、支持带版本号的文件名、失败可回滚。

    不做 taskkill——通配符 stories-in-wx* 会匹配到当前进程自己，更新线程
    会连同旧进程一起被杀，用户连错误提示都收不到。运行中的 exe 在 Windows
    上允许改名（不允许覆盖/删除），「改名旧文件 → 落新文件」即可平滑换血。
    """
    app_dir = paths.app_root()
    running = Path(sys.executable).resolve()
    cur = current_version()
    target = app_dir / _target_exe_name(running.name, cur, ver)
    # 落点断言：target 只能是 app_dir 下的文件名，绝不允许穿越到别处再 Popen
    if not validate.within(target, app_dir):
        log.warn("update", f"[install] 安装目标越界，已拒绝: {target}")
        return {"ok": False, "message": "安装目标路径越界，已拒绝安装"}
    try:
        if target.resolve() != running:
            # 带版本号命名：新文件是新名字，完全不碰正在运行的旧 exe
            if target.exists():
                target.unlink()
            _install_copy(new_exe, target, expected_sha)
        else:
            # 就地替换：先改名运行中的 exe（同盘原子操作），失败可改回
            backup = app_dir / (running.stem + ".backup" + running.suffix)
            backup.unlink(missing_ok=True)
            os.rename(running, backup)
            try:
                _install_copy(new_exe, target, expected_sha)
            except Exception:
                os.rename(backup, running)
                raise
    except Exception as e:
        log.warn("update", f"[install] 替换失败: {type(e).__name__}: {e}")
        return {"ok": False, "message": f"替换失败: {e}"}

    try:
        subprocess.Popen([str(target), "serve"], cwd=str(app_dir))
    except Exception as e:
        # 新文件已就位，但拉起失败：不退出旧进程，让用户手动启动
        log.warn("update", f"[install] 新版拉起失败，需手动启动: {e}")
        return {"ok": True, "message": f"已更新到 v{ver}，请手动启动新版程序"}
    _schedule_exit()
    return {"ok": True, "message": f"已更新到 v{ver}，新版即将自动启动"}


# ── macOS 安装 ─────────────────────────────────────────────────

def _find_app_bundle(binary: Path) -> Path | None:
    """从可执行文件路径向上找所属的 .app bundle（不在 bundle 内则 None）。"""
    for p in binary.parents:
        if p.suffix == ".app":
            return p
    return None


def _mount_dmg(dmg: Path) -> Path | None:
    """挂载 DMG，返回挂载点。优先固定挂载点（免去解析 /Volumes 输出）。"""
    mp = Path(tempfile.mkdtemp(prefix="siwx_mount_"))
    r = subprocess.run(
        ["hdiutil", "attach", str(dmg), "-readonly", "-nobrowse",
         "-mountpoint", str(mp)],
        capture_output=True, timeout=120)
    if r.returncode == 0 and mp.is_dir():
        return mp
    # 兜底：默认挂载，从输出解析挂载点
    r = subprocess.run(
        ["hdiutil", "attach", str(dmg), "-readonly", "-nobrowse"],
        capture_output=True, timeout=120)
    m = re.search(r"(/Volumes/.+)", r.stdout.decode("utf-8", "replace"))
    if m:
        p = Path(m.group(1).strip())
        if p.is_dir():
            return p
    return None


def _detach_dmg(mount: Path) -> None:
    try:
        subprocess.run(["hdiutil", "detach", str(mount)],
                       capture_output=True, timeout=30)
    except Exception:
        pass
    # 固定挂载点是我们建的临时目录，卸载后顺手清掉
    if str(mount).startswith(tempfile.gettempdir()):
        shutil.rmtree(mount, ignore_errors=True)


def _swap_app_bundle(src_app: Path, bundle: Path) -> None:
    """整包换血：旧包改名（原子）→ 拷入新包 → 成功删旧包，失败改回去。

    删除正在运行的应用 bundle 是允许的（POSIX unlink），运行中的进程
    不受影响；改名同理。拷新包到 <bundle>.new 再原子改名，避免拷贝
    中途失败留下半个包。
    """
    parent = bundle.parent
    tmp_new = parent / (bundle.stem + ".updating.app")
    old = parent / (bundle.name + ".old")
    for p in (tmp_new, old):
        if p.exists():
            shutil.rmtree(p, ignore_errors=True)
    shutil.copytree(src_app, tmp_new, symlinks=True)
    os.rename(bundle, old)
    try:
        os.rename(tmp_new, bundle)
    except Exception:
        os.rename(old, bundle)
        shutil.rmtree(tmp_new, ignore_errors=True)
        raise
    shutil.rmtree(old, ignore_errors=True)


def _replace_macos_app(dmg: Path, ver: str) -> dict:
    """安装 macOS 更新：挂载 DMG，替换 .app bundle（或裸二进制），重启。"""
    running = Path(sys.executable).resolve()
    bundle = _find_app_bundle(running)

    mount = _mount_dmg(dmg)
    if not mount:
        return {"ok": False, "message": "挂载更新镜像失败"}
    try:
        src_apps = sorted(mount.glob("*.app"))
        if not src_apps:
            return {"ok": False, "message": "更新镜像内未找到 .app"}
        src_app = src_apps[0]
        try:
            if bundle is not None:
                _swap_app_bundle(src_app, bundle)
                launch_cmd = ["open", "-n", str(bundle), "--args", "serve"]
            else:
                # 非 .app 布局（裸二进制）：替换二进制本身
                src_bin = next((src_app / "Contents" / "MacOS").glob("*"), None)
                if src_bin is None:
                    return {"ok": False, "message": "更新镜像内未找到可执行文件"}
                tmp_bin = running.with_name(running.name + ".new")
                shutil.copyfile(src_bin, tmp_bin)
                tmp_bin.chmod(0o755)
                os.rename(tmp_bin, running)
                launch_cmd = [str(running), "serve"]
        except Exception as e:
            log.warn("update", f"[install] macOS 替换失败: {type(e).__name__}: {e}")
            return {"ok": False, "message": f"替换失败: {e}"}

        try:
            subprocess.Popen(launch_cmd)
        except Exception as e:
            log.warn("update", f"[install] 新版拉起失败，需手动启动: {e}")
            return {"ok": True, "message": f"已更新到 v{ver}，请手动启动新版程序"}
        _schedule_exit()
        return {"ok": True, "message": f"已更新到 v{ver}，新版即将自动启动"}
    finally:
        _detach_dmg(mount)


# ── 重启衔接 ───────────────────────────────────────────────────

def _schedule_exit(delay: float = 2.0) -> None:
    """延迟退出旧进程。

    留 2 秒让 /api/update/do 的响应先返回给前端，再硬退出释放端口；
    新进程侧由 server 的「等端口释放」逻辑兜底（见 server.run_server）。
    """
    threading.Timer(delay, os._exit, args=(0,)).start()


# ── 后台检查 ───────────────────────────────────────────────────

def check_in_background(callback):
    """后台线程检查更新。callback(has_update, remote, current)。"""

    def _check():
        has, remote, cur = has_update()
        callback(has, remote, cur)

    t = threading.Thread(target=_check, daemon=True)
    t.start()
