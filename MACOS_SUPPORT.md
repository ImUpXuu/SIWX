# macOS 支持

## 实现原理

微信 4.1.x（4.1.13 实测）不再在内存中缓存 raw key（`x'<96hex>'`），只保留 32 字节 passphrase。本实现通过以下步骤提取密钥：

1. **LLDB 断点捕获**：在 `wechat.dylib` 的 `sqlite3_key` / `sqlite3_key_v2` 函数上设断点，等微信打开数据库时，从寄存器中捕获 32 字节 passphrase
2. **PBKDF2 派生**：用 PBKDF2-SHA512（256000 次迭代，salt 取自每个数据库文件前 16 字节）为每个数据库派生独立密钥
3. **HMAC 验证**：用 SQLCipher 4 的 page-1 HMAC 验证派生密钥的正确性

## 依赖

- macOS（已实测 12.7.6、27.0；Apple Silicon 需先关闭 SIP，见下方「注意事项」）
- LLDB 命令行工具（Xcode Command Line Tools 自带）
- Python 3.10+

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
   ```bash
   # 重启进入恢复模式 (Apple Silicon: 开机按住电源键 → 选项)
   # 恢复模式的 Terminal 里：
   csrutil disable
   # 重启回正常系统后再执行提取；用完记得改回来：
   csrutil enable
   ```
   SIP 关闭期间系统级调试保护整体降级，不是只影响本工具，提取完成后应尽快改回。
4. **断点窗口只在数据库连接新建时命中一次**（PR #27）：微信跑久了不会再触发断点，必须**完整退出微信再重开**，并在窗口期内立即执行 `keys extract`。退出建议用 `killall WeChat`——`osascript … quit` 需要「自动化」权限，未授权时会静默失败，表现为「看着重启了其实还是旧 PID」
   ```bash
   killall WeChat   # 用 kill，不要用 "osascript ... quit"：
                     # 后者需要系统「自动化」权限，未授权时会静默失败，
                     # 看起来像是重启了但其实还是同一个旧进程/旧 PID
   open -a WeChat
   python run.py keys extract
   ```
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
- [x] WeChat 4.1.8 (Intel Mac, macOS 12.7.6) — 早期文档记录（原文写作 "4.1.80"，应为 4.1.8.0 笔误）
- [ ] WeChat 4.1.8.106 / 4.1.15 (Apple Silicon, macOS 15.7.4) — issue #30/#32 报告版本；修复前被版本门槛误拦，待复测
- [ ] 更高版本微信
