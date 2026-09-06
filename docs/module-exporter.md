# module-exporter.py — 多格式导出引擎

> **文件**: `siwx/exporter.py` | **角色**: 聊天记录多格式导出（8 种格式 + 打包）

---

## 职责

1. **消息收集**: 调用 `api_chat.build_messages()` 获取消息列表
2. **媒体解密**: 按需解密图片落盘到导出目录
3. **头像提取**: 从 head_image.db 提取头像
4. **多格式写入**: JSON / HTML / TXT / CSV / Markdown / TOML / SQLite / XLSX
5. **进度回调**: `progress(pct, msg)` 实时上报
6. **打包**: zip 压缩 + 清理临时目录

---

## 支持的格式

| 格式 | 扩展名 | 写入函数 | 说明 |
|---|---|---|---|
| JSON | `.json` | `_write_json` | 结构化数据（含 exportInfo + session + messages） |
| HTML | `.html` | `render_html` | 交互式查看器（JS 渲染，明暗主题） |
| TXT | `.txt` | `_write_txt` | 纯文本（时间 + 发送者 + 内容） |
| CSV | `.csv` | `_write_csv` | Excel 兼容（UTF-8 BOM） |
| Markdown | `.md` | `_write_md` | 按日期分组，支持图片引用 |
| TOML | `.toml` | `_write_toml` | 结构化配置格式 |
| SQLite | `.db` | `_write_sqlite` | 双表（session + messages） |
| XLSX | `.xlsx` | `_write_xlsx` | Excel 工作簿（openpyxl） |

---

## 关键函数

### `run_export(acc_out_dir, account, chat, display, fmt, ...) → dict`

**主导出入口**。

```python
def run_export(
    acc_out_dir: Path,      # 解密产物目录
    account: str,           # 账号 wxid
    chat: str,              # 会话 username
    display: str,           # 显示名称
    fmt: str,               # 格式 (json/html/txt/csv/markdown/toml/sqlite/xlsx)
    start_ts=None,          # 起始时间戳
    end_ts=None,            # 结束时间戳
    want_messages=True,     # 是否导出消息
    want_media=True,        # 是否导出媒体
    want_avatars=True,      # 是否导出头像
    export_root=None,       # 导出根目录（默认 ./exports）
    pack="zip",             # 打包方式（zip / 无）
    progress=lambda pct, msg: None,  # 进度回调
) -> dict:
```

**流程**:
```
1. build_messages() → 读取消息
2. _contact_names() → 获取联系人名称
3. collect_avatars() → 提取头像（可选）
4. export_media_files() → 解密图片（可选）
5. _write_xxx() → 写入目标格式
6. shutil.make_archive() → 打包 zip（可选）
7. 返回结果
```

**返回**:
```python
{
    "export_dir": str,
    "zip": str,            # zip 路径（若打包）
    "file": str,           # 导出文件路径
    "format": str,
    "pack": str,
    "message_count": int,
    "media_count": int,
    "avatar_count": int,
    "duration_ms": int,
}
```

---

### `collect_avatars(acc_out_dir, usernames, dest, progress) → dict`

**头像提取**: `head_image.db` → `avatars/<md5(username)>.jpg`。

```python
for un in usernames:
    row = conn.execute("SELECT image_buffer FROM head_image WHERE username=?", (un,))
    if row and row[0]:
        fn = md5(un).hexdigest() + ".jpg"
        (dest / fn).write_bytes(row[0])
        mapping[un] = f"avatars/{fn}"
```

---

### `export_media_files(acc_out_dir, account, msgs, dest, progress) → dict`

**媒体解密**: 解密图片 → `media/0001_<md5>.jpg`。

```python
for i, m in enumerate(imgs):
    body, ctype = media.get_image(account, m["md5"], acc_out_dir,
                                  chat=m["_chat"], local_id=m["localId"],
                                  ts=m["createTime"], bubble_md5=m.get("bubbleMd5"))
    if body:
        ext = "png" if "png" in ctype else "jpg"
        fn = f"{i:04d}_{md5[:12]}.{ext}"
        (dest / fn).write_bytes(body)
        out[m["localId"]] = f"media/{fn}"
```

---

## 进度回调

```python
def progress(pct: int, msg: str):
    """
    pct: 0-100
    msg: 描述性消息
    """
```

**进度分配**:
- 3%: 读取消息
- 55%: 提取头像
- 40-85%: 解密媒体
- 88%: 写入格式
- 94%: 打包 zip
- 100%: 完成

---

## 安全限制

```python
def _safe_name(name: str) -> str:
    for ch in '<>:"/\\|?*':
        name = name.replace(ch, "_")
    return name[:48].strip() or "chat"
```

导出文件名清理，防止路径穿越。

---

## 错误处理

```python
try:
    # 导出逻辑...
    _ok = True
    return result
finally:
    if not _ok:
        shutil.rmtree(export_dir, ignore_errors=True)
```

导出失败时自动清理临时目录。

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
    want_messages=True,
    want_media=True,
    want_avatars=True,
    pack="zip",
    progress=lambda pct, msg: print(f"{pct}% {msg}")
)

print(f"导出完成: {result['message_count']} 条消息")
print(f"文件: {result['file']}")
```

---

## 添加新格式

1. 添加 `_write_xxx()` 函数
2. 在 `run_export()` 中添加分支:

```python
elif fmt == "myformat":
    _write_myformat(out_file, session, content_msgs)
```

3. 在 `ext` 字典中添加扩展名映射:

```python
ext = {..., "myformat": "ext"}
```
