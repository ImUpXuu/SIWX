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
      "total_salts": 32,
      "manual": false
    }
  ],
  "stored_salts": 32,
  "conflicts": []
}
```

`manual`：是否为手动添加的数据目录；`conflicts`：同名账号冲突列表（多个 db_dir 共用 `output/<wxid>/`，解密会互相覆盖）。

---

### `POST /api/run`

**启动后台任务**。

```json
// 请求
{
  "mode": "auto",          // "keys" / "decrypt" / "auto" / "sync" / "export" / "sns_export"
  "db_dir": null,          // 可选，指定单账号（sns_export 忽略此参数）
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
    "voice": true,
    "avatars": true,
    "pack": "zip"
  }
}

// 响应
{"started": true}
// 或
{"error": "已有任务在运行"}, 409
```

`mode=sns_export` 时 `export_opts` 传朋友圈导出参数，见 [POST /api/sns/export](#post-api_snsexport)；该模式只依赖已解密产物，不做全盘扫描。

---

### `GET /api/job`

**当前任务状态**（前端每 800ms 轮询，展示进度与最后一条日志）。

```json
// 响应
{
  "running": true,
  "done": false,
  "ok": false,
  "mode": "sns_export",
  "logs": ["[sns] 12/40 媒体 ..."],
  "report": null
}
```

`done=true` 后 `report` 携带本次任务的报告对象（聊天导出 / 朋友圈导出形状不同）。

---

### `POST /api/discover/validate`

**验证并保存手动输入的微信存储路径**（引导页"找不到账号"时使用）。

```json
// 请求
{"path": "D:/xwechat_files/wxid_xxx_1234"}

