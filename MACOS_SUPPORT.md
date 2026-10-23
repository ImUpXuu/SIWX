# macOS 支持

## 实现原理

微信 4.1.x（4.1.13 实测）不再在内存中缓存 raw key（`x'<96hex>'`），只保留 32 字节 passphrase。本实现通过以下步骤提取密钥：

1. **LLDB 断点捕获**：在 `wechat.dylib` 的 `sqlite3_key` / `sqlite3_key_v2` 函数上设断点，等微信打开数据库时，从寄存器中捕获 32 字节 passphrase
2. **PBKDF2 派生**：用 PBKDF2-SHA512（256000 次迭代，salt 取自每个数据库文件前 16 字节）为每个数据库派生独立密钥
3. **HMAC 验证**：用 SQLCipher 4 的 page-1 HMAC 验证派生密钥的正确性

## 依赖

- macOS（已实测：源码运行 Intel Mac 12.7.6 与 Apple Silicon；Release DMG 仅 arm64）
- LLDB 命令行工具（Xcode Command Line Tools 自带）
- Python 3.10+

## 下载安装（未签名 DMG）

Release 页面的 macOS 产物是 `stories-in-wx-v*-macos.dmg`（`version.json` 的 `macos_dmg` 字段与自动更新链都指向这个文件名，未改名），它是 **Apple Silicon (arm64) 专用**构建：

- **仅 arm64**：`packaging/siwx-mac.spec` 声明 `target_arch="arm64"`，CI 在 `macos-latest` 上构建前还会断言 `uname -m` 为 `arm64`；Intel Mac 无法直接运行该 DMG。
- **未签名、未公证**：构建链没有 codesign 与 Apple notarization，首次打开会被 Gatekeeper 拦下，提示"无法验证开发者"或"已损坏，无法打开"——文件并未损坏，按下述步骤放行即可。

### Apple Silicon 安装步骤

1. 双击 DMG，把 `stories-in-wx.app` 拖进「应用程序」。
2. 解除 Gatekeeper 隔离（二选一）：
   - 在「应用程序」里 **右键（或 Control + 点击）→ 打开**，弹窗中再点一次「打开」，之后即可正常双击启动；
   - 或在终端执行（路径按实际安装位置调整）：
     ```bash
     xattr -dr com.apple.quarantine /Applications/stories-in-wx.app
     ```
3. 首次运行仍需先按下文「注意事项」关闭 SIP 并允许 LLDB 调试，密钥提取才能工作。

### Intel Mac

当前 DMG 不含 x86_64 切片，Intel Mac 请改用源码运行：见下方[从源码运行](#使用)一节的 `pip install -r requirements.txt` 与 `python run.py ...` 步骤（需 Python 3.10+，LLDB 由 Xcode Command Line Tools 提供）。

## 数据目录（打包版，2026-10 决策）

- **应用数据**：`~/Library/Application Support/stories-in-wx/` —— `keystore.bin`、`media_key.json`、`wechat_paths.json`、`log_level.json`。
- **相对路径产物**：`logs/`、`output/`、`exports/`、`templates/` 仍跟随程序根，即 `stories-in-wx.app/Contents/MacOS/` 内；默认权限沿用进程 umask（常见目录 0755、文件 0644）。
- 如果 `.app` 位于只读位置，写目录会回退到**当前工作目录**下的 `stories-in-wx/<subdir>`（不一定是 Application Support）。
- 想让所有产物都落在私有目录，可创建 `~/Library/Application Support/stories-in-wx` 后以 `SIWX_ROOT="$HOME/Library/Application Support/stories-in-wx"` 启动；keystore/media_key 本来就在该目录。
- 本轮**不做自动迁移**（P3-21 决策）：升级不会移动既有 `output/exports/logs`；卸载 `.app` 前请先自行备份或删除这些目录，避免明文聊天库随程序目录残留。
- `media_key.json` 是明文派生密钥缓存，POSIX 权限已收紧到 0600；Windows 依赖用户目录 ACL（P3-22 决策，见 `THREAT_MODEL.md`）。

## 使用

```bash
# 安装依赖
pip install -r requirements.txt

# 全自动
python run.py auto

# 仅提取密钥
python run.py keys extract

# 仅解密（需要先提取密钥）
python run.py decrypt --db-dir ~/Library/Containers/com.tencent.xinWeChat/Data/Documents/xwechat_files/<wxid>/db_storage
```

## 注意事项

1. **微信必须运行并登录**：密钥只存在于运行中的进程内存
2. **微信版本基线 4.1.8**：4.1.8 以下未验证，仍会尝试但失败概率较高。注意：5.0.9~5.0.10 曾把基线误写成 **4.1.80**（"4.1.8.0"的笔误），而 macOS 微信最高只有 4.1.15，导致所有 macOS 用户被版本判定直接拦截、一次 attach 都没发生（issue #30/#32）；当前代码已修正为 4.1.8 基线，且低于基线只警告、不再拦路
3. **SIP 必须关闭**（PR #27 纠偏）：SIP 开启时 AMFI 拒绝 attach 没有 `get-task-allow` 权限的 hardened-runtime 进程，**root / `sudo` 也不行**，只能 `csrutil disable` 后重启。文档旧版「可能需要 sudo」的说法不成立
4. **断点窗口只在数据库连接新建时命中一次**（PR #27）：微信跑久了不会再触发断点，必须**完整退出微信再重开**，并在窗口期内立即执行 `keys extract`。退出建议用 `killall WeChat`——`osascript … quit` 需要「自动化」权限，未授权时会静默失败，表现为「看着重启了其实还是旧 PID」
5. **多账号机器**：用 `python run.py keys extract --db-dir <目标账号的 db_storage 路径>` 把捕获窗口留给目标账号
6. **首次使用**：建议先用 `python run.py keys extract` 测试密钥提取

## 与 Windows 版本的差异

| 特性 | Windows | macOS |
|------|---------|-------|
| 密钥提取方式 | Config.Cipher 内存扫描 | LLDB 断点 + PBKDF2 |
| 进程读取 API | kernel32.dll | Mach VM / LLDB |
| DPAPI 密钥库 | 支持 | 不支持（用 JSON 文件） |
| 自动发现 | 全盘扫描 | 标准路径扫描 |

## 测试状态

- [x] WeChat 4.1.13 (Apple Silicon, macOS 26.6.2) — PR #26 作者实测，31/31 salt 验证通过
- [x] WeChat 4.1.8 (Intel Mac, macOS 12.7.6) — 早期文档记录，**源码运行**实测，不是当前 DMG（DMG 仅 arm64）（原文写作 "4.1.80"，应为 4.1.8.0 笔误）
- [ ] WeChat 4.1.8.106 / 4.1.15 (Apple Silicon, macOS 15.7.4) — issue #30/#32 报告版本；修复前被版本门槛误拦，待复测
- [ ] 更高版本微信
