"""编排器：全局收割 → 统一验证 → salt 索引密钥库 → 并行解密（带缓存）。

两层缓存（"不要每次都重新跑"）：
  1. 密钥缓存：DPAPI 密钥库能覆盖全部账号全部 salt 时，跳过内存扫描；
  2. 解密缓存：输出目录 .siwx_cache.json 记录 (mtime,size)，源库未变直接命中。

全局收割：微信内存只扫一次，用全部账号 salt 联合集验证，再分发给各账号。
日志脱敏：只输出 salt 与打码密钥，永不出明文。
"""
import time
from pathlib import Path

from siwx import logger as log
from siwx import keystore
from siwx.discover import find_wechat_data_dirs, wxid_of
from siwx.pool import decrypt_parallel, load_manifest, save_manifest
from siwx.sqlcipher import collect_db_files, decrypt_database, parse_key, verify_enc_key
from siwx.strategies import run_strategies


def mask_key(k: str) -> str:
    if len(k) <= 10:
        return "…"
    return f"{k[:6]}…{k[-4:]}"


def _round_mb(n: float) -> float:
    return round(n / 1048576.0, 1)


def _discover(log_fn):
    """发现微信数据目录。"""
    dirs = find_wechat_data_dirs()
    if not dirs:
        log_fn("✗ 未找到微信数据目录 (xwechat_files/*/db_storage)")
        log.detailed("discover", "搜索路径: USERPROFILE/Documents/xwechat_files, USERPROFILE/xwechat_files, A-Z:/xwechat_files")
    else:
        log_fn(f"自动扫描到 {len(dirs)} 个微信账号")
        for wxid, db_dir in dirs:
            log.detailed("discover", f"账号={wxid}, 路径={db_dir}")
    return dirs
    dirs = find_wechat_data_dirs()
    if not dirs:
        log("✗ 未找到微信数据目录 (xwechat_files/*/db_storage)")
    else:
        log(f"自动扫描到 {len(dirs)} 个微信账号")
    return dirs


def _keystore_preset(entries_by_dir, log) -> dict:
    """检查密钥库覆盖率：全部命中返回全量 preset，否则返回 None（需收割）。"""
    store = keystore.load()
    if not store:
        return None
    total = covered = 0
    for entries in entries_by_dir.values():
        for e in entries:
            total += 1
            rec = store.get(e.salt_hex)
            if rec:
                try:
                    if verify_enc_key(parse_key(rec["key"]), e.page1):
                        covered += 1
                        continue
                except ValueError:
                    pass
    if total and covered == total:
        log(f"[keystore] 密钥缓存全覆盖 ({covered}/{total})，跳过内存扫描")
        return {s: r["key"] for s, r in store.items()}
    log(f"[keystore] 密钥缓存覆盖 {covered}/{total}，需收割缺失部分")
    return None


def global_harvest(dirs, entries_by_dir, log=print, only_missing=None):
    """一次内存扫描，用 salt 联合集验证 → (global_key_map, global_attrib)。

    only_missing: None=验证全部；set=只针对缺失的 salt（收割补漏）。
    """
    from siwx.strategies import config_cipher

    page1_by_salt = {}
    for entries in entries_by_dir.values():
        for e in entries:
            if only_missing is None or e.salt_hex in only_missing:
                page1_by_salt.setdefault(e.salt_hex, e.page1)
    if not page1_by_salt:
        return {}, {}

    log(f"[harvest] 收割目标 {len(page1_by_salt)} 个 salt，一次内存扫描联合验证")
    key_map, attrib = {}, {}
    ctx = {
        "db_dir": "", "entries": [], "page1_by_salt": page1_by_salt,
        "key_map": key_map, "attrib": attrib, "log": log,
    }
    config_cipher.extract(ctx)
    if key_map:
        log(f"[harvest] 收割完成: {len(key_map)} 个新密钥验证通过")
    return key_map, attrib


