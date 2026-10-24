"""Linux 专用：在微信主二进制的 ELF 里定位 WCDB cipher-config 消费者。

微信 4.1+ 不再把每库的原始 SQLCipher key 以 ``x'<hex>'`` 字面量缓存在内存里，
只有登录时算出来的 32 字节 passphrase。WCDB 每建立一条数据库连接都会把
passphrase 交给 cipher-config 消费函数，所以只要能在这个函数入口读到
``rsi`` 指向的结构体，就能拿到 passphrase。

定位算法与 wxextract（boujuan/extract-wechat-messages-linux，MIT）同源：
在 ``.rodata`` 里找 ``com.Tencent.WCDB.Config.Cipher`` 锚点，顺着
``lea rsi, [rip+disp32]`` 交叉引用找到构造 config 的调用点，再从另一个
``lea rsi`` 找到真正的消费函数，回退不超过 0x500 字节对其函数序言
（``push rbp; push r15``）。

偏移是构建相关的：微信升级后用 ``find_hook_offset`` 重算即可，不需要改代码。
"""
from __future__ import annotations

import struct
from pathlib import Path

_WCDB_ANCHOR = b"com.Tencent.WCDB.Config.Cipher"
_LEA_RSI = b"\x48\x8D\x35"   # lea rsi, [rip+disp32]
_LEA_RDI = b"\x48\x8D\x3D"   # lea rdi, [rip+disp32]
_FUNC_HEAD = b"\x55\x41\x57"  # push rbp; push r15

_REQUIRED_MACHINE = 62  # EM_X86_64


class HookNotFoundError(RuntimeError):
    """二进制里找不到可识别的 WCDB cipher hook。"""


def _elf_sections(path: Path) -> dict[str, tuple[int, bytes]]:
    """返回 {section_name: (vaddr, data)}，只取 .rodata 和 .text。"""
    data = Path(path).read_bytes()
    if data[:4] != b"\x7fELF" or data[4] != 2 or data[5] != 1:
        raise HookNotFoundError(f"{path}: 不是 little-endian ELF64")
    machine, = struct.unpack_from("<H", data, 0x12)
    if machine != _REQUIRED_MACHINE:
        raise HookNotFoundError(f"{path}: 不是 x86-64（e_machine={machine}）")

    e_shoff, = struct.unpack_from("<Q", data, 0x28)
    e_shentsize, e_shnum, e_shstrndx = struct.unpack_from("<HHH", data, 0x3A)
    headers = []
    for i in range(e_shnum):
        off = e_shoff + i * e_shentsize
        headers.append(struct.unpack_from("<IIQQQQ", data, off))

    shstr_off = headers[e_shstrndx][4]
    out: dict[str, tuple[int, bytes]] = {}
    for name, _type, _flags, addr, offset, size in headers:
        end = data.index(b"\0", shstr_off + name)
        sname = data[shstr_off + name:end].decode("ascii", "replace")
        if sname in (".rodata", ".text"):
            out[sname] = (addr, data[offset:offset + size])

    if ".rodata" not in out or ".text" not in out:
        raise HookNotFoundError(f"{path}: 缺 .rodata / .text 段")
    return out


def _rip_relative_refs(text: bytes, text_va: int, opcode: bytes, target_va: int) -> list[int]:
    """找出 .text 里所有解析到 target_va 的 `opcode reg, [rip+disp32]`。"""
    hits: list[int] = []
    for off in range(len(text) - 6):
        if text[off:off + 3] != opcode:
            continue
        disp, = struct.unpack_from("<i", text, off + 3)
        if text_va + off + 7 + disp == target_va:
            hits.append(off)
    return hits


def find_hook_offset(binary: Path) -> int:
    """返回 WCDB cipher-config 消费函数相对镜像基址的偏移。"""
    sections = _elf_sections(Path(binary))
    rodata_va, rodata = sections[".rodata"]
    text_va, text = sections[".text"]

    candidates: list[int] = []
    pos = 0
    while True:
        anchor_off = rodata.find(_WCDB_ANCHOR, pos)
        if anchor_off == -1:
            break
        pos = anchor_off + 1
        anchor_va = rodata_va + anchor_off

        for ref in _rip_relative_refs(text, text_va, _LEA_RSI, anchor_va):
            # 调用点前 7 字节应是 lea rdi, [rip+disp32]（匿名 config 结构体）
            if ref < 7 or text[ref - 7:ref - 4] != _LEA_RDI:
                continue
            struct_disp, = struct.unpack_from("<i", text, ref - 4)
            struct_va = text_va + ref + struct_disp

            for consumer_ref in _rip_relative_refs(text, text_va, _LEA_RSI, struct_va):
                lo = max(0, consumer_ref - 0x500)
                for back in range(consumer_ref, lo - 1, -1):
                    if text[back:back + 3] == _FUNC_HEAD:
                        va = text_va + back
                        if va not in candidates:
                            candidates.append(va)
                        break

    if not candidates:
        raise HookNotFoundError(
            f"{binary}: 找不到 WCDB cipher-config 函数"
            f"（锚点 {_WCDB_ANCHOR!r} 的交叉引用链断了）"
        )
    return sorted(candidates)[0]
