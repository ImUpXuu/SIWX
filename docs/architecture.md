# 整体架构与设计哲学

> 本文解释 stories-in-wx 的整体数据流、核心抽象、设计决策。
> 读完后应能理解"为什么这样设计"，而不只是"怎么实现的"。

---

## 1. 系统全景

```
┌─────────────────────────────────────────────────────────────────────┐
│                         用户入口                                     │
│  CLI (cli.py / run.py)  ←→  Web 控制台 (server.py + ui/)           │
└──────────────┬──────────────────────────────────┬───────────────────┘
               │                                  │
               ▼                                  ▼
┌──────────────────────────┐        ┌──────────────────────────┐
│     extract.py 编排器     │        │   api_chat.py            │
│  (全局收割 + 两层缓存)    │        │   api_export.py          │
│                          │        │   api_settings.py        │
└──────────────┬───────────┘        └─────────────┬────────────┘
               │                                  │
       ┌───────┴───────┐                  ┌───────┴───────┐
       ▼               ▼                  ▼               ▼
┌─────────────┐ ┌─────────────┐   ┌─────────────┐ ┌─────────────┐
│ 策略链       │ │ 解密池       │   │ 媒体解密     │ │ 导出引擎     │
│ strategies/ │ │ pool.py     │   │ media.py    │ │ exporter.py │
│ (4 个策略)   │ │ (多进程)     │   │ (V0/V1/V2)  │ │ (8 种格式)  │
└──────┬──────┘ └──────┬──────┘   └──────┬──────┘ └──────┬──────┘
       │               │                  │               │
       ▼               ▼                  ▼               ▼
┌─────────────┐ ┌─────────────┐   ┌─────────────┐ ┌─────────────┐
│ winproc.py  │ │ sqlcipher.py│   │ VoipEngine  │ │ html_tmpl   │
│ (内存访问)   │ │ (页级解密)   │   │ (wxgf转码)  │ │ (HTML查看)  │
└─────────────┘ └─────────────┘   └─────────────┘ └─────────────┘
       │               │
       ▼               ▼
┌─────────────────────────────┐
│  keystore.py (DPAPI 密钥库)  │
│  discover.py (目录发现)      │
└─────────────────────────────┘
```

---

## 2. 数据流：从密钥到明文

### 阶段 1：发现

```
discover.py
  find_wechat_data_dirs()  → 全盘扫描 A-Z + 用户目录 → [(wxid, db_storage路径)]
  find_wechat_pids()       → psutil 枚举 weixin.exe / wechat.exe → [pid, ...]
```

### 阶段 2：密钥提取（核心）

```
extract.py 编排器
  │
  ├─ 1. collect_db_files() → 收集所有 .db 文件 → DbEntry(rel, path, size, salt_hex, page1)
  │
  ├─ 2. 密钥库缓存判定 (_keystore_preset)
  │     └─ 全部 salt 在密钥库中有有效 key？→ 跳过内存扫描（秒回）
  │
  ├─ 3. 全局收割 (global_harvest) — 仅针对缺失 salt
  │     └─ config_cipher.extract() → 一次内存扫描，联合验证
  │
  ├─ 4. 逐账号提取 (extract_keys_for_dir)
  │     ├─ 预置密钥（全局收割/密钥库）→ HMAC 复核
  │     ├─ run_strategies(ctx) → 策略链
  │     │    ├─ keystore_source  (密钥库缓存)
  │     │    ├─ mmkv             (MMKV 离线)
  │     │    ├─ config_cipher    (WCDB Config.Cipher 主力)
  │     │    └─ memscan          (内存字面量兜底)
  │     └─ 交叉验证：已知密钥复测缺失 salt
  │
  └─ 5. 保存到 DPAPI 密钥库
```

### 阶段 3：数据库解密

```
extract.py → decrypt_dir()
  ├─ 加载密钥库 → 为每个 DbEntry 解析对应 key
  ├─ 缓存判定 (manifest: mtime/size) → 源库未变直接命中
  ├─ decrypt_parallel() → 多进程池
  │    └─ _worker() → sqlcipher.decrypt_database() → 流式页级解密
  └─ 保存 manifest
```

### 阶段 4：媒体解密（按需）

```
media.py → get_image()
  ├─ 内存 LRU 缓存（200 张，程序关闭释放）
  ├─ 多级来源：
  │    ├─ ⓪ attach 原图目录直查（hq=True 优先 _h 高清版）
  │    ├─ ① hardlink 原图
  │    ├─ ② Bubble 气泡缓存
  │    └─ ③ Thumb 明文缩略图
  ├─ 按头分派：V2(账号key) / V1(固定key) / V0(XOR自动检测)
  └─ wxgf → VoipEngine.dll 转码
```

---

## 3. 核心抽象

### 3.1 DbEntry — 数据库身份

```python
class DbEntry:
    __slots__ = ("rel", "path", "size", "salt_hex", "page1")
```