def extract_keys_for_dir(db_dir: str, log=print, preset=None,
                         entries=None, use_memory=True) -> dict:
    """对单个账号执行提取。preset = 全局收割/密钥库的 {salt: key}。"""
    t0 = time.time()
    wxid = wxid_of(db_dir)
    log(f"── 账号 {wxid} ──")

    if entries is None:
        entries = collect_db_files(db_dir)
    log(f"[extract] 收集到 {len(entries)} 个数据库文件")
    page1_by_salt, salt_to_dbs = {}, {}
    for e in entries:
        page1_by_salt.setdefault(e.salt_hex, e.page1)
        salt_to_dbs.setdefault(e.salt_hex, []).append(e.rel)
    log(f"[extract] 唯一 salt 数: {len(page1_by_salt)}")
    for salt_hex, dbs in salt_to_dbs.items():
        log.detailed("extract", f"salt={salt_hex[:16]}... 关联{len(dbs)}个库: {', '.join(dbs[:3])}{'...' if len(dbs)>3 else ''}")

    key_map, attrib = {}, {}
    cached_keys = 0

    # 0) 预置密钥（全局收割 / 密钥库缓存），逐个 HMAC 复核
    preset_miss = 0
    for salt, key in (preset or {}).items():
        if salt in page1_by_salt and salt not in key_map:
            try:
                kb = parse_key(key)
            except ValueError:
                log.detailed("extract", f"预置密钥解析失败 salt={salt[:16]}...")
                continue
            if verify_enc_key(kb, page1_by_salt[salt]):
                key_map[salt] = key.lower()
                attrib[salt] = "缓存"
                cached_keys += 1
            else:
                preset_miss += 1
                log.detailed("extract", f"预置密钥HMAC失败 salt={salt[:16]}...")
    if cached_keys:
        log(f"[keystore] 缓存命中 {cached_keys} 个")
    if preset_miss:
        log(f"[extract] 预置密钥未命中 {preset_miss} 个")
    log.detailed("extract", f"预置密钥处理完成: 命中{cached_keys}, 未命中{preset_miss}, 待验证{len(page1_by_salt)-len(key_map)}")

    ctx = {
        "db_dir": str(db_dir), "entries": entries,
        "page1_by_salt": page1_by_salt,
        "key_map": key_map, "attrib": attrib, "log": log,
        "use_memory": use_memory,
    }
    run_strategies(ctx)
    log.detailed("extract", f"策略链执行后: 已验证{len(key_map)}/{len(page1_by_salt)}")

    # 交叉验证：已知密钥复测缺失 salt
    cross_ok = 0
    for salt, page1 in page1_by_salt.items():
        if salt in key_map:
            continue
        for k in set(key_map.values()):
            try:
                kb = parse_key(k)
            except ValueError:
                continue
            if verify_enc_key(kb, page1):
                log(f"  [交叉验证] salt={salt[:16]}… 复用已知密钥")
                key_map[salt] = k
                attrib[salt] = "交叉验证"
                cross_ok += 1
                break
    if cross_ok:
        log(f"[extract] 交叉验证命中 {cross_ok} 个")

    if key_map:
        store = keystore.load()
        for salt, key in key_map.items():
            keystore.insert(store, salt, key, attrib.get(salt, "extract"))
        keystore.save(store)
        log("[keystore] 密钥已保存到 DPAPI 加密密钥库")

    salts = []
    for salt in sorted(salt_to_dbs, key=lambda s: (s not in key_map, s)):
        key = key_map.get(salt)
        salts.append({
            "salt": salt, "dbs": salt_to_dbs[salt],
            "verified": key is not None,
            "strategy": attrib.get(salt),
            "key_masked": mask_key(key) if key else None,
        })

    report = {
        "wxid": wxid, "db_dir": str(db_dir), "db_count": len(entries),
        "total_salts": len(page1_by_salt), "verified": len(key_map),
        "cached": cached_keys,
        "duration_ms": int((time.time() - t0) * 1000),
        "salts": salts,
    }
    log(f"提取完成: {report['verified']}/{report['total_salts']} salt 已验证 "
        f"(耗时 {report['duration_ms']} ms)")
    return report


def decrypt_dir(db_dir: str, out_dir: str, log=print, entries=None,
                workers=None, use_cache=True) -> dict:
    """并行解密 + 产物缓存：源库 (mtime,size) 未变直接命中，秒回。"""
    t0 = time.time()
    wxid = wxid_of(db_dir)
    store = keystore.load()
    if entries is None:
        entries = collect_db_files(db_dir)
    uniq = list(dict.fromkeys(keystore.unique_keys(store)))
    out_root = Path(out_dir)
    log(f"── 解密 {wxid}: {len(entries)} 个数据库 → {out_root} ──")
    log(f"[decrypt] 密钥库: {len(store)} 条, 唯一密钥: {len(uniq)} 个")

    manifest = load_manifest(out_root) if use_cache else {}
    files, tasks = [], []
    ok = failed = skipped = cached = 0

    def _resolve_key(e):
        rec = store.get(e.salt_hex)
        if rec:
            try:
                kb = parse_key(rec["key"])
                if verify_enc_key(kb, e.page1):
                    return rec["key"]
            except ValueError:
                pass
        for k in uniq:
            try:
                kb = parse_key(k)
            except ValueError:
                continue
            if verify_enc_key(kb, e.page1):
                return k
        return None

    for e in entries:
        key_hex = _resolve_key(e)
        if key_hex is None:
            log(f"  [decrypt] 跳过 {e.rel} (salt={e.salt_hex[:16]}… 无密钥)")
            files.append({"rel": e.rel, "size_mb": _round_mb(e.size), "pages": 0,
                          "status": "skipped", "key_masked": ""})
            skipped += 1
            continue
        try:
            mtime = int(e.path.stat().st_mtime)
        except OSError:
            mtime = 0
        m = manifest.get(e.rel)
        dst = out_root / e.rel
        if (use_cache and m and m.get("size") == e.size and m.get("mtime") == mtime
                and m.get("key") == key_hex and dst.is_file()):
            cached += 1
            files.append({"rel": e.rel, "size_mb": _round_mb(e.size),
                          "pages": m.get("pages", 0), "status": "cached",
                          "key_masked": mask_key(key_hex)})
            log(f"  [decrypt] 缓存命中 {e.rel} ({_round_mb(e.size)}MB, 未变)")
            continue
        tasks.append((e.rel, str(e.path), str(dst), key_hex))

    log(f"[decrypt] 待解密: {len(tasks)} 个, 缓存命中: {cached}, 缺密钥: {skipped}")

    def _on_done(r):
        rel, pages, status, err = r
        if status == "ok":
            log(f"  [cipher] 已解密 {rel} ({pages} 页)")
        else:
            log(f"  [err] 失败 {rel}: {err}")

    results = decrypt_parallel(tasks, workers=workers, on_done=_on_done)
    for rel, pages, status, err in results:
        e = next(x for x in entries if x.rel == rel)
        key_hex = next(t[3] for t in tasks if t[0] == rel)
        if status == "ok":
            ok += 1
            manifest[rel] = {"size": e.size,
                             "mtime": int(e.path.stat().st_mtime),
                             "pages": pages, "key": key_hex}
        else:
            failed += 1
        files.append({"rel": rel, "size_mb": _round_mb(e.size), "pages": pages,
                      "status": status, "key_masked": mask_key(key_hex)})

    if tasks:
        save_manifest(out_root, manifest)

    report = {
        "wxid": wxid, "out_dir": str(out_root),
        "ok": ok, "failed": failed, "skipped": skipped, "cached": cached,
        "duration_ms": int((time.time() - t0) * 1000),
        "files": files,
    }
    log(f"解密完成: {ok} 成功（缓存命中 {cached}）"
        + (f"，{failed} 失败" if failed else "")
        + (f"，{skipped} 缺密钥" if skipped else "")
        + f" (耗时 {report['duration_ms']} ms)")
    return report


