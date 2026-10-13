"""macOS 上把 wxgf 转码为可预览格式（实测输出 HEIC）。

## 背景

wxgf 是微信自研的图片容器（不止用于动画表情，实测普通静态照片也会存成
wxgf），Windows 版靠微信安装目录下的 VoipEngine.dll（ctypes.WinDLL 直接
加载调用）转码。macOS 版没有对应的独立 DLL：承载同一套 wxam_dec_* 符号
的 MultiMediaDyn.framework 本身只是一组"跳转表"桩函数——每个导出符号的
机器码都是 `adrp/ldr/br`：从 `__DATA,__bss`（文件里零字节、纯运行时内容）
里的一个表项取真正的函数地址再跳过去。这张表只在微信自身进程把该
framework 实际初始化后才会被真正填入；脱离微信进程单独 dlopen 这个库，
表项是全零，跳转地址是 0x0，调用即崩溃——这不是调用约定猜错，是刻意的
反篡改设计（把真正实现放进运行时才解密/生成的匿名可执行内存，静态文件
和脱离宿主进程的直接调用都拿不到）。

## 实现方式

因此 macOS 上转码只能在微信自身进程**内部**完成：用 LLDB 附加到正在运行
的微信，在其地址空间里：

1. 找到 MultiMediaDyn 模块、解析导出符号 `wxam_dec_wxam2pic_5`（桩函数）；
2. 动态反汇编该桩函数的 `adrp`+`ldr` 两条指令算出跳转表项地址，读出表项
   值——这是桩函数当前真正跳向的"外层 wrapper"实现地址（运行时决定，
   每次进程重启都可能变，必须动态算，不能硬编码一个具体版本观测到的
   绝对地址）；
3. 外层 wrapper 本身还是个 9 行左右的转发桩（把调用方传入的参数重新打包
   后 `bl` 到真正的内层实现），动态扫描它的头 32 条指令找第一条 `bl`，
   解码其 26 位带符号立即数得到内层实现（真正做转码的函数）地址；
4. 用 LLDB 表达式求值机制在微信进程里 `malloc` 输入/输出/配置缓冲区、
   写入 wxgf 字节、调用内层实现、读回输出——该函数签名是实测反汇编
   （Ghidra 反编译确认）得到的 9 参数形式，不是 Windows 版 VoipEngine.dll
   对外文档假设的 5 参数形式，两者完全不兼容，不能照搬 Windows 侧的
   ctypes 签名。

内层实现的 mode=4 配置路径实测对聊天图片/朋友圈图片（而不仅仅是传统意义
的"动画表情"）稳定产出合法 HEIC（ftyp/heic/mif1heic 签名，macOS 原生可
显示）。

## 前提条件（与 macOS 密钥提取相同量级的限制）

- 微信必须正在运行（表项只在其运行时被填入）；
- SIP 必须关闭（LLDB attach 到其它进程的系统限制，与
  MACOS_SUPPORT.md 记录的密钥提取前提完全一致）；
- 这两条任一不满足，本模块的转码会优雅失败（返回 None / 空结果），
  调用方应回退到"已解密但当前平台暂不支持预览"的现有提示，而不是
  让整条图片服务链路跟着报错。

（实现吸收自上游 PR #28，作者已在 Apple Silicon 真机端到端验证。）
"""
import inspect
import json
import os
import subprocess
import tempfile
import threading
from pathlib import Path

from siwx.strategies.macos_lldb import _lldb_cmd_prefix

# 并发防护：siwx 自身是单进程 Flask（werkzeug 开发服务器默认多线程处理
# 请求），一个聊天页同时懒加载多张 wxgf 缩略图会对 /api/chat/media/image
# 发起多个并发请求；若不加锁，每个请求各自 spawn 一个 lldb 子进程去
# attach 同一个正在运行的微信进程——多个调试器并发 attach/操作同一个目标
# 进程既浪费（重复的符号解析开销），也有真实风险（并发状态下谁先谁后
# 不确定，更容易撞上未知的边界情况）。全局串行化成"一次只有一个 LLDB
# 会话在跟微信打交道"，牺牲一点并发吞吐换来确定性和对微信进程的最小
# 打扰；单张图片本身的转码开销只有不到 1 秒，串行排队可接受。
_LLDB_LOCK = threading.Lock()
_LOCK_WAIT_TIMEOUT = 45  # 排队等锁的上限；超时宁可报失败也不无限阻塞请求线程


