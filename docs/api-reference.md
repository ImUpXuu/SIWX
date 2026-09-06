# Web API 参考

> **基础 URL**: `http://127.0.0.1:8787` | **Content-Type**: `application/json`

---

## 状态与任务

### `GET /api/status`

**状态总览**。

```json
// 响应
{
  "wechat_running": true,
  "pids": [1234, 5678],
  "accounts": [
    {
      "wxid": "wxid_xxx",
      "db_dir": "C:/Users/.../db_storage",
      "db_count": 32,
      "keys_cached": 30,
      "total_salts": 32
    }
  ],
  "stored_salts": 32
}
```

---

### `POST /api/run`

**启动后台任务**。

```json
// 请求
{
  "mode": "auto",          // "keys" / "decrypt" / "auto" / "sync" / "export"
  "db_dir": null,          // 可选，指定单账号
  "out_dir": null,         // 可选，默认 ./output
  "no_cache": false,
  "workers": null,
  "export_opts": {         // mode=export 时
    "account": "wxid_xxx",
    "chat": "wxid_yyy",
    "display": "张三",
    "format": "json",
    "start": "2026-01-01",
    "end": "2026-09-06",
    "messages": true,
    "media": true,
    "avatars": true,
    "pack": "zip"
  }
}

// 响应
{"started": true}
// 或
{"error": "已有任务在运行"}, 409
```

---

### `GET /api/logs`

**环形日志缓冲**。

```json
// 响应
{
  "logs": [
    [1725600000000, "[cipher] 扫描完成"],
    [1725600000100, "…"]
  ]
}
```

---

## 聊天查看

### `GET /api/chat/accounts`

**账号列表**。

```json
// 响应
{
  "accounts": [
    {"wxid": "wxid_xxx", "sessions": 123}
  ]
}
```

---

### `GET /api/chat/sessions?account=wxid_xxx`

**会话列表**（轻量，只读 session.db）。

```json
// 响应
{
  "account": "wxid_xxx",
  "sessions": [
    {
      "username": "wxid_yyy",
      "display": "张三",
      "is_group": false,
      "preview": "你好…",
      "last_time": 1725600000,
      "msg_count": 0
    }
  ]
}
```

---

### `GET /api/chat/messages?account=&chat=&before=&limit=`

**分页消息**（SQL LIMIT/OFFSET）。

| 参数 | 说明 | 默认值 |
|---|---|---|
| `account` | 账号 wxid | 必填 |
| `chat` | 会话 username | 必填 |
| `before` | 时间戳，加载此时间之前的消息 | 0 |
| `limit` | 每页条数（最大 300） | 100 |

```json
// 响应
{
  "account": "wxid_xxx",
  "chat": "wxid_yyy",
  "display": "张三",
  "is_group": false,
  "messages": [
    {
      "id": 123,
      "ts": 1725600000,
      "type": 1,
      "kind": "text",
      "sender_wxid": "wxid_yyy",
      "sender_name": "张三",
      "is_me": false,
      "md5": null,
      "bubble_md5": null,
      "quote": null,
      "link": null,
      "text": "你好"
    }
  ],
  "has_more": true
}
```

---

### `GET /api/chat/avatar?account=&username=`

**联系人头像**（明文 JPEG）。

```
响应: image/jpeg (Cache-Control: private, max-age=86400)
```

---

### `GET /api/chat/media/image?account=&md5=&chat=&bubble_md5=&hq=&local_id=&ts=`

**按需解密单张图片**。

| 参数 | 说明 | 默认值 |
|---|---|---|
| `account` | 账号 wxid | 必填 |
| `md5` | 消息 XML md5 | |
| `chat` | 会话 username | |
| `bubble_md5` | packed_info md5 | |
| `hq` | 优先高清版 (1/true) | false |
| `local_id` | 消息 local_id | 0 |
| `ts` | 时间戳 | 0 |

```
响应: image/jpeg 或 image/png (Cache-Control: private, max-age=86400)
错误: {"error": "未找到文件"}, 404
```

---

## 导出

### `GET /api/export/list`

**导出历史列表**。

```json
// 响应
{
  "exports": [
    {
      "name": "20260906_200000_张三",
      "files": ["张三_20260906.json"],
      "zips": ["张三_20260906_json.zip"]
    }
  ]
}
```

---

### `GET /api/export/download?path=`

**下载导出文件**。

| 参数 | 说明 |
|---|---|
| `path` | 文件路径（必须在 exports 根内） |

安全限制: `is_relative_to(root)` 防止路径穿越。

---

### `POST /api/export/open`

**在文件资源管理器中打开**。

```json
// 请求
{"path": "exports/20260906_200000_张三/张三.json"}
```

---

### `POST /api/export/render`

**预览导出 JSON**。

```json
// 请求
{"path": "exports/.../张三.json"}

// 响应
{
  "session": {...},
  "preview": [前20条消息]
}
```

---

## 设置

### `GET /api/settings/overview`

**缓存总览**。

```json
// 响应
{
  "keystore": {"count": 32, "path": "C:/.../keystore.bin"},
  "outputs": [
    {"wxid": "wxid_xxx", "size_mb": 556.2, "manifest": true}
  ],
  "output_root": "G:/project/.../output"
}
```

---

### `POST /api/settings/clear`

**清除缓存**。

```json
// 请求
{"kind": "output", "wxid": "wxid_xxx"}  // 清除指定账号解密产物
{"kind": "output"}                       // 清除全部解密产物
{"kind": "keys"}                         // 清除密钥库

// 响应
{"removed": ["wxid_xxx"]}
```

| kind | 说明 |
|---|---|
| `output` | 删除解密产物目录 |
| `keys` | 删除密钥库文件 |

---

## MCP

### `GET /api/mcp/info`

**MCP 配置信息**：启动命令、客户端 JSON 配置、工具开关列表。

```json
// 响应
{
  "command": {"command": "python", "args": [".../run.py", "mcp"]},
  "client_config": "{ \"mcpServers\": { ... } }",
  "config_path": "C:/Users/.../stories-in-wx/mcp_config.json",
  "tools": [
    {"name": "get_status", "description": "获取运行状态", "enabled": true},
    {"name": "list_accounts", "description": "列出已解密的账号", "enabled": true},
    {"name": "list_sessions", "description": "列出某账号的全部会话", "enabled": true},
    {"name": "get_messages", "description": "读取某会话的消息", "enabled": true},
    {"name": "search_messages", "description": "按关键词搜索消息", "enabled": true},
    {"name": "export_chat", "description": "导出某会话聊天记录", "enabled": true}
  ]
}
```

---

### `POST /api/mcp/config`

**保存工具开关**。配置持久化到 `%LOCALAPPDATA%\stories-in-wx\mcp_config.json`。

```json
// 请求
{"tools": {"get_status": true, "search_messages": false}}

// 响应
{"saved": true}
```

| 字段 | 说明 |
|---|---|
| `tools` | `{工具名: 是否启用}`，未列出的工具保持原状 |

---

## 错误码

| HTTP 码 | 含义 |
|---|---|
| 200 | 成功 |
| 400 | 参数错误 |
| 404 | 资源不存在 |
| 409 | 冲突（已有任务运行） |