def extract_all(log=print, use_cache=True):
    """自动发现全部账号 → 缓存判定 → 收割补漏 → 逐账号提取。"""
    dirs = _discover(log)
    if not dirs:
        return []
    entries_by_dir = {db: collect_db_files(db) for _w, db in dirs}
    preset_full = _keystore_preset(entries_by_dir, log) if use_cache else None
    preset = preset_full
    if preset is None:
        # 收割补漏：只针对密钥库未覆盖的 salt
        store = keystore.load()
        covered = set()
        for e in entries_by_dir.values():
            for e2 in e:
                rec = store.get(e2.salt_hex)
                if rec:
                    try:
                        if verify_enc_key(parse_key(rec["key"]), e2.page1):
                            covered.add(e2.salt_hex)
                    except ValueError:
                        pass
        missing = {e.salt_hex for es in entries_by_dir.values() for e in es
                   if e.salt_hex not in covered}
        gm, ga = ({}, {})
        if missing:
            gm, ga = global_harvest(dirs, entries_by_dir, log, only_missing=missing)
        preset = {**{s: r["key"] for s, r in store.items()}, **gm}
        # 合并全局收割的 attrib 信息
        attrib = {**{s: "keystore" for s in store}, **ga}
    else:
        attrib = {s: "keystore" for s in preset}
    return [extract_keys_for_dir(db, log, preset=preset,
                                 entries=entries_by_dir.get(db), use_memory=False)
            for _w, db in dirs]


def auto_all(out_dir: str, log=print, use_cache=True, workers=None):
    """全自动：发现 → 缓存判定 → 收割补漏 → 提取 → 并行解密。"""
    dirs = _discover(log)
    if not dirs:
        return []
    entries_by_dir = {db: collect_db_files(db) for _w, db in dirs}
    preset_full = _keystore_preset(entries_by_dir, log) if use_cache else None
    attrib = {}
    if preset_full is not None:
        preset = preset_full
        attrib = {s: "keystore" for s in preset}
    else:
        store = keystore.load()
        covered = set()
        for es in entries_by_dir.values():
            for e in es:
                rec = store.get(e.salt_hex)
                if rec:
                    try:
                        if verify_enc_key(parse_key(rec["key"]), e.page1):
                            covered.add(e.salt_hex)
                    except ValueError:
                        pass
        missing = {e.salt_hex for es in entries_by_dir.values() for e in es
                   if e.salt_hex not in covered}
        gm, ga = ({}, {})
        if missing:
            gm, ga = global_harvest(dirs, entries_by_dir, log, only_missing=missing)
        preset = {**{s: r["key"] for s, r in store.items()}, **gm}
        attrib = {**{s: "keystore" for s in store}, **ga}

    accounts = []
    for wxid, db in dirs:
        rep = extract_keys_for_dir(db, log, preset=preset,
                                   entries=entries_by_dir.get(db),
                                   use_memory=False)
        log(f"账号 {wxid}: 密钥 {rep['verified']}/{rep['total_salts']}")
        dec = None
        if rep["verified"] > 0:
            dec = decrypt_dir(db, str(Path(out_dir) / wxid), log,
                              entries=entries_by_dir.get(db),
                              workers=workers, use_cache=use_cache)
        accounts.append({**rep, "decrypt": dec})
    return accounts