# ── ARM64 指令解码：ADRP / LDR(立即数,无符号偏移,64位) / BL ──────────
#
# 这三个函数既是本模块自己的纯函数（可直接单测，见
# tests/test_media_wxgf_macos.py），也会通过 inspect.getsource() 原样
# 注入进下面的 LLDB 内嵌脚本里执行——LLDB 跑的是它自带的独立 Python
# 解释器（不在我们的 venv 里，import siwx 不可行），只有这一份源码，
# 避免手动维护两份逻辑导致漂移。各自的编码细节见 ARM64 ISA 手册：
#   ADRP:  bit31=op(1)  bits30:29=immlo  bits28:24=10000  bits23:5=immhi  bits4:0=Rd
#   LDR:   bits31:24=0xF9(size=64位+111+V=0+01)  bits23:22=01(opc)  bits21:10=imm12(×8)
#   BL:    bits31:26=100101  bits25:0=imm26（带符号，×4）
def _adrp_target(insn, pc):
    if (insn >> 31) & 1 != 1 or (insn >> 24) & 0x1f != 0x10:
        return None
    immlo = (insn >> 29) & 0b11
    immhi = (insn >> 5) & 0x7ffff
    imm = (immhi << 2) | immlo
    if imm & (1 << 20):
        imm -= (1 << 21)
    return (pc & ~0xfff) + (imm << 12)


def _ldr_imm_offset(insn):
    if (insn >> 24) & 0xff != 0xf9 or (insn >> 22) & 0b11 != 0b01:
        return None
    imm12 = (insn >> 10) & 0xfff
    return imm12 * 8


def _bl_target(insn, pc):
    if (insn >> 26) != 0b100101:
        return None
    imm26 = insn & 0x3ffffff
    if imm26 & (1 << 25):
        imm26 -= (1 << 26)
    return pc + (imm26 << 2)


_DECODER_SOURCE = "\n".join(
    inspect.getsource(fn) for fn in (_adrp_target, _ldr_imm_offset, _bl_target))

