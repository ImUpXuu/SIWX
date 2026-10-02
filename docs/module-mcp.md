# module-mcp — MCP 服务器与配置

> **文件**: `siwx/mcp_server.py` + `siwx/api_mcp.py` | **角色**: 把聊天记录与朋友圈能力以 MCP 工具暴露给 AI 客户端

---

## 职责

1. **MCP 服务器**（`mcp_server.py`）：stdio 传输的 JSON-RPC 2.0 服务器，实现 MCP 2024-11-05 规范，提供 **11 个工具**（6 聊天 + 5 朋友圈）供 AI 客户端（Claude Desktop / ZCode 等）调用
2. **配置 API**（`api_mcp.py`）：Web 控制台 MCP 页的后端，提供工具开关读写 + 客户端配置生成

---

## MCP 协议实现

### 传输

- **stdio**：通过 stdin/stdout 收发 newline-delimited JSON-RPC 2.0 消息
- **编码**：强制 UTF-8（Windows 管道默认 GBK 会炸）
- **零依赖**：不依赖 `mcp` SDK，纯 Python 标准库实现

### 支持的方法

| 方法 | 说明 |
|---|---|
| `initialize` | 握手，返回协议版本、服务器信息、能力声明 |
| `ping` | 健康检查，返回 `{}` |
| `tools/list` | 返回已启用的工具列表（含 name/description/inputSchema） |
| `tools/call` | 调用指定工具，返回 `{content: [{type:"text", text:"..."}]}` |
| `notifications/*` | 客户端通知，忽略 |

### 工具列表

#### 聊天（6 个）

| 工具 | 说明 | 关键参数 |
|---|---|---|
| `get_status` | 微信运行状态、已解密账号、密钥库条数 | 无 |
| `list_accounts` | 列出已解密的账号 wxid | 无 |
| `list_sessions` | 列出某账号的全部会话（含预览） | `account`, `limit?` |
| `get_messages` | 读取某会话最新 N 条消息（正序） | `account`, `chat`, `limit?` |
| `search_messages` | 按关键词搜索（指定会话或全库扫描） | `account`, `keyword`, `chat?`, `limit?` |
| `export_chat` | 导出某会话到文件 | `account`, `chat`, `format?`, `media?`, `avatars?`, `voice?`, `pack?` |

#### 朋友圈（5 个，v5.0.5 新增）

| 工具 | 说明 | 关键参数 |
|---|---|---|
| `list_sns_accounts` | 列出有朋友圈数据的账号（动态总数、最新动态时间） | 无 |
| `get_sns_timeline` | 朋友圈时间线（游标分页 + 关键词/发布者/时间范围过滤） | `account`, `limit?`, `before_tid?`, `keyword?`, `username?`, `start?`, `end?` |
| `get_sns_detail` | 单条动态完整详情（不截断评论；点赞/评论/表情齐全） | `account`, `tid` |
| `get_sns_friends` | 按发布者聚合（谁发了多少条，按数量降序，含备注昵称） | `account`, `limit?` |
| `export_sns` | 导出朋友圈到文件（json/markdown/txt/html），可选下载媒体 | `account`, `format?`, `keyword?`, `username?`, `start?`, `end?`, `limit?`, `media?` |

朋友圈工具的实现与 `api_sns.py` 同源但独立于 flask：直接复用 `sns.py` 的非 flask 函数
（`parse_ts_arg` / `ts_to_tid_bounds` / `parse_timeline` / `search_text` / `public_card` /
`iter_authors` / `timeline_stats`）与 `sns_export.run_sns_export()`。

**设计红线（token 节约）**：MCP 返回纯文本，AI 客户端拿不到 CDN 媒体，
所以朋友圈工具的**所有形状都去掉 URL**（`_slim_card` / `_slim_post` / `_detail_post`）——
长 encfilekey token 对 AI 只是无意义噪音。其余设计沿既有口径：

- 时间线只回互动**计数**，评论/点赞正文在 `get_sns_detail`（一页 20 条 × 全量评论会淹没 token）
- 发布者与时间范围下推 SQL（`tid` 内含毫秒时间戳，`ts_to_tid_bounds`）；
  关键词需逐条解析 XML，命中 limit 即停 + 扫描上限 5000 条兜底（与 `api_sns.timeline` 同策略）
- 账号名校验与 `api_sns` 同一条正则（路径穿越防护）；`tid` 为 SQLite 有符号 int64（可为负），原样传回即可
- `end` 传纯日期补到当日 23:59:59（与 `api_sns._ts_arg` 同规则）

---

## 使用方式

### 命令行启动

```bash
python run.py mcp
```

### 客户端配置（Claude Desktop 示例）

```json
{
  "mcpServers": {
    "stories-in-wx": {
      "command": "python",
      "args": ["G:/project/stories-in-wx/stories-in-wx-py/run.py", "mcp"]
    }
  }
}
```

PyInstaller 打包后：
```json
{
  "mcpServers": {
    "stories-in-wx": {
      "command": "G:/path/to/stories-in-wx.exe",
      "args": ["mcp"]
    }
  }
}
```

### Web 控制台配置

启动 `python run.py serve` → 进入 MCP 页 → 复制自动生成的命令和 JSON 配置。

---

## 配置 API

### `GET /api/mcp/info`

返回 MCP 配置信息：

```json
{
  "command": {"command": "python", "args": ["...run.py", "mcp"]},
  "client_config": "{ \"mcpServers\": { ... } }",
  "config_path": "C:/Users/.../stories-in-wx/mcp_config.json",
  "tools": [
    {"name": "get_status", "description": "...", "enabled": true},
    ...
  ]
}
```

### `POST /api/mcp/config`

保存工具开关：

```json
{"tools": {"get_status": true, "search_messages": false}}
```

配置文件位置：`paths.data_dir() / "mcp_config.json"`（跨平台，Windows 上为 `%LOCALAPPDATA%\stories-in-wx\mcp_config.json`）

---

## 关键实现细节

### 全库搜索（`search_messages`）

- **指定会话**：调用 `build_messages()` 加载全部消息，Python 侧过滤关键词
- **全库扫描**：构建 `md5(chat) → chat` 反查表，遍历所有 `message_*.db` 的 `Msg_*` 表，逐行解码后匹配
- **扫描上限**：默认 20 万行（`SCAN_CAP`），命中即停，避免长时间阻塞
- **内容截断**：每条匹配结果最多返回 300 字符，节省 token

### 数据助手

`mcp_server.py` 包含独立的数据访问层（`_accounts()`、`_session_list()`），与 `api_chat.py` 逻辑同源但不依赖 Flask，保持 MCP 进程轻量。

### 安全

- MCP 服务器**查询只读**：`get_status` / `list_accounts` / `list_sessions` / `get_messages` / `search_messages` / `list_sns_accounts` / `get_sns_timeline` / `get_sns_detail` / `get_sns_friends` 不执行解密、不修改密钥库；`export_chat` / `export_sns` 会执行导出（`export_chat` 走 `exporter.run_export()` 解密媒体，`export_sns` 走 `sns_export.run_sns_export()`、`media=true` 时从 CDN 下载）并写入 `exports/` 目录
- 工具开关：可在配置页禁用敏感工具（如 `export_chat` / `export_sns`）
- 数据不外流：stdio 本地通信，无网络端口
