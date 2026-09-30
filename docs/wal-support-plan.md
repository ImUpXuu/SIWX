# SIWX 增加 WAL 支持 —— 完整设计方案

> 状态：**设计稿，未实施**
> 版本基线：v5.0.3
> 目标读者：SIWX 维护者
> 约束：本文档只描述方案，不包含代码改动

---

## 0. 实测结论（2026-09-27，真机）—— 请先读这一节

> 本节由 `scripts/wal_probe.py`（只读探测脚本）在真实微信 4.x 环境测得，
> **推翻了本文档初稿的核心假设**，并直接影响 §8 的优先级建议。

### 0.1 结论：**本方案的收益远低于初稿估计，不建议实施 Phase 1**

实测数据显示微信的 checkpoint **极其频繁**，WAL 可见性窗口只有**秒级**：

```
[+19.4s] message_6 / message_fts / session 开始写入 WAL
[+19.4s→23.8s] 已提交帧增长  message_6: 16→88   message_fts: 18→101   session: 8→44
[+29.6s] ★ CHECKPOINT 三库同时 salt1+1、cp_seq+1，新 WAL 已提交帧归 0
[+38.8s] ★ contact.db CHECKPOINT
```

**消息发出到主库可见，实测只有约 6~10 秒。** 而 SIWX 的自动同步间隔默认 30 分钟（且默认关闭）。

因此：
- SIWX 排除 WAL 造成的额外延迟 ≈ **6~10 秒**
- 真正主导端到端延迟的是**同步间隔**（最长 30 分钟）
- **加 WAL 的收益 ≈ 把 30 分钟 10 秒变成 30 分钟** —— 性价比极低

**建议：优先缩短自动同步间隔（零改动），而不是加 WAL。**

### 0.2 副产物：两个必须记录的格式事实（若将来仍要做 WAL）

**事实 A：WAL 文件大小 ≠ 有效数据。**
80 个 `.db` 中 76 个带非空 `-wal`，但**只有 6 个含有效提交帧，70 个是零有效帧的陈旧残留**。

机理：checkpoint **不清空 WAL 文件**，只是把 `salt1` 加一并重写文件头，旧帧原地留存。
实证：`contact.db-wal` 头 `salt1=113d2681`，首帧 `salt1=113d2680`（差 1）→ 解析器正确停在首帧。

→ **对 §4 决策 4 / §7 的修正**：缓存键**必须**基于 `salt1` + 有效提交帧数，
**不能**用 `wal_size` / `wal_mtime`。否则那 70 个库每次 WAL 被 reset 都会触发零收益的全量重解。

**事实 B：checkpoint 不由 4 MiB 阈值驱动。**
观测到 14 个 WAL 恰好 4.00 MiB（1024 页），但触发 checkpoint 时 WAL 只有 88 帧（≈352 KB），
远低于阈值 → 微信另有触发机制（定时 / 空闲 / 会话切换），与阈值无关。

### 0.3 实测同时验证了本文档的格式假设（全部成立）

| §3 的假设 | 实测结果 |
|---|---|
| `magic = 0x377f0682`（小端校验和） | ✅ 一致 |
| 头 32 字节 + 帧 24 字节 + 页 | ✅ 一致 |
| 头校验和覆盖前 24 字节 | ✅ 自洽 |
| 帧校验和覆盖 `帧头[0:8] + 页数据`，滚动累加 | ✅ 匹配 |
| 提交语义（`dbSize != 0` 为提交点） | ✅ `commitDbSize` 正确读出 |
| 逐帧 salt 与头 salt 必须一致 | ✅ 陈旧帧被正确识别并截断 |

**§3~§7 的技术内容经真机校验有效**，若将来因故需要实施，可直接作为实现依据。

### 0.4 若仍要实施：优先级调整

| 阶段 | 初稿建议 | 实测后建议 |
|---|---|---|
| Phase 1（合并 WAL） | 建议先做 | **不建议** —— 收益约 10 秒，不值这个复杂度与风险 |
| Phase 2（增量 patch） | 按需 | 更不建议 |
| 缩短同步间隔 | 列为非目标 | **这才是正确的杠杆**，且零改动 |