_SCRIPT_TMPL = r"""
import lldb, sys, struct

PID = {pid}
MANIFEST_PATH = {manifest_path!r}

{decoder_funcs}

def main():
    debugger = lldb.SBDebugger.Create()
    debugger.SetAsync(False)
    target = debugger.CreateTarget("")
    error = lldb.SBError()
    process = target.AttachToProcessWithID(debugger.GetListener(), PID, error)
    if error.Fail() or not process.IsValid():
        print("FAIL:Attach:" + str(error))
        return
    process.Stop()

    mm_module = None
    for m in target.modules:
        fn = m.GetFileSpec().GetFilename() or ""
        if "MultiMediaDyn" in fn:
            mm_module = m
            break
    if mm_module is None:
        print("FAIL:NoMultiMediaDynModule")
        process.Detach()
        return

    syms = mm_module.FindSymbols("wxam_dec_wxam2pic_5")
    stub_addr = None
    for i in range(syms.GetSize()):
        sym = syms.GetContextAtIndex(i).symbol
        if sym and sym.IsValid():
            a = sym.GetStartAddress().GetLoadAddress(target)
            if a and a != lldb.LLDB_INVALID_ADDRESS:
                stub_addr = a
                break
    if stub_addr is None:
        print("FAIL:NoStubSymbol")
        process.Detach()
        return

    def read_u32(addr):
        err = lldb.SBError()
        data = process.ReadMemory(addr, 4, err)
        if err.Fail() or not data or len(data) != 4:
            return None
        return struct.unpack("<I", data)[0]

    def read_u64(addr):
        err = lldb.SBError()
        data = process.ReadMemory(addr, 8, err)
        if err.Fail() or not data or len(data) != 8:
            return None
        return struct.unpack("<Q", data)[0]

    insn0 = read_u32(stub_addr)
    insn1 = read_u32(stub_addr + 4)
    if insn0 is None or insn1 is None:
        print("FAIL:StubUnreadable")
        process.Detach()
        return
    page = _adrp_target(insn0, stub_addr)
    off = _ldr_imm_offset(insn1)
    if page is None or off is None:
        print("FAIL:StubDecodeMismatch adrp=%s ldr=%s" % (hex(insn0), hex(insn1)))
        process.Detach()
        return
    table_addr = page + off
    wrapper_addr = read_u64(table_addr)
    if not wrapper_addr:
        print("FAIL:TableSlotEmpty (WeChat 可能未完成初始化，或版本内部布局已变)")
        process.Detach()
        return

    inner_addr = None
    for i in range(32):
        a = wrapper_addr + i * 4
        insn = read_u32(a)
        if insn is None:
            break
        t = _bl_target(insn, a)
        if t is not None:
            inner_addr = t
            break
    if inner_addr is None:
        print("FAIL:NoBlFoundInWrapper")
        process.Detach()
        return
    print("RESOLVED:wrapper=0x%x inner=0x%x" % (wrapper_addr, inner_addr))

    frame = process.GetSelectedThread().GetFrameAtIndex(0)

    def ev(expr):
        v = frame.EvaluateExpression(expr)
        err = v.GetError()
        if err.Fail():
            return None, str(err)
        return v.GetValueAsUnsigned(), None

    def alloc(n):
        v, e = ev("(long)malloc(%d)" % n)
        return v

    def call9(addr, a0, a1, a2, a3, a4, a5, a6, a7, a8):
        expr = ("((long(*)(long,long,long,long,long,long,long,long,long))0x%x)"
                "(%d,%d,%d,%d,%d,%d,%d,%d,%d)") % (addr, a0, a1, a2, a3, a4, a5, a6, a7, a8)
        return ev(expr)

    # 复用缓冲区：逐张 malloc 的表达式求值 JIT 开销很大，批量转码时应只分配一次
    IN_CAP = 8 * 1024 * 1024
    OUT_CAP = 16 * 1024 * 1024
    in_ptr = alloc(IN_CAP)
    out_ptr = alloc(OUT_CAP)
    outlen_ptr = alloc(8)
    cfg_ptr = alloc(64)
    if not (in_ptr and out_ptr and outlen_ptr and cfg_ptr):
        print("FAIL:AllocFailed")
        process.Detach()
        return

    werr = lldb.SBError()
    process.WriteMemory(cfg_ptr, (4).to_bytes(4, "little") + bytes(60), werr)

    manifest = json.load(open(MANIFEST_PATH))
    results = []
    for item in manifest:
        in_path = item["in"]
        out_path = item["out"]
        try:
            data = open(in_path, "rb").read()
        except Exception as e:
            results.append({{"in": in_path, "ok": False, "reason": "read:%s" % e}})
            continue
        if len(data) > IN_CAP:
            results.append({{"in": in_path, "ok": False, "reason": "too_large"}})
            continue
        werr = lldb.SBError()
        process.WriteMemory(in_ptr, data, werr)
        if werr.Fail():
            results.append({{"in": in_path, "ok": False, "reason": "write_fail:%s" % werr}})
            continue
        process.WriteMemory(outlen_ptr, (OUT_CAP).to_bytes(8, "little"), lldb.SBError())
        r, e = call9(inner_addr, in_ptr, len(data), out_ptr, outlen_ptr, cfg_ptr, 0, 0, 0, 0)
        if r != 0:
            results.append({{"in": in_path, "ok": False, "reason": "ret=%s err=%s" % (r, e)}})
            continue
        lerr = lldb.SBError()
        outlen_bytes = process.ReadMemory(outlen_ptr, 8, lerr)
        outlen = struct.unpack("<Q", outlen_bytes)[0] if not lerr.Fail() else 0
        if outlen <= 0 or outlen > OUT_CAP:
            results.append({{"in": in_path, "ok": False, "reason": "bad_outlen:%d" % outlen}})
            continue
        oerr = lldb.SBError()
        body = process.ReadMemory(out_ptr, outlen, oerr)
        if oerr.Fail() or not body:
            results.append({{"in": in_path, "ok": False, "reason": "read_output_fail:%s" % oerr}})
            continue
        try:
            Path(out_path).parent.mkdir(parents=True, exist_ok=True)
            open(out_path, "wb").write(body)
        except Exception as e:
            results.append({{"in": in_path, "ok": False, "reason": "write_out:%s" % e}})
            continue
        results.append({{"in": in_path, "ok": True, "bytes": len(body)}})

    json.dump(results, open(MANIFEST_PATH + ".result", "w"))
    print("DONE:%d" % len(results))
    try:
        process.Detach()
    except Exception:
        pass

try:
    from pathlib import Path
    import json
    main()
except Exception as e:
    import traceback
    traceback.print_exc(file=sys.stdout)
    print("FAIL:ScriptError:%s: %s" % (type(e).__name__, e))
"""


