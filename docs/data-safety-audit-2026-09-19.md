# SIWX 数据安全专项审查 · v5.0.2

- **审查日期**：2026-09-19
- **审查范围**：本地单用户可信环境下的**数据完整性 / 数据丢失 / 数据覆盖**风险
- **审查口径**：**排除**「本机其他进程可访问 127.0.0.1」「无鉴权」「DNS rebinding」等本地边界类问题（见 `security-audit-2026-09-19.md`）。本地个人工具假定单用户可信环境，此处只关心**会破坏、覆盖、错配、丢失用户数据**的缺陷。
- **审查基线**：commit `1fd2d1d`（main，工作区干净）
- **审查性质**：只读审查，未修改任何业务代码

---

## 修复状态（2026-09-19 更新）

P0/P1 项已实施，方案见第九章兼容性分析。改动范围：

| 文件 | 行数 | 内容 |
|---|---|---|
| `siwx/sqlcipher.py` | +31 / -10 | D-2：两处临时文件改 `mkstemp`，并补齐失败路径清理 |
| `siwx/discover.py` | +22 | 方案 C：新增 `find_account_conflicts()` |
| `siwx/extract.py` | +44 / -4 | 方案 B：`SOURCE_FIELD` + 来源保护 + report 增加 `conflicts` |
| `siwx/server.py` | +18 / -6 | 方案 C：作业启动告警 + `/api/status` 暴露 `conflicts` |
| `tests/test_regressions.py` | +300 | 15 个新测试（含升级兼容场景） |

**验证结果**：`python -m unittest discover -s tests` → **199 passed (skipped=1)**（修复前 184）。

**未实施**：方案 A（输出目录加指纹）—— 有破坏性，见 9.4。

| 编号 | 问题 | 严重度 | 修复状态 |
|---|---|---|---|
| **D-1** | 多账号同名目录 → 解密产物互相覆盖 | **严重** | ✅ 已缓解（方案 B + C） |
| **D-2** | `sqlcipher` 临时文件 PID 命名 → 并发 page1 错配 | **高** | ✅ 已修复 |
| D-3 | `manifest` 键不带来源标签 | 中 | ✅ 已修复（`@source`） |
| D-4 | `_IMG_CACHE` 无锁 | 低 | 未修复（不损数据） |
| D-5 | 批量导出 `export_{秒级时间戳}` 撞名 | 低 | 未修复 |

---

## 二、D-1 严重：多账号同名目录导致数据覆盖（本机已存在）

### 事实

`output/<wxid>/` 这个目录名**只取 `db_dir` 的父目录名**：

```python
# siwx/discover.py:77
def wxid_of(db_dir) -> str:
    p = Path(db_dir)
    return p.parent.name or "unknown"
```

而 `find_wechat_data_dirs()` 的**去重只看 `db_dir` 路径本身**，不看推导出的 `wxid`：

```python
# siwx/discover.py:155-163
def add(wxid, db):
    key = str(Path(db).resolve()).casefold()
    if key in seen:          # ← 只按路径去重
        return
    seen.add(key)
    out.append((wxid, str(db)))
```

而 `roots` 会扫描**所有盘符 + 所有用户目录**（`discover.py:110-128`）。因此「同一账号在 C 盘和 D 盘各有一份 xwechat_files」这种极常见的情况（换过数据盘、迁移残留、备份副本），会产生两条 `wxid` 相同、`db_dir` 不同的记录，**进而映射到同一个输出目录**。

### 本机实测（真实复现）

```
$ python -c "from siwx.discover import find_wechat_data_dirs; ..."

本机扫描到 3 个账号:
  wxid=wxid_bpk8p2yx5den22_645e
    db_dir=D:\xwechat_files\wxid_bpk8p2yx5den22_645e\db_storage
  wxid=wxid_z30ttr1sg8h222_6409
    db_dir=C:\Users\li\xwechat_files\wxid_z30ttr1sg8h222_6409\db_storage
  wxid=wxid_z30ttr1sg8h222_6409
    db_dir=D:\xwechat_files\wxid_z30ttr1sg8h222_6409\db_storage

wxid 重复的组: 1
  !! wxid_z30ttr1sg8h222_6409: 2 个不同 db_dir 指向同一输出目录
       C:\Users\li\xwechat_files\wxid_z30ttr1sg8h222_6409\db_storage
       D:\xwechat_files\wxid_z30ttr1sg8h222_6409\db_storage
```