- `salt_hex`: page1 前 16 字节的 hex — **数据库的真实身份**
- `page1`: 文件前 4KB — 验证密钥的唯一所需
- `rel`: 相对路径（用于缓存 manifest 的 key）

### 3.2 ctx 字典 — 策略上下文

```python
ctx = {
    "db_dir": str,           # 账号数据目录
    "entries": list[DbEntry], # 该账号所有数据库
    "page1_by_salt": dict,   # {salt_hex: page1_bytes}
    "key_map": dict,         # {salt_hex: key_hex} — 策略写入
    "attrib": dict,          # {salt_hex: strategy_name} — 来源标注
    "log": callable,         # 日志函数
    "use_memory": bool,      # 是否使用内存扫描策略
}
```

### 3.3 策略签名 — propose-verify 分离

```python
def extract(ctx) -> int:
    """策略只产出候选，统一 HMAC 验证入库。返回新发现密钥数。"""
```

策略不直接验证密钥，而是将候选 key 写入 `ctx["key_map"]`，
编排器统一用 `verify_enc_key()` 做 HMAC 验证。

---

## 4. 两层缓存模型

### 缓存 1：密钥缓存（keystore.bin）

| 特性 | 说明 |
|---|---|
| 索引方式 | salt_hex（数据库真实身份） |
| 加密方式 | Windows DPAPI + 项目熵 (`CryptProtectData`) |
| 存储位置 | `%LOCALAPPDATA%\stories-in-wx\keystore.bin` |
| 命中条件 | 全部 salt 在库中有有效 key |
| 效果 | 跳过内存扫描，秒回 |

### 缓存 2：解密产物缓存 (.siwx_cache.json)

| 特性 | 说明 |
|---|---|
| 索引方式 | 数据库相对路径 (rel) |
| 判定条件 | `(mtime, size, key)` 三者一致 |
| 存储位置 | `output/<wxid>/.siwx_cache.json` |
| 效果 | 源库未变直接命中，秒回 |

---

## 5. 设计决策

### 5.1 为什么按 salt 索引而不是按路径？

原项目 pc_wechat_exp 按路径存 key + 明文 JSON，有两个缺陷：
1. 同 salt 的多分片共享密钥，salt 才是密钥的真实身份
2. 静止状态明文不安全

### 5.2 为什么全局收割优于逐账号扫描？

微信内存只扫一次，用全部账号 salt 的联合集做 HMAC 验证。
- 原项目：N 个账号 = N 次内存扫描
- 本项目：N 个账号 = 1 次内存扫描 + 联合验证

### 5.3 为什么 propose-verify 分离？

策略只产出候选（propose），编排器统一验证（verify）：
- 策略不需要知道验证细节
- 新增策略只需产出候选，不重复实现验证
- 验证逻辑集中，易于维护

### 5.4 为什么手动 CBC 而不是用 PyCryptodome 的 CBC 模式？

```python
# 手动 CBC：同 key 的 ECB cipher 跨页复用
aes = AES.new(enc_key, AES.MODE_ECB)
# 大整数 XOR 恢复链 → 消掉 13.7 万次 AES.new 的 key schedule 开销
```

SQLCipher 每页 4KB，一个数据库可能有 13.7 万页。
PyCryptodome 的 CBC 模式每页调用一次 `AES.new()`，
手动复用 ECB cipher + 大整数 XOR 消除 key schedule 开销。

### 5.5 为什么媒体解密不做全量？

实测样本：朋友圈 184 张约 60MB，但聊天历史媒体可达数 GB。
按需解密 + 内存 LRU 缓存（200 张，程序关闭释放）是正确姿势。

---

## 6. 安全边界

| 边界 | 实现 |
|---|---|
| 密钥静止加密 | DPAPI + 项目熵，不落明文 |
| 进程只读访问 | `PROCESS_VM_READ \| PROCESS_QUERY_INFORMATION` |
| 日志脱敏 | 只输出 salt + 打码密钥 (`xxxxxx…xxxx`) |
| 媒体不落盘 | 内存 LRU 缓存，程序关闭释放 |
| Web 仅本地 | 绑定 `127.0.0.1`，无外网暴露 |
| 导出路径限制 | `is_relative_to(root)` 防止路径穿越 |

---

## 7. 扩展点

### 7.1 添加新策略

```python
# 1. 在 strategies/ 创建新模块
def extract(ctx) -> int:
    """我的新策略。返回新发现密钥数。"""
    # 产出候选写入 ctx["key_map"][salt] = key_hex
    # 标注来源 ctx["attrib"][salt] = "my_strategy"
    return found_count

# 2. 在 strategies/__init__.py 注册
from siwx.strategies import my_strategy
STRATEGY_REGISTRY.append(my_strategy)
```

### 7.2 添加新导出格式

在 `exporter.py` 添加 `_write_xxx()` 函数 + 在 `run_export()` 的格式分派中添加分支。

### 7.3 添加新 API 端点

创建新 Blueprint 模块（参考 `api_chat.py`），在 `server.py` 注册。
