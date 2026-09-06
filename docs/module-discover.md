# module-discover.py — 目录发现与进程发现

> **文件**: `siwx/discover.py` | **角色**: 全盘自动扫描 + 微信进程枚举

---

## 职责

1. **全盘目录发现**: 扫描 A-Z 盘符 + 用户目录，找到所有 `xwechat_files/*/db_storage`
2. **微信进程发现**: 枚举运行中的微信进程，按内存占用降序

---

## 关键函数

### `find_wechat_data_dirs() → list[(wxid, db_storage)]`

**全盘自动扫描**微信数据目录。

```
扫描根目录:
1. %USERPROFILE%\Documents\xwechat_files
2. %USERPROFILE%\xwechat_files
3. A:\xwechat_files ~ Z:\xwechat_files

流程:
for root in roots:
    for entry in root.iterdir():
        db = entry / "db_storage"
        if db.is_dir() and db not in seen:
            → (entry.name, str(db))

按 wxid 排序返回
```

**返回**: `[(wxid, db_storage_path), ...]`

---

### `find_wechat_pids() → list[int]`

**枚举运行中的微信进程**，按内存占用降序（主进程优先）。

```
流程:
1. psutil.process_iter()
2. 过滤 name in ("weixin.exe", "wechat.exe")
3. 按 rss 降序排序
4. 返回 [pid, ...]
```

**设计要点**: 主进程内存最大，排前面优先扫描。

---

### `wxid_of(db_dir) → str`

从 db_storage 路径提取 wxid。

```python
>>> wxid_of("C:/Users/xxx/xwechat_files/wxid_abc123/db_storage")
'wxid_abc123'
```

---

## 进程发现机制

### WECHAT_PROCESSES

```python
WECHAT_PROCESSES = ("weixin.exe", "wechat.exe")
```

- `weixin.exe`: 微信国内版
- `wechat.exe`: 微信国际版

### 异常处理

```python
try:
    name = (proc.info["name"] or "").lower()
    if name in WECHAT_PROCESSES:
        rss = proc.info["memory_info"].rss
        out.append((rss, proc.info["pid"]))
except (psutil.NoSuchProcess, psutil.AccessDenied):
    continue
```

进程消失或权限不足时静默跳过。

---

## 使用示例

```python
from siwx.discover import find_wechat_data_dirs, find_wechat_pids, wxid_of

# 发现数据目录
dirs = find_wechat_data_dirs()
for wxid, db in dirs:
    print(f"{wxid}: {db}")

# 发现微信进程
pids = find_wechat_pids()
print(f"微信运行中: {pids}")

# 提取 wxid
wxid = wxid_of("C:/.../wxid_abc/db_storage")
```

---

## 注意事项

- 多实例并存时（不同盘符），全部发现并处理
- 未登录账号的目录可能不存在或为空
- 进程发现依赖 psutil，需要 `pip install psutil`
