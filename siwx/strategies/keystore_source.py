"""提取 ctx 统一签名：已存密钥（DPAPI 密钥库，命中即秒回）。"""
from siwx import keystore
from siwx.sqlcipher import parse_key, verify_enc_key


def extract(ctx) -> int:
    store = keystore.load()
    key_map, attrib = ctx["key_map"], ctx["attrib"]
    found = 0
    for salt, page1 in ctx["page1_by_salt"].items():
        rec = store.get(salt)
        if not rec:
            continue
        try:
            kb = parse_key(rec["key"])
        except ValueError:
            continue
        if verify_enc_key(kb, page1):
            key_map[salt] = rec["key"].lower()
            attrib[salt] = "keystore"
            found += 1
    if found:
        ctx["log"](f"[keystore] 本机密钥库命中 {found} 个")
    return found
