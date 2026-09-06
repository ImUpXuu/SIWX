"""决定性诊断：字面量 key/salt 与三个账号数据库的匹配验证。"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import re

from diag_blobs import collect_blobs
from siwx.strategies.config_cipher import CONFIG_XOR_MASK, _xor_repeat
from siwx.sqlcipher import collect_db_files, verify_enc_key
from siwx.winproc import psutil_pid_list

ACCOUNTS = [
    r"D:\xwechat_files\wxid_other_5678\db_storage",
    r"C:\Users\li\xwechat_files\wxid_demo_1234\db_storage",
    r"D:\xwechat_files\wxid_demo_1234\db_storage",
]

HEX_RE = re.compile(rb"[xX]'([0-9a-fA-F]{64,192})'")


def main():
    # 全部账号的 salt → page1
    salt_map = {}
    for acc in ACCOUNTS:
        try:
            for e in collect_db_files(acc):
                salt_map.setdefault(e.salt_hex, e.page1)
        except Exception as err:
            print(f"[跳过] {acc}: {err}")
    print(f"全部账号唯一 salt 总数: {len(salt_map)}")

    pids = psutil_pid_list()
    blobs = []
    for pid in pids:
        blobs.extend(collect_blobs(pid))
    print(f"blobs: {len(blobs)}")

    verified = {}
    tried = 0
    for bi, blob in enumerate(blobs):
        for m in HEX_RE.finditer(_xor_repeat(blob, CONFIG_XOR_MASK)):
            run = m.group(1).decode().lower()
            tried += 1
            # 变体：key 取不同 64 窗口，salt 取其余部分
            variants = []
            if len(run) >= 96:
                variants.append(("head64+salt", run[:64], run[64:96]))
                variants.append(("tail64+head_salt", run[-64:], run[:32]))
                variants.append(("mid64", run[32:96], run[0:32]))
            if len(run) == 64:
                variants.append(("pure64", run, None))
            for name, key_hex, emb in variants:
                kb = bytes.fromhex(key_hex)
                if emb and emb in salt_map:
                    if verify_enc_key(kb, salt_map[emb]):
                        verified.setdefault(key_hex, []).append((bi, name, emb))
                        print(f"  ✔ blob#{bi} {name}: salt={emb[:16]}… key={key_hex[:16]}…")
                        break
                # 无内嵌盐 / 盐不匹配 → 对全部盐试
                hits = [s for s, p1 in salt_map.items() if s not in verified and verify_enc_key(kb, p1)]
                if hits:
                    for s in hits:
                        verified.setdefault(key_hex, []).append((bi, name, s))
                    print(f"  ✔ blob#{bi} {name}: 命中 {len(hits)} 个盐 {hits[0][:16]}… key={key_hex[:16]}…")
                    break
    print(f"\n字面量总数 {tried}，验证通过的 key 数 {len(verified)}")


if __name__ == "__main__":
    main()
