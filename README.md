# stories-in-wx
本站部分逻辑已开源至我的博客[[https://upxuu.com/](https://upxuu.com/posts/wechat-dat-image-decrypt/)](https://upxuu.com/posts/wechat-dat-image-decrypt/) 媒体解密思路 后续会进一步发送文章解析
微信 4.x 数据库密钥提取、解密、聊天查看、媒体解密、多格式导出、MCP 接管。

> **跨平台**: Windows（完整支持）+ macOS（4.1.80+，需 LLDB 密钥提取）

## 快速开始

### 方式一：下载 Release（推荐）

1. 前往 [Releases](https://github.com/ImUpXuu/SIWX/releases) 下载对应平台产物
   - Windows: `stories-in-wx-v*-windows-x64.exe`
   - macOS: `stories-in-wx-v*-macos.dmg`
2. 双击运行 → 自动打开浏览器进入 Web 控制台
3. 跟随引导页操作：欢迎 → 选号 → 提取并解密

### 方式二：源码运行

```bash
# 1. 克隆
git clone https://github.com/ImUpXuu/SIWX.git
cd SIWX

# 2. 安装依赖
pip install -r requirements.txt

# 3. 启动 Web 控制台
python run.py serve
# 浏览器自动打开 http://127.0.0.1:8787

# 或全自动（命令行模式）
python run.py auto
```

## 使用流程

### 引导页（3 步）

1. **欢迎** — 检测微信状态、账号数、密钥缓存
2. **选号** — 选择要提取密钥的微信号
3. **提取并解密** — 一键完成密钥提取 + 数据库解密

### ⚠️ macOS 特别说明

macOS 版使用 LLDB 断点捕获密钥，**需要微信在提取时处于登录状态**：

- 若密钥已缓存 → 秒回，无需操作
- 若密钥未缓存 → 请在点击「提取并解密」前 **60 秒内重新登录微信**（退出再打开），确保密钥在内存中

### 功能页

| 页面 | 说明 |
|---|---|
| 💬 聊天查看 | 浏览会话列表、分页加载消息、查看图片/头像 |
| 📦 导出 | 多选会话批量导出（8 种格式 + ZIP 打包） |
| 🔌 MCP | AI 客户端接入配置（Claude / ZCode 等） |
| 📋 日志 | 实时查看详细运行日志 |
| ⚙️ 设置 | 缓存管理、版本检查、更新 |

## 导出格式

| 格式 | 说明 |
|---|---|
| JSON | 结构化全量（含 session + messages） |
| HTML | 自包含网页查看器（明暗主题、搜索、筛选） |
| TXT | 纯文本 |
| CSV | 表格（Excel 可开） |
| Markdown | 按日期分组 |
| TOML | 结构化配置 |
| SQLite | 数据库（可 SQL 查询） |
| XLSX | Excel 工作簿 |

## MCP 配置

复制 MCP 页的 JSON 配置到你的 AI 客户端：

```json
{
  "mcpServers": {
    "stories-in-wx": {
      "command": "python",
      "args": ["G:/path/to/run.py", "mcp"]
    }
  }
}
```

提供 6 个工具：`get_status` / `list_accounts` / `list_sessions` / `get_messages` / `search_messages` / `export_chat`。

## 自动更新

打包产物启动时自动检查 GitHub Release → 有新版本则在设置页提示 → 一键更新。

## 性能

| 指标 | 数值 |
|---|---|
| 解密速度 | ~175 MB/s |
| 导出速度 | 6000-8000 条/s |
| 内存占用 | < 3 MB（万级消息不 OOM） |
| 11992 条消息导出 | ~2s |

## 架构

```
siwx/
├─ sqlcipher.py            # HMAC 验证 + 流式页级解密
├─ keystore.py             # salt 索引密钥库（DPAPI 加密）
├─ discover.py             # 跨平台目录/进程发现
├─ strategies/             # 插件缝：keystore/mmkv/config_cipher/memscan/macos_lldb
├─ extract.py              # 编排：全局收割 → 策略链 → 解密
├─ export_stream.py        # 流式消息解析 + 增量导出
├─ exporter.py             # 多格式导出引擎
├─ mcp_server.py           # MCP 服务器（stdio, JSON-RPC 2.0）
├─ auto_update.py          # 自动更新
└─ ui/                     # Flask + 原生 HTML/CSS/JS
```

## 构建发布

```bash
# Windows
pip install pyinstaller
pyinstaller packaging/siwx-win.spec

# macOS
pyinstaller packaging/siwx-mac.spec

# 推送 tag 触发 GitHub Actions 自动构建 + Release
git tag v0.3.5
git push origin v0.3.5
```

## 贡献者

| 贡献者 | 说明 |
|---|---|
| [ImUpXuu](https://github.com/ImUpXuu) | 项目作者，核心架构与 Windows 端 |
| [lcrworld-jtl](https://github.com/lcrworld-jtl) | macOS 端密钥提取（LLDB + PBKDF2） |

## 许可证

AGPL-3.0（不可商用）。详见 [LICENSE](./LICENSE)。

## 免责声明

本项目仅供个人数据备份与研究使用。使用者应遵守所在法律法规，严禁用于未经授权访问他人数据。使用本项目产生的任何法律责任由使用者自行承担。
