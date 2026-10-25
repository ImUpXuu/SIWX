"""策略：Linux 进程内 INT3 钩子提取 WCDB passphrase（微信 4.1.x）。

微信 4.1+ 只用一条 32 字节 passphrase 经 PBKDF2-SHA512(256000) 派生每个库的
密钥，原始密钥不再以可扫描字面量存在于内存。Linux 上常见的做法是外部
ptrace tracer 在登录时读寄存器 —— 但微信 4.1.2x 内置 Chromium crashpad 会
自己 ptrace 附加写 minidump，一个进程只能有一个 tracer，外部 tracer 抢先
附加后 crashpad 附加失败并 abort()：

    [ERROR:scoped_ptrace_attach.cc(27)] ptrace: Operation not permitted (1)

所以本策略把断点打在**微信自己的进程内**：LD_PRELOAD 一个 shim，构造函数
把 WCDB cipher-config 消费函数首字节改成 INT3，用自己的 SIGTRAP 处理器从
ucontext 读寄存器，命中后复原字节、RIP 拨回断点并拆掉断点（一次性布点）。
全程没有 ptrace，微信无感，也不需要 root。

注入方式：桌面环境（DDE / UOS / GNOME）通过 systemd 用户管理器拉起微信，
直接给子进程设 `LD_PRELOAD=` 会被丢掉。改用
`systemctl --user set-environment` 把 `LD_PRELOAD` 放进用户管理器环境，
之后新建的瞬态单元就会继承；捕获结束立刻还原。没有 systemd 用户会话时，
退化为带环境变量直接执行二进制。

前置条件：
  * Linux x86-64；
  * 系统编译器（cc / gcc），用于现场构建 shim；
  * 用户在窗口期内扫码登录（或微信已登录，重启后建连即触发）。

偏移与构建绑定：换微信版本后用 `siwx.linux_hook.elfhook.find_hook_offset`
重算，无需改代码。
"""
from __future__ import annotations

import hashlib
import os
import platform
import re
import shutil
import subprocess
import tempfile
import time
from pathlib import Path

from siwx.sqlcipher import verify_enc_key

# 与 macOS LLDB 策略一致的 PBKDF2 参数（微信 4.1.x 实测）
PBKDF2_ITERATIONS = 256000
PBKDF2_DKLEN = 32
PBKDF2_DIGEST = "sha512"

_LOG_ENV = "WXS_HOOK_LOG"
_DEFAULT_LOG = "/tmp/siwx_linux_hook.log"

# 启动器把微信拉起来需要的宽限期：这之前没看到进程属正常（dde-am → transient unit）。
_LAUNCH_GRACE = 20.0

# 一次 extract 里只允许"停微信 + 注入"发生一次。钩子抓的是当前登录态，
# 多账号目录下未登录的账号再重启一次微信只会杀掉用户的会话并空等超时。
_ATTEMPTED = False

_CANDIDATE_RE = re.compile(r"a=([0-9a-f]+) b=([0-9a-f]+) data=([0-9a-f]{64})")


def _resolve_binary() -> Path | None:
    """定位真正承载 WCDB 的微信主二进制（符号链接要 resolve）。"""
    env = os.environ.get("WXS_WECHAT_BIN")
    if env and Path(env).is_file():
        return Path(env).resolve()

    for cand in ("/opt/wechat/wechat", "/usr/bin/wechat", "/usr/bin/weixin"):
        p = Path(cand)
        if p.is_file():
            return p.resolve()

    # 退而求其次：从已运行的微信反推 exe
    for entry in Path("/proc").iterdir():
        if not entry.name.isdigit():
            continue
        try:
            exe = os.readlink(entry / "exe")
        except OSError:
            continue
        name = Path(exe).name
        if name in ("wechat", "weixin") and ("com.tencent" in exe or exe.endswith("/wechat")):
            return Path(exe)
    return None


def _wechat_pids(binary: Path) -> list[int]:
    """按 /proc 精确匹配主二进制（Helper 进程是别的 exe，不会误伤）。"""
    out: list[int] = []
    target = str(binary)
    for entry in Path("/proc").iterdir():
        if not entry.name.isdigit():
            continue
        try:
            if os.readlink(entry / "exe") == target:
                out.append(int(entry.name))
        except OSError:
            continue
    return out


def _stop_wechat(binary: Path, log) -> None:
    pids = _wechat_pids(binary)
    if not pids:
        return
    log(f"[linux_hook] 关闭微信 (pid={pids[0]})…")
    for pid in pids:
        try:
            os.kill(pid, 15)
        except OSError:
            pass
    for _ in range(20):
        if not _wechat_pids(binary):
            return
        time.sleep(0.25)


def _account_is_live(db_dir) -> bool:
    """该账号此刻是否已登录：有进程开着它 db_storage 下的文件。

    只有登录态会建立数据库连接，所以这个探针等价于"这个账号现在能不能被抓到"。
    """
    if not db_dir:
        return False
    try:
        root = str(Path(db_dir).resolve())
    except OSError:
        return False
    if not root:
        return False
    for entry in Path("/proc").iterdir():
        if not entry.name.isdigit():
            continue
        try:
            fds = list((entry / "fd").iterdir())
        except OSError:
            continue
        for fd in fds:
            try:
                target = os.readlink(fd)
            except OSError:
                continue
            if target.startswith(root):
                return True
    return False


