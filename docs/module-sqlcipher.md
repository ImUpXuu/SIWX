# module-sqlcipher.py — SQLCipher 4 原语与页级解密

> **文件**: `siwx/sqlcipher.py` | **角色**: 全系统验证咽喉 + 流式整库解密

---

## 职责

1. **密钥验证原语**: SQLCipher 4 page-1 HMAC-SHA512 校验
2. **数据库文件收集**: 递归扫描 db_storage 下全部 .db
3. **流式整库解密**: 页级 AES-256-CBC 解密 → 明文 SQLite 写出

---

## SQLCipher 4 页级编解码参数

| 参数 | 值 | 说明 |
|---|---|---|
| `PAGE_SZ` | 4096 | 每页大小 |
| `KEY_SZ` | 32 | AES-256 密钥长度 |
| `SALT_SZ` | 16 | page1 前 16 字节 |
| `RESERVE_SZ` | 80 | 每页尾部保留区 |
| `IV_SZ` | 16 | 每页 IV 长度 |
| `HMAC_SZ` | 64 | SHA-512 HMAC 长度 |
| KDF | PBKDF2-SHA512 × 2 | 密钥派生 |
| Cipher | AES-256-CBC | 加密算法 |

---

## 关键函数

### `parse_key(hex_key: str) → bytes`

解析 64 位 hex 密钥字符串为 32 字节。

```python
>>> parse_key("abcdef1234567890abcdef1234567890abcdef1234567890abcdef1234567890")
b'\xab\xcd\xef\x12...'  # 32 bytes
```

**异常**: 长度不是 64 时抛出 `ValueError`。

---

### `verify_enc_key(enc_key: bytes, page1: bytes) → bool`

**SQLCipher 4 page-1 HMAC 校验** — 全系统验证咽喉。

```
算法:
1. salt = page1[:16]
2. mac_salt = bytes(b ^ 0x3A for b in salt)
3. mac_key = PBKDF2-SHA512(enc_key, mac_salt, iterations=2, dklen=32)
4. hmac_data = page1[16 : 4096 - 80 + 16]
5. stored_mac = page1[4096 - 64 :]
6. computed = HMAC-SHA512(mac_key, hmac_data + pack("<I", 1))
7. return constant_time_compare(computed, stored_mac)
```

**关键**: 验证一个候选密钥只需文件前 4KB — 全系统的验证咽喉。

---

### `collect_db_files(db_dir: str) → list[DbEntry]`

递归收集 db_storage 下全部 .db 文件。

```
规则:
- 排除 -wal / -shm 文件
- 跳过 < 4096 字节的文件
- 跳过全零 page1
- 微信占用时：复制到临时文件再读 page1
- 按 rel 排序返回
```

**DbEntry 结构**:
```python
class DbEntry:
    __slots__ = ("rel", "path", "size", "salt_hex", "page1")
    # rel: 相对路径
    # path: 绝对路径
    # size: 文件大小
    # salt_hex: page1[:16].hex()
    # page1: 文件前 4096 字节
```

---

### `decrypt_database(src, dst, enc_key, progress=None) → int`

**流式整库解密** — 核心性能路径。

```
算法:
1. 打开源文件（4MB 缓冲）
   - 若打开失败（微信占用）→ 复制到临时文件
2. 读取 page1 → verify_enc_key() 验证
3. 创建 AES-256-ECB cipher（复用，不每页新建）
4. 手动 CBC 解密:
   - page1: 前 16 字节是 salt，密文从 16 开始
   - 后续页: 整页密文
   - 每页 IV = page[4096-80 : 4096-80+16]
   - CBC 解密 = ECB_decrypt(ct) XOR (iv + prev_ct[:len-16])
   - 使用大整数 XOR 恢复链（消掉 Python 循环开销）
5. 写出明文 SQLite（4MB 缓冲）
6. 返回总页数
```

**性能优化**:
- 4MB 读写缓冲（减少系统调用）
- ECB cipher 复用（消掉 13.7 万次 AES.new 的 key schedule 开销）
- 大整数 XOR（消掉 Python 字节循环）
- 自动走 AES-NI 硬件加速

---

## 页级解密详解

### 页结构

```
┌──────────────────────────────────────────────────────┐
│  0        16      16+body_len    4096-80    4096-64  4096  │
│  │ salt  │      密文        │   IV(16B)  │  HMAC(64B) │      │
│  │(p1only)│  (4016B)       │  (reserve)  │            │      │
└──────────────────────────────────────────────────────┘
```

### 手动 CBC 实现

```python
def cbc_decrypt(ct: bytes, iv: bytes) -> bytes:
    raw = aes.decrypt(ct)  # ECB 解密
    prev = iv + ct[: len(ct) - 16]  # CBC 链
    n = int.from_bytes(raw, "little") ^ int.from_bytes(prev, "little")
    return n.to_bytes(len(ct), "little")
```

**为什么不用 PyCryptodome 的 CBC 模式？**

SQLCipher 每页 4KB，一个数据库可能有 13.7 万页。
PyCryptodome 的 CBC 模式每页调用一次 `AES.new()`，
手动复用 ECB cipher + 大整数 XOR 消除 key schedule 开销。

---

## 文件占用处理

微信运行时数据库文件被独占锁定：

```python
try:
    fin = open(src, "rb", buffering=4 * 1024 * 1024)
except OSError:
    # 微信占用中：复制到临时文件再读
    tmp_copy = Path(tempfile.gettempdir()) / f"siwx_db_{os.getpid()}.tmp"
    shutil.copy2(src, tmp_copy)
    fin = open(tmp_copy, "rb", buffering=4 * 1024 * 1024)
```

同样适用于 `collect_db_files()` 中的 page1 读取。

---

## 使用示例

```python
from siwx.sqlcipher import collect_db_files, decrypt_database, verify_enc_key, parse_key

# 1. 收集数据库
entries = collect_db_files("C:/Users/xxx/xwechat_files/wxid_xxx/db_storage")

# 2. 验证密钥
enc_key = parse_key("abcdef1234567890abcdef1234567890abcdef1234567890abcdef1234567890")
for e in entries:
    if verify_enc_key(enc_key, e.page1):
        print(f"{e.rel}: 密钥匹配")

# 3. 解密
pages = decrypt_database(
    Path("C:/.../message_1.db"),
    Path("output/wxid_xxx/message/message_1.db"),
    enc_key,
    progress=lambda p, t: print(f"{p}/{t}")
)
print(f"解密完成: {pages} 页")
```
