"""策略：macOS LLDB 断点 + PBKDF2 派生（WeChat 4.1.80+）。

微信 4.1.80 起不再在内存中缓存 raw key（x'<96hex>'），只保留 32 字节
passphrase。本策略通过 LLDB 在 sqlite3_key / sqlite3_key_v2 上设断点，
捕获 passphrase，再用 PBKDF2-SHA512（256000 次迭代）为每个数据库派生独立密钥。

依赖：macOS + lldb 命令行 + 微信已登录。
"""
import hashlib
import os
import re
import subprocess
import tempfile
from pathlib import Path

from siwx.sqlcipher import verify_enc_key

# PBKDF2 参数（与微信 4.1.80 一致）
PBKDF2_ITERATIONS = 256000
PBKDF2_DKLEN = 32
PBKDF2_DIGEST = "sha512"

# LLDB 脚本：在 sqlite3_key 上设断点，捕获 passphrase
_LLDB_SCRIPT = r"""
import lldb

def run(target_pid):
    target = lldb.debugger.CreateTarget("")
    if not target:
        return None

    # 附加到微信进程
    error = lldb.SBError()
    process = target.AttachToProcessWithID(lldb.SBDebugger(), target_pid, error)
    if error.Fail():
        return None

    # 找到 wechat.dylib 中的 sqlite3_key / sqlite3_key_v2
    module = target.FindModule("wechat.dylib")
    if not module:
        module = target.FindModule("libsqlcipher")

    key_fns = ["sqlite3_key", "sqlite3_key_v2"]
    bp_addrs = []
    for fn in key_fns:
        sym = module.FindSymbol(fn)
        if sym:
            bp_addrs.append(sym.addr)

    if not bp_addrs:
        process.Kill()
        return None

    # 设断点
    bps = []
    for addr in bp_addrs:
        bp = target.BreakpointCreateByAddress(addr)
        bps.append(bp)

    # 继续执行，等待断点命中
    process.Continue()

    # 等待 passphrase 出现在参数中（最多等 30 秒）
    import time
    deadline = time.time() + 30
    passphrase = None
    while time.time() < deadline:
        if not process.IsValid():
            break
        state = process.GetState()
        if state == lldb.eStateStopped:
            # 检查线程停止原因
            for thread in process:
                if thread.GetStopReason() == lldb.eStopReasonBreakpoint:
                    # sqlite3_key(db, pKey, nKey)
                    # args[1] = pKey (passphrase 指针), args[2] = nKey (长度)
                    frame = thread.GetFrameAtIndex(0)
                    pKey = frame.EvaluateExpression("(const void*)$arg2")
                    nKey = frame.EvaluateExpression("(int)$arg3")
                    if pKey.IsValid() and nKey.IsValid():
                        key_len = nKey.GetValueAsUnsigned()
                        if key_len == 32:
                            # 读取 32 字节 passphrase
                            error = lldb.SBError()
                            data = process.ReadMemory(pKey.GetValueAsUnsigned(), key_len, error)
                            if not error.Fail() and len(data) == key_len:
                                passphrase = data.hex()
                                break
            if passphrase:
                break
            # 继续执行
            process.Continue()
        time.sleep(0.1)

    # 清理
    for bp in bps:
        target.BreakpointDeleteByBreakpointSiteID(bp.GetBreakpointSite().GetID())
    process.Kill()

    return passphrase


def extract(ctx) -> int:
    """macOS 专用：LLDB 断点抓 passphrase + PBKDF2 派生。"""
    import sys
    if sys.platform != "darwin":
        return 0  # 非 macOS 跳过

    page1_by_salt = ctx["page1_by_salt"]
    key_map = ctx["key_map"]
    attrib = ctx["attrib"]
    log = ctx["log"]

    # 检查 lldb 是否可用
    try:
        subprocess.run(["lldb", "--version"], capture_output=True, check=True)
    except (FileNotFoundError, subprocess.CalledProcessError):
        log("[macos_lldb] lldb 未安装，跳过")
        return 0

    # 找微信进程
    try:
        result = subprocess.run(
            ["pgrep", "-x", "WeChat"],
            capture_output=True, text=True
        )
        pids = [int(p) for p in result.stdout.split() if p.strip().isdigit()]
    except Exception:
        pids = []

    if not pids:
        log("[macos_lldb] 未检测到微信进程")
        return 0

    log(f"[macos_lldb] 微信进程 {pids}")

    entry = len(key_map)
    for pid in pids:
        if len(key_map) >= len(page1_by_salt):
            break

        log(f"[macos_lldb] PID={pid}: 启动 LLDB 断点捕获...")
        passphrase = _capture_passphrase_via_lldb(pid)
        if not passphrase:
            log(f"[macos_lldb] PID={pid}: 未捕获到 passphrase")
            continue

        log(f"[macos_lldb] PID={pid}: 捕获到 passphrase {passphrase[:16]}...")

        # 用 passphrase 派生每个数据库的密钥
        for salt_hex, page1 in page1_by_salt.items():
            if salt_hex in key_map:
                continue
            salt_bytes = bytes.fromhex(salt_hex)
            dk = hashlib.pbkdf2_hmac(
                PBKDF2_DIGEST,
                bytes.fromhex(passphrase),
                salt_bytes,
                PBKDF2_ITERATIONS,
                dklen=PBKDF2_DKLEN,
            )
            if verify_enc_key(dk, page1):
                key_hex = dk.hex()
                log(f"  [macos_lldb] salt={salt_hex[:16]}… 已验证")
                key_map[salt_hex] = key_hex
                attrib[salt_hex] = "macos_lldb"
            if len(key_map) >= len(page1_by_salt):
                break

    found = len(key_map) - entry
    if found:
        log(f"[macos_lldb] 完成: +{found}")
    return found


def _capture_passphrase_via_lldb(pid: int) -> str | None:
    """用 LLDB 脚本捕获 sqlite3_key 的 passphrase 参数。"""
    script = f"""