**唯一可能让 WAL 重新有价值的场景**：同步间隔被压到远小于 checkpoint 周期（例如 1~5 秒的实时预览）。
但即便如此，checkpoint 只有 ~10 秒周期，收益上限仍是约 10 秒。

---

## 1. 背景与现状

### 1.1 当前行为

SIWX 在收集数据库时**主动排除** `-wal` / `-shm`：

- `siwx/sqlcipher.py:85-93` — `collect_db_files()` 的过滤条件
  `if not name.endswith(".db") or name.endswith("-wal") or name.endswith("-shm"): continue`
- `docs/data-safety-audit-2026-09-19.md:279` 记录了理由：**避免读到未提交的中间态**
- `docs/module-sqlcipher.md:70` 同样记录「排除 -wal / -shm 文件」

### 1.2 由此产生的可见性延迟

缓存判定只看主库（`siwx/extract.py:275-281`）：

```python
mtime = int(e.path.stat().st_mtime)          # 只 stat .db 本身
if (use_cache and m and m.get("size") == e.size and m.get("mtime") == mtime
        and m.get("key") == key_hex and dst.is_file()):
    # 缓存命中
```

新消息先写入 `-wal`，主库的 `size` / `mtime` **不会变化**，因此：

```
新消息写入 -wal
  → 等微信 checkpoint 回写主库（主库 size/mtime 才变）
  → 等自动同步间隔（siwx/server.py:678-713，默认 30 分钟，且默认关闭）
  → 解密落盘
  → 可见
```

**端到端延迟：分钟级到数十分钟。**

### 1.3 本方案要解决的问题

让 SIWX 能够读到 `-wal` 中**已提交但尚未 checkpoint** 的消息，把可见性延迟的上限从「依赖微信 checkpoint 时机」变为「依赖同步间隔」。

---

## 2. 目标与非目标

### 目标

1. 解密产物包含 WAL 中已提交的数据
2. 输出形态不变 —— 仍是单个明文 `.db`，**下游（api_chat / stats / exporter / mcp_server）零改动**
3. 不带 `-wal` 时，产物与旧版**字节级一致**（回归红线）
4. 不破坏现有数据安全铁律（原子写、page1 HMAC 前置校验、临时文件唯一性）

### 非目标（明确排除）

| 项 | 原因 |
|---|---|
| 缩短自动同步间隔 | 属调度策略，独立议题（见 §8 Phase 2） |
| 实时预览 UI / 轮询 | 与 SIWX「备份归档」定位不符 |
| 主库逐页 HMAC 校验 | 性能不可接受（大库十几万页）；WAL 页数少，可负担 |
| 处理 `-shm` | `-shm` 是 WAL 索引，解析 WAL 本身不需要它 |
| 加密 / 密钥策略改动 | 完全无关 |

---

## 3. SQLCipher WAL 格式事实（实现依据）

以下为已核实的格式事实，实现必须严格遵循：

### 3.1 WAL 文件结构

```
[WAL 头 32 字节]
  magic(4) + version(4) + page_size(4) + checkpoint_seq(4) + salt1(4) + salt2(4) + checksum(8)

[帧 1] [帧 2] ... [帧 N]
  帧头 24 字节：pgno(4) + dbSize(4) + salt1(4) + salt2(4) + checksum(8)
  帧体：page_size 字节的加密页
```

- `magic = 0x377F0682` → 校验和使用**平台（小端）**字节序
- `magic = 0x377F0683` → 校验和使用**字节交换（大端）**字节序
- 合法 `page_size`：512 / 1024 / 2048 / 4096 / 8192 / 16384 / 32768 / 65536
- **帧校验和只覆盖帧头前 8 字节（pgno + dbSize）+ 页数据**，不含 salt 字节
- 校验和是**滚动**的：每帧基于前一帧的结果累加

### 3.2 提交语义（关键）

