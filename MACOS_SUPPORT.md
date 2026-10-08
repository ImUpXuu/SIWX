# macOS 支持

## 实现原理

微信 4.1.80+ 版本不再在内存中缓存 raw key（`x'<96hex>'`），只保留 32 字节 passphrase。本实现通过以下步骤提取密钥：

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
2. **LLDB 权限：`sudo` 不够，必须关闭 SIP**（实测结论，非推测）。当前版本微信
   开启了 hardened runtime 且不带 `get-task-allow` entitlement，SIP 开启时
   内核 AMFI 会拒绝任何进程（包括 root）对它 `attach`，报错
   `Not allowed to attach to process`；`sudo lldb ... attach` 会原样复现同一
   错误。唯一可行路径：
   ```bash
   # 重启进入恢复模式 (Apple Silicon: 开机按住电源键 → 选项)
   # 恢复模式的 Terminal 里：
   csrutil disable
   # 重启回正常系统后再执行提取；用完记得改回来：
   csrutil enable
   ```
   SIP 关闭期间系统级调试保护整体降级，不是只影响本工具，提取完成后应尽快改回。
3. **断点只在数据库连接“新建”时命中一次**：若微信已经运行了一段时间，所有
   `db_storage/*.db` 的连接早已建立，`sqlite3_key`/`CCKeyDerivationPBKDF`
   不会再触发，`keys extract` 会稳定拿到 0/N。解决方法是强制微信重新建立
   连接——完整退出并重新打开微信后，**立即**执行提取命令（启动后的几秒内
   窗口很短）：
   ```bash
   killall WeChat   # 用 kill，不要用 "osascript ... quit"：
                     # 后者需要系统「自动化」权限，未授权时会静默失败，
                     # 看起来像是重启了但其实还是同一个旧进程/旧 PID
   open -a WeChat
   python run.py keys extract
   ```
4. **首次使用**：建议先用 `python run.py keys extract` 测试密钥提取
5. **多账号时按需用 `--db-dir` 缩小范围**：`keys extract --db-dir <path>` 只
   处理指定账号，能避免把上面第 3 条的短暂捕获窗口浪费在不需要的账号上。

## 与 Windows 版本的差异

| 特性 | Windows | macOS |
|------|---------|-------|
| 密钥提取方式 | Config.Cipher 内存扫描 | LLDB 断点 + PBKDF2 |
| 进程读取 API | kernel32.dll | Mach VM / LLDB |
| DPAPI 密钥库 | 支持 | 不支持（用 JSON 文件） |
| 自动发现 | 全盘扫描 | 标准路径扫描 |

## 测试状态

- [x] WeChat 4.1.80 (Intel Mac, macOS 12.7.6)
- [x] WeChat 4.1.13 (Apple Silicon Mac, macOS 27.0) —— 需 SIP 关闭 +
      重启微信后立即提取（见「注意事项」第 2、3 条），27/27 salt 全部验证通过
- [ ] 更高版本微信