def _has_user_systemd() -> bool:
    try:
        r = subprocess.run(["systemctl", "--user", "show-environment"],
                           capture_output=True, timeout=5)
        return r.returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        return False


def _user_env(varname: str) -> str | None:
    """读用户管理器当前的某个环境变量（没有返回 None）。"""
    try:
        r = subprocess.run(["systemctl", "--user", "show-environment"],
                           capture_output=True, text=True, timeout=5)
    except (OSError, subprocess.TimeoutExpired):
        return None
    if r.returncode != 0:
        return None
    prefix = varname + "="
    for line in r.stdout.splitlines():
        if line.startswith(prefix):
            return line[len(prefix):]
    return None


def _set_user_env(**kv) -> bool:
    args = [f"{k}={v}" for k, v in kv.items()]
    try:
        r = subprocess.run(["systemctl", "--user", "set-environment", *args],
                           capture_output=True, timeout=5)
        return r.returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        return False


def _unset_user_env(*names: str) -> None:
    try:
        subprocess.run(["systemctl", "--user", "unset-environment", *names],
                       capture_output=True, timeout=5)
    except (OSError, subprocess.TimeoutExpired):
        pass


def _launch_wechat(binary: Path, log, extra_env: dict | None = None) -> None:
    """优先走桌面启动器（官方 deb 布局下直启二进制会 SIGABRT）。"""
    launcher = shutil.which("dde-am")
    env = dict(os.environ)
    if extra_env:
        env.update(extra_env)
    try:
        if launcher:
            subprocess.Popen([launcher, "com.tencent.wechat"],
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                             start_new_session=True, env=env)
            log("[linux_hook] 已通过 dde-am 拉起微信")
        else:
            subprocess.Popen([str(binary)],
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                             start_new_session=True, env=env)
            log(f"[linux_hook] 已启动 {binary}")
    except OSError as e:
        log(f"[linux_hook] ⚠ 启动微信失败: {e}")


def _build_shim(hook_off: int, target_exe: str, workdir: Path) -> Path | None:
    """现场编译 shim。返回 .so 路径，失败返回 None。"""
    src = Path(__file__).resolve().parent.parent / "linux_hook" / "wxhook.c"
    if not src.is_file():
        return None
    cc = shutil.which("cc") or shutil.which("gcc")
    if not cc:
        return None
    out = workdir / "wxhook.so"
    cmd = [
        cc, "-shared", "-fPIC", "-O2",
        f'-DTARGET_EXE="{target_exe}"',
        f"-DHOOK_OFFSET=0x{hook_off:x}",
        "-o", str(out), str(src), "-ldl", "-lpthread",
    ]
    try:
        subprocess.run(cmd, check=True, capture_output=True, timeout=120)
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired, OSError):
        return None
    return out if out.is_file() else None


def _wait_for_candidates(log_path: Path, timeout: float, log,
                         alive=None) -> list[str]:
    """轮询 shim 日志，收集 32 字节候选（去重、保序）。

    ``alive``：可选的"微信是否还活着"探针。启动后短暂没进程属正常（启动器在拉），
    一旦出现过又消失，说明这一轮抓不到了——立刻结束等待，别拖到超时（否则
    systemd 用户环境里的 LD_PRELOAD 要一起拖到最后才还原）。
    """
    seen: list[str] = []
    deadline = time.monotonic() + timeout
    start = time.monotonic()
    told = False
    appeared = False
    while time.monotonic() < deadline:
        try:
            text = log_path.read_text(errors="replace")
        except OSError:
            text = ""
        for m in _CANDIDATE_RE.finditer(text):
            hexv = m.group(3)
            if hexv not in seen:
                seen.append(hexv)
        if seen:
            return seen
        if alive is not None:
            if alive():
                appeared = True
            elif appeared or time.monotonic() - start > _LAUNCH_GRACE:
                log("[linux_hook] 微信进程已退出，停止等待并还原环境")
                break
        if not told and time.monotonic() > deadline - timeout + 15:
            log("[linux_hook] >> 等待登录：如果微信显示扫码界面，请立刻扫码登录")
            told = True
        time.sleep(0.5)
    return seen