两个副本的规模对比：

| 副本 | 库数 | 总大小 |
|---|---|---|
| `C:\Users\li\xwechat_files\...` | 16 | **6.6 MB** |
| `D:\xwechat_files\...` | 32 | **566.7 MB** |

C 盘那份是**空壳残留**（微信换数据盘后的旧目录），D 盘才是真实数据。

### 覆盖后果

两者共用输出目录 `output/wxid_z30ttr1sg8h222_6409/`，其中 `rel` 路径重合 **16 个**，且内容规模差异巨大：

| rel | C 盘 | D 盘 |
|---|---|---|
| `contact\contact.db` | 1.23 MB | **3.46 MB** |
| `contact\contact_fts.db` | 0.54 MB | **1.48 MB** |
| `emoticon\emoticon.db` | 2.11 MB | **2.52 MB** |

`decrypt_dir` 的写入路径是 `dst = out_root / e.rel` 且用 `os.replace` **原子覆盖**（`sqlcipher.py:174`）——原子性保证了「不写坏文件」，但**不保证「不覆盖更好的数据」**。

同时 `save_manifest` 用**当前这次扫描到的 entries 全量重建** manifest（`extract.py:289-290`），所以 C 盘跑完后 manifest 只剩 16 条，D 盘的 32 条记录全部丢失。

**完整损失链**：
1. 微信在 C 盘副本上运行（或用户切回 C 盘）→ 跑一次解密/同步
2. 16 个同名库被 6.6 MB 空壳覆盖（`contact.db` 3.46 MB → 1.23 MB）
3. manifest 被改写成 16 条 → 下次跑 D 盘时全部判为「未缓存」
4. D 盘需**全量重解 566 MB**（白跑）
5. 若 C 盘副本被删除，输出目录里残留的是空壳数据 + 失效 manifest

### 修复建议

**方案 A（推荐，改动小）**：输出目录名加入 `db_dir` 的稳定指纹，避免跨盘同名冲突：

```python
# siwx/discover.py
import hashlib

def account_key(db_dir: str) -> str:
    """输出目录名：wxid + db_dir 指纹，避免多盘同名账号互相覆盖。"""
    wxid = wxid_of(db_dir)
    digest = hashlib.md5(str(Path(db_dir).resolve()).casefold().encode()).hexdigest()[:6]
    return f"{wxid}_{digest}"
```

但这会改变现有 `output/<wxid>/` 布局，需要一次**迁移兼容**（旧目录仍可读，新数据写新目录），可能影响 `api_chat._accounts()`、`stats`、`exporter` 中所有 `_out_root() / account` 的调用点 —— 改动面较大。

**方案 B（最小改动，先止血）**：在 `find_wechat_data_dirs()` 的去重阶段把**同名冲突显式暴露**，并让后续解密拒绝覆盖非本次来源的产物：

```python
# discover.py: 增加按 wxid 的冲突检测
seen_wxid = {}
conflicts = []
for wxid, db in out:
    if wxid in seen_wxid:
        conflicts.append((wxid, seen_wxid[wxid], db))
    else:
        seen_wxid[wxid] = db
```

配合 `decrypt_dir` 在 manifest 中记录来源 `db_dir`，来源不一致时**默认跳过并警告**，而不是静默覆盖：

```python
# extract.py decrypt_dir: 命中已有产物但来源不同 -> 要求显式确认
src_tag = str(Path(db_dir).resolve()).casefold()
prev = manifest.get("@source")
if prev and prev != src_tag and dst.is_file():
    log(f"  [decrypt] ⚠ {e.rel} 已存在但来源不同（{prev} → {src_tag}），跳过以免覆盖")
    continue
```

**方案 C（兜底，必做）**：无论选 A 还是 B，`save_manifest` 都应**合并**而非**重建**：

```python
# extract.py: 保留不在本次 entries 中的历史记录
merged = dict(manifest or {})   # 以旧 manifest 为基础
for rel in updated_this_run:
    merged[rel] = new_rec
save_manifest(out_root, merged)
```

这能直接消除「换了副本 → 缓存全废 → 全量重解」的次生损失。

---

## 三、D-2 高：临时文件 PID 命名导致 page1 错配

### 位置

`siwx/sqlcipher.py:64` 与 `siwx/sqlcipher.py:117`

