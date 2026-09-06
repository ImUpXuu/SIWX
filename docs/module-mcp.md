# module-mcp — MCP 服务器与配置

> **文件**: `siwx/mcp_server.py` + `siwx/api_mcp.py` | **角色**: 把聊天记录能力以 MCP 工具暴露给 AI 客户端

---

## 职责

1. **MCP 服务器**（`mcp_server.py`）：stdio 传输的 JSON-RPC 2.0 服务器，实现 MCP 2024-11-05 规范，提供 6 个工具供 AI 客户端（Claude Desktop / ZCode 等）调用
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

| 工具 | 说明 | 关键参数 |
|---|---|---|
| `get_status` | 微信运行状态、已解密账号、密钥库条数 | 无 |
| `list_accounts` | 列出已解密的账号 wxid | 无 |
| `list_sessions` | 列出某账号的全部会话（含预览） | `account`, `limit?` |
| `get_messages` | 读取某会话最新 N 条消息（正序） | `account`, `chat`, `limit?` |
| `search_messages` | 按关键词搜索（指定会话或全库扫描） | `account`, `keyword`, `chat?`, `limit?` |
| `export_chat` | 导出某会话到文件 | `account`, `chat`, `format?`, `media?`, `avatars?` |

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

配置文件位置：`%LOCALAPPDATA%\stories-in-wx\mcp_config.json`

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

- MCP 服务器**只读**：不执行解密、不修改密钥库
- 工具开关：可在配置页禁用敏感工具（如 `export_chat`）
- 数据不外流：stdio 本地通信，无网络端口
