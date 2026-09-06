# module-exporter.py — 流式多格式导出引擎

> **文件**: `siwx/exporter.py` + `siwx/export_stream.py` | **角色**: 聊天记录多格式导出（8 种格式 + 多选会话批量导出）

---

## 职责

1. **流式消息读取**：`heapq.merge` K 路归并，内存 O(分片数) 而非 O(消息总数)
2. **增量文件写入**：JSON/TXT/CSV/MD 直接写文件句柄，不构建巨型字符串
3. **并行媒体解密**：`multiprocessing.Pool` 多进程 AES 解密图片
4. **多格式**：JSON / HTML / TXT / CSV / Markdown / TOML / SQLite / XLSX
5. **多选批量**：支持同时选择多个会话，每个会话独立子文件夹

---

## 性能优化（v0.3.0 重写）

### 旧版瓶颈

| 问题 | 影响 |
|---|---|
| `build_messages()` 全量读入内存 | 1万条 × ~15 字段/dict ≈ 30MB+，翻倍后 60MB+ |
| `rawContent` 与 `content` 重复存储 | 每条消息多存一份完整文本 |
| `json.dumps(整个列表)` | 构建巨型字符串，1万条 ≈ 50-100MB |
| 媒体解密串行 | 单进程逐张 AES 解密 |

### 新版方案

| 优化 | 实现 | 效果 |
|---|---|---|
| **流式读取** | `heapq.merge` K 路归并，每分片只缓存第一条 | 内存 O(分片数)，与消息总数无关 |
| **增量写入** | `IncrementalJSONWriter` 逐条写文件 | 内存 O(1)，不受消息数影响 |
| **并行媒体** | `multiprocessing.Pool.imap_unordered` | CPU 核数倍加速 |
| **双遍扫描** | 第一遍轻量采集元数据，第二遍流式写出 | 避免全量加载 |
| **精简字段** | 去掉前端专用字段，导出形状独立 | 减少 40% 内存 |

### 实测

```
4939 条消息：读取 0.20s + 写入 0.26s = 0.46s（旧版 >2s 且内存翻倍）
1万条消息：内存 <50MB（旧版 >200MB，低端 Mac 直接崩）
```

---

## 关键函数

### `run_export(acc_out_dir, account, chat, ...) → dict`

**主导出入口**。双遍扫描 + 流式写出。

```
流程:
1. 第一遍：_collect_metadata() → 计数/发送者/图片引用（轻量）
2. 头像提取：collect_avatars()（仅需要的发送者）
3. 媒体解密：_decrypt_parallel()（多进程池）
4. 第二遍：流式写出到目标格式
5. 打包（可选）
```

**返回**:
```python
{
    "export_dir": str, "zip": str, "file": str,
    "format": str, "pack": str,
    "message_count": int, "media_count": int, "avatar_count": int,
    "duration_ms": int,
}
```

### `run_export_multi(acc_out_dir, account, chats, ...) → dict`

**多选会话批量导出**。循环调用 `run_export`，每个会话独立子文件夹。

```
输出结构:
exports/export_20260906_200000/
├─ 01_会话A/
│  ├─ 会话A_20260906.json
│  ├─ media/
│  └─ avatars/
├─ 02_会话B/
│  └─ ...
```

### `message_stream(acc, chat, start_ts, end_ts, account) → generator`

**流式消息生成器**（在 `export_stream.py`）。

```
算法:
1. 每个分片执行 SELECT ... ORDER BY create_time
2. heapq.merge 做 K 路归并（每分片只缓存第一条）
3. 逐条 yield 精简后的消息 dict
内存: O(分片数)，与消息总数无关
```

### `IncrementalJSONWriter`

**流式 JSON 写入器**。直接写文件句柄，不构建中间字符串。

```python
writer = IncrementalJSONWriter(path, session)
for msg in message_stream(...):
    writer.write_msg(msg)
writer.close()
```

---

## 支持的格式

| 格式 | 扩展名 | 写入方式 | 说明 |
|---|---|---|---|
| JSON | `.json` | 流式增量 | 结构化数据（含 exportInfo + session + messages） |
| HTML | `.html` | 分批渲染 | 交互式查看器（JS 渲染，明暗主题） |
| TXT | `.txt` | 流式行写 | 纯文本（时间 + 发送者 + 内容） |
| CSV | `.csv` | 流式行写 | Excel 兼容（UTF-8 BOM） |
| Markdown | `.md` | 流式行写 | 按日期分组，支持图片引用 |
| TOML | `.toml` | 流式行写 | 结构化配置格式 |
| SQLite | `.db` | 分批提交 | 双表（session + messages），每 1000 条 commit |
| XLSX | `.xlsx` | 流式行写 | Excel 工作簿（openpyxl） |

---

## 打包方式

| pack | 说明 |
|---|---|
| `folder` | 仅文件夹（每会话一个子文件夹） |
| `single` | 单个 ZIP（全部会话打包一个） |
| `each` | 每会话一个 ZIP |

---

## 媒体解密

### 并行解密流程

```
1. 第一遍扫描收集图片引用: [(md5, bubble_md5, localId), ...]
2. 构建任务列表: [(acc_dir, account, md5, bubble_md5, local_id, dst_path), ...]
3. multiprocessing.Pool.imap_unordered(_decrypt_one, tasks)
4. 返回 {localId: "media/xxx.jpg"} 映射
```

### 单张解密（`_decrypt_one`）

子进程入口，调用 `media.get_image()` 尝试所有候选密钥，成功即写出到目标路径。

---

## 使用示例

```python
from pathlib import Path
from siwx.exporter import run_export

result = run_export(
    acc_out_dir=Path("output/wxid_xxx"),
    account="wxid_xxx",
    chat="wxid_yyy",
    display="张三",
    fmt="json",
    want_media=True,
    want_avatars=True,
    pack="single",
    progress=lambda pct, msg: print(f"{pct}% {msg}")
)
print(f"导出 {result['message_count']} 条，耗时 {result['duration_ms']}ms")
```