// 响应：校验结果对象（合法则持久化，下次启动直接识别）
```

---

### `GET /api/logs`

**合并日志流**：文件日志（siwx.log tail）+ 环形任务日志 + MCP 调用日志 + 结构化日志，按时间排序去重。

| 参数 | 说明 | 默认值 |
|---|---|---|
| `limit` | 返回条数（最大 5000） | 2000 |

```json
// 响应
{
  "logs": [
    [1725600000000, "[cipher] 扫描完成"],
    [1725600000100, "…"]
  ],
  "level": "rough"
}
```

历史版本曾把浏览器探测 / 旧资源 404 记录成 ERROR，该类噪音行已过滤不返回。

---

### `GET /api/logs/settings` / `POST /api/logs/settings`

**读取 / 设置日志模式**（粗略 `rough` / 详细 `detailed`）。

```json
// POST 请求
{"level": "detailed"}
// 响应
{"level": "detailed"}
```

---

### `GET /api/logs/export`

**导出脱敏日志**（text/plain 附件，文件名 `siwx_log.txt`）。

| 参数 | 说明 | 默认值 |
|---|---|---|
| `start` / `end` | 时间范围（unix 秒） | 不限 |
| `desensitize` | 是否脱敏（`1`/`0`） | `1` |

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

### `GET /api/chat/timeline?account=&chat=&month=`

**单个会话时间轴**：默认只按月聚合（快），展开某月时传 `month` 查该月的日期分布。

| 参数 | 说明 | 默认值 |
|---|---|---|
| `account` | 账号 wxid | 必填 |
| `chat` | 会话 username | 必填 |
| `month` | 月份（`YYYY-MM`），传则返回按日聚合 | 不传 |

```json
// 响应（不传 month；传 month 时 "months" 换成 "days"，并附 "month" 字段）
{
  "account": "wxid_xxx",
  "chat": "wxid_yyy",
  "total": 1234,
  "ts_min": 1700000000,
  "ts_max": 1725600000,
  "months": [
    {"month": "2026-09", "count": 320, "first_ts": 1725148800, "last_ts": 1725600000}
  ]
}
```

非法 `month` 返回 400；账号未解密返回 404。

---

### `GET /api/chat/stats?account=&chat=`

**单个会话统计**（聊天页右上角弹窗）。

```json
// 响应
{
  "account": "wxid_xxx",
  "chat": "wxid_yyy",
  "display": "张三",
  "total": 1234,
  "sent": 620,
  "received": 614,
  "first_ts": 1700000000,
  "last_ts": 1725600000,
  "active_days": 210,
  "busiest_day": {"day": "2026-09-06", "count": 45},
  "busiest_hour": 21,
  "types": [{"type": 1, "label": "文本", "count": 900}]
}
```

---

### `GET /api/chat/avatar?account=&username=`

**联系人头像**（明文 JPEG）。

```
响应: image/jpeg (Cache-Control: private, max-age=86400)
```

---

### `GET /api/chat/media/voice?account=&chat=&local_id=&svr_id=&ts=&format=`

**读取或转码单条语音**。

| 参数 | 说明 | 默认值 |
|---|---|---|
| `account` | 账号 wxid | 必填 |
| `chat` | 会话 username | |
| `local_id` | 消息 local_id | 0 |
| `svr_id` / `server_id` | 消息 server_id | 0 |
| `ts` | 时间戳 | 0 |
| `format` / `fmt` | `silk` 原始下载，或 `wav` 转浏览器可播放 WAV | `silk` |

```
响应: audio/silk 或 audio/wav
错误: {"error": "...", "fallback": "silk"}, 415  # pilk/兜底解码器不可用时
```

说明：`format=wav` 默认使用项目依赖 `pilk` 直接把 SILK 解成裸 PCM，然后由 Python
标准库封装为 WAV，不要求 ffmpeg。若 pilk 不可用，再依次尝试项目内置解码器、
`SIWX_SILK_DECODER` 或 PATH 中的本机解码器。不可转码时仍可使用默认 `format=silk` 下载原始语音。

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

## 朋友圈（SNS）

> 朋友圈媒体与聊天图片是**两套完全独立的加密体系**（ISAAC64 vs AES-ECB+XOR），接口形状也不同。原理见 [module-sns.md](./module-sns.md)。

### `GET /api/sns/accounts`

**有朋友圈数据的账号列表**（扫 `output/<账号>/sns/sns.db`）。

```json
// 响应
{"accounts": [{"wxid": "wxid_xxx", "count": 5684}]}
```

---

### `GET /api/sns/timeline?account=&before_tid=&limit=&keyword=&username=&start=&end=`

**时间线**（游标分页，按 tid 倒序）。

| 参数 | 说明 | 默认值 |
|---|---|---|
| `account` | 账号 wxid | 必填 |
| `before_tid` | 上一页最后一条的 tid，加载更早 | 不传 |
| `limit` | 每页条数（最大 100） | 20 |
| `keyword` | 关键词（匹配正文 + 卡片字段 + 媒体描述 + 位置） | 不传 |
| `username` | 发布者过滤（SQL 下推） | 不传 |
| `start` / `end` | 时间范围（unix 秒或 `YYYY-MM-DD`，end 补到 23:59:59） | 不传 |

```json
// 响应
{
  "timeline": [ { "tid": 123, "ts_ms": 1725600000000, "ts": 1725600000,
                  "user_name": "wxid_yyy", "contentDesc": "…",
                  "card": {…}, "medias": […], "likes": […], "comments": […] } ],
  "next_before_tid": 122,
  "has_more": true
}
```

实现要点：发布者与时间范围下推到 SQL（tid 内含毫秒时间戳）；关键词需逐条解析 XML（无法下推），设 `KEYWORD_MAX_SCAN=5000` 上限兜底；`card` 统一走 `sns.public_card()` 形状（与导出一致）。`GET /api/sns/search` 复用本接口。

---

### `GET /api/sns/detail?account=&tid=`

**单条动态完整详情**（不截断评论；列表流只显示最近 20 条）。

```
200: {"post": {…同 timeline 单条，含完整 likes/comments…}}
404: 动态不存在 ｜ 400: tid 无效 ｜ 422: XML 无法解析
```

---

### `GET /api/sns/media?account=&url=&key=&token=`

**代理下载朋友圈媒体**（图片 / 视频 / 实况），带磁盘缓存（`output/<账号>/sns_media/`）。

| 参数 | 说明 |
|---|---|
| `url` | 动态 XML 里的媒体 URL（**必须是微信 CDN 域名**） |
| `key` / `token` | XML 里的 `key` 属性 / `token` 属性（ISAAC64 种子与下载凭据） |

```
200: 媒体字节流（响应头 X-SIWX-SNS-Encrypted / X-SIWX-SNS-Cached）
400: 非 CDN 地址（reason=not-cdn，防任意 URL 代理）
404/400/502: CDN 侧失败（附 reason / hosts_tried，失败已落 logs/siwx.log）
```

---

### `GET /api/sns/emoji?account=&emoji=`

**按需获取评论表情**（明文 `url` 优先——实测表情直链无需解密；`encrypt_url + aes_key` 仅备用）。

| 参数 | 说明 |
|---|---|
| `emoji` | JSON 对象（含 `url` 或 `encrypt_url`，来自动态 XML 的 `sns_emoji_data` 结构化节点） |

```
200: 表情字节流（响应头 X-SIWX-SNS-Via: plain/cache）
400: 缺参 / JSON 非法 ｜ 502: 下载失败
```

注意：表情域名（`vweixinf.tc.qq.com` 等）不同于朋友圈图床（`mmsns.qpic.cn`），不适用 CDN 域名回退。

---

### `GET /api/sns/friends?account=&limit=&names=`

**发布者聚合列表**（纯 SQL GROUP BY，实测 ~111ms vs 全量解析 1230ms）。

| 参数 | 说明 | 默认值 |
|---|---|---|
| `account` | 账号 wxid | 必填 |
| `limit` | 条数（最大 2000） | 200 |
| `names` | `0` 时不解析联系人备注/昵称 | `1` |

```json
// 响应
{"friends": [{"username": "wxid_yyy", "count": 120, "display": "张三", "has_avatar": true}],
 "total": 113}