- 帧头 `dbSize != 0` 表示**该帧是一个提交点**
- **只有到最后一个提交点为止的帧才有效**，其后的帧是未提交的尾巴，必须丢弃
- 最后一个提交点的 `dbSize` = **提交后数据库的总页数**
- 若不存在任何 `dbSize != 0` 的帧 → 无有效提交 → 应按「无 WAL」处理

### 3.3 SQLCipher 的叠加约定

- **WAL 页使用主库的密钥和 salt**，帧内**没有** per-frame 的 salt 前缀
- 页 1 的 16 字节 salt **保留在 WAL 的页 1 帧内**（与主库页 1 布局一致）
- 因此 SIWX 现有页解密逻辑可直接复用于 WAL 帧，布局完全一致：

```
页 1：  [salt 16][密文 4000][IV 16][HMAC 64] = 4096
其他页：[密文 4016][IV 16][HMAC 64]        = 4096
```

- **强校验机会**：解密 WAL 页 1 帧后，其前 16 字节 salt 必须等于主库的 salt。不等 → WAL 与主库不匹配，必须拒绝。

---

## 4. 关键设计决策

### 决策 1：输出形态 —— **合并进明文库**（方案 A）

| 方案 | 做法 | 评价 |
|---|---|---|
| **A. 合并**（选定） | 把 WAL 已提交帧应用到解密后的明文库，产出等价于「已 checkpoint」的单个 `.db` | 下游零改动；产物语义不变 |
| B. 并行输出 `-wal` 明文 | 额外落盘 `<rel>-wal` | 每个下游都要知道 WAL 的存在，blast radius 大 |
| C. 仅内存合并 | 不落盘 | 破坏「落盘归档」定位 |

**选定 A。** 理由：SIWX 的产物被 `api_chat` / `stats` / `exporter` / `mcp_server` 用 `sqlite3` 直接打开，方案 A 让它们完全无感。

### 决策 2：WAL 是主库的**附件**，不是独立 DbEntry

`-wal` 文件开头是 WAL 头，不是 SQLCipher 的 salt。若把它当成独立 `DbEntry`，`_read_page1()` 会读到 WAL 头，导致 salt 与密钥张冠李戴。

**做法**：给 `DbEntry` 增加 `wal_path` 槽位，把 WAL 挂在它所属的主库上。

```python
class DbEntry:
    __slots__ = ("rel", "path", "size", "salt_hex", "page1", "wal_path")
    #                                                      ^^^^^^^^^ 新增
```

`collect_db_files()` 在遍历时，若存在 `<db>-wal` 则一并记录。`wal_path` 默认 `None`，保证向后兼容。

> ⚠️ `DbEntry` 被 `extract.py`（3 处）和 `server.py`（3 处）通过 `collect_db_files` 间接使用，但构造只在 `collect_db_files` 内部发生 —— 加默认参数是安全的。

### 决策 3：一致性 —— **先读 WAL（小），再流式读主库（大），双端签名校验**

这是最容易写错的地方。WAL 与主库必须来自**同一时刻**，否则会把新 WAL 应用到旧主库，产出不一致的库。

主库是流式读取（不整库进内存），所以不能简单照搬「整文件读入 + 前后 stat」的做法。

**选定算法**：

```
1. sig_db_0  = stat(src)                # (size, mtime_ns)
2. sig_wal_0 = stat(wal)                # 不存在则 None
3. wal_bytes = read_stable(wal)         # WAL 小，整读；内部重试
4. 解析 WAL → 已提交帧 + commit_db_size
5. 流式解密主库（把 WAL 覆盖的页替换掉）
6. sig_db_1  = stat(src)
7. sig_wal_1 = stat(wal)
8. 若 sig_db_1 != sig_db_0 或 sig_wal_1 != sig_wal_0 → 抛 SourceChanged
```

**为什么这样可行**：WAL 很小（SQLite 默认约 1000 页 / 4MB 触发自动 checkpoint），整读代价低；主库只在 checkpoint 时变化（罕见）。因此冲突概率低，重试能收敛。

`read_stable()` 的实现参照已验证的做法：读取前后各 `stat` 一次，`(size, mtime_ns)` 一致且 `len(data) == size` 才接受，最多重试 2 次，否则抛错。

