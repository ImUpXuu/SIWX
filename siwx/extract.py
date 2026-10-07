"""编排器：全局收割 → 统一验证 → salt 索引密钥库 → 并行解密（带缓存）。

两层缓存（"不要每次都重新跑"）：
  1. 密钥缓存：DPAPI 密钥库能覆盖全部账号全部 salt 时，跳过内存扫描；
  2. 解密缓存：输出目录 .siwx_cache.json 记录 (mtime,size)，源库未变直接命中。

全局收割：微信内存只扫一次，用全部账号 salt 联合集验证，再分发给各账号。
日志脱敏：只输出 salt 与打码密钥，永不出明文。
"""
import platform
import time
from pathlib import Path

from siwx import logger as _slog
from siwx import keystore
from siwx.discover import find_wechat_data_dirs, wxid_of
from siwx.pool import (decrypt_parallel, load_manifest, manifest_has_keys,
                       save_manifest)
from siwx.sqlcipher import (collect_db_files, decrypt_database, mask_key,
                            parse_key, verify_enc_key)
from siwx.strategies import run_strategies

# manifest 中记录「产出该输出目录的 db_dir」的保留键。
# 以 @ 开头，与业务键（rel 路径，均以目录名开头）不会冲突。
# 注意：pool.load_manifest 只有 extract.py 一处读取，且只用 .get(rel) 查询，
# 从不遍历，因此新增该键不会影响任何既有逻辑。
SOURCE_FIELD = "@source"


def _round_mb(n: float) -> float:
    return round(n / 1048576.0, 1)


def _discover(log_fn):
    """发现微信数据目录。"""
    dirs = find_wechat_data_dirs()
    if not dirs:
        log_fn("✗ 未找到微信数据目录 (xwechat_files/*/db_storage)")
        _slog.detailed("discover", "搜索路径: USERPROFILE/Documents/xwechat_files, USERPROFILE/xwechat_files, A-Z:/xwechat_files")
    else:
        log_fn(f"自动扫描到 {len(dirs)} 个微信账号")
        for wxid, db_dir in dirs:
            _slog.detailed("discover", f"账号={wxid}, 路径={db_dir}")
    return dirs


def _keystore_preset(entries_by_dir, log) -> dict:
    """检查密钥库覆盖率：全部命中返回全量 preset，否则返回 None（需收割）。"""
    log = _slog.ensure_dual(log, "keystore")
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
                except ValueError as ve:
                    # 核查 C.1：用条目变量 e（except 块里的 ve 是 ValueError，
                    # 用 ve.salt_hex 会 AttributeError）；存量损坏记录 warn
                    _slog.warn("keystore",
                               f"密钥库记录解析失败 salt={e.salt_hex[:16]}… err={ve}")
    if total and covered == total:
        log(f"[keystore] 密钥缓存全覆盖 ({covered}/{total})，跳过内存扫描")
        return {s: r["key"] for s, r in store.items()}
    log(f"[keystore] 密钥缓存覆盖 {covered}/{total}，需收割缺失部分")
    return None


def _use_memory_per_account() -> bool:
    """逐账号提取时是否允许依赖微信进程的策略。

    Windows：内存已由 global_harvest 一次扫完，逐账号不再重复扫 (False)。
    非 Windows：没有全局收割，macOS 的 LLDB 策略只能在逐账号阶段运行 (True)；
    密钥已被缓存全覆盖时 run_strategies 会提前退出，不会白白附加进程。
    """
    return platform.system() != "Windows"