```python
tmp = Path(tempfile.gettempdir()) / f"siwx_p1_{os.getpid()}.tmp"
tmp_copy = Path(tempfile.gettempdir()) / f"siwx_db_{os.getpid()}.tmp"
```

### 问题

文件名只含 **PID**，不含线程/调用标识。而：

- Flask 以 `threaded=True` 运行（`server.py:761`）；
- `/api/status` **每次请求**都会对所有账号执行 `collect_db_files(db)` → `_read_page1()`（`server.py:471`）;
- 前端会**轮询** `/api/status`，后台 auto-sync 调度器（30s 轮询）也会触发同类扫描。

因此**同一进程内两个线程会同时走 `_read_page1` 的「微信占用中」回退分支**（`sqlcipher.py:62-70`）：

```python
T1: shutil.copy2(A, tmp)      # tmp = siwx_p1_<pid>.tmp
T2: shutil.copy2(B, tmp)      # 同名覆盖，A 的内容被冲掉
T1: page1 = read(tmp)         # ← 读到的是 B 的 page1！
    tmp.unlink()              # T2 后续 read 失败
T2: read(tmp) → OSError → return None
```

### 数据影响

1. **A 的 `salt_hex` / `page1` 被登记为 B 的值** → `extract_keys_for_dir` 中 `page1_by_salt` / `salt_to_dbs` 张冠李戴；
2. 更糟的是走 `keystore.insert(store, salt, key, ...)` 时，**把 B 的 salt 与 A 的 key 配对写入 DPAPI 密钥库**，污染长期密钥账本；
3. 交叉验证阶段（`extract.py:148-165`）可能因错配的 salt 误判「复用已知密钥」成功，导致**用错误密钥去解密**——`decrypt_database` 有 `verify_enc_key` 兜底（`sqlcipher.py:126`）会拒绝并标记 failed，**不会写坏文件**，但会产生大量假失败与错误缓存记录。

**好在**：`decrypt_database` 的 page1 HMAC 校验 + `os.replace` 原子替换构成了最后一道防线，**不会产出损坏的明文库**。损害集中在**密钥库污染**与**错误状态**。

### 修复建议

用 `tempfile.mkstemp`（或 `NamedTemporaryFile`）获得唯一名，并放进 `try/finally` 清理：

```python
def _read_page1(path: Path):
    try:
        with open(path, "rb") as f:
            page1 = f.read(PAGE_SZ)
    except OSError:
        tmp = None
        try:
            fd, name = tempfile.mkstemp(prefix="siwx_p1_", suffix=".tmp")
            os.close(fd)
            tmp = Path(name)
            shutil.copy2(path, tmp)
            with open(tmp, "rb") as f:
                page1 = f.read(PAGE_SZ)
        except OSError:
            return None
        finally:
            if tmp is not None:
                tmp.unlink(missing_ok=True)
    ...
```

`decrypt_database` 的 `siwx_db_<pid>.tmp`（`sqlcipher.py:117`）同理 —— 它在 `n<=1` 的串行分支里被逐任务调用，虽然同进程内不会并行到同一次调用，但只要将来有人把 `decrypt_parallel` 改成线程池，立刻变成同类 bug。**建议一并改为 `mkstemp`。**

---

## 四、D-3 中：manifest 账本失真（D-1 的次生影响）

> **⚠️ 更正（2026-09-19 复核）**：本节初版描述有误。初版称 `save_manifest` 会「全量重建 manifest」，经复核**不成立**。
>
> `extract.py:220` 处 `manifest = load_manifest(out_root) if use_cache else {}` 会**先载入旧 manifest**，`extract.py:281` 的 `manifest[rel] = {...}` 是在旧值上**叠加本次成功项**，`extract.py:290` 写回叠加结果。因此**不属于本次 entries 的历史记录会被保留**。
>
> **实测确认**（`diag_d1d2_compat.py` 场景 1/2）：写入 2 条旧记录，只更新其中 1 个 rel 后，条目数仍为 2，另一条完整保留。
>
> **后果修正**：不会出现「换副本后 manifest 被清空 → 全量重解 566 MB」。真实后果仅为：被覆盖的那 16 个 rel 因 size/mtime 变了而被判为未缓存（**这是正确行为**，因为它们确实变了），另 16 个 D 盘独有记录因文件仍在而正常命中缓存。
>
> 原 D-3 的核心问题仍然存在但严重度下降：遗留问题是**键 `rel` 不带来源标识**，当两个副本共用 `out_root` 时，manifest 中会同时存在「来自 C 盘的 rel」和「来自 D 盘的 rel」，二者无法区分，且下次任一副本运行时都会用自己的一份去覆盖同名记录。

