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


def _build_lldb_script(pid: int) -> str:
    """渲染 LLDB 内嵌 Python 脚本。

    注意: pid 必须在此处插值注入到脚本顶部。此前版本脚本内直接引用了
    未定义的 `pid` 变量，`command script import` 执行时立即抛出
    NameError，LLDB 对外只显示 "error: module importing failed"，
    导致密钥捕获永远失败 (issue #3)。
    """
    script = f"""
import lldb, time, sys

pid = {pid}

debugger = lldb.SBDebugger.Create()
# 异步模式: process.Continue() 立即返回，下方轮询循环的 30s deadline 才有效
debugger.SetAsync(True)
target = debugger.CreateTarget("")
if not target:
    print("FAIL:CreateTarget")
    sys.exit(1)

error = lldb.SBError()
process = target.AttachToProcessWithID(debugger, pid, error)
if error.Fail() or not process.IsValid():
    print(f"FAIL:Attach:{{error}}")
    sys.exit(1)

# Attach 后按名字在所有模块上创建断点（兼容符号表/导出表两种形态）
bp_key = target.BreakpointCreateByName("sqlite3_key")
bp_v2 = target.BreakpointCreateByName("sqlite3_key_v2")
n_loc = bp_key.GetNumLocations() + bp_v2.GetNumLocations()

# Fallback: 扫描各模块符号表按地址建断点
# (SBModule.FindSymbol 返回 SBSymbolContextList，需取 .symbol 才有 .addr)
if n_loc == 0:
    for mod in target.module_iter():
        for fn in ["sqlite3_key", "sqlite3_key_v2"]:
            try:
                sc_list = mod.FindSymbol(fn)
            except Exception:
                continue
            if not sc_list:
                continue
            for i in range(sc_list.GetSize()):
                sym = sc_list.GetContextAtIndex(i).symbol
                if not sym:
                    continue
                sa = sym.addr
                if sa and sa.IsValid():
                    la = sa.GetLoadAddress(target)
                    if la != lldb.LLDB_INVALID_ADDRESS:
                        target.BreakpointCreateByAddress(la)
                        n_loc += 1

print(f"BP:{{n_loc}}")
if n_loc == 0:
    try:
        process.Detach()
    except Exception:
        pass
    print("FAIL:NoSymbol")
    sys.exit(1)

process.Continue()

def _reg(regs, names):
    for n in names:
        r = regs.GetRegisterByName(n)
        if r and r.IsValid():
            return r.GetValueAsUnsigned()
    return None

deadline = time.time() + 30
found = False
hits = 0
while time.time() < deadline:
    if not process.IsValid():
        print(f"PROCESS_DEAD:{{process.GetState()}}")
        break
    if process.GetState() == lldb.eStateStopped:
        for thread in process:
            if thread.GetStopReason() != lldb.eStopReasonBreakpoint:
                continue
            hits += 1
            # 用断点 ID 区分命中了哪个函数，二者参数位不同:
            # sqlite3_key(db, pKey, nKey)         -> pKey=arg2, nKey=arg3
            # sqlite3_key_v2(db, zDb, pKey, nKey) -> pKey=arg3, nKey=arg4
            is_v2 = (thread.GetStopReasonDataAtIndex(0) == bp_v2.GetID())
            frame = thread.GetFrameAtIndex(0)
            regs = frame.GetRegisters()
            # x86_64: rdi/rsi/rdx/rcx = arg1/2/3/4; arm64: x0..x3
            if is_v2:
                pKey_val = _reg(regs, ["rdx", "x2"])
                nKey_val = _reg(regs, ["rcx", "x3"])
            else:
                pKey_val = _reg(regs, ["rsi", "x1"])
                nKey_val = _reg(regs, ["rdx", "x2"])
            if pKey_val is None:
                expr = "(const void*)$arg3" if is_v2 else "(const void*)$arg2"
                v = frame.EvaluateExpression(expr)
                if v and v.IsValid():
                    pKey_val = v.GetValueAsUnsigned()
            if nKey_val is None:
                expr = "(int)$arg4" if is_v2 else "(int)$arg3"
                v = frame.EvaluateExpression(expr)
                if v and v.IsValid():
                    nKey_val = v.GetValueAsUnsigned()
            if nKey_val is not None:
                nKey_val &= 0xFFFFFFFF  # int 参数高位可能残留脏数据 (arm64 w3)
            if pKey_val and nKey_val == 32:
                data = process.ReadMemory(pKey_val, 32, error)
                if not error.Fail() and len(data) == 32:
                    print(f"OK:{{data.hex()}}")
                    found = True
                    break
            else:
                print(f"HIT:v2={{is_v2}} pk={{pKey_val}} nk={{nKey_val}} hits={{hits}}")
        if found:
            break
        try:
            process.Continue()
        except Exception:
            break
    time.sleep(0.05)

if not found:
    print(f"FAIL:Timeout hits={{hits}}")

# Detach（而非 Kill），保留断点现场恢复，让微信继续运行
try:
    if process.IsValid():
        process.Detach()
except Exception:
    pass
"""
    return script


def _capture_passphrase_via_lldb(pid: int, log) -> str | None:
    """LLDB breakpoint to capture sqlite3_key passphrase arg, with detailed logging."""
    script = _build_lldb_script(pid)

    try:
        with tempfile.NamedTemporaryFile(mode='w', suffix='.py', delete=False) as f:
            f.write(script)
            script_path = f.name

        result = subprocess.run(
            ["lldb", "-b", "-O", f"command script import {script_path}"],
            capture_output=True, text=True, timeout=90)
        os.unlink(script_path)

        # Parse and log output
        for line in result.stdout.splitlines():
            if line.startswith("OK:"):
                return line[3:].strip()
            elif line.startswith("BP:"):
                log(f"[macos_lldb] 断点位置数: {line[3:]}")
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
        log("[macos_lldb] error: LLDB timeout (90s)")
    except Exception as e:
        log(f"[macos_lldb] error: {e}")

    return None