def global_harvest(dirs, entries_by_dir, log=print, only_missing=None):
    """一次内存扫描，用 salt 联合集验证 → (global_key_map, global_attrib)。

    only_missing: None=验证全部；set=只针对缺失的 salt（收割补漏）。

    config_cipher 依赖 winproc (ctypes.windll)，仅 Windows 可用；非 Windows 直接
    返回空，由 extract_keys_for_dir 中的平台策略（macOS 为 LLDB）负责提取。
    """
    if platform.system() != "Windows":
        return {}, {}
    from siwx.strategies import config_cipher

    # 双写注入点（审计 §2.1 模式 A）：任务轨保留，结构化轨受 Debug 开关控制
    log = _slog.ensure_dual(log, "harvest")
    page1_by_salt = {}
    for entries in entries_by_dir.values():
        for e in entries:
            if only_missing is None or e.salt_hex in only_missing:
                page1_by_salt.setdefault(e.salt_hex, e.page1)
    if not page1_by_salt:
        _slog.detailed("harvest", "无待收割 salt，跳过内存扫描")
        return {}, {}

    log(f"[harvest] 收割目标 {len(page1_by_salt)} 个 salt，一次内存扫描联合验证")
    t0 = time.time()
    key_map, attrib = {}, {}
    ctx = {
        "db_dir": "", "entries": [], "page1_by_salt": page1_by_salt,
        "key_map": key_map, "attrib": attrib,
        # 直调 config_cipher（不经 run_strategies）：按策略标签单独包装
        "log": _slog.dual_log(getattr(log, "task", log), "strategy:cipher"),
        "dbg": _slog.dbg_log("strategy:cipher"),
    }
    config_cipher.extract(ctx)
    if key_map:
        log(f"[harvest] 收割完成: {len(key_map)} 个新密钥验证通过 "
            f"(耗时 {time.time() - t0:.1f}s)")
    else:
        _slog.detailed("harvest",
                       f"收割 0 命中，目标 {len(page1_by_salt)} 个 salt，"
                       f"耗时 {time.time() - t0:.1f}s")
    return key_map, attrib


def extract_keys_for_dir(db_dir: str, log=print, preset=None,
                         entries=None, use_memory=True) -> dict:
    """对单个账号执行提取。preset = 全局收割/密钥库的 {salt: key}。"""
    t0 = time.time()
    # 双写注入点（审计 §2.1 模式 A）
    log = _slog.ensure_dual(log, "extract")
    wxid = wxid_of(db_dir)
    log(f"── 账号 {wxid} ──")
    _d = lambda m: log(f"[extract] {m}") if log else None

    if entries is None:
        entries = collect_db_files(db_dir)
    log(f"[extract] 收集到 {len(entries)} 个数据库文件")
    page1_by_salt, salt_to_dbs = {}, {}
    for e in entries:
        page1_by_salt.setdefault(e.salt_hex, e.page1)
        salt_to_dbs.setdefault(e.salt_hex, []).append(e.rel)
    log(f"[extract] 唯一 salt 数: {len(page1_by_salt)}")
    for salt_hex, dbs in salt_to_dbs.items():
        _d(f"salt={salt_hex[:16]}... 关联{len(dbs)}个库: {', '.join(dbs[:3])}{'...' if len(dbs)>3 else ''}")

    key_map, attrib = {}, {}
    cached_keys = 0

    # 0) 预置密钥（全局收割 / 密钥库缓存），逐个 HMAC 复核
    preset_miss = 0
    for salt, key in (preset or {}).items():
        if salt in page1_by_salt and salt not in key_map:
            try:
                kb = parse_key(key)
            except ValueError:
                _d(f"预置密钥解析失败 salt={salt[:16]}...")
                continue
            if verify_enc_key(kb, page1_by_salt[salt]):
                key_map[salt] = key.lower()
                attrib[salt] = "缓存"
                cached_keys += 1
            else:
                preset_miss += 1
                _d(f"预置密钥HMAC失败 salt={salt[:16]}...")
    if cached_keys:
        log(f"[keystore] 缓存命中 {cached_keys} 个")
    if preset_miss:
        log(f"[extract] 预置密钥未命中 {preset_miss} 个")
    _d(f"预置密钥处理完成: 命中{cached_keys}, 未命中{preset_miss}, 待验证{len(page1_by_salt)-len(key_map)}")

    ctx = {
        "db_dir": str(db_dir), "entries": entries,
        "page1_by_salt": page1_by_salt,
        "key_map": key_map, "attrib": attrib, "log": log,
        "use_memory": use_memory,
    }
    run_strategies(ctx)
    _d(f"策略链执行后: 已验证{len(key_map)}/{len(page1_by_salt)}")

    # 交叉验证：已知密钥复测缺失 salt
    cross_ok = 0
    for salt, page1 in page1_by_salt.items():
        if salt in key_map:
            continue
        for k in set(key_map.values()):
            try:
                kb = parse_key(k)
            except ValueError:
                _slog.detailed("extract", "交叉验证: 已知密钥解析失败")
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
        # 审计 §3.1:174-179：磁盘满/权限等 OSError 不应炸穿整个提取
        try:
            keystore.save(store)
            log("[keystore] 密钥已保存到 DPAPI 加密密钥库")
        except OSError as exc:
            _slog.error("keystore", f"密钥库保存失败: {exc}")

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

    # 提取失败时给出明确提示
    if report["verified"] == 0 and report["total_salts"] > 0:
        log("[extract] ✗ 未能提取到任何密钥！可能原因:")
        log("  1. 微信未登录 — 请先启动并登录微信")
        log("  2. 微信版本不支持 — 需要 WeChat 4.1.x (Windows) 或 4.1.80+ (macOS)")
        log("  3. 密钥已过期 — 尝试在微信中重新打开聊天后重跑")
        from siwx.discover import find_wechat_pids
        if not find_wechat_pids():
            log("[extract] ⚠ 未检测到微信进程！请先启动微信")

    return report


