# 打包与发布

> **工具**: PyInstaller | **平台**: Windows / macOS

---

## 打包配置

### Windows (`packaging/siwx-win.spec`)

```python
# 关键配置
- 入口: run.py
- 名称: stories-in-wx
- 图标: (可选)
- 数据文件: siwx/ui/ → ui/
- 可选二进制: siwx/vendor/silk-decoder/**/silk_v3_decoder(.exe) → vendor/silk-decoder/
- 隐藏导入: siwx.strategies.*
```

### macOS (`packaging/siwx-mac.spec`)

```python
# 关键配置
- 入口: run.py
- 名称: stories-in-wx
- 格式: .dmg
- 数据文件: siwx/ui/ → ui/
- 可选二进制: siwx/vendor/silk-decoder/**/silk_v3_decoder → vendor/silk-decoder/
```

---

## 打包命令

### Windows

```bash
# 安装 PyInstaller
pip install pyinstaller

# 打包
pyinstaller packaging/siwx-win.spec

# 输出: dist/stories-in-wx.exe
```

### macOS

```bash
pyinstaller packaging/siwx-mac.spec

# 输出: dist/stories-in-wx.app
```

---

## PyInstaller 注意事项

### freeze_support

```python
# run.py 中必须最先调用
from multiprocessing import freeze_support
freeze_support()
```

Windows spawn 模式下，子进程以本 exe 重新拉起时，
由 `freeze_support` 分流到 `spawn_main`，避免整包重跑 CLI。

### UI 资源

```python
# server.py 中
def _ui_dir() -> Path:
    if getattr(sys, "frozen", False):
        return Path(sys._MEIPASS) / "ui"
    return Path(__file__).resolve().parent / "ui"
```

打包后资源在 `_MEIPASS` 临时目录。

### 隐藏导入

```python
# spec 文件中
hiddenimports=[
    'siwx.strategies',
    'siwx.strategies.config_cipher',
    'siwx.strategies.keystore_source',
    'siwx.strategies.mmkv',
    'siwx.strategies.memscan',
]
```

---

## 发布流程

### GitHub Releases

1. **更新版本**: `version.json`
2. **打包**: 生成 exe / dmg
3. **计算 SHA256**: `SHA256SUMS.txt`
4. **创建 Release**: GitHub Actions 自动 / 手动
5. **上传资产**: exe + dmg + SHA256SUMS.txt

### version.json 格式

```json
{
  "version": "0.2.5",
  "date": "2026-09-06 20:00:00",
  "notes": "更新说明",
  "assets": {
    "windows": "https://github.com/ImUpXuu/SIWX/releases/download/v0.2.5/stories-in-wx-v0.2.5-windows-x64.exe",
    "macos_dmg": "https://github.com/ImUpXuu/SIWX/releases/download/v0.2.5/stories-in-wx-v0.2.5-macos.dmg"
  },
  "sha256": "https://github.com/ImUpXuu/SIWX/releases/download/v0.2.5/SHA256SUMS.txt"
}
```

---

## CI/CD

### GitHub Actions (`.github/workflows/`)

```yaml
# 触发条件: push tag v*
# 1. 安装依赖
# 2. 运行测试（如有）
# 3. 打包 exe / dmg
# 4. 计算 SHA256
# 5. 创建 Release 并上传资产
```

---

## 依赖清单

```
pycryptodome>=3.20    # AES / PBKDF2 / HMAC
flask>=3.0            # Web 控制台
psutil>=5.9           # 进程发现
openpyxl>=3.1         # XLSX 导出
rich>=13.0            # 终端 UI
zstandard>=0.22       # zstd 解压（消息内容）
```

---

## 测试打包产物

```bash
# Windows
dist/stories-in-wx.exe --help
dist/stories-in-wx.exe auto
dist/stories-in-wx.exe serve

# 验证 UI 资源加载
# 验证 multiprocessing 子进程
# 验证 DPAPI 加密
```