**重试策略**：上层重试 2 次，仍失败则**回退到不读 WAL**（产出与旧版一致的结果），并记录日志。绝不因为 WAL 读不稳而让整个解密失败。

### 决策 4：缓存键必须基于 `salt1` + 有效提交帧数（**不可用文件大小/时间戳**）

> ⚠️ 本节已按 §0.2 事实 A 修正。初稿曾建议用 `wal_size` / `wal_mtime`，实测证明是错的。

**陷阱 1（性能）**：WAL 每条消息都在变。若 manifest 只要「WAL 变了就重解」，
会导致每次同步都全量重解所有数据库 —— 性能灾难。

**陷阱 2（更隐蔽，实测发现）**：checkpoint **不清空 WAL 文件**，只把 `salt1` 加一。
因此 `wal_size` / `wal_mtime` 会在**零有效帧**的情况下变化。实测 76 个带 WAL 的库中，
70 个是这种陈旧残留 —— 用 size/mtime 做缓存键会让这 70 个库产生**零收益的全量重解**。

**正确做法**：缓存键 = `wal_salt1` + `wal_salt2` + 有效提交帧数 + `commit_db_size`。

- **Phase 1（正确性优先）**：manifest 记录上述四元组。变化则重解。
- **Phase 2（增量）**：`salt1/salt2` 未变且帧数只增 → **只解密并 patch 新增帧**，不做全量重解。

详见 §8。**注意：§0 已判定本方案整体不建议实施，本节保留作为技术记录。**

---

## 5. 算法

### 5.1 WAL 解析（新增模块）

建议新建 `siwx/wal.py`（与 `sqlcipher.py` 同层，职责单一）：

```python
WAL_HDR_SZ = 32
FRAME_HDR_SZ = 24
MAGIC_LE = 0x377F0682
MAGIC_BE = 0x377F0683
VALID_PAGE_SIZES = (512, 1024, 2048, 4096, 8192, 16384, 32768, 65536)

@dataclass(frozen=True)
class WalParse:
    frames: list[tuple[int, bytes]]   # (pgno, 原始加密页)；仅含到最后一个提交点
    header_ok: bool
    corrupt_before_commit: bool       # 头非法或首帧校验失败 → 显式失败
    commit_db_size: int | None        # 提交后总页数；None 表示无有效提交

def parse_committed(wal_bytes: bytes) -> WalParse: ...
def _checksum(data, s0, s1, little_endian) -> tuple[int, int]: ...
```

**语义要点**：

- 头 32 字节的校验和必须自洽，否则 `header_ok=False`
- 逐帧滚动校验：salt 必须等于头里的 salt1/salt2，且校验和必须匹配；一旦不匹配**立即停止**（后续帧全部不可信）
- `dbSize != 0` 时把当前帧序列**快照为已提交集**（后写的提交覆盖前面的）
- 文件长度不足 32 字节 → 空结果，不报错
- 区分两种情况：
  - 头非法 / 首帧就坏 → `corrupt_before_commit=True`（**显式失败**，因为无法判断是坏 WAL 还是非 WAL 文件）
  - 尾部有未提交的不完整帧 → 良性，保留最后的提交

### 5.2 解密合并（改造 `decrypt_database`）

签名扩展（**`wal_path` 带默认值，保证现有调用与测试不变**）：

```python
def decrypt_database(src: Path, dst: Path, enc_key: bytes,
                     progress=None, wal_path: Path | None = None) -> int:
```

流程：