**修复**：manifest 增加来源标签（如 `@source`），并在键上或值上携带来源指纹。详见第九章兼容性分析。

---

## 五、D-4 低：`_IMG_CACHE` 并发不安全

`siwx/media.py:39` 的 `OrderedDict` 在 `threaded=True` 下被多个请求线程同时 `move_to_end` / `__setitem__` / `popitem`（`media.py:383-396`）。CPython 的 GIL 能保证单次操作原子，但 `move_to_end` + 读取的两步组合（383-385）在 `popitem` 交错时可能抛 `KeyError`。

**影响**：图片接口返回 500，用户刷新即可。**不损坏落盘数据**，故列低危。

**修复**：加一个 `threading.Lock` 包住 383-396，或直接用 `collections.OrderedDict` + 锁的简单 LRU 封装。

---

## 六、D-5 低：批量导出目录撞名

`siwx/exporter.py:586` 的 `stamp = datetime.now().strftime("%Y%m%d_%H%M%S")` 是**秒级**，`run_export_multi` 用它构造 `total_dir = root / f"export_{stamp}"`（`exporter.py:587`）。同一秒内触发两次批量导出会共用根目录。

由于各会话子目录是 `f"{i+1:02d}_{display or chat}"`（`exporter.py:606`）带序号，**实际冲突概率低**；但同一用户在同一秒点两次「导出」并非不可能，且 `mkdir(exist_ok=True)` 不会报错。

**修复**：`stamp` 加毫秒（`%f` 前 3 位）或加短随机后缀。

---

## 七、值得肯定的数据保护设计

审查中确认了几处**做得正确、应保持**的实现：

1. **`decrypt_database` 的原子替换**（`sqlcipher.py:135-175`）：先写同目录 `.part` 临时文件，整库成功后才 `os.replace`；失败路径 `finally` 里 `unlink` 半成品。注释里还明确记录了「原实现直接写 dst 会把有效明文库覆盖成截断文件」的历史教训 —— 这是本项目数据安全的关键防线。
2. **page1 HMAC 校验前置**（`sqlcipher.py:126`）：密钥不匹配立刻抛错，绝不产出错解文件。
3. **`save_manual_data_dirs` 的稳定去重**（`discover.py:42-58`）：按 `resolve().casefold()` 去重，写入用 `tmp.replace(p)` 原子替换。
4. **keystore / media_key / auto_sync 的写入均为 `tmp` + `os.replace`**（`keystore.py:145-147`、`media.py:68-70`、`api_settings.py:60`），无半写风险。
5. **`collect_db_files` 排除 `-wal`/`-shm`**（`sqlcipher.py:84`），避免读到未提交的中间态。
6. **导出 zip 失败时保留 `export_dir`**（`exporter.py:348-350`）：`make_archive` 抛异常则 `rmtree` 不执行，用户可手动取回产物 —— 失败时的降级行为正确。

---

## 八、修复优先级建议

| 优先级 | 动作 | 涉及文件 | 说明 |
|---|---|---|---|
| **P0** | D-1 止血：`save_manifest` 改为**合并**而非重建 | `extract.py:289` | 1 行改动，立刻消除「换副本 → 全量重解」损失 |
| **P0** | D-1 暴露：`find_wechat_data_dirs` 检测同名 wxid 并告警 | `discover.py:155` | ~8 行，让用户至少知道有冲突 |
| **P1** | D-2：两处临时文件改 `mkstemp` | `sqlcipher.py:64,117` | ~10 行，消除密钥库污染源 |
| **P1** | D-1 根治：输出目录名加 db_dir 指纹 + 迁移兼容 | `discover.py` + 全部 `_out_root()/account` 调用点 | 改动面大，建议单独排期 |
| **P2** | D-3：manifest 增加来源标签 | `extract.py` | 配合 P0 的合并逻辑 |
| **P3** | D-4 / D-5：缓存加锁 / 时间戳加毫秒 | `media.py` / `exporter.py` | 体验类 |

---

## 九、兼容性分析：修复对升级用户是否具有破坏性

