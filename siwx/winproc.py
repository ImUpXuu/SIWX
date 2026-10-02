"""跨进程只读内存访问原语（ctypes，逻辑移植自原项目已验证实现）。

只申请 PROCESS_VM_READ | PROCESS_QUERY_INFORMATION —— 无写入、无注入。
"""
import ctypes
from ctypes import wintypes

import psutil

kernel32 = ctypes.windll.kernel32

MEM_COMMIT = 0x1000
READABLE_PROTECT = {0x02, 0x04, 0x08, 0x10, 0x20, 0x40, 0x80}
MAX_USER_ADDRESS = 0x0000_8000_0000_0000
REGION_LIMIT = 500 * 1024 * 1024
PAGE_SZ = 4096
PROCESS_VM_READ = 0x0010
PROCESS_QUERY_INFORMATION = 0x0400


class MBI(ctypes.Structure):
    _fields_ = [
        ("BaseAddress", ctypes.c_uint64), ("AllocationBase", ctypes.c_uint64),
        ("AllocationProtect", wintypes.DWORD), ("_pad1", wintypes.DWORD),
        ("RegionSize", ctypes.c_uint64), ("State", wintypes.DWORD),
        ("Protect", wintypes.DWORD), ("Type", wintypes.DWORD),
        ("_pad2", wintypes.DWORD),
    ]


def open_process(pid: int):
    """返回只读句柄；失败（权限不足等）返回 None。"""
    h = kernel32.OpenProcess(PROCESS_VM_READ | PROCESS_QUERY_INFORMATION, False, pid)
    return h or None


def close_handle(h) -> None:
    if h:
        kernel32.CloseHandle(h)


def read_mem(h, addr: int, size: int):
    buf = ctypes.create_string_buffer(size)
    n = ctypes.c_size_t(0)
    if kernel32.ReadProcessMemory(h, ctypes.c_uint64(addr), buf, size, ctypes.byref(n)):
        return buf.raw[: n.value]
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
            break
        if (mbi.State == MEM_COMMIT and mbi.Protect in READABLE_PROTECT
                and 0 < mbi.RegionSize < REGION_LIMIT):
            regs.append((mbi.BaseAddress, mbi.RegionSize))
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
