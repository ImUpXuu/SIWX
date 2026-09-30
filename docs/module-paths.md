# module-paths.py — 统一应用路径

> **文件**: `siwx/paths.py` | **角色**: 与 cwd 完全解耦的路径管理

---

## 职责

统一管理应用路径，区分 PyInstaller 打包和源码运行两种模式。

---

## 路径规则

| 函数 | 源码运行 | PyInstaller 打包 |
|---|---|---|
| `app_root()` | SIWX_ROOT 环境变量 → 入口脚本目录 → `__file__` 上一级 | exe 所在目录 |
| `data_dir()` | `%LOCALAPPDATA%\stories-in-wx`（Win）/ `~/Library/Application Support/stories-in-wx`（mac）/ `~/.local/share/stories-in-wx`（Linux） | 同左 |
| `out_root()` | `<app_root>/output`（带可写回退） | `<exe_dir>/output`（带可写回退） |
| `exports_root()` | `<app_root>/exports`（带可写回退） | `<exe_dir>/exports`（带可写回退） |

---

## 关键函数

### `app_root() → Path`

```python
def app_root() -> Path:
    # 1. 显式环境变量覆盖
    env_root = os.environ.get("SIWX_ROOT", "")
    if env_root and Path(env_root).is_dir():
        return Path(env_root)
    # 2. PyInstaller 打包：exe 所在目录
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    # 3. 源码运行：入口脚本（run.py）所在目录
    main_mod = sys.modules.get("__main__")
    if main_mod and getattr(main_mod, "__file__", None):
        entry = Path(main_mod.__file__).resolve()
        if entry.name == "run.py" or (entry.parent / "siwx").is_dir():
            return entry.parent
    # 4. 兜底：从 __file__ 推导（siwx/paths.py → 上一级 = 项目根）
    return Path(__file__).resolve().parent.parent
```

**原理**:
- `SIWX_ROOT`: 显式环境变量覆盖（最高优先级）
- `sys.frozen`: PyInstaller 打包后存在
- `sys._MEIPASS`: 打包后临时解压目录
- 源码运行: 从入口脚本位置推导（比 `__file__` 更可靠）

---

### `out_root() → Path`

```python
def out_root() -> Path:
    return _writable_fallback("output", app_root() / "output")
```

解密产物目录。主路径不可写时回退到 `%USERPROFILE%\stories-in-wx\output`。

---

### `exports_root() → Path`

```python
def exports_root() -> Path:
    return _writable_fallback("exports", app_root() / "exports")
```

导出产物目录。主路径不可写时回退到 `%USERPROFILE%\stories-in-wx\exports`。

---

### `data_dir() → Path`

```python
def data_dir() -> Path:
    if os.name == "nt":
        base = os.environ.get("LOCALAPPDATA") or os.environ.get("USERPROFILE") or tempfile.gettempdir()
        return Path(base) / "stories-in-wx"
    home = Path.home()
    if sys.platform == "darwin":
        return home / "Library" / "Application Support" / "stories-in-wx"
    xdg = os.environ.get("XDG_DATA_HOME") or str(home / ".local" / "share")
    return Path(xdg) / "stories-in-wx"
```

跨平台应用数据目录。密钥库、MCP 配置、插件配置、媒体密钥缓存均存放于此。

---

### `_writable_fallback(subdir, primary) → Path`

```python
def _writable_fallback(subdir: str, primary: Path) -> Path:
    key = (subdir, str(primary))
    cached = _PATH_CACHE.get(key)
    if cached is not None:
        return cached
    try:
        if primary.is_dir() and os.access(primary, os.W_OK):
            _PATH_CACHE[key] = primary
            return primary
        primary.mkdir(parents=True, exist_ok=True)
        probe = primary / ".siwx_probe"
        probe.write_bytes(b"")
        probe.unlink(missing_ok=True)
        _PATH_CACHE[key] = primary
        return primary
    except OSError:
        pass
    fallback = Path(os.environ.get("USERPROFILE", ".")) / "stories-in-wx" / subdir
    fallback.mkdir(parents=True, exist_ok=True)
    _PATH_CACHE[key] = fallback
    return fallback
```

可写回退机制。主路径不可创建/不可写时，回退到 `%USERPROFILE%\stories-in-wx\<subdir>`。
结果进程内缓存（`_PATH_CACHE`），避免每次调用都做写探针。

---

## 设计决策

### 为什么与 cwd 解耦？

- 用户可能从任意目录运行 `python /path/to/run.py`
- PyInstaller 打包后 exe 可能在任意位置
- 路径必须从模块位置推导，不能依赖 `os.getcwd()`

### 为什么不使用 `~/.config/stories-in-wx`？

- 解密产物可能很大（数百 MB），放在用户目录不合适
- 与项目目录一起便于管理和清理
- 密钥库等持久化数据使用 `paths.data_dir()`（已在 keystore.py 中处理）

---

## 使用示例

```python
from siwx.paths import app_root, out_root, exports_root

print(f"应用根目录: {app_root()}")
print(f"解密目录: {out_root()}")
print(f"导出目录: {exports_root()}")
```
