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
        subprocess.run(["lldb", "--version"], capture_output=True, check=True)
    except (FileNotFoundError, subprocess.CalledProcessError):
        log("[macos_lldb] lldb not installed, skip")
        return 0

    try:
        result = subprocess.run(
            ["pgrep", "-x", "WeChat"],
            capture_output=True, text=True)
        pids = [int(p) for p in result.stdout.split() if p.strip().isdigit()]
    except Exception:
        pids = []

    if not pids:
        log("[macos_lldb] WeChat process not found")
        return 0

    log(f"[macos_lldb] WeChat PIDs: {pids}")

    entry = len(key_map)
    for pid in pids:
        if len(key_map) >= len(page1_by_salt):
            break
        log(f"[macos_lldb] PID={pid}: starting LLDB breakpoint capture...")
        passphrase = _capture_passphrase_via_lldb(pid)
        if not passphrase:
            log(f"[macos_lldb] PID={pid}: no passphrase captured")
            continue
        log(f"[macos_lldb] PID={pid}: captured passphrase {passphrase[:16]}...")

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
                log(f"  [macos_lldb] salt={salt_hex[:16]}... verified")
                key_map[salt_hex] = key_hex
                attrib[salt_hex] = "macos_lldb"
            if len(key_map) >= len(page1_by_salt):
                break

    found = len(key_map) - entry
    if found:
        log(f"[macos_lldb] done: +{found}")
    return found


def _capture_passphrase_via_lldb(pid: int) -> str | None:
    """Use LLDB script to capture sqlite3_key passphrase argument."""
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
                pKey_reg = frame.GetRegisters().GetRegisterAtIndex(4)
                nKey_reg = frame.GetRegisters().GetRegisterAtIndex(5)
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
            ["lldb", "-b", "-o",
             f"script import sys; sys.path.insert(0, '{script_path}'); "
             f"exec(open('{script_path}').read())"],
            capture_output=True, text=True, timeout=45)
        os.unlink(script_path)

        for line in result.stdout.splitlines():
            if line.startswith("OK:"):
                return line[3:].strip()
    except Exception:
        pass

    return None
