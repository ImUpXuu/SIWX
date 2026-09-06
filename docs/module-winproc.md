# module-winproc.py — 跨进程只读内存访问原语

> **文件**: `siwx/winproc.py` | **角色**: 微信进程内存只读访问（ctypes，无注入）

---

## 职责

1. **打开进程**: 只申请 `PROCESS_VM_READ | PROCESS_QUERY_INFORMATION`
2. **枚举内存区域**: `VirtualQueryEx` 遍历已提交可读区域
3. **分块读取内存**: `ReadProcessMemory` 分块迭代
4. **辅助函数**: u64 读取、句柄管理

**安全边界**: 只读访问，无写入、无注入。

---

## 常量

| 常量 | 值 | 说明 |
|---|---|---|
| `MEM_COMMIT` | 0x1000 | 已提交内存 |
| `READABLE_PROTECT` | {0x02,0x04,0x08,0x10,0x20,0x40,0x80} | 可读保护标志 |
| `MAX_USER_ADDRESS` | 0x0000_8000_0000_0000 | 用户空间上限 |
| `REGION_LIMIT` | 500MB | 单区域大小上限 |
| `PROCESS_VM_READ` | 0x0010 | 读取权限 |
| `PROCESS_QUERY_INFORMATION` | 0x0400 | 查询权限 |

---

## 关键函数

### `open_process(pid: int) → handle | None`

打开进程的只读句柄。

```python
h = open_process(1234)
if h:
    # 使用句柄...
    close_handle(h)
```

**失败返回 None**: 权限不足、进程已退出等。

---

### `close_handle(h) → None`

关闭句柄。

```python
close_handle(h)
```

---

### `read_mem(h, addr: int, size: int) → bytes | None`

从指定地址读取内存。

```python
data = read_mem(h, 0x7FF123450000, 1024)
if data:
    print(f"读取 {len(data)} 字节")
```

**失败返回 None**: 地址无效、权限不足等。

---

### `read_u64(h, addr: int) → int | None`

从指定地址读取 8 字节作为小端 u64。

```python
ptr = read_u64(h, 0x7FF123450000)
```

---

### `enum_regions(h) → list[(base, size)]`

枚举进程的已提交可读内存区域。

```
流程:
1. addr = 0
2. while addr < MAX_USER_ADDRESS:
   a. VirtualQueryEx(addr) → MBI
   b. 检查 State == MEM_COMMIT 且 Protect 可读
   c. 检查 0 < RegionSize < REGION_LIMIT
   d. 收集 (BaseAddress, RegionSize)
   e. addr = BaseAddress + RegionSize
3. 返回 [(base, size), ...]
```

**MBI 结构**:
```python
class MBI(ctypes.Structure):
    _fields_ = [
        ("BaseAddress", c_uint64),
        ("AllocationBase", c_uint64),
        ("AllocationProtect", DWORD),
        ("RegionSize", c_uint64),
        ("State", DWORD),
        ("Protect", DWORD),
        ("Type", DWORD),
    ]
```

---

### `iter_chunks(h, regions, chunk_size=2MB, overlap=0)`

分块读取内存区域并产出 `(块起始地址, 数据)`。

```
流程:
for base, size in regions:
    offset = 0
    while offset < size:
        chunk = read_mem(h, base + offset, min(chunk_size, size - offset))
        yield (base + offset, chunk)
        offset += chunk_size
```

**overlap 参数**: 保证跨块模式可命中（如搜索字符串跨越块边界时）。

```python
# 搜索 needle，需要 overlap = len(needle) - 1
for base, data in iter_chunks(h, regions, overlap=len(needle) - 1):
    pos = data.find(needle)
```

---

### `open_wechat_handles() → list[(handle, pid)]`

打开全部微信进程的只读句柄。

```python
handles = open_wechat_handles()
for h, pid in handles:
    # 使用...
    close_handle(h)
```

**注意**: 用完需要逐个 `close_handle`。

---

### `psutil_pid_list() → list[int]`

委托给 `discover.find_wechat_pids()`。

---

## 使用示例

```python
from siwx import winproc

# 打开进程
h = winproc.open_process(1234)
if not h:
    print("无法打开进程")
    exit()

try:
    # 枚举内存区域
    regions = winproc.enum_regions(h)
    print(f"{len(regions)} 个区域")

    # 分块读取
    for base, data in winproc.iter_chunks(h, regions, chunk_size=4*1024*1024):
        # 处理数据...
        pass
finally:
    winproc.close_handle(h)
```

---

## 设计决策

### 为什么只申请 PROCESS_VM_READ？

- 最小权限原则
- 不需要写入权限（策略只读取密钥）
- 不需要 PROCESS_ALL_ACCESS（触发安全软件警报）

### 为什么分块读取？

- 避免一次性分配大块内存
- 支持增量处理（找到目标可提前终止）
- 2MB 块大小是性能与内存的平衡点

### 为什么 overlap？

搜索模式可能跨越块边界。例如：
- needle 长度 30 字节
- 块 1 末尾 15 字节 + 块 2 开头 15 字节 = 完整 needle
- overlap = len(needle) - 1 = 29 保证不遗漏
