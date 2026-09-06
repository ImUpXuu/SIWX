# module-keystore.py — DPAPI 加密密钥库

> **文件**: `siwx/keystore.py` | **角色**: 静止状态加密的密钥持久化

---

## 职责

1. **salt 索引**: 以 salt_hex 为 key（数据库的真实身份）
2. **DPAPI 加密**: Windows `CryptProtectData` + 项目熵
3. **原子写入**: 先写 `.tmp` 再 `os.replace`
4. **密钥管理**: 插入、加载、列出唯一密钥

---

## 存储位置

```
%LOCALAPPDATA%\stories-in-wx\keystore.bin
```

通常是：
`C:\Users\<user>\AppData\Local\stories-in-wx\keystore.bin`

---

## 数据格式

### 磁盘格式（加密后）

DPAPI 加密的二进制 blob，不可直接读取。

### 内存格式（解密后）

```json
{
  "abcdef1234567890abcdef1234567890": {
    "key": "abcdef1234567890abcdef1234567890abcdef1234567890abcdef1234567890",
    "strategy": "cipher",
    "updated": 1725600000
  },
  "fedcba9876543210fedcba9876543210": {
    "key": "fedcba9876543210fedcba9876543210fedcba9876543210fedcba9876543210",
    "strategy": "mmkv",
    "updated": 1725600000
  }
}
```

| 字段 | 说明 |
|---|---|
| `key` | 64 位 hex 密钥（小写） |
| `strategy` | 来源策略名称 |
| `updated` | 更新时间戳 |

---

## 关键函数

### `store_path() → Path`

返回密钥库文件路径。

```python
>>> store_path()
WindowsPath('C:/Users/xxx/AppData/Local/stories-in-wx/keystore.bin')
```

---

### `load() → dict`

读取并解密密钥库。

```
流程:
1. 检查文件是否存在
2. 读取二进制内容
3. CryptUnprotectData() 解密
4. JSON 解析
5. 返回 dict
```

**异常处理**: 任何错误（文件不存在、解密失败、JSON 解析错误）返回空 dict。

---

### `save(store: dict) → None`

加密并保存密钥库。

```
流程:
1. 确保父目录存在
2. JSON 序列化
3. CryptProtectData() 加密（带项目熵）
4. 写入 .tmp 临时文件
5. os.replace() 原子替换
```

**原子写入**: 先写 `.tmp` 再 `os.replace`，防止写入过程中断导致密钥库损坏。

---

### `insert(store, salt, key, strategy) → None`

插入或更新一条密钥记录。

```python
insert(store, "abcdef...", "64hex...", "cipher")
# store["abcdef..."] = {"key": "64hex...", "strategy": "cipher", "updated": 1725600000}
```

**注意**: key 自动转小写。

---

### `unique_keys(store) → generator`

遍历密钥库中的唯一密钥（去重）。

```python
for key in unique_keys(store):
    print(key)  # 64位 hex，每个只出现一次
```

**过滤**: 只输出长度为 64 的有效 hex 密钥。

---

## DPAPI 加密实现

### 项目熵

```python
_ENTROPY = b"stories-in-wx::keystore::v1"
```

额外的熵绑定，确保只有本应用能解密（即使其他程序调用 `CryptUnprotectData` 没有这个熵也无法解密）。

### CryptProtectData

```python
def _protect(data: bytes) -> bytes:
    # 创建输入 blob
    blob_in = _DATA_BLOB(len(data), ctypes.cast(buf, ctypes.c_void_p))
    # 创建熵 blob
    blob_ent = _DATA_BLOB(len(_ENTROPY), ctypes.cast(ent, ctypes.c_void_p))
    # 调用 DPAPI
    _crypt32.CryptProtectData(blob_in, None, blob_ent, None, None, 0, blob_out)
    # 提取结果
    return ctypes.string_at(blob_out.pbData, blob_out.cbData)
```

### CryptUnprotectData

```python
def _unprotect(data: bytes) -> bytes:
    # 类似 _protect，但调用 CryptUnprotectData
    # 使用相同的熵
    ...
```

---

## 设计决策

### 为什么按 salt 索引？

原项目 pc_wechat_exp 按路径存 key + 明文 JSON：
1. 同 salt 的多分片共享密钥，salt 才是密钥的真实身份
2. 路径可能变化（分片轮转），salt 不变

### 为什么用 DPAPI？

- Windows 原生加密，无需管理密钥
- 绑定当前 Windows 用户账户
- 其他用户/系统无法解密

### 为什么原子写入？

防止写入过程中断（崩溃、断电）导致密钥库损坏。
先写 `.tmp` 再 `os.replace` 保证原子性。

---

## 使用示例

```python
from siwx import keystore

# 加载密钥库
store = keystore.load()
print(f"已有 {len(store)} 条密钥")

# 插入新密钥
keystore.insert(store, "salt_hex...", "64hex_key...", "cipher")

# 保存
keystore.save(store)

# 遍历唯一密钥
for key in keystore.unique_keys(store):
    print(key)
```
