# module-pool.py — 多进程解密池与输出缓存

> **文件**: `siwx/pool.py` | **角色**: 并行解密 + 产物缓存清单

---

## 职责

1. **多进程解密池**: `multiprocessing.Pool` 并行解密多个数据库
2. **输出缓存清单**: `.siwx_cache.json` 记录 (mtime, size, key)，源库未变秒回
3. **任务分发**: `imap_unordered` 流式返回结果

---

## 关键函数

### `load_manifest(out_dir) → dict`

加载缓存清单。

```python
# .siwx_cache.json 格式
{
  "message/message_1.db": {
    "size": 524288000,
    "mtime": 1725600000,
    "pages": 1234,
    "key": "abcdef1234567890..."
  },
  "contact/contact.db": {
    "size": 1048576,
    "mtime": 1725500000,
    "pages": 256,
    "key": "fedcba9876543210..."
  }
}
```

**失败返回空 dict**: 文件不存在或 JSON 解析错误。

---

### `save_manifest(out_dir, manifest) → None`

保存缓存清单。

```python
def save_manifest(out_dir, manifest):
    p = Path(out_dir)
    p.mkdir(parents=True, exist_ok=True)
    (p / ".siwx_cache.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=1),
        encoding="utf-8"
    )
```

---

### `_worker(task) → (rel, pages, status, err)`

**子进程工作函数**（模块顶层，Windows spawn 要求）。

```python
def _worker(task):
    rel, src, dst, key_hex = task
    from siwx.sqlcipher import decrypt_database
    try:
        pages = decrypt_database(Path(src), Path(dst), bytes.fromhex(key_hex))
        return (rel, pages, "ok", "")
    except Exception as e:
        return (rel, 0, "failed", str(e))
```

**注意**: 必须是模块顶层函数（Windows spawn 模式要求）。

---

### `decrypt_parallel(tasks, workers=None, on_done=None) → list`

**并行解密入口**。

```python
def decrypt_parallel(
    tasks: list[(rel, src, dst, key_hex)],
    workers: int = None,     # 默认 min(8, cpu_count)
    on_done: callable = None, # 每完成一个任务回调
) -> list[(rel, pages, status, err)]:
```

**流程**:
```
1. 空任务 → 返回 []
2. workers <= 1 或 len(tasks) <= 1 → 串行执行
3. 否则:
   a. multiprocessing.Pool(n)
   b. pool.imap_unordered(_worker, tasks)
   c. 流式返回结果
   d. 每完成一个调用 on_done(r)
```

**返回**: `[(rel, pages, status, err), ...]`

---

## 缓存判定

在 `extract.decrypt_dir()` 中:

```python
m = manifest.get(e.rel)
if (use_cache and m
    and m.get("size") == e.size
    and m.get("mtime") == mtime
    and m.get("key") == key_hex
    and dst.is_file()):
    → cached += 1  # 跳过解密
```

**三者一致**: 文件大小 + 修改时间 + 密钥 → 源库未变。

---

## 进程数选择

```python
n = workers or min(8, (os.cpu_count() or 4))
```

- 默认上限 8（避免 IO 瓶颈）
- 可通过 `--workers` 参数覆盖
- 单任务时自动退化为串行

---

## Windows spawn 模式

```python
# run.py 中
from multiprocessing import freeze_support
freeze_support()  # PyInstaller 打包后必须
```

子进程通过 `sys.path` 注入定位 siwx 包。

---

## 使用示例

```python
from siwx.pool import decrypt_parallel, load_manifest, save_manifest

# 准备任务
tasks = [
    ("message_1.db", "src/path", "dst/path", "64hexkey"),
    ("message_2.db", "src/path", "dst/path", "64hexkey"),
]

# 并行解密
results = decrypt_parallel(tasks, workers=4, on_done=lambda r: print(f"完成: {r[0]}"))

# 处理结果
for rel, pages, status, err in results:
    print(f"{rel}: {status} ({pages} 页)")
```
