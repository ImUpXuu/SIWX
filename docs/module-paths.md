# module-paths.py — 统一应用路径

> **文件**: `siwx/paths.py` | **角色**: 与 cwd 完全解耦的路径管理

---

## 职责

统一管理应用路径，区分 PyInstaller 打包和源码运行两种模式。

---

## 路径规则

| 函数 | 源码运行 | PyInstaller 打包 |
|---|---|---|
| `app_root()` | 项目根目录（siwx/ 上一级） | exe 所在目录 |
| `out_root()` | `<app_root>/output` | `<exe_dir>/output` |
| `exports_root()` | `<app_root>/exports` | `<exe_dir>/exports` |

---

## 关键函数

### `app_root() → Path`

```python
def app_root() -> Path:
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent.parent
```

**原理**:
- `sys.frozen`: PyInstaller 打包后存在
- `sys._MEIPASS`: 打包后临时解压目录
- 源码运行: 从 `__file__` 推导（siwx/ → 项目根）

---

### `out_root() → Path`

```python
def out_root() -> Path:
    return app_root() / "output"
```

解密产物目录。

---

### `exports_root() → Path`

```python
def exports_root() -> Path:
    return app_root() / "exports"
```

导出产物目录。

---

## 设计决策

### 为什么与 cwd 解耦？

- 用户可能从任意目录运行 `python /path/to/run.py`
- PyInstaller 打包后 exe 可能在任意位置
- 路径必须从模块位置推导，不能依赖 `os.getcwd()`

### 为什么不使用 `~/.config/stories-in-wx`？

- 解密产物可能很大（数百 MB），放在用户目录不合适
- 与项目目录一起便于管理和清理
- 密钥库使用 `%LOCALAPPDATA%`（已在 keystore.py 中处理）

---

## 使用示例

```python
from siwx.paths import app_root, out_root, exports_root

print(f"应用根目录: {app_root()}")
print(f"解密目录: {out_root()}")
print(f"导出目录: {exports_root()}")
```
