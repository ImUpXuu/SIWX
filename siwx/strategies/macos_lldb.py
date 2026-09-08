"""Strategy: macOS LLDB breakpoint + PBKDF2 derivation (WeChat 4.1.80+).

Since WeChat 4.1.80, raw keys are no longer cached in memory, only a 32-byte
passphrase remains. This strategy uses LLDB to set a breakpoint on sqlite3_key
/ sqlite3_key_v2, captures the passphrase, then derives per-database keys using
PBKDF2-SHA512 (256000 iterations).

Requires: macOS + lldb CLI + WeChat logged in.
"""
import hashlib
import os
import re
import subprocess
import tempfile
from pathlib import Path

from siwx.sqlcipher import verify_enc_key

# PBKDF2 params (matches WeChat 4.1.80)
PBKDF2_ITERATIONS = 256000
PBKDF2_DKLEN = 32
PBKDF2_DIGEST = "sha512"


def extract(ctx) -> int:
    """macOS only: LLDB breakpoint to capture passphrase + PBKDF2 derivation."""
    import sys
    if sys.platform != "darwin":
        return 0

    page1_by_salt = ctx["page1_by_salt"]
    key_map = ctx["key_map"]
    attrib = ctx["attrib"]
    log = ctx["log"]

    try:
        result = subprocess.run(["lldb", "--version"], capture_output=True, check=True, timeout=10)
        ver = result.stdout.decode("utf-8", errors="replace").strip()[:100]
        log(f"[macos_lldb] lldb 版本: {ver}")
    except FileNotFoundError:
        log("[macos_lldb] 错误: lldb 未安装 (需要 Xcode Command Line Tools)")
        return 0
    except subprocess.TimeoutExpired:
        log("[macos_lldb] 错误: lldb --version 超时")
        return 0
    except subprocess.CalledProcessError as e:
        log(f"[macos_lldb] 错误: lldb 执行失败: {e}")
        return 0

    try:
        result = subprocess.run(["pgrep", "-x", "WeChat"], capture_output=True, text=True)
        pids = [int(p) for p in result.stdout.split() if p.strip().isdigit()]
    except Exception as e:
        log(f"[macos_lldb] 错误: pgrep 失败: {e}")
        pids = []

    if not pids:
        log("[macos_lldb] 错误: 未检测到 WeChat 进程")
        return 0

    log(f"[macos_lldb] WeChat PIDs: {pids}, 需验证 salt: {len(page1_by_salt)}个")

    entry = len(key_map)
    for pid in pids:
        if len(key_map) >= len(page1_by_salt):
            break
        log(f"[macos_lldb] PID={pid}: 开始 LLDB 断点捕获...")
        passphrase = _capture_passphrase_via_lldb(pid, log)
        if not passphrase:
            log(f"[macos_lldb] PID={pid}: 未捕获到 passphrase")
            continue
        log(f"[macos_lldb] PID={pid}: 捕获到 passphrase ({len(passphrase)} hex chars)")

        # 用 passphrase 派生每个数据库的密钥
        derived = 0
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
                log(f"  [macos_lldb] salt={salt_hex[:16]}... 已验证 (key={key_hex[:8]}...)")
                key_map[salt_hex] = key_hex
                attrib[salt_hex] = "macos_lldb"
                derived += 1
            if len(key_map) >= len(page1_by_salt):
                break
        log(f"[macos_lldb] PID={pid}: 派生 {derived} 个密钥")

    found = len(key_map) - entry
    if found:
        log(f"[macos_lldb] done: +{found}")
    return found