def _run_lldb_batch(pid: int, items: list, log=print, timeout: int = 180):
    """items: [(in_path, out_path), ...]。返回 {in_path: {"ok": bool, ...}}。

    全程持有 _LLDB_LOCK：同一时刻只允许一个 LLDB 会话跟微信进程打交道。
    等锁超过 _LOCK_WAIT_TIMEOUT 秒就放弃，给每个 item 一个明确的
    server_busy 失败原因，而不是让请求线程无限期卡住。
    """
    if not items:
        return {}
    acquired = _LLDB_LOCK.acquire(timeout=_LOCK_WAIT_TIMEOUT)
    if not acquired:
        log(f"[media_wxgf_macos] 等待 LLDB 会话超过 {_LOCK_WAIT_TIMEOUT}s，放弃（可能有并发请求排队）")
        return {str(a): {"ok": False, "reason": "server_busy_lldb_lock_timeout"}
                for a, _b in items}
    try:
        manifest = [{"in": str(a), "out": str(b)} for a, b in items]
        with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False) as mf:
            json.dump(manifest, mf)
            manifest_path = mf.name
        script = _SCRIPT_TMPL.format(pid=pid, manifest_path=manifest_path,
                                     decoder_funcs=_DECODER_SOURCE)
        with tempfile.NamedTemporaryFile(mode="w", suffix=".py", delete=False) as sf:
            sf.write(script)
            script_path = sf.name

        out = {}
        try:
            proc = subprocess.run(
                _lldb_cmd_prefix(log) + ["lldb", "-b", "-o",
                                         f"command script import {script_path}"],
                capture_output=True, text=True, timeout=timeout)
            for line in proc.stdout.splitlines():
                if line.startswith(("RESOLVED:", "FAIL:", "DONE:")):
                    log(f"[media_wxgf_macos] {line}")
                elif line.startswith(("Traceback", "  File")):
                    log(f"[media_wxgf_macos] 脚本异常: {line}")
            if proc.stderr:
                err = proc.stderr.strip()[:2000]
                if err:
                    log(f"[media_wxgf_macos] stderr: {err}")
            result_path = manifest_path + ".result"
            if os.path.exists(result_path):
                results = json.load(open(result_path))
                for r in results:
                    out[r["in"]] = r
                os.unlink(result_path)
        except subprocess.TimeoutExpired:
            log(f"[media_wxgf_macos] error: LLDB timeout ({timeout}s)")
        except Exception as e:
            log(f"[media_wxgf_macos] error: {e}")
        finally:
            for p in (manifest_path, script_path):
                try:
                    os.unlink(p)
                except OSError:
                    pass
        return out
    finally:
        _LLDB_LOCK.release()