```
1. 若 wal_path 存在：
     wal_bytes = read_stable(wal_path)
     parsed = parse_committed(wal_bytes)
     if parsed.corrupt_before_commit: raise ValueError("WAL 在首个 commit 前损坏")
     wal_pages = {}
     for pgno, raw in parsed.frames:
         wal_pages[pgno] = decrypt_page(raw, enc_key, pgno)   # 含 HMAC 校验 + 页1 salt 交叉校验
     final_pages = parsed.commit_db_size        # 可能为 None

2. 主库流式解密（现有逻辑），但：
     - 页号若在 wal_pages 中 → 写入 WAL 版本，而非主库版本
     - 页 1 来自 WAL 时，先校验其 salt == 主库 salt，不等则拒绝

3. 收尾：
     - final_pages 为 None        → 保持主库页数
     - final_pages > 主库页数      → 按页号升序追加 WAL 中 [主库页数+1, final_pages] 的页
                                     若该区间有缺口 → 放弃 WAL，回退到不读 WAL
     - final_pages < 主库页数      → 截断到 final_pages

4. 仍保持：先写 .part → 整库成功 → os.replace 原子替换
5. 源签名复核（§4 决策 3 的第 6-8 步），不符则抛 SourceChanged
```

**逐页 HMAC 校验**：WAL 页数少，建议对**每个 WAL 帧**做完整 HMAC 校验（现有 `verify_enc_key` 只校验页 1，可抽出通用的 `_verify_page_hmac(enc_key, page, pgno)`）。这是把「坏数据进归档」的风险压到最低的关键一步，成本可忽略。

**输出页布局**（与现有完全一致，不得改动）：

```
页 1：  [b"SQLite format 3\x00" 16][明文 4000][零 80] = 4096
其他页：[明文 4016][零 80]                          = 4096
```

### 5.3 解密池的任务结构

`siwx/pool.py` 的 `_worker` 任务元组需扩展第 5 项：

```python
# 旧：(rel, src, dst, key_hex)
# 新：(rel, src, dst, key_hex, wal_path_or_None)
def _worker(task):
    rel, src, dst, key_hex, wal_path = task
    ...
    pages = decrypt_database(Path(src), Path(dst), bytes.fromhex(key_hex),
                            wal_path=Path(wal_path) if wal_path else None)
```

`decrypt_parallel()` 本身无需改动（只透传）。

---

## 6. 改动清单

### 新增

| 文件 | 内容 | 预估规模 |
|---|---|---|
| `siwx/wal.py` | WAL 解析（头/帧校验和、提交语义、`WalParse`） | ~150 行 |
| `docs/module-wal.md` | 模块文档（格式事实 + 提交语义 + 校验规则） | ~80 行 |
| `docs/wal-support-plan.md` | 本文档 | — |

### 修改

| 文件 | 位置 | 改动 |
|---|---|---|
| `siwx/sqlcipher.py` | `DbEntry.__slots__` | 加 `"wal_path"`（默认 `None`） |
| `siwx/sqlcipher.py` | `collect_db_files()` | 探测 `<db>-wal` 并挂到 `DbEntry`；docstring 从「排除 -wal/-shm」改为「排除 -shm，-wal 作为附件记录」 |
| `siwx/sqlcipher.py` | 新增 `read_stable()` | 稳定性读取助手（前后 stat 一致 + 重试） |
| `siwx/sqlcipher.py` | 新增 `_verify_page_hmac()` | 通用逐页 HMAC 校验（由 `verify_enc_key` 抽出，保持原函数签名不变） |
| `siwx/sqlcipher.py` | `decrypt_database()` | 加 `wal_path` 参数；主库流式循环支持页替换；收尾追加/截断；双端签名复核 |
| `siwx/pool.py` | `_worker()` | 任务元组第 5 项；透传 `wal_path` |
| `siwx/extract.py` | `decrypt_dir()` | manifest 判定纳入 WAL 签名；`tasks` 元组加第 5 项；`files` 报告增加 `wal` 字段 |
| `siwx/extract.py` | `manifest[rel] = {...}` | 写入 `wal_size` / `wal_mtime`（Phase 1）；Phase 2 再加 `wal_salt1/2` / `wal_frames` |
| `siwx/server.py` | `decrypt_dir` 调用点（269/288/307） | 无需改（`entries` 已含 `wal_path`） |
| `siwx/server.py` | `collect_db_files` 调用点（237/302/481） | 无需改（`wal_path` 是附件，不参与 salt/密钥统计） |

### 不需要改动（重要）