def _capture_passphrase_via_lldb(pid: int, log) -> str | None:
    """LLDB breakpoint to capture sqlite3_key passphrase arg, with detailed logging."""
    script = f"""
import lldb, time, sys

debugger = lldb.SBDebugger.Create()
debugger.SetAsync(False)
target = debugger.CreateTarget("")
if not target:
    print("FAIL:CreateTarget")
    sys.exit(1)

error = lldb.SBError()
process = target.AttachToProcessWithID(debugger, pid, error)
if error.Fail():
    print(f"FAIL:Attach:{{error}}")
    sys.exit(1)

# Search all modules for sqlite3_key
addr = None
found_mod = None
for mod in target.module_iter():
    for fn in ["sqlite3_key", "sqlite3_key_v2"]:
        sym = mod.FindSymbol(fn)
        if sym and sym.addr.IsValid():
            addr = sym.addr
            found_mod = mod.GetFileSpec().GetFilename()
            break
    if addr:
        break

if not addr:
    process.Kill()
    print("FAIL:NoSymbol")
    sys.exit(1)

print(f"SYM:{{found_mod}}:{{hex(addr.GetLoadAddress(target))}}")
bp = target.BreakpointCreateByAddress(addr)
process.Continue()

deadline = time.time() + 30
found = False
hits = 0
while time.time() < deadline:
    if not process.IsValid():
        print(f"PROCESS_DEAD:{{process.GetState()}}")
        break
    state = process.GetState()
    if state == lldb.eStateStopped:
        for thread in process:
            if thread.GetStopReason() == lldb.eStopReasonBreakpoint:
                hits += 1
                frame = thread.GetFrameAtIndex(0)
                regs = frame.GetRegisters()
                # x86_64: rdi=arg1, rsi=arg2, rdx=arg3
                # arm64: x0=arg1, x1=arg2, x2=arg3
                pKey_val = None
                nKey_val = None
                for reg_name in ["rsi", "x1"]:
                    reg = regs.GetRegisterByName(reg_name)
                    if reg and reg.IsValid():
                        pKey_val = reg.GetValueAsUnsigned()
                        break
                for reg_name in ["rdx", "x2"]:
                    reg = regs.GetRegisterByName(reg_name)
                    if reg and reg.IsValid():
                        nKey_val = reg.GetValueAsUnsigned()
                        break
                if pKey_val is None:
                    val = frame.EvaluateExpression("(const void*)$arg2")
                    if val and val.IsValid():
                        pKey_val = val.GetValueAsUnsigned()
                if nKey_val is None:
                    val = frame.EvaluateExpression("(int)$arg3")
                    if val and val.IsValid():
                        nKey_val = val.GetValueAsUnsigned()
                if pKey_val and nKey_val and nKey_val == 32:
                    data = process.ReadMemory(pKey_val, 32, error)
                    if not error.Fail() and len(data) == 32:
                        print(f"OK:{{data.hex()}}")
                        found = True
                        break
                else:
                    print(f"HIT:pk={{pKey_val}} nk={{nKey_val}} hits={{hits}}")
        if found:
            break
        process.Continue()
    time.sleep(0.05)

if not found:
    print(f"FAIL:Timeout hits={{hits}}")
process.Kill()
"""

    try:
        with tempfile.NamedTemporaryFile(mode='w', suffix='.py', delete=False) as f:
            f.write(script)
            script_path = f.name

        result = subprocess.run(
            ["lldb", "-b", "-O", f"command script import {script_path}"],
            capture_output=True, text=True, timeout=45)
        os.unlink(script_path)

        # Parse and log output
        for line in result.stdout.splitlines():
            if line.startswith("OK:"):
                return line[3:].strip()
            elif line.startswith("FAIL:"):
                log(f"[macos_lldb] LLDB: {line}")
            elif line.startswith("SYM:"):
                log(f"[macos_lldb] symbol: {line[4:]}")
            elif line.startswith("HIT:"):
                log(f"[macos_lldb] breakpoint hit: {line[4:]}")
            elif line.startswith("PROCESS_DEAD:"):
                log(f"[macos_lldb] process died: {line[13:]}")
        if result.stderr:
            err = result.stderr.strip()[:200]
            if err:
                log(f"[macos_lldb] stderr: {err}")
    except subprocess.TimeoutExpired:
        log("[macos_lldb] error: LLDB timeout (45s)")
    except Exception as e:
        log(f"[macos_lldb] error: {e}")

    return None