def decrypt_dir(db_dir: str, out_dir: str, log=print, entries=None,
                workers=None, use_cache=True) -> dict:
    """并行解密 + 产物缓存：源库 (mtime,size) 未变直接命中，秒回。

    来源保护：manifest 记录产出该目录的 db_dir（`@source`）。当同名账号存在
    多个副本目录时，它们共用同一个 output/<wxid>/；若当前 db_dir 与记录不符，
    说明会覆盖另一副本的产物，此时跳过并计入 conflict，避免静默丢数据。
    旧 manifest 无 `@source`（升级自 v5.0.x）视为放行，行为与旧版一致。
    """
    t0 = time.time()
    # 双写注入点（审计 §2.1 模式 A）
    log = _slog.ensure_dual(log, "decrypt")
    wxid = wxid_of(db_dir)
    store = keystore.load()
    if entries is None:
        entries = collect_db_files(db_dir)
    uniq = list(dict.fromkeys(keystore.unique_keys(store)))
    out_root = Path(out_dir)
    log(f"── 解密 {wxid}: {len(entries)} 个数据库 → {out_root} ──")
    log(f"[decrypt] 密钥库: {len(store)} 条, 唯一密钥: {len(uniq)} 个")

    manifest = load_manifest(out_root) if use_cache else {}
    # S6 迁移：旧版缓存清单把每个库的 SQLCipher 明文密钥落盘了——加载时已
    # 就地剥离，这里立即重写，把磁盘上的存量明文也清掉。
    if manifest and manifest_has_keys(manifest):
        log("[decrypt] ⚠ 检测到旧版缓存清单含明文密钥，已剥离并重写"
            "（密钥改为每次会话从加密密钥库重取，见审计 S6）")
        save_manifest(out_root, manifest)
    # 本次来源标签 + 历史来源标签（用于覆盖保护）
    try:
        src_tag = str(Path(db_dir).resolve()).casefold()
    except OSError:
        src_tag = str(db_dir).casefold()
        _slog.detailed("decrypt", f"resolve 失败，来源标签用原始路径: {db_dir}")
    source_guard = (manifest.get(SOURCE_FIELD) or "").strip() or None
    if source_guard and src_tag and source_guard != src_tag:
        log(f"[decrypt] ⚠ 该输出目录上次由其他副本目录产出：")
        log(f"[decrypt]     历史来源 {source_guard}")
        log(f"[decrypt]     本次来源 {src_tag}")
        log(f"[decrypt]     已存在的产物将跳过，不覆盖（如需切换请清空该账号输出目录）")
    files, tasks = [], []
    ok = failed = skipped = cached = 0
    conflicts = []

    def _resolve_key(e):
        rec = store.get(e.salt_hex)
        if rec:
            kb = None
            try:
                kb = parse_key(rec["key"])
            except ValueError:
                # 核查 C.1：ValueError 块里用条目变量，勿用异常对象取属性
                _slog.detailed("decrypt",
                               f"{e.rel} keystore 记录解析失败 salt={e.salt_hex[:16]}…")
            if kb is not None:
                if verify_enc_key(kb, e.page1):
                    return rec["key"]
                # 有记录但 HMAC 不过 = 密钥轮换，重要诊断信号（审计 §3.1:255-271）
                _slog.detailed("decrypt",
                               f"{e.rel} keystore 记录 HMAC 未命中 salt={e.salt_hex[:16]}…")
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
        except OSError as oe:
            mtime = 0
            _slog.warn("decrypt", f"stat 失败 {e.rel}: {oe}（该库缓存将永不命中）")
        m = manifest.get(e.rel)
        dst = out_root / e.rel
        # S6：缓存命中不再比对 manifest 里的 key（密钥已不落盘）。
        # 能走到这里说明密钥已从 keystore 解析成功；(mtime,size) 未变且
        # 产物存在即为有效缓存。
        if (use_cache and m and m.get("size") == e.size and m.get("mtime") == mtime
                and dst.is_file()):
            cached += 1
            files.append({"rel": e.rel, "size_mb": _round_mb(e.size),
                          "pages": m.get("pages", 0), "status": "cached",
                          "key_masked": mask_key(key_hex)})
            log(f"  [decrypt] 缓存命中 {e.rel} ({_round_mb(e.size)}MB, 未变)")
            continue
        # 来源保护：同名账号的多个副本目录会共用 output/<wxid>/，若当前来源与
        # 产出该目录的来源不同，直接覆盖会把另一副本（可能数据更全）的产物
        # 冲掉。这里跳过并告警，由用户决定是否清理目录后重跑。
        # 兼容：旧 manifest 无 @source（升级用户）视为放行，保持原有行为。
        if source_guard and dst.is_file() and src_tag and src_tag != source_guard:
            conflicts.append(e.rel)
            files.append({"rel": e.rel, "size_mb": _round_mb(e.size), "pages": 0,
                          "status": "conflict", "key_masked": ""})
            log(f"  [decrypt] ⚠ 跳过 {e.rel}：已有产物来自其他副本目录")
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
            # S6：manifest 不再持久化密钥（size/mtime/pages 足以判定缓存命中，
            # 密钥每次会话从 keystore 重取）
            # 审计 §3.1:327-329：源库被微信删除的竞态不应炸掉已成功任务与缓存
            try:
                mtime = int(e.path.stat().st_mtime)
            except OSError as oe:
                mtime = 0
                _slog.warn("decrypt", f"{rel} manifest 记录失败: {oe}")
            manifest[rel] = {"size": e.size,
                             "mtime": mtime,
                             "pages": pages}
        else:
            failed += 1
        files.append({"rel": rel, "size_mb": _round_mb(e.size), "pages": pages,
                      "status": status, "key_masked": mask_key(key_hex)})

    if tasks:
        # 记录来源：本次确实写出了产物，该目录即归属于本 db_dir。
        # 无历史来源时才写入；来源不一致时产物已被跳过，不应改写标记。
        if not source_guard:
            manifest[SOURCE_FIELD] = src_tag
        save_manifest(out_root, manifest)
    if conflicts:
        log(f"[decrypt] ⚠ {len(conflicts)} 个库因来源不同被跳过（已有产物来自其他副本目录）")

    report = {
        "wxid": wxid, "out_dir": str(out_root),
        "ok": ok, "failed": failed, "skipped": skipped, "cached": cached,
        "conflicts": len(conflicts),
        "duration_ms": int((time.time() - t0) * 1000),
        "files": files,
    }
    log(f"解密完成: {ok} 成功（缓存命中 {cached}）"
        + (f"，{failed} 失败" if failed else "")
        + (f"，{skipped} 缺密钥" if skipped else "")
        + (f"，{len(conflicts)} 来源冲突跳过" if conflicts else "")
        + f" (耗时 {report['duration_ms']} ms)")
    return report