`api_chat.py` / `api_stats.py` / `stats.py` / `exporter.py` / `export_stream.py` / `mcp_server.py` / `media.py` / `voice.py` —— **全部零改动**。这是决策 1 的价值所在。

---

## 7. manifest 迁移与兼容

### 7.1 新格式

```jsonc
{
  "message/message_0.db": {
    "size": 12345678,
    "mtime": 1758888888,
    "pages": 3015,
    "key": "…",
    "wal_size": 81920,        // 新增；无 WAL 时为 0
    "wal_mtime": 1758888999,  // 新增；无 WAL 时为 0
    "wal_salt1": "…",         // Phase 2
    "wal_salt2": "…",         // Phase 2
    "wal_frames": 37          // Phase 2：已应用的帧数
  },
  "@source": "d:\\…"
}
```

### 7.2 兼容策略

判定改为：

```python
wal_size  = wal.stat().st_size     if wal else 0
wal_mtime = int(wal.stat().st_mtime) if wal else 0

hit = (m.get("size") == e.size
       and m.get("mtime") == mtime
       and m.get("key") == key_hex
       and m.get("wal_size", 0) == wal_size       # 旧条目默认 0
       and m.get("wal_mtime", 0) == wal_mtime     # 旧条目默认 0
       and dst.is_file())
```

**兼容性分析**：

| 场景 | 结果 | 评价 |
|---|---|---|
| 旧 manifest（无 wal 字段）+ 无 WAL 文件 | 0 == 0 → **命中** | ✅ 升级用户不触发无谓重解 |
| 旧 manifest（无 wal 字段）+ 有 WAL 文件 | 0 != size → **未命中** → 重解一次 | ✅ 正确；一次性代价 |
| 新 manifest + WAL 未变 | 命中 | ✅ |
| 新 manifest + WAL 变了 | 未命中 → 重解 | ✅ 正确（Phase 1 语义） |

`@source` 来源保护逻辑**完全不变**。

### 7.3 是否需要缓存版本号

**不需要**。靠 `default 0` 的字段级兼容即可，无需像 `stats.py` 那样引入 `_CACHE_VERSION`。理由：manifest 的键是 `rel`，新增字段是加法式的，不会让旧条目误判为有效或无效。

---

## 8. 阶段划分

### Phase 1：正确性（建议先做）

- 完成 §5 全部算法
- manifest 纳入 `wal_size` / `wal_mtime`
- **收益**：可见性不再依赖微信 checkpoint，延迟上限 = 同步间隔（默认 30 分钟）
- **代价**：WAL 变化时全量重解该库。30 分钟一次全量重解在可接受范围（实测解密约 175 MB/s）
- **不做**：缩短同步间隔（此时会退化为频繁全量重解）

### Phase 2：增量 patch（缩短同步间隔的前提）

- manifest 记录 `wal_salt1` / `wal_salt2` / `wal_frames_applied`
- 下次解密时：
  - salt 未变 且 帧数 ≥ 已应用数 → **只解析新增帧**，`seek` 到输出文件对应页偏移覆写
  - salt 变了（发生 checkpoint）→ 主库必然也变了 → 走全量路径
  - salt 未变 但 主库签名变了 → 可疑，走全量路径（保守）
- **收益**：同步间隔可降到分钟级甚至更短
- **风险**：patch 路径绕过了「整库重写」，需要额外的输出文件完整性校验（建议 patch 后重算页数并与 `commit_db_size` 比对）

**建议**：~~Phase 1 先落地并观察真实收益；Phase 2 只在确实需要短间隔时再做。~~
→ **已由 §0 的实测结论取代：两个阶段都不建议做。** 真正的瓶颈是同步间隔，不是 WAL 可见性。
本节保留作为技术记录，供将来同步间隔被压到秒级时重新评估。

---

## 9. 风险与缓解

