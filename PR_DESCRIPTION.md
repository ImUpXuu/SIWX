# PR: 添加 macOS 支持（LLDB + PBKDF2 密钥提取）

## 问题背景

微信 4.1.80+ 版本不再在内存中缓存 raw key（`x'<96hex>'`），只保留 32 字节 passphrase。这导致：
- 原有的内存扫描方法（`memscan.py`、`config_cipher.py`）失效
- macOS 版本无法提取密钥

## 解决方案

通过 LLDB 在 `sqlite3_key` / `sqlite3_key_v2` 函数上设断点，捕获 passphrase，再用 PBKDF2-SHA512 派生每个数据库的独立密钥。

## 改动文件

1. **新增 `siwx/strategies/macos_lldb.py`**
   - macOS 专用策略
   - LLDB 断点捕获 passphrase
   - PBKDF2-SHA512 派生（256000 次迭代）
   - HMAC 验证

2. **修改 `siwx/discover.py`**
   - 添加 macOS 进程名（`WeChat`）
   - 添加 macOS 路径扫描（`~/Library/Containers/`）

3. **修改 `siwx/strategies/__init__.py`**
   - 注册 `macos_lldb` 策略（仅 macOS）
   - 添加平台条件检查

4. **修改 `siwx/cli.py`**
   - 移除 Windows 独占限制
   - 添加 macOS 支持提示

5. **新增 `MACOS_SUPPORT.md`**
   - macOS 使用说明
   - 与 Windows 版本的差异对比

## 测试状态

- [x] WeChat 4.1.80 (Intel Mac, macOS 12.7.6)
- [ ] WeChat 4.1.80+ (Apple Silicon)
- [ ] 更高版本微信

## 注意事项

1. macOS 上需要 LLDB（Xcode Command Line Tools 自带）
2. 可能需要 `sudo` 或关闭 SIP 才能附加到微信进程
3. macOS 版本不支持 DPAPI 密钥库（用 JSON 文件替代）

## 相关 Issue

- 微信 4.1.80+ 内存中不再缓存 raw key
- macOS 版本无法提取密钥

## 优先级

高 - 这是 macOS 用户使用本项目的必要功能
