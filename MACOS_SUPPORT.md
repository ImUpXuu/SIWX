# macOS 支持

## 实现原理

微信 4.1.80+ 版本不再在内存中缓存 raw key（`x'<96hex>'`），只保留 32 字节 passphrase。本实现通过以下步骤提取密钥：

1. **LLDB 断点捕获**：在 `wechat.dylib` 的 `sqlite3_key` / `sqlite3_key_v2` 函数上设断点，等微信打开数据库时，从寄存器中捕获 32 字节 passphrase
2. **PBKDF2 派生**：用 PBKDF2-SHA512（256000 次迭代，salt 取自每个数据库文件前 16 字节）为每个数据库派生独立密钥
3. **HMAC 验证**：用 SQLCipher 4 的 page-1 HMAC 验证派生密钥的正确性

## 依赖

- macOS 12+（已实测）
- LLDB 命令行工具（Xcode Command Line Tools 自带）
- Python 3.10+
- psutil

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
2. **LLDB 权限**：可能需要 `sudo` 或关闭 SIP（`csrutil disable`）
3. **首次使用**：建议先用 `python run.py keys extract` 测试密钥提取

## 与 Windows 版本的差异

| 特性 | Windows | macOS |
|------|---------|-------|
| 密钥提取方式 | Config.Cipher 内存扫描 | LLDB 断点 + PBKDF2 |
| 进程读取 API | kernel32.dll | Mach VM / LLDB |
| DPAPI 密钥库 | 支持 | 不支持（用 JSON 文件） |
| 自动发现 | 全盘扫描 | 标准路径扫描 |

## 测试状态

- [x] WeChat 4.1.80 (Intel Mac, macOS 12.7.6)
- [ ] WeChat 4.1.80+ (Apple Silicon)
- [ ] 更高版本微信