def _covered_salts(store, entries_by_dir) -> set:
    """密钥库能 HMAC 覆盖的 salt 集合（extract_all / auto_all 共用，审计 §3.1）。

    损坏记录（parse_key ValueError）detailed 记录后按未覆盖处理，不影响其余。"""
    covered = set()
    for entries in entries_by_dir.values():
        for e in entries:
            rec = store.get(e.salt_hex)
            if not rec:
                continue
            try:
                if verify_enc_key(parse_key(rec["key"]), e.page1):
                    covered.add(e.salt_hex)
            except ValueError as ve:
                _slog.detailed("keystore",
                               f"覆盖判定: 记录解析失败 salt={e.salt_hex[:16]}… err={ve}")
    return covered


def _collect_entries_by_dir(dirs) -> dict:
    """逐账号收集 db 文件（审计 §3.1:364/400）：单账号目录损坏只跳过该账号，
    不再让 extract_all/auto_all 整体失败。"""
    entries_by_dir = {}
    for _w, db in dirs:
        try:
            entries_by_dir[db] = collect_db_files(db)
        except Exception as exc:
            _slog.error("discover", f"收集 {db} 失败: {exc}")
    return entries_by_dir


def extract_all(log=print, use_cache=True):
    """自动发现全部账号 → 缓存判定 → 收割补漏 → 逐账号提取。"""
    log = _slog.ensure_dual(log, "extract")
    dirs = _discover(log)
    if not dirs:
        return []
    entries_by_dir = _collect_entries_by_dir(dirs)
    preset_full = _keystore_preset(entries_by_dir, log) if use_cache else None
    preset = preset_full
    if preset is None:
        # 收割补漏：只针对密钥库未覆盖的 salt
        store = keystore.load()
        covered = _covered_salts(store, entries_by_dir)
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
                                 entries=entries_by_dir.get(db),
                                 use_memory=_use_memory_per_account())
            for _w, db in dirs if db in entries_by_dir]