> **本节回答的核心问题**：老用户从 v5.0.x 升级到含修复的新版本后，已有的 `output/` 目录、`.siwx_cache.json`、密钥库会不会失效？会不会导致「数据消失」或强制重跑？
>
> 验证脚本：`diag_d1d2_compat.py`、`diag_d1_compat_plan_b.py`（隔离临时目录，不触碰真实数据）。

### 9.1 破坏性矩阵（实测结论）

| 修复项 | 对升级用户有破坏性？ | 说明 |
|---|---|---|
| **D-2**（临时文件改 `mkstemp`） | **无** | 纯内部实现替换，产物格式/目录结构零变更 |
| **D-1 方案 C**（仅检测同名并告警） | **无** | 只加日志，零副作用 |
| **D-1 方案 B**（manifest 加 `@source` 来源校验） | **无** | 首次运行行为不变，保护从第二次运行起生效 |
| **D-1 方案 A**（输出目录加 `db_dir` 指纹） | **有** | 目录名变更，9 处调用点需改，须配套迁移脚本 + 备份 |

### 9.2 D-2：完全无破坏性

改动仅是把 `siwx_p1_{pid}.tmp` / `siwx_db_{pid}.tmp` 换成 `tempfile.mkstemp()` 的随机名。实测确认：

- 临时文件只在本函数内 `copy → read → unlink`，**不持久化**、不被其他代码引用（`grep` 全仓库确认仅 `sqlcipher.py` 内部两处）；
- 位于系统 `TEMP`，**不在 `output/` 也不在 `data_dir()`**，不影响 manifest / keystore / 输出结构；
- 旧版本遗留的 `siwx_p1_*.tmp` 不再被读取，仅占少量 TEMP 空间，**不参与任何逻辑**。

**结论：可安全发布，升级用户零感知。** 唯一注意点是 `mkstemp` 会**直接创建文件并返回 fd**（权限 0600），需显式 `os.close(fd)`，与原先 `shutil.copy2` 自动创建/覆盖的语义不同——改动时留意。

### 9.3 D-1 方案 B：无破坏性，但保护不是立即生效

方案 B 在 manifest 中增加 `@source` 字段记录来源 `db_dir`。关键设计点是**把「无 `@source`」视为放行**，而不是视为冲突：

```python
prev_source = manifest.get("@source")
if prev_source is None:
    # 升级用户的历史产物，无来源标记 -> 放行，保持旧行为
    pass
elif prev_source != cur_source and dst.is_file():
    log(f"  [decrypt] ⚠ 来源变更，跳过以免覆盖")
    continue
```

实测各场景行为（`diag_d1_compat_plan_b.py`）：

| 场景 | 决策 | 结果 |
|---|---|---|
| 老用户升级（manifest 无 `@source`） | `run` | **行为与旧版本完全一致**，不卡住用户 |
| 写入 `@source` 后切到另一盘副本 | `skip-conflict` | 拦截，避免真实数据被空壳覆盖 |
| 同一来源反复运行 | `run` | 缓存判定照常生效，不影响日常使用 |
| 删除 `output/` 后重跑（全新用户） | `run` | 零影响 |
| `@source` 与业务 key 混存 | — | 读回完整，无冲突 |

**为什么加 `@source` 是安全的**：`manifest` 在全仓库**只有一处读取**（`extract.py:220`），且只用 `manifest.get(e.rel)` 查询具体 rel，**从不遍历**。因此新增一个非 `rel` 的键不会影响任何业务逻辑。（`@` 开头的键也不会与 rel 路径冲突——rel 均以目录名开头。）

**唯一代价**：保护从**第二次运行**起才生效。升级后第一次运行仍可能发生覆盖。若要在升级瞬间就提供保护，需把方案 C（同名 wxid 检测告警）**与方案 B 一起上线**——告警让用户至少知道存在冲突。

### 9.4 D-1 方案 A：有破坏性，需谨慎

若把输出目录从 `output/<wxid>/` 改为 `output/<wxid>_<db_dir指纹>/`：

**影响面（grep 实测）**：
- `siwx/api_chat.py` 8 处 `_out_root() / account`
- `siwx/mcp_server.py` 5 处
- `siwx/stats.py` 5 处
- `siwx/server.py` 2 处

