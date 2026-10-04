"""提取 ctx 统一签名：已存密钥（DPAPI 密钥库，命中即秒回）。"""
from siwx import keystore
from siwx.sqlcipher import parse_key, verify_enc_key


def extract(ctx) -> int:
    store = keystore.load()
    key_map, attrib = ctx["key_map"], ctx["attrib"]
    log = ctx["log"]
    dbg = ctx.get("dbg", log)
    found = 0
    for salt, page1 in ctx["page1_by_salt"].items():
        rec = store.get(salt)
        if not rec:
            continue
        try:
            kb = parse_key(rec["key"])
        except ValueError:
            # 审计 §3.4：记录损坏静默 continue → 记录数对不上时无从排查
            dbg(f"[keystore] salt={salt[:16]}… 记录解析失败")
            continue
        if verify_enc_key(kb, page1):
            key_map[salt] = rec["key"].lower()
            attrib[salt] = "keystore"
            found += 1
    # 审计 §3.4:10-23：无条件汇总，区分"库空"与"全过期"
    log(f"[keystore] 密钥库 {len(store)} 条, 命中 {found} 个")
    return found