def _heic_to_jpeg(heic_bytes: bytes, log=print) -> bytes | None:
    """HEIC → JPEG，供浏览器端展示用。

    与 wxgf→HEIC 那一步完全不同：这一步不需要微信进程、不需要 LLDB，
    只是调用 macOS 自带的 sips，任何时候都能跑，不受"微信是否在运行"
    "SIP 是否关闭"影响。单独转这一步是因为 HEIC 在浏览器里的原生支持
    并不可靠——Safari 能直接显示，Chrome/Firefox 的 <img> 普遍不支持。
    """
    with tempfile.NamedTemporaryFile(suffix=".heic", delete=False) as inf:
        inf.write(heic_bytes)
        in_path = inf.name
    out_path = in_path + ".jpg"
    try:
        proc = subprocess.run(
            ["sips", "-s", "format", "jpeg", in_path, "--out", out_path],
            capture_output=True, text=True, timeout=20)
        if proc.returncode != 0 or not os.path.exists(out_path):
            log(f"[media_wxgf_macos] sips 转 JPEG 失败: {proc.stderr.strip()[:500]}")
            return None
        return Path(out_path).read_bytes()
    except Exception as e:
        log(f"[media_wxgf_macos] sips 转 JPEG 异常: {e}")
        return None
    finally:
        for p in (in_path, out_path):
            try:
                os.unlink(p)
            except OSError:
                pass


def convert_wxgf_for_web(data: bytes, log=print) -> tuple[bytes, str] | None:
    """wxgf → (JPEG 字节, "image/jpeg")，供 media.py 按需解密路径调用。

    内部先 wxgf→HEIC（需要活的微信进程 + LLDB），再 HEIC→JPEG（纯本地
    sips，不依赖微信）。任一步失败都返回 None，调用方据此回退到
    "已解密但当前平台暂不支持预览"的现有提示。
    """
    heic = convert_wxgf_macos(data, log=log)
    if heic is None:
        return None
    jpeg = _heic_to_jpeg(heic, log=log)
    if jpeg is None:
        return None
    return jpeg, "image/jpeg"


def find_wechat_pid():
    """复用 discover.find_wechat_pids() 里同一套查找逻辑，取第一个。"""
    from siwx.discover import find_wechat_pids
    pids = find_wechat_pids()
    return pids[0] if pids else None


def convert_wxgf_macos(data: bytes, log=print) -> bytes | None:
    """单张 wxgf → HEIC（供 media.py 按需解密路径调用）。

    失败（微信未运行 / SIP 未关闭 / 该版本内部布局不匹配等）一律返回
    None，调用方据此回退到"已解密但当前平台暂不支持预览"的现有提示。
    """
    pid = find_wechat_pid()
    if pid is None:
        log("[media_wxgf_macos] 未检测到微信进程，跳过转码")
        return None
    with tempfile.NamedTemporaryFile(suffix=".wxgf", delete=False) as inf:
        inf.write(data)
        in_path = inf.name
    out_path = in_path + ".out"
    try:
        results = _run_lldb_batch(pid, [(in_path, out_path)], log=log, timeout=60)
        r = results.get(in_path)
        if r and r.get("ok") and os.path.exists(out_path):
            return Path(out_path).read_bytes()
        if r:
            log(f"[media_wxgf_macos] 转码失败: {r.get('reason')}")
        return None
    finally:
        for p in (in_path, out_path):
            try:
                os.unlink(p)
            except OSError:
                pass


def convert_wxgf_batch(pairs: list, log=print, batch_size: int = 200) -> dict:
    """批量 wxgf → HEIC（供 media_backup.py 对已备份的 .wxgf 文件重新转码）。

    pairs: [(Path(wxgf文件), Path(目标heic路径)), ...]
    一次 LLDB attach 处理一批（batch_size 张），避免每张都单独 attach/detach
    的开销；返回 {str(in_path): {"ok": bool, "reason"/"bytes": ...}}。
    """
    if not pairs:
        return {}
    pid = find_wechat_pid()
    if pid is None:
        log("[media_wxgf_macos] 未检测到微信进程，跳过批量转码")
        return {str(a): {"ok": False, "reason": "no_wechat_process"} for a, _ in pairs}

    out = {}
    for i in range(0, len(pairs), batch_size):
        chunk = pairs[i:i + batch_size]
        log(f"[media_wxgf_macos] 转码批次 {i + 1}-{i + len(chunk)}/{len(pairs)}")
        res = _run_lldb_batch(pid, chunk, log=log,
                              timeout=max(60, len(chunk) * 2))
        out.update(res)
    return out
