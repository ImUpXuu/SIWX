# module-extract.py — 编排器

> **文件**: `siwx/extract.py` | **角色**: 系统中枢，串联发现→提取→验证→解密全流程

---

## 职责

`extract.py` 是整个系统的**编排器**（Orchestrator），负责：

1. 自动发现微信数据目录
2. 判断密钥库缓存覆盖率
3. 全局收割（一次内存扫描联合验证）
4. 逐账号执行策略链提取密钥
5. 交叉验证已知密钥
6. 并行解密（带产物缓存）

---

## 核心数据结构

### DbEntry（来自 sqlcipher.py）

```python
class DbEntry:
    rel: str          # 相对路径 (如 "message/message_1.db")
    path: Path        # 绝对路径
    size: int         # 文件大小
    salt_hex: str     # page1 前 16 字节 hex = 数据库身份
    page1: bytes      # 文件前 4KB
```

### ctx 字典

```python
ctx = {
    "db_dir": str,              # 账号数据目录
    "entries": list[DbEntry],   # 该账号所有数据库
    "page1_by_salt": dict,      # {salt_hex: page1_bytes}
    "key_map": dict,            # {salt_hex: key_hex} — 策略写入
    "attrib": dict,             # {salt_hex: strategy_name}
    "log": callable,            # 日志函数
    "use_memory": bool,         # 是否使用内存扫描策略
}
```

---

## 关键函数

### `extract_all(log, use_cache=True) → list[dict]`

**全自动密钥提取**（不解密）。

```
流程:
1. _discover() → 自动发现所有账号目录
2. collect_db_files() → 收集每个账号的数据库
3. _keystore_preset() → 密钥库全覆盖判定
4. 若未覆盖 → global_harvest() → 仅收割缺失 salt
5. 逐账号 extract_keys_for_dir() → 提取 + 保存到密钥库
```

**返回**: 每个账号的报告字典列表。

---

### `auto_all(out_dir, log, use_cache=True, workers=None) → list[dict]**

**全自动流水线**（提取 + 解密）。

```
流程:
1. _discover() → 发现账号
2. _keystore_preset() → 缓存判定
3. 若未覆盖 → global_harvest() → 收割缺失
4. 逐账号:
   a. extract_keys_for_dir() → 提取密钥
   b. decrypt_dir() → 并行解密（若有已验证密钥）
```

**返回**: 每个账号的报告字典（含 `decrypt` 子报告）。

---

### `global_harvest(dirs, entries_by_dir, log, only_missing=None) → (key_map, attrib)`

**全局收割** — 核心优化。

| 参数 | 说明 |
|---|---|
| `dirs` | [(wxid, db_dir)] 账号列表 |
| `entries_by_dir` | {db_dir: [DbEntry]} |
| `only_missing` | None=验证全部；set=只针对缺失 salt |

```
流程:
1. 收集所有账号的 page1（按 salt 去重）
2. 仅针对 only_missing 指定的 salt
3. 调用 config_cipher.extract() → 一次内存扫描
4. 返回 (key_map, attrib)
```

**设计要点**: 微信内存只扫一次，用全部账号 salt 的联合集做验证。

---

### `extract_keys_for_dir(db_dir, log, preset, entries, use_memory) → dict`

**单账号密钥提取**。

```
流程:
1. 收集 entries → page1_by_salt + salt_to_dbs 映射
2. 预置密钥（全局收割/密钥库）→ HMAC 复核
3. run_strategies(ctx) → 策略链
4. 交叉验证：已知密钥复测缺失 salt
5. 保存到 DPAPI 密钥库
6. 生成报告
```

**返回报告格式**:
```python
{
    "wxid": str,
    "db_dir": str,
    "db_count": int,
    "total_salts": int,       # 唯一 salt 数
    "verified": int,          # 已验证密钥数
    "cached": int,            # 缓存命中数
    "duration_ms": int,
    "salts": [{               # 每个 salt 的状态
        "salt": str,
        "dbs": list[str],
        "verified": bool,
        "strategy": str,      # 来源策略
        "key_masked": str,    # 打码密钥
    }]
}
```

---

### `decrypt_dir(db_dir, out_dir, log, entries, workers, use_cache) → dict`

**并行解密 + 产物缓存**。

```
流程:
1. 加载密钥库
2. 为每个 DbEntry 解析对应 key（先查 salt，再遍历 unique_keys）
3. 缓存判定 (manifest: mtime/size/key) → 命中则跳过
4. decrypt_parallel() → 多进程解密
5. 保存 manifest
```

**返回报告格式**:
```python
{
    "wxid": str,
    "out_dir": str,
    "ok": int,
    "failed": int,
    "skipped": int,
    "cached": int,
    "duration_ms": int,
    "files": [{               # 每个文件的状态
        "rel": str,
        "size_mb": float,
        "pages": int,
        "status": str,        # "ok" / "failed" / "skipped" / "cached"
        "key_masked": str,
    }]
}
```

---

### `_keystore_preset(entries_by_dir, log) → dict | None`

**密钥库覆盖率检查**。

- 遍历所有账号的所有 salt
- 检查密钥库中是否存在有效 key（HMAC 验证）
- 全部命中 → 返回 `{salt: key}` 字典（跳过内存扫描）
- 未全部命中 → 返回 None（需要收割）

---

### `mask_key(k: str) → str`

**密钥脱敏**。

```python
>>> mask_key("abcdef1234567890abcdef1234567890abcdef1234567890abcdef1234567890")
'abcdef…7890'
>>> mask_key("short")
'…'
```

---

## 缓存机制详解

### 密钥缓存判定

```
_keystore_preset():
  for 每个账号:
    for 每个 salt:
      rec = store.get(salt)
      if rec 且 verify_enc_key(rec["key"], page1):
        covered += 1
  if covered == total: → 全部命中，跳过内存扫描
```

### 解密产物缓存判定

```python
# 在 decrypt_dir() 中
m = manifest.get(e.rel)
if (m and m["size"] == e.size
    and m["mtime"] == mtime
    and m["key"] == key_hex
    and dst.is_file()):
    → cached += 1  # 跳过解密
```

---

## 交叉验证机制

在策略链执行完成后，对仍未验证的 salt：

```python
for salt, page1 in page1_by_salt.items():
    if salt in key_map: continue
    for k in set(key_map.values()):  # 已验证的密钥集合
        if verify_enc_key(k, page1):
            key_map[salt] = k
            attrib[salt] = "交叉验证"
            break
```

**原理**: 同一账号的多个数据库可能共享密钥，
已知密钥可复测缺失 salt。

---

## 日志输出示例

```
── 账号 wxid_xxx ──
[keystore] 密钥缓存覆盖 5/79，需收割缺失部分
[harvest] 收割目标 74 个 salt，一次内存扫描联合验证
[cipher] 微信进程 [1234] — 只读扫描 (无需管理员、无需重启)
[cipher] PID=1234: 1523 个区域 / 764 MB
[cipher] Config.Cipher 扫描完成: 验证 32 个密钥
[keystore] 本机密钥库命中 5 个
提取完成: 32/79 salt 已验证 (耗时 1234 ms)
── 解密 wxid_xxx: 32 个数据库 → output/wxid_xxx ──
[cipher] 已解密 message/message_1.db (1234 页)
[keystore] 缓存命中 contact/contact.db (源库未变)
解密完成: 31 成功（缓存命中 1） (耗时 5678 ms)
```