def auto_all(out_dir: str, log=print, use_cache=True, workers=None):
    """全自动：发现 → 缓存判定 → 收割补漏 → 提取 → 并行解密。"""
    log = _slog.ensure_dual(log, "extract")
    dirs = _discover(log)
    if not dirs:
        return []
    entries_by_dir = _collect_entries_by_dir(dirs)
    preset_full = _keystore_preset(entries_by_dir, log) if use_cache else None
    attrib = {}
    if preset_full is not None:
        preset = preset_full
        attrib = {s: "keystore" for s in preset}
    else:
        store = keystore.load()
        covered = _covered_salts(store, entries_by_dir)
        missing = {e.salt_hex for es in entries_by_dir.values() for e in es
                   if e.salt_hex not in covered}
        gm, ga = ({}, {})
        if missing:
            gm, ga = global_harvest(dirs, entries_by_dir, log, only_missing=missing)
        preset = {**{s: r["key"] for s, r in store.items()}, **gm}
        attrib = {**{s: "keystore" for s in store}, **ga}

    accounts = []
    for wxid, db in dirs:
        if db not in entries_by_dir:
            continue   # 收集失败的账号已记 error，此处跳过（勿再触发重收集）
        rep = extract_keys_for_dir(db, log, preset=preset,
                                   entries=entries_by_dir.get(db),
                                   use_memory=_use_memory_per_account())
        log(f"账号 {wxid}: 密钥 {rep['verified']}/{rep['total_salts']}")
        dec = None
        if rep["verified"] > 0:
            dec = decrypt_dir(db, str(Path(out_dir) / wxid), log,
                              entries=entries_by_dir.get(db),
                              workers=workers, use_cache=use_cache)
        else:
            # 审计 §3.1:432-436：0 密钥跳过解密不再静默
            _slog.detailed("extract", f"账号 {wxid} 无已验证密钥，跳过解密")
        accounts.append({**rep, "decrypt": dec})
    return accounts