| # | 风险 | 影响 | 缓解 |
|---|---|---|---|
| R1 | 误读未提交帧 | 归档被污染（最严重） | 严格实现「只取到最后一个提交点」；逐帧滚动校验；任何校验失败立即截断 |
| R2 | 撕裂读：新 WAL 配旧主库 | 产出不一致的库 | 双端签名（db + wal）前后各校验一次；不符则抛 `SourceChanged`，上层重试 2 次后**回退到不读 WAL** |
| R3 | WAL 页区间有缺口 | 输出文件损坏 | 追加区间缺口检测；发现缺口 → 放弃 WAL，回退到旧行为 |
| R4 | WAL 与主库 salt 不匹配 | 解密出垃圾数据 | 解密 WAL 页 1 帧后校验其 salt == 主库 salt；不等则拒绝整个 WAL |
| R5 | 性能回归（WAL 频繁变化 → 频繁全量重解） | CPU / 磁盘压力 | Phase 1 限制在长间隔同步；Phase 2 用增量 patch 解决 |
| R6 | 微信正在写 WAL 时读到半帧 | 解析失败或截断错误 | 滚动校验天然拒绝半帧；`read_stable` 前后 stat 一致 |
| R7 | 改动破坏原子写 | 已有明文库被覆盖成截断文件 | **红线**：`.part` + `os.replace` 结构保持不动；回归测试必须覆盖 |
| R8 | 旧用户升级后首次全量重解 | 一次性耗时 | 可接受；manifest 默认 0 的设计已最小化影响面 |
| R9 | `-wal` 文件被微信独占无法读 | WAL 读不到 | 复用现有「复制到 mkstemp 临时文件再读」的模式；失败则回退不读 WAL |

### 回退开关（建议）

增加环境变量 `SIWX_NO_WAL=1` 强制关闭 WAL 读取，行为与旧版完全一致。参照现有 `SIWX_NO_PLUGINS=1` 的既有约定，便于线上快速止损。

---

## 10. 测试计划

### 10.1 单元测试（新增，建议放 `tests/test_regressions.py` 或新建 `tests/test_wal.py`）

**WAL 解析**

1. 合成合法 WAL（1 个提交）→ 帧数与 `commit_db_size` 正确
2. 多个提交 → 只保留最后一个提交的帧
3. 未提交尾巴 → 尾巴被丢弃，最后提交保留
4. 头校验和不匹配 → `header_ok=False`
5. 首帧校验和不匹配 → `corrupt_before_commit=True`
6. 帧 salt 与头 salt 不一致 → 截断到该帧之前
7. 文件不足 32 字节 → 空结果，不抛异常
8. 大端 magic（`0x377F0683`）→ 校验和按大端计算
9. 非 4096 的 page_size（如 1024）→ 正确解析

**解密合并**

10. 无 WAL → 产物与旧版**字节级一致**（关键回归）
11. WAL 覆盖已有页 → 产物含 WAL 版本
12. WAL 增长数据库（`commit_db_size > 主库页数`）→ 正确追加，页数 == `commit_db_size`
13. WAL 截断数据库（`commit_db_size < 主库页数`）→ 正确截断
14. WAL 页 1 的 salt 与主库不符 → 拒绝
15. WAL 帧 HMAC 校验失败 → 拒绝
16. 追加区间有缺口 → 回退到不读 WAL
17. 源在读取过程中变化 → 抛 `SourceChanged`
18. WAL 损坏且无法回退 → 产物仍与旧版一致（不读 WAL）

**原子性与残留**（复用现有 `TestDecryptAtomic` 的思路）

19. WAL 路径下失败 → 已有明文库不被覆盖
20. WAL 路径下成功 → 无 `.part` / `.tmp` 残留

**manifest**

21. 旧条目（无 wal 字段）+ 无 WAL → 缓存命中
22. 旧条目 + 有 WAL → 缓存未命中，重解一次
23. WAL 变化 → 缓存未命中
24. WAL 未变、主库未变 → 缓存命中
25. `@source` 保护逻辑不受影响

### 10.2 必须保持的既有测试

以下现有测试**必须继续通过**，是本方案的验收门槛：

- `TestDecryptAtomic`（原子写 + 无残留）
- `TestCryptoIntact`（页布局 + 手写 CBC 与标准库一致）
- `TestTempFileUniqueness`（D-2 临时文件唯一性）
- `TestManifestSourceGuard`（`@source` 来源保护）
- `TestAccountConflicts`（D-1 同名账号冲突）