**升级用户的实际后果**：
1. 旧目录 `output/<wxid>/` 仍在磁盘，但新代码查找 `<wxid>_<指纹>/` → **聊天页、统计、MCP 全部读不到**，用户会感知为「数据消失」；
2. 必须写一次性迁移：扫描 `output/` 下无指纹的旧目录并改名；
3. 改名后 manifest 内的 `rel` 可复用（`rel` 相对 `db_dir`，不含目录名），**但 `@source` 需按新目录归属重新判定**；
4. **致命问题**：若用户同时存在 C/D 两副本，迁移脚本**无法自动判断旧目录属于哪一个副本** —— 只能先备份，再让用户重跑一次。

**结论：不建议作为本次修复方案。** 它把一个「静默覆盖」问题换成了一个「用户可见的数据消失」问题，且在冲突场景下无法自动迁移。若将来要做，必须：
- 提供 `python run.py migrate` 迁移命令；
- 迁移前强制备份 `output/`；
- 冲突时保留旧目录并提示用户手动选择，而非自动改名。

### 9.5 推荐的上线组合（已实施）

| 顺序 | 内容 | 破坏性 | 效果 |
|---|---|---|---|
| 1 | **方案 C**：同名 wxid 检测 + 告警日志 | 无 | 立即让用户知道存在冲突 |
| 2 | **方案 B**：manifest 加 `@source` + 来源校验 | 无 | 从第二次运行起自动拦截覆盖 |
| 3 | **D-2**：临时文件改 `mkstemp` | 无 | 消除密钥库污染源 |

这三项**均为零破坏性**，升级用户无需任何操作。方案 A（改目录名）建议长期观察后单独排期。

### 9.6 实施细节与验证记录

**方案 C**（`siwx/discover.py`）
```python
def find_account_conflicts(dirs=None) -> list:
    """返回 [{"wxid": str, "dirs": [db_dir, ...]}]，无冲突时 []。"""
```
- `find_wechat_data_dirs()` 的签名与返回类型**未改动**，新增独立函数，零调用点影响。
- 接入两处：作业启动时逐条打印告警（`server.py::_run_job`）；`/api/status` 返回 `conflicts` 字段。
- 实测本机抓到真实冲突：`wxid_z30ttr1sg8h222_6409` 的 C/D 双副本。

**方案 B**（`siwx/extract.py`）
- 新增模块级常量 `SOURCE_FIELD = "@source"`。
- `decrypt_dir` 中：`source_guard` 取历史来源，为空则**放行**（升级兼容）；来源不符且 `dst.is_file()` 时跳过并计入 `conflicts`。
- 仅在无历史来源时写入 `@source`（避免把被跳过的冲突目录标记成本次来源）。
- `report` 新增 `conflicts` 计数，日志汇总会打印。

**D-2**（`siwx/sqlcipher.py`）
- `_read_page1` 与 `decrypt_database` 的临时名改为 `tempfile.mkstemp(prefix=..., suffix=".tmp")`。
- 顺带修复：`decrypt_database` 中 `mkstemp` 之后若 `copy2`/`open` 失败，原先会残留空文件（异常跳过第二个 `try` 的 `finally`），现已在 `except` 内立即清理。

**关键验证数据**

| 验证项 | 旧实现 | 修复后 |
|---|---|---|
| `_read_page1` 8 线程并发（强制走回退分支） | **正确 1/8，错配 7**（`db_2/3/4` 全读到 `db_7` 的 page1） | **正确 8/8，零残留** |
| 临时名唯一性（20 次调用） | 恒为 1 个名 | 20 个唯一名 |
| 来源变更 + 产物存在 | 静默覆盖 | 拦截，产物字节不变 |
| 升级用户（无 `@source`） | — | 放行，`ok=1`，行为与旧版一致 |
| 全新安装（无 manifest） | — | 零影响 |

端到端验证（复刻本机真实 C/D 冲突结构）：C 盘副本运行时被拦截，D 盘真实产物**未被覆盖**。

---

## 十、审查局限

- 未在真实并发压力下复现 D-2 的线程交错（需构造「微信占用中」状态 + 并发请求），结论基于代码路径与命名空间的静态推演。
- D-1 的覆盖后果基于**本机两份真实副本的规模对比**推演，未实际执行覆盖操作（审查为只读）。
- 兼容性验证基于**隔离临时目录中的模拟 manifest**，未在真实升级路径上端到端验证（未实际安装旧版本再升级）。
- 未审查 `strategies/` 各策略在异常输入下的行为，以及 `stats.py` 对异常 schema 的容错。
