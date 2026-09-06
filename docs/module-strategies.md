# module-strategies — 策略链插件系统

> **目录**: `siwx/strategies/` | **角色**: propose-verify 分离的密钥提取插件

---

## 设计理念

### propose-verify 分离

```
策略（propose） → 产出候选 key → 编排器（verify） → 统一 HMAC 验证
```

- 策略只负责产出候选，不直接验证
- 编排器统一用 `verify_enc_key()` 做 HMAC 验证
- 新增策略只需产出候选，不重复实现验证

### 策略注册表

```python
# strategies/__init__.py
STRATEGY_REGISTRY = [keystore_source, mmkv, config_cipher, memscan]
# 顺序即优先级
```

---

## 策略优先级

| 优先级 | 策略 | 说明 | 依赖微信进程 |
|---|---|---|---|
| 1 | `keystore_source` | 本机密钥库缓存命中 | 否 |
| 2 | `mmkv` | MMKV 离线文件提取 | 否 |
| 3 | `config_cipher` | WCDB Config.Cipher 只读扫描 | 是 |
| 4 | `memscan` | 全内存 x'<hex>' 字面量兜底 | 是 |

**跳过逻辑**:
- `use_memory=False` 时跳过 `config_cipher` 和 `memscan`（全局收割已覆盖）
- 全部 salt 已验证时提前终止

---

## ctx 字典接口

每个策略的统一签名：

```python
def extract(ctx: dict) -> int:
    """
    ctx = {
        "db_dir": str,              # 账号数据目录
        "entries": list[DbEntry],   # 该账号所有数据库
        "page1_by_salt": dict,      # {salt_hex: page1_bytes} — 验证用
        "key_map": dict,            # {salt_hex: key_hex} — 策略写入候选
        "attrib": dict,             # {salt_hex: strategy_name} — 来源标注
        "log": callable,            # 日志函数
        "use_memory": bool,         # 是否使用内存扫描策略
    }
    返回: 新发现的密钥数量
    """
```

---

## 策略详解

### 1. keystore_source — 密钥库缓存

**文件**: `strategies/keystore_source.py`

```python
def extract(ctx) -> int:
    store = keystore.load()
    for salt, page1 in ctx["page1_by_salt"].items():
        rec = store.get(salt)
        if rec and verify_enc_key(parse_key(rec["key"]), page1):
            ctx["key_map"][salt] = rec["key"].lower()
            ctx["attrib"][salt] = "keystore"
            found += 1
    return found
```

**原理**: 从 DPAPI 密钥库读取，逐个 HMAC 验证。
**特点**: 最快（纯文件 IO），无需微信进程。

---

### 2. mmkv — MMKV 离线提取

**文件**: `strategies/mmkv.py`

**原理**: 
1. 扫描 `db_storage/MMKV/f<hex>tinfo.mmkv` 文件
2. 用 `MD5(str(code)+wxid)[:16]` 派生 AES-128-GCM 密钥
3. 解密 mmkv 文件 → 明文中含数据库相对路径 + 64 位 hex 密钥
4. 按路径匹配 salt → HMAC 验证

**派生候选**:
```python
_derive_candidates(code, wxid):
    ("code+wxid", MD5(f"{code}{wxid}")[:16])
    ("wxid+code", MD5(f"{wxid}{code}")[:16])
    ("code+wxid_full", MD5(f"{code}{wxid}").hexdigest())
    ("sha256:16", SHA256(f"{code}{wxid}")[:16])
    ("sha256:32", SHA256(f"{code}{wxid}")[:32])
```

**文件格式**:
```
0      4      总长度 (LE u32)
4      16      IV
20     N       密文
20+N   16      GCM tag
```

**特点**: 无需微信进程，纯本地文件解密。

---

### 3. config_cipher — WCDB Config.Cipher 扫描（主力）

**文件**: `strategies/config_cipher.py`

**原理**:
1. 定位微信内存中的 `com.Tencent.WCDB.Config.Cipher` 字符串
2. 找指向该字符串的 `(ptr, len)` 节点 → config 对象 → blob
3. blob 为 32 字节 XOR 混淆的配置串
4. 解码后含 `x'<64~192 hex>'` 字面量（key64 + 可选 salt32）