import lldb, time

debugger = lldb.SBDebugger.Create()
debugger.SetAsync(False)
target = debugger.CreateTarget("")
if not target:
    print("FAIL:CreateTarget")
    exit(1)

error = lldb.SBError()
process = target.AttachToProcessWithID(debugger, pid, error)
if error.Fail():
    print(f"FAIL:Attach:{{error}}")
    exit(1)

# 搜索所有模块找 sqlite3_key
addr = None
for mod in target.module_iter():
    for fn in ["sqlite3_key", "sqlite3_key_v2"]:
        sym = mod.FindSymbol(fn)
        if sym and sym.addr.IsValid():
            addr = sym.addr
            break
    if addr:
        break

if not addr:
    process.Kill()
    print("FAIL:NoSymbol")
    exit(1)

bp = target.BreakpointCreateByAddress(addr)
process.Continue()

deadline = time.time() + 30
found = False
while time.time() < deadline:
    if not process.IsValid():
        break
    state = process.GetState()
    if state == lldb.eStateStopped:
        for thread in process:
            if thread.GetStopReason() == lldb.eStopReasonBreakpoint:
                frame = thread.GetFrameAtIndex(0)
                # x86_64: rdi=arg1, rsi=arg2, rdx=arg3
                # sqlite3_key(db, pKey, nKey) → arg2=pKey, arg3=nKey
                pKey_expr = frame.EvaluateExpression("(const void*)$rdi + 8")  # 偏移到第2个参数
                # 更可靠：直接读寄存器
                pKey_reg = frame.GetRegisters().GetRegisterAtIndex(4)  # rsi
                nKey_reg = frame.GetRegisters().GetRegisterAtIndex(5)  # rdx
                if pKey_reg and nKey_reg:
                    pKey_val = pKey_reg.GetValueAsUnsigned()
                    nKey_val = nKey_reg.GetValueAsUnsigned()
                    if nKey_val == 32:
                        data = process.ReadMemory(pKey_val, 32, error)
                        if not error.Fail() and len(data) == 32:
                            print(f"OK:{{data.hex()}}")
                            found = True
                            break
        if found:
            break
        process.Continue()
    time.sleep(0.1)

target.BreakpointDeleteByBreakpointSiteID(bp.GetBreakpointSite().GetID())
process.Kill()
if not found:
    print("FAIL:Timeout")
"""

    try:
        with tempfile.NamedTemporaryFile(mode='w', suffix='.py', delete=False) as f:
            f.write(script)
            script_path = f.name

        result = subprocess.run(
            ["lldb", "-b", "-o", f"script import sys; sys.path.insert(0, '{script_path}'); exec(open('{script_path}').read())"],
            capture_output=True, text=True, timeout=45
        )
        os.unlink(script_path)

        for line in result.stdout.splitlines():
            if line.startswith("OK:"):
                return line[3:].strip()
    except Exception as e:
        pass

    return None
