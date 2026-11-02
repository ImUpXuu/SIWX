"""从 Weixin.exe 内存里的 WCDB ``Config.Cipher`` 对象提取 4.x 数据库密钥。

微信 4.x 用 WCDB 打开每个库，每个库有独立的 32 字节密钥。密钥以
``x'<64 位 hex>'`` 形式放在进程内存的配置对象里，数据块整体与一段固定
掩码异或。这里复刻"找字符串 → 找引用 → 解出配置对象 → 取数据块 → 反异或
→ 正则取 hex"这条链路，得到候选密钥后逐个库用页 1 校验确定归属。
"""

from __future__ import annotations

import re
import struct
from pathlib import Path
from typing import Iterable, Iterator, Sequence

from ..crypto.sqlcipher import WIN4, verify_key
from .memory import ProcessMemory, find_all
from .processes import find_pids

CONFIG_CIPHER_NAME = b"com.Tencent.WCDB.Config.Cipher"
CONFIG_XOR_MASK = bytes.fromhex(
    "d2c7442458020000004889442450488b"
    "450048844c2448488944254048584c24"
)
HEX_LITERAL_RE = re.compile(rb"[xX]'([0-9a-fA-F]{64,192})'")

ProgressFn = "callable[[str], None] | None"


def _xor(buf: bytes) -> bytes:
    return bytes(v ^ CONFIG_XOR_MASK[i % len(CONFIG_XOR_MASK)] for i, v in enumerate(buf))


def _search(pm: ProcessMemory, needle: bytes) -> Iterator[int]:
    for region in pm.regions():
        data = pm.read_chunked(region.base, region.size)
        if not data:
            continue
        for offset in find_all(data, needle):
            yield region.base + offset


def _candidates_from_pid(pid: int) -> list[bytes]:
    out: list[bytes] = []
    seen: set[bytes] = set()
    with ProcessMemory(pid) as pm:
        needles = list(_search(pm, CONFIG_CIPHER_NAME))
        if not needles:
            return out
        for addr in needles:
            pair = struct.pack("<QQ", addr, len(CONFIG_CIPHER_NAME))
            for qaddr in _search(pm, pair):
                node = pm.read(qaddr - 0x10, 0x50)
                if not node or len(node) < 0x40:
                    continue
                if struct.unpack_from("<Q", node, 0x10)[0] not in needles:
                    continue
                if struct.unpack_from("<Q", node, 0x18)[0] != len(CONFIG_CIPHER_NAME):
                    continue
                config_ptr = struct.unpack_from("<Q", node, 0x28)[0]
                if not (0x10000 <= config_ptr < 0x800000000000):
                    continue
                obj = pm.read(config_ptr + 0x88, 0x28)
                if not obj or len(obj) < 0x18:
                    continue
                data_ptr = struct.unpack_from("<Q", obj, 0x8)[0]
                data_len = struct.unpack_from("<Q", obj, 0x10)[0]
                if not (0 < data_len <= 1024 and 0x10000 <= data_ptr < 0x800000000000):
                    continue
                blob = pm.read(data_ptr, int(data_len))
                if not blob or len(blob) != data_len:
                    continue
                for match in HEX_LITERAL_RE.finditer(_xor(blob)):
                    run = match.group(1).decode().lower()
                    starts = [0]
                    if len(run) > 96:
                        starts += list(range(0, len(run) - 63, 32))
                        starts.append(len(run) - 64)
                    for start in dict.fromkeys(starts):
                        if start + 64 > len(run):
                            continue
                        cand = bytes.fromhex(run[start : start + 64])
                        if cand not in seen:
                            seen.add(cand)
                            out.append(cand)
    return out


def collect_candidates(
    pids: Sequence[int] | None = None, progress=None
) -> list[bytes]:
    """扫描微信进程内存，返回所有候选的 32 字节密钥。"""
    if pids is None:
        pids = find_pids("Weixin.exe", "WeChat.exe")
    found: list[bytes] = []
    for pid in pids:
        if progress:
            progress(pid)
        try:
            found.extend(_candidates_from_pid(pid))
        except OSError:
            continue
    return list(dict.fromkeys(found))


def db_files(root: Path) -> dict[str, Path]:
    """列出账号目录下所有可校验的加密库（相对 db_storage 的 posix 路径）。"""
    storage = root / "db_storage" if (root / "db_storage").is_dir() else root
    result: dict[str, Path] = {}
    for path in sorted(storage.rglob("*.db")):
        if not path.is_file():
            continue
        try:
            head = path.read_bytes()[:16]
        except OSError:
            continue
        if len(head) < 16:
            continue
        if head == b"\x00" * 16:
            continue
        result[path.relative_to(storage).as_posix()] = path
    return result


def match_keys(
    root: Path,
    candidates: Iterable[bytes],
    progress=None,
) -> dict[str, bytes]:
    """把候选密钥按页 1 校验分配到各库。返回 {相对路径: 32 字节密钥}。"""
    files = db_files(root)
    cand_list = list(candidates)
    matched: dict[str, bytes] = {}
    for i, (rel, path) in enumerate(files.items(), 1):
        for cand in cand_list:
            try:
                if verify_key(path, cand, WIN4):
                    matched[rel] = cand
                    break
            except OSError:
                break
        if progress and (i % 5 == 0 or i == len(files)):
            progress(i, len(files))
    return matched


def extract_keys(
    root: Path,
    pids: Sequence[int] | None = None,
    progress=None,
) -> dict[str, bytes]:
    """一步到位：扫内存取候选 + 逐库校验。"""
    candidates = collect_candidates(pids)
    if not candidates:
        return {}
    return match_keys(root, candidates, progress=progress)


def format_keys(keys: dict[str, bytes]) -> str:
    return "\n".join(f"{rel}\t{key.hex()}" for rel, key in keys.items())