**两遍扫描**:
1. 第一遍：定位 needle 字符串出现地址
2. 第二遍：找指向 needle 的 (ptr, len) 节点 → config 对象 → blob

**内置掩码**:
```python
CONFIG_XOR_MASK = bytes.fromhex(
    "d2c7442458020000004889442450488b"
    "450048844c2448488944254048584c24"
)
```

**自研掩码恢复（crib-drag 约束求解）**:
1. 穷举 `x'` 字面量起点
2. 残差类掩码集合交集（`i % 32`）
3. 每个候选掩码立即经 HMAC 验证裁决
4. 命中即停（验证引导搜索）

**ASCII 打分法（兜底）**:
- 无 `x'` 结构时按可打印占比恢复
- 每个残差类选使解码 mostly 可打印的掩码字节

**内存节点结构**:
```
node+0x10 = 数据指针 (指向 needle 字符串)
node+0x18 = 字符串长度 (30)
node+0x28 = config 指针
config_ptr+0x88 → 对象:
  obj+0x8  = blob 数据指针
  obj+0x10 = blob 长度
```

---

### 4. memscan — 内存字面量兜底

**文件**: `strategies/memscan.py`

**原理**: 全内存搜索 `x'<64~192 hex>'` 字面量。

```python
HEX_RE = re.compile(rb"x'([0-9a-fA-F]{64,192})'")
```

**两种形态**:
1. 96 位 hex = key64 + salt32（精确匹配 salt）
2. 64 位 hex（遍历所有 salt 试验证）

**适用场景**: WeChat < 4.1.10（无 Config.Cipher 配置对象）。

---

## 添加新策略

### 步骤

1. **创建策略模块**:

```python
# strategies/my_strategy.py
from siwx.sqlcipher import verify_enc_key

def extract(ctx) -> int:
    """我的新策略"""
    page1_by_salt = ctx["page1_by_salt"]
    key_map = ctx["key_map"]
    attrib = ctx["attrib"]
    log = ctx["log"]
    
    found = 0
    # 1. 产出候选
    for salt, page1 in page1_by_salt.items():
        if salt in key_map:
            continue
        candidate_key = my_extraction_logic(salt)
        if candidate_key:
            # 2. 写入 key_map（编排器统一验证）
            try:
                kb = bytes.fromhex(candidate_key)
                if verify_enc_key(kb, page1):
                    key_map[salt] = candidate_key.lower()
                    attrib[salt] = "my_strategy"
                    found += 1
            except ValueError:
                pass
    return found
```

2. **注册到注册表**:

```python
# strategies/__init__.py
from siwx.strategies import my_strategy
STRATEGY_REGISTRY.append(my_strategy)  # 追加到末尾（最低优先级）
```

---

## 策略异常处理

```python
def run_strategies(ctx):
    for mod in STRATEGY_REGISTRY:
        if len(ctx["key_map"]) >= len(ctx["page1_by_salt"]):
            break
        if not ctx.get("use_memory", True) and mod in (config_cipher, memscan):
            continue
        try:
            mod.extract(ctx)
        except Exception as e:
            ctx["log"](f"[策略 {mod.__name__}] 异常: {e}")
```

**单策略失败不影响整链**: 任何策略抛出异常只记录日志，继续执行下一个策略。

---

## 调试技巧

```python
# 单独测试某个策略
from siwx.strategies import config_cipher
from siwx.sqlcipher import collect_db_files
from siwx.discover import find_wechat_data_dirs

dirs = find_wechat_data_dirs()
db_dir = dirs[0][1]
entries = collect_db_files(db_dir)

page1_by_salt = {}
for e in entries:
    page1_by_salt.setdefault(e.salt_hex, e.page1)

ctx = {
    "db_dir": db_dir,
    "entries": entries,
    "page1_by_salt": page1_by_salt,
    "key_map": {},
    "attrib": {},
    "log": print,
    "use_memory": True,
}

found = config_cipher.extract(ctx)
print(f"发现 {found} 个密钥")
```
