"""跨进程只读内存访问原语（ctypes，逻辑移植自原项目已验证实现）。

只申请 PROCESS_VM_READ | PROCESS_QUERY_INFORMATION —— 无写入、无注入。
"""
import ctypes
from ctypes import wintypes

import psutil

kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

MEM_COMMIT = 0x1000
READABLE_PROTECT = {0x02, 0x04, 0x08, 0x10, 0x20, 0x40, 0x80}
MAX_USER_ADDRESS = 0x0000_8000_0000_0000
REGION_LIMIT = 500 * 1024 * 1024
PAGE_SZ = 4096
PROCESS_VM_READ = 0x0010
PROCESS_QUERY_INFORMATION = 0x0400

# 模块级失败计数 / 最近错误码（审计 §3.6）：本层是热路径原语，绝不逐次打日志，
# 策略层在扫描结束后读 _stats 汇总。*_last_err 为最近一次失败的 GetLastError
# 码（5=拒绝访问，87=参数错误 等），依赖上方 use_last_error=True 才有效。
# 签名兼容（核查 C.4）：open_process / read_mem 签名与返回值不变，7 个调用点无需改动。
_stats = {
    "open_fail": 0, "open_last_err": 0,
    "rpm_fail": 0, "rpm_last_err": 0,
    "enum_breaks": 0, "enum_break_addr": 0,
    "big_region_skipped": 0,
}


class MBI(ctypes.Structure):
    _fields_ = [
        ("BaseAddress", ctypes.c_uint64), ("AllocationBase", ctypes.c_uint64),
        ("AllocationProtect", wintypes.DWORD), ("_pad1", wintypes.DWORD),
        ("RegionSize", ctypes.c_uint64), ("State", wintypes.DWORD),
        ("Protect", wintypes.DWORD), ("Type", wintypes.DWORD),
        ("_pad2", wintypes.DWORD),
    ]


def open_process(pid: int):
    """返回只读句柄；失败（权限不足等）返回 None，错误码见 _stats["open_last_err"]。"""
    h = kernel32.OpenProcess(PROCESS_VM_READ | PROCESS_QUERY_INFORMATION, False, pid)
    if not h:
        _stats["open_fail"] += 1
        _stats["open_last_err"] = ctypes.get_last_error()
        return None
    return h


def close_handle(h) -> None:
    if h:
        kernel32.CloseHandle(h)


def read_mem(h, addr: int, size: int):
    buf = ctypes.create_string_buffer(size)
    n = ctypes.c_size_t(0)
    if kernel32.ReadProcessMemory(h, ctypes.c_uint64(addr), buf, size, ctypes.byref(n)):
        return buf.raw[: n.value]
    # 热路径：只计数 + 记最近错误码，由策略层结束汇总（审计 §7.5）
    _stats["rpm_fail"] += 1
    _stats["rpm_last_err"] = ctypes.get_last_error()
    return None


def read_u64(h, addr: int):
    data = read_mem(h, addr, 8)
    if data and len(data) == 8:
        return int.from_bytes(data, "little")
    return None


def enum_regions(h):
    """枚举已提交可读区域 → [(base, size)]。"""
    regs = []
    addr = 0
    mbi = MBI()
    while addr < MAX_USER_ADDRESS:
        if kernel32.VirtualQueryEx(h, ctypes.c_uint64(addr), ctypes.byref(mbi),
                                   ctypes.sizeof(mbi)) == 0:
            # 枚举提前终止（句柄失效等），与正常走完区分（审计 §3.6:63-65）
            _stats["enum_breaks"] += 1
            _stats["enum_break_addr"] = addr
            break
        if mbi.State == MEM_COMMIT and mbi.Protect in READABLE_PROTECT:
            if 0 < mbi.RegionSize < REGION_LIMIT:
                regs.append((mbi.BaseAddress, mbi.RegionSize))
            elif mbi.RegionSize >= REGION_LIMIT:
                # ≥500MB 区域被过滤（needle 恰在大堆里时扫描必然失败的线索）
                _stats["big_region_skipped"] += 1
        nxt = mbi.BaseAddress + mbi.RegionSize
        if nxt <= addr:
            break
        addr = nxt
    return regs


def iter_chunks(h, regions, chunk_size=2 * 1024 * 1024, overlap=0):
    """分块读取区域并产出 (块起始地址, 数据)，overlap 保证跨块模式可命中。"""
    for base, size in regions:
        offset = 0
        tail = b""
        tail_base = base
        while offset < size:
            cur = min(chunk_size, size - offset)
            chunk = read_mem(h, base + offset, cur) or b""
            data_base = tail_base if tail else base + offset
            data = tail + chunk
            if data:
                yield data_base, data
                if overlap:
                    tail = data[-overlap:]
                    tail_base = data_base + max(0, len(data) - len(tail))
                else:
                    tail = b""
                    tail_base = base + offset + cur
            else:
                tail = b""
                tail_base = base + offset + cur
            offset += cur


def open_wechat_handles():
    """打开全部微信进程的只读句柄，返回 [(handle, pid)]；用完需 close_handle。"""
    out = []
    for pid in psutil_pid_list():
        h = open_process(pid)
        if h:
            out.append((h, pid))
    return out


def psutil_pid_list():
    from siwx.discover import find_wechat_pids
    return find_wechat_pids()