def _derive_keys(passphrase_hex: str, page1_by_salt: dict, key_map: dict,
                 attrib: dict, log, dbg) -> int:
    """先拿单个 salt 快速否定，通过后再派生全部。"""
    passphrase = bytes.fromhex(passphrase_hex)
    ordered = [s for s in page1_by_salt if s not in key_map]
    if not ordered:
        return 0

    def derive(salt_hex: str) -> bytes:
        return hashlib.pbkdf2_hmac(
            PBKDF2_DIGEST, passphrase, bytes.fromhex(salt_hex),
            PBKDF2_ITERATIONS, dklen=PBKDF2_DKLEN,
        )

    probe = ordered[0]
    if not verify_enc_key(derive(probe), page1_by_salt[probe]):
        dbg(f"[linux_hook] 候选 {passphrase_hex[:8]}… 与 salt {probe[:8]}… 不匹配")
        return 0

    derived = 0
    for salt_hex in ordered:
        key = derive(salt_hex)
        if verify_enc_key(key, page1_by_salt[salt_hex]):
            key_map[salt_hex] = key.hex()
            attrib[salt_hex] = "linux_hook"
            derived += 1
        else:
            dbg(f"[linux_hook] salt={salt_hex[:16]}… 派生 HMAC 未命中")
    return derived


def extract(ctx) -> int:
    """Linux：进程内 INT3 钩子抓 passphrase，再 PBKDF2 派生每库密钥。"""
    if platform.system() != "Linux":
        return 0
    if platform.machine() not in ("x86_64", "amd64"):
        ctx["log"]("[linux_hook] 仅支持 x86-64，跳过")
        return 0

    page1_by_salt = ctx["page1_by_salt"]
    key_map = ctx["key_map"]
    attrib = ctx["attrib"]
    log = ctx["log"]
    dbg = ctx.get("dbg", log)

    entry = len(key_map)
    if entry >= len(page1_by_salt):
        return 0

    global _ATTEMPTED
    # 钩子只能抓到"当前登录态"的 passphrase。已经注入过一个账号之后再遇到
    # 未登录的账号目录（多账号机器很常见），重启微信只会打断用户正在用的会话
    # 然后空等到超时——直接跳过，按文档要求换该账号登录后重跑。
    if _ATTEMPTED and not _account_is_live(ctx.get("db_dir")):
        log("[linux_hook] 该账号当前未登录（没有进程打开它的 db_storage），"
            "跳过——换该账号登录后重跑")
        return 0

    binary = _resolve_binary()
    if binary is None:
        log("[linux_hook] 未找到微信二进制，跳过")
        return 0
    log(f"[linux_hook] 微信二进制: {binary}")

    try:
        from siwx.linux_hook.elfhook import HookNotFoundError, find_hook_offset
        hook_off = find_hook_offset(binary)
    except HookNotFoundError as e:
        log(f"[linux_hook] 无法定位 WCDB cipher hook: {e}")
        return 0
    log(f"[linux_hook] hook 偏移: 0x{hook_off:x}")

    workdir = Path(tempfile.mkdtemp(prefix="siwx-hook-"))
    log_path = Path(os.environ.get(_LOG_ENV, _DEFAULT_LOG))
    use_systemd = _has_user_systemd()
    prev_preload = _user_env("LD_PRELOAD") if use_systemd else None
    env_set = False
    try:
        so = _build_shim(hook_off, str(binary), workdir)
        if so is None:
            log("[linux_hook] shim 构建失败（需要系统 cc/gcc）")
            return 0

        _stop_wechat(binary, log)
        if log_path.exists():
            log_path.unlink()

        if use_systemd:
            # 把 shim 放进用户管理器环境，DDE 新建的瞬态单元会继承
            env_set = _set_user_env(LD_PRELOAD=str(so), **{_LOG_ENV: str(log_path)})
            if env_set:
                log("[linux_hook] 已通过 systemctl --user 注入 shim")
            else:
                log("[linux_hook] ⚠ systemctl --user set-environment 失败，改用直接注入")
                use_systemd = False

        extra = None if use_systemd else {"LD_PRELOAD": str(so), _LOG_ENV: str(log_path)}
        _ATTEMPTED = True
        _launch_wechat(binary, log, extra_env=extra)

        timeout = float(os.environ.get("WXS_HOOK_TIMEOUT", "180"))
        candidates = _wait_for_candidates(
            log_path, timeout, log, alive=lambda: bool(_wechat_pids(binary)))
        if not candidates:
            log("[linux_hook] 未捕获到候选 passphrase（未登录 / 超时 / 需要扫码）")
            return 0
        log(f"[linux_hook] 捕获 {len(candidates)} 个候选，开始校验")

        for cand in candidates:
            derived = _derive_keys(cand, page1_by_salt, key_map, attrib, log, dbg)
            if derived:
                log(f"[linux_hook] 候选 {cand[:8]}… 命中，派生 {derived} 个密钥")
                break
    finally:
        if env_set:
            # 精确还原：原来有 LD_PRELOAD 就恢复原值，没有就删掉
            if prev_preload is not None:
                _set_user_env(LD_PRELOAD=prev_preload)
            else:
                _unset_user_env("LD_PRELOAD")
            _unset_user_env(_LOG_ENV)
            log("[linux_hook] 已还原 systemd 用户环境")
        shutil.rmtree(workdir, ignore_errors=True)
        try:
            if log_path.exists():
                log_path.unlink()
        except OSError:
            pass

    found = len(key_map) - entry
    if found:
        log(f"[linux_hook] done: +{found}")
    return found
