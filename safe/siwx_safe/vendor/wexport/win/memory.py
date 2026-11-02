"""读取进程内存（OpenProcess + VirtualQueryEx + ReadProcessMemory）。"""

from __future__ import annotations

import ctypes
from ctypes import wintypes
from dataclasses import dataclass
from typing import Iterator

PROCESS_QUERY_INFORMATION = 0x0400
PROCESS_VM_READ = 0x0010
MEM_COMMIT = 0x1000
PAGE_NOACCESS = 0x01
PAGE_GUARD = 0x100
MEM_IMAGE = 0x1000000

kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
kernel32.OpenProcess.restype = wintypes.HANDLE
kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
kernel32.VirtualQueryEx.argtypes = [
    wintypes.HANDLE,
    ctypes.c_void_p,
    ctypes.c_void_p,
    ctypes.c_size_t,
]
kernel32.VirtualQueryEx.restype = ctypes.c_size_t
kernel32.ReadProcessMemory.argtypes = [
    wintypes.HANDLE,
    ctypes.c_void_p,
    ctypes.c_void_p,
    ctypes.c_size_t,
    ctypes.POINTER(ctypes.c_size_t),
]
kernel32.ReadProcessMemory.restype = wintypes.BOOL


class MEMORY_BASIC_INFORMATION64(ctypes.Structure):
    _fields_ = [
        ("BaseAddress", ctypes.c_ulonglong),
        ("AllocationBase", ctypes.c_ulonglong),
        ("AllocationProtect", wintypes.DWORD),
        ("PartitionId", wintypes.WORD),
        ("RegionSize", ctypes.c_ulonglong),
        ("State", wintypes.DWORD),
        ("Protect", wintypes.DWORD),
        ("Type", wintypes.DWORD),
    ]


MEM_PRIVATE = 0x20000
MEM_MAPPED = 0x40000
MEM_IMAGE = 0x1000000


@dataclass(frozen=True)
class Region:
    base: int
    size: int
    protect: int
    type: int = 0

    @property
    def end(self) -> int:
        return self.base + self.size


class ProcessMemory:
    """一个进程句柄上的读取操作。"""

    def __init__(self, pid: int):
        self.pid = pid
        self.handle = kernel32.OpenProcess(
            PROCESS_QUERY_INFORMATION | PROCESS_VM_READ, False, pid
        )
        if not self.handle:
            raise OSError(ctypes.get_last_error(), f"OpenProcess({pid}) 失败")

    def close(self) -> None:
        if self.handle:
            kernel32.CloseHandle(self.handle)
            self.handle = None

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()
        return False

    def regions(self) -> Iterator[Region]:
        """遍历所有已提交、可读的虚拟内存区域。"""
        address = 0
        limit = 0x7FFF_FFFF_FFFF
        while address < limit:
            mbi = MEMORY_BASIC_INFORMATION64()
            got = kernel32.VirtualQueryEx(
                self.handle,
                ctypes.c_void_p(address),
                ctypes.byref(mbi),
                ctypes.sizeof(mbi),
            )
            if not got:
                break
            base = int(mbi.BaseAddress)
            size = int(mbi.RegionSize)
            if size == 0:
                break
            protect = int(mbi.Protect)
            if (
                int(mbi.State) == MEM_COMMIT
                and not (protect & PAGE_NOACCESS)
                and not (protect & PAGE_GUARD)
            ):
                yield Region(base, size, protect, int(mbi.Type))
            address = base + size

    def read(self, base: int, size: int) -> bytes | None:
        """读取一段内存；失败返回 None。"""
        if size <= 0:
            return b""
        buf = ctypes.create_string_buffer(size)
        got = ctypes.c_size_t(0)
        ok = kernel32.ReadProcessMemory(
            self.handle,
            ctypes.c_void_p(base),
            ctypes.cast(buf, ctypes.c_void_p),
            size,
            ctypes.byref(got),
        )
        if not ok:
            return None
        return buf.raw[: got.value]

    def read_chunked(self, base: int, size: int, chunk: int = 4 << 20) -> bytes:
        """分块读取，跳过不可读的块（返回已读部分拼接）。"""
        parts: list[bytes] = []
        pos = base
        end = base + size
        while pos < end:
            n = min(chunk, end - pos)
            data = self.read(pos, n)
            if data:
                parts.append(data)
            pos += n
        return b"".join(parts)


def find_all(haystack: bytes, needle: bytes) -> Iterator[int]:
    start = 0
    while True:
        idx = haystack.find(needle, start)
        if idx < 0:
            return
        yield idx
        start = idx + 1
