# 打包与发布

> ***工具****: PyInstaller |&#x20;****平台****: Windows / macOS*

---

## 打包配置

### Windows (`packaging/siwx-win.spec`)

```python
# 关键配置（与 spec 一致）
- 入口: run.py
- 名称: stories-in-wx
- 图标: (可选)
- 数据文件:
    siwx/ui/         → ui/
    siwx/assets/     → siwx/assets/
    siwx/templates/  → siwx/templates/
    version.json     → .（自动更新链从 _MEIPASS 根目录读）
- 可选二进制: siwx/vendor/silk-decoder/**/silk_v3_decoder(.exe) → vendor/silk-decoder/（仓库中不存在，代码会优雅跳过）
- 隐藏导入: pilk
```

### macOS (`packaging/siwx-mac.spec`)

```python
# 关键配置（与 spec 一致）
- 入口: run.py
- 名称: stories-in-wx
- 格式: .app（CI 再打成 .dmg）
- 架构: target_arch="arm64"（仅 Apple Silicon）
- 数据文件:
    siwx/ui/         → ui/
    siwx/assets/     → siwx/assets/
    siwx/templates/  → siwx/templates/
    version.json     → .
- 可选二进制: siwx/vendor/silk-decoder/**/silk_v3_decoder → vendor/silk-decoder/
- 隐藏导入: pilk
```

> 漏列 `siwx/assets/`、`siwx/templates/` 或 `version.json` 时，源码态一切正常、
> 打包后才会在资源/模板/自动更新处 404 或 `FileNotFoundError` —— 改动 spec 的
> datas 后必须打一次包并按「构建后冒烟」验证。

---

## 打包命令

构建依赖单独锁定在 `requirements-build.txt`（`pyinstaller==6.19.0`）：PyInstaller
的 minor 版本会影响 spec 解析与 mac 的 `target_arch`/BUNDLE 行为，用 `>=` 会让
同一个 tag 在不同日期打出不同产物。

### Windows

```bash
# 安装依赖（运行时 + 构建专用）
pip install -r requirements.txt -r requirements-build.txt

# 打包
pyinstaller --clean -y packaging/siwx-win.spec

# 输出: dist/stories-in-wx.exe
```

### macOS

```bash
pip install -r requirements.txt -r requirements-build.txt
pyinstaller --clean -y packaging/siwx-mac.spec

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
# spec 文件中（实际配置）
hiddenimports=["pilk"]
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
  "version": "5.0.3",
  "date": "2026-09-19 04:40:19",
  "notes": "stories-in-wx v5.0.3",
  "assets": {
    "windows": "https://github.com/ImUpXuu/SIWX/releases/download/v5.0.3/stories-in-wx-v5.0.3-windows-x64.exe",
    "macos_dmg": "https://github.com/ImUpXuu/SIWX/releases/download/v5.0.3/stories-in-wx-v5.0.3-macos.dmg"
  },
  "sha256": "https://github.com/ImUpXuu/SIWX/releases/download/v5.0.3/SHA256SUMS.txt",
  "update_scripts": {
    "windows": "https://raw.gh.1s.fan/ImUpXuu/SIWX/main/scripts/update_win.bat",
    "macos": "https://raw.gh.1s.fan/ImUpXuu/SIWX/main/scripts/update_mac.sh"
  }
}
```

> 说明：`version.json` 由 `scripts/generate_version_json.py` 从 `siwx.__version__`
> 与 tag 注释自动生成，勿手工维护版本号。

---

## CI/CD

### GitHub Actions (`.github/workflows/release.yml`)

```yaml
# 触发条件: PR / push main / push tag v* / workflow_dispatch
# test（矩阵，fail-fast=false）:
#   windows-latest × Python 3.12 / 3.11 / 3.10  +  macos-latest × 3.12
#   1. 安装 requirements.txt + requirements-dev.txt
#   2. 前端 JS 语法检查（app.js / common.js / pages/*.js / renderer.js
#      复制成临时 .mjs 后逐个 node --check）
#   3. 全量 pytest（含 issue 模板不得被静默跳过的断言）
# version-guard: tag ref 上校验 tag ↔ siwx.__version__
# build-windows / build-macos（仅 tag 或手动触发）:
#   1. 安装 requirements.txt + requirements-build.txt
#   2. PyInstaller 打包 → 产物 --help 冒烟 → 重命名 / 打 DMG
# release: 汇总产物 → SHA256SUMS.txt → GitHub Release
# version-json: Release 产出资产后再回写 version.json 到 main
```

> macOS 的 test job 是全量 pytest，不是 `continue-on-error`：平台差异要暴露成红灯，
> 而不是被静默忽略。

---

## 依赖清单

```
# requirements.txt（运行时）
pycryptodome>=3.20    # AES / PBKDF2 / HMAC
flask>=3.0            # Web 控制台
psutil>=5.9           # 进程发现
openpyxl>=3.1         # XLSX 导出
rich>=13.0            # 终端 UI
zstandard>=0.22       # zstd 解压（消息内容）
pilk>=0.2.4           # SILK 语音解码（纯 Python）

# requirements-build.txt（仅构建）
pyinstaller==6.19.0
```

---

## 构建后冒烟

打包成功不等于产物能跑（漏 datas、`freeze_support` 位置、mac 包结构变化都只在
真实产物里暴露）。CI 的 build jobs 与本地发版都必须先过这两条：

```bash
# Windows：在仓库根目录，exit code 必须为 0
dist/stories-in-wx.exe --help

# macOS：直接跑 .app 内部二进制（不是 open 应用）
dist/stories-in-wx.app/Contents/MacOS/stories-in-wx --help
```

CI 里统一用 `bash` 执行这两步（bash 默认 `-e`，非零退出码即让 job 失败），
省掉 PowerShell 下手工判断 `$LASTEXITCODE` 的坑。`--help` 之外的手工验证：

```bash
# Windows
dist/stories-in-wx.exe auto
dist/stories-in-wx.exe serve

# 验证 UI 资源加载
# 验证 multiprocessing 子进程
# 验证 DPAPI 加密
```