```

`has_avatar` 由后端批量查 head_image.db 得出（避免前端逐个请求刷 404）；排序在前端做（拼音依赖浏览器 `Intl.Collator`）。

---

### `GET /api/sns/stats?account=`

**朋友圈统计**（动态总数、类型分布等，来自 `sns.timeline_stats()`）。

---

### `GET /api/sns/formats`

**可用导出格式**。`{"formats": ["json", "markdown", "txt", "html"]}`。

---

### `POST /api/sns/export`

**启动朋友圈导出任务**（异步，走任务槽，前端轮询 `GET /api/job`；已有任务时 409）。

```json
// 请求
{
  "account": "wxid_xxx",       // 必填
  "format": "json",            // json / markdown / txt / html，默认 json
  "media": true,               // 是否一并导出媒体
  "images": true, "videos": true, "livephotos": true,
  "concurrency": 5,            // CDN 并发路数（自动钳制）
  "keyword": null,             // 可选过滤
  "username": null, "usernames": null,
  "start": null, "end": null,  // unix 秒
  "limit": null
}

// 响应
{"started": true}
// 或
{"error": "已有任务在运行"}, 409 ｜ {"error": "账号或朋友圈数据库不存在"}, 404 ｜ 非法格式 400
```

导出产物落在 `paths.exports_root()`，媒体命名 `<tid>_<index>.<ext>`、实况视频 `<tid>_<i>_live.mp4`。

---

### `GET /api/sns/export/download?path=`

**下载导出产物**。`relative_to(exports_root)` 越界校验：越界 403、不存在 404、缺参 400。

---

## 聊天统计（全账号）

### `GET /api/stats/accounts`

**可统计的账号列表**（有 `message/*.db` 分片的账号）。

```json
// 响应
{"accounts": [{"wxid": "wxid_xxx", "shards": 3, "cached": true}]}
```

---

### `GET /api/stats/overview?account=&start=&end=&refresh=`

**统计概览**：总量、类型分布、月度趋势、活跃度、私聊发送者排行。

| 参数 | 说明 | 默认值 |
|---|---|---|
| `account` | 账号 wxid | 必填 |
| `start` / `end` | `YYYY-MM` 或 `YYYY-MM-DD`（填反自动对调） | 不限 |
| `refresh` | `1` 时强制重算（否则走签名缓存） | 0 |

统计失败返回 500 但不拖垮面板；账号无产物返回 404。

---

### `GET /api/stats/types?account=`

**完整类型分布**（未归并，供明细表）：`{"total": 1234, "types": {…}}`。

---

### `POST /api/stats/refresh`

**清统计缓存并重算**。`{"account": "wxid_xxx"}`（缺省清全部）→ `{"ok": true, …}`。

---

## 自动更新

### `GET /api/update/check`

**检查新版本**（响应禁止缓存，避免显示旧判断）。

```json
// 响应
{
  "has_update": true,
  "current": "5.0.5",
  "remote": "5.0.6",
  "frozen": true,
  "platform": "windows",
  "update_available": true    // has_update && frozen（源码运行不提供一键更新）
}
```

---

### `POST /api/update/do`

**执行更新**（仅 frozen 打包产物有意义）。请求体可空（重新拉取远程版本信息）。

---

### `GET /api/update/current`

**当前版本信息**：`{"version": "5.0.5", "frozen": false, "platform": "windows"}`。

---

## 导出

### `GET /api/export/formats`

**可用导出格式** = 内置 8 种 + 插件贡献（插件追加在内置之后，内置同名额优先）。

```json
// 响应
{"formats": [
  {"fmt": "json", "label": "JSON", "ext": "json", "owner": ""},
  {"fmt": "demo", "label": "Demo 格式", "ext": "demo", "owner": "demo_stats"}
]}
```

---

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

### `GET /api/settings/env`

**环境信息**（供「复制环境信息」按钮与 bug 报告）。

```json
// 响应
{
  "info": {…结构化环境信息…},
  "text": "…已对路径中的用户名打码，可直接粘贴到公开 issue…"
}
```

---

### `GET /api/settings/auto-sync`

**读取自动刷新数据库配置**。

```json
{
  "enabled": true,
  "interval_minutes": 30,
  "last_run": 1789197000,
  "last_ok": true,
  "last_message": "增量同步完成"
}
```

---

### `POST /api/settings/auto-sync`

**保存自动刷新数据库配置**。启用后 serve 模式会在微信在线且到达间隔时执行增量 `sync`。

```json
{"enabled": true, "interval_minutes": 30}
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

**MCP 配置信息**：启动命令、客户端 JSON 配置、工具开关列表（**11 个内置**：6 聊天 + 5 朋友圈，另加插件贡献）。

```json
// 响应
{
  "command": {"command": "python", "args": [".../run.py", "mcp"]},
  "client_config": "{ \"mcpServers\": { ... } }",
  "config_path": "C:/Users/.../stories-in-wx/mcp_config.json",
  "tools": [
    {"name": "get_status", "description": "获取运行状态", "owner": "", "enabled": true},
    {"name": "list_accounts", "description": "列出已解密的账号", "owner": "", "enabled": true},
    {"name": "list_sessions", "description": "列出某账号的全部会话", "owner": "", "enabled": true},
    {"name": "get_messages", "description": "读取某会话的消息", "owner": "", "enabled": true},
    {"name": "search_messages", "description": "按关键词搜索消息", "owner": "", "enabled": true},
    {"name": "export_chat", "description": "导出某会话聊天记录", "owner": "", "enabled": true},
    {"name": "list_sns_accounts", "description": "列出有朋友圈数据的账号", "owner": "", "enabled": true},
    {"name": "get_sns_timeline", "description": "读取朋友圈时间线", "owner": "", "enabled": true},
    {"name": "get_sns_detail", "description": "读取单条朋友圈动态详情", "owner": "", "enabled": true},
    {"name": "get_sns_friends", "description": "按发布者聚合朋友圈动态", "owner": "", "enabled": true},
    {"name": "export_sns", "description": "导出朋友圈到文件", "owner": "", "enabled": true}
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

### `GET /api/mcp/logs?limit=`

**MCP 调用日志**（最近 N 行，默认 200，最大 2000）。

```json
// 响应
{"logs": ["2026-09-06 20:00:00 [INFO] …"], "path": "C:/.../mcp_server.log", "total": 12}
```

---

## 插件

所有端点都遵循"零插件时返回空集合"的原则，前端据此静默跳过。

### `GET /api/plugins`

**插件加载状态与 hook 贡献统计**。

```json
// 响应
{
  "plugins": [
    {
      "name": "demo_stats", "version": "1.0.0",
      "source": "dir:C:/Users/.../plugins/demo_stats/__init__.py",
      "status": "ok",
      "hooks": {"pages": 2, "settings": 3, "mcp_tools": 1},
      "missing_requires": [], "error": ""
    }
  ],
  "counts": {"ok": 1, "degraded": 0, "error": 0},
  "hooks": {"pages": 2, "settings": 3, "mcp_tools": 1}
}
```

`status` 三态：`ok` / `degraded`（依赖缺失或同名被跳过）/ `error`（加载失败）。

---

### `GET /api/plugins/pages`

**左侧菜单的插件页数据**。默认只返回显示条件已满足的页面。

| 参数 | 说明 |
|---|---|
| `all` | `1` 时返回全部页面，并附 `conditions` 原文与 `reason`（调试"为什么没显示"） |

```json
// 响应
{
  "pages": [
    {
      "id": "demo_stats:stats", "name": "stats", "plugin": "demo_stats",
      "title": "统计面板", "icon": "📊", "badge": "DEMO", "tip": "",
      "group": "", "entry": "index", "order": 10,
      "base": "/plugin-pages/demo_stats", "conditions_met": true
    }
  ]
}
```

插件页 id 全局化为 `<插件名>:<页面名>`，前端路由为 `#/demo_stats:stats`。

---

### `GET /api/plugins/settings`

**全部插件的设置项**（含 schema 与当前值），供设置页自动渲染表单。

```json
// 响应
{
  "plugins": [
    {
      "plugin": "demo_stats", "version": "1.0.0", "description": "...",
      "items": [
        {"plugin": "demo_stats", "group": "统计面板",
         "key": "refresh_seconds", "type": "int", "label": "自动刷新间隔（秒）",
         "default": 30, "choices": [], "min": 5, "max": 600,
         "help": "...", "value": 30}
      ]
    }
  ]
}
```

---

### `POST /api/plugins/settings`

**更新某插件的设置**，按 schema 校验后原子写入。

```json
// 请求
{"plugin": "demo_stats", "values": {"refresh_seconds": 120, "theme": "warm"}}

// 响应
{"plugin": "demo_stats", "values": {"refresh_seconds": 120, "theme": "warm"}}
```

| 情况 | 结果 |
|---|---|
| 缺少 `plugin` | 400 |
| 未声明过的字段 | 忽略 + 记 warn |
| 类型/范围非法 | 回退该字段默认值 |
| 未知插件或无设置项 | 404 |

---

### `GET /api/plugins/config?plugin=`

**读取某插件的当前配置**（已按 schema 校验），供插件页 JS 便捷调用。

```json
// 响应
{"plugin": "demo_stats",
 "values": {"refresh_seconds": 30, "show_media": true, "theme": "auto"}}
```

---

### `GET /api/plugins/themes`

**插件声明的 CSS 主题**。

```json
// 响应
{"themes": [
  {"name": "demo-skin", "owner": "demo_stats", "priority": 0,
   "url": "/plugin-pages/demo_stats/theme.css", "inline": null}
]}
```

`url` 非空时注入 `<link>`；`inline` 非空时注入内联 `<style>`。

---

### `GET /plugin-pages/<plugin>/<path>`

**插件页静态资源**（HTML / CSS / JS）。只读，带路径逃逸防护；
插件未加载或路径越界返回 404。

---

## 错误码

| HTTP 码 | 含义 |
|---|---|
| 200 | 成功 |
| 400 | 参数错误 |
| 403 | 路径越界（下载限制在 exports 根内） |
| 404 | 资源不存在 |
| 409 | 冲突（已有任务运行） |
| 415 | 语音转码不可用（附 `fallback: "silk"`） |
| 422 | 动态 XML 无法解析 |
| 500 | 服务端错误（如统计失败） |
| 502 | 上游失败（朋友圈 CDN / 表情下载） |