### 10.3 实测验证（真机）

- 用真实微信数据库跑一次，对比「读 WAL」与「不读 WAL」的产物：**消息条数应增加或持平，绝不应减少**
- 用 `sqlite3 PRAGMA integrity_check` 校验产物完整性
- 对比 `SELECT COUNT(*)` 与微信客户端显示的条数

---

## 11. 需要同步更新的文档

| 文档 | 位置 | 改动 |
|---|---|---|
| `docs/module-sqlcipher.md` | 第 70 行「排除 -wal / -shm 文件」 | 改为「排除 -shm；-wal 作为附件参与合并」 |
| `docs/data-safety-audit-2026-09-19.md` | 第 279 行 | 该条已不再是当前行为，需标注「已由 WAL 方案取代」并说明新的安全保证 |
| `docs/architecture.md` | 「阶段 3：数据库解密」 | 补充 WAL 合并步骤 |
| `docs/module-wal.md` | 新建 | 格式事实 + 提交语义 |
| `README.md` | 「性能表现」表 | 若解密速度有变化，更新实测值 |
| `docs/README.md` | 文档地图 | 加入 `module-wal.md` |
| `siwx/__init__.py` | `__version__` | 按发布流程递增（此功能属 minor） |

---

## 12. 验收标准

方案完成的判定条件：

1. ✅ 无 `-wal` 时，产物与 v5.0.3 **字节级一致**（用同一源库、同一密钥对比 SHA-256）
2. ✅ 有 `-wal` 时，产物包含已提交帧的数据，且 `PRAGMA integrity_check` 通过
3. ✅ 未提交帧**永不**出现在产物中
4. ✅ WAL 损坏 / 读不稳时，**自动回退**到不读 WAL，且不报错、不产生坏产物
5. ✅ 现有 89 个回归测试 + 5 个既有安全测试类全部通过
6. ✅ `.part` + `os.replace` 原子写结构未被改动
7. ✅ 临时文件仍全部使用 `tempfile.mkstemp`
8. ✅ `api_chat` / `stats` / `exporter` / `mcp_server` 零改动
9. ✅ `SIWX_NO_WAL=1` 可一键回退到旧行为

---

## 13. 未决问题（需维护者决策）

1. **Phase 2 是否要做？** 取决于是否真的需要把同步间隔降到分钟级。若只做 Phase 1，收益是「不再依赖 checkpoint」，这本身可能已经够用。
2. **是否需要 `-shm`？** 本方案判定不需要（直接解析 WAL 即可）。若实测发现某些微信版本 WAL 不可直接解析，需重新评估。
3. **回退开关命名**：`SIWX_NO_WAL=1`（建议）还是复用 `use_cache=False` 之类的现有入口？
4. **`files` 报告字段**：是否需要在解密报告里区分「主库页数」与「WAL 贡献页数」，便于用户判断是否真的读到了 WAL？
5. **版本号**：v5.1.0（minor，新增能力）还是 v5.0.4（patch）？建议 minor。

---

## 附：与 WechatVibe 实现的对照

本方案在 WAL 解析与提交语义上参考了 WechatVibe 的 `native-reader/wr/wal.py`（Apache-2.0，独立项目）。**差异在于**：

| 维度 | WechatVibe | 本方案（SIWX） |
|---|---|---|
| 产物 | 内存快照，不落盘 | 落盘明文 `.db`（归档） |
| 合并方式 | 全页进内存后组装 | **流式 + 页替换**，不整库进内存 |
| 主库逐页 HMAC | 全页校验 | 仅 WAL 帧校验（性能取舍） |
| 刷新 | 4 秒轮询 + 指纹 | 同步间隔（Phase 1）/ 增量 patch（Phase 2） |
| 一致性 | 整文件读入 + 前后 stat | WAL 整读 + 主库流式 + 双端签名 |

若采用本方案，建议在 `siwx/wal.py` 的模块 docstring 中注明参考来源与许可，保持项目的合规习惯（参照 `electron/laya/` 的做法）。
