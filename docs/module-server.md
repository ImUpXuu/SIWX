# module-server.py — Flask Web 控制台

> **文件**: `siwx/server.py` | **角色**: Web 控制台 —— 页面路由 + 任务槽 + 日志流

---

## 职责

1. **静态资源服务**: 提供 UI 页面和静态文件
2. **任务调度**: 单任务槽（同一时间只允许一个后台任务）
3. **日志流**: 环形日志缓冲，供日志页展示
4. **API 注册**: 模块化 Blueprint（chat / export / settings / mcp / update / plugins / stats / sns）
5. **状态监控**: 微信进程 / 账号 / 密钥库状态

---

## 架构

```
Flask App (server.py)
├─ 静态路由:
│   ├─ GET /              → index.html
│   ├─ GET /app.css       → CSS
│   ├─ GET /app.js        → JS
│   ├─ GET /common.js     → 公共 JS
│   └─ GET /pages/*       → 模块化页面资源
│
├─ API 路由:
│   ├─ GET  /api/status   → 状态总览
│   ├─ POST /api/run      → 启动任务
│   ├─ GET  /api/logs     → 环形日志缓冲
│   ├─ GET  /api/job      → 任务状态
│   ├─ POST /api/discover/validate → 验证微信路径
│   ├─ GET  /api/logs/settings → 日志设置
│   ├─ POST /api/logs/settings → 更新日志设置
│   ├─ GET  /api/logs/export → 导出日志
│   └─ GET  /plugin-pages/<plugin>/<path> → 插件页面资源
│
├─ Blueprint 注册:
│   ├─ /api/chat/*        → api_chat.py
│   ├─ /api/export/*      → api_export.py
│   ├─ /api/settings/*    → api_settings.py
│   ├─ /api/mcp/*         → api_mcp.py
│   ├─ /api/update/*      → api_update.py
│   ├─ /api/plugins/*     → api_plugins.py
│   ├─ /api/stats/*       → api_stats.py
│   └─ /api/sns/*         → api_sns.py
│
└─ 任务执行器:
    └─ _run_job()         → 后台线程执行
```

---

## 任务槽

### 状态机

```
空闲 → 运行中 → 完成
              → 失败
```

### 任务类型

| mode | 说明 |
|---|---|
| `keys` | 仅提取密钥 |
| `decrypt` | 仅解密 |
| `auto` | 提取 + 解密 |
| `sync` | 增量同步（密钥缓存优先 → 收割缺失 → 只解密变更库） |
| `export` | 导出聊天记录 |
| `sns_export` | 导出朋友圈 |

### 任务状态

```python
_job = {
    "running": False,    # 是否运行中
    "mode": None,        # 任务类型
    "done": False,       # 是否完成
    "ok": False,         # 是否成功
    "logs": [],          # 任务日志 [[ts, msg], ...]
    "report": None,      # 结果报告
}
```

---

## 关键函数

### `run_server(host, port, open_browser) → None`

**启动 Web 控制台**。

```
流程:
1. 打印 TUI banner
2. 启动 Flask（后台线程，静默模式）
3. 注册 media.event → tui.log
4. 打开浏览器（0.8s 延迟）
5. 主线程：常驻状态栏（Ctrl+C 退出）
```

**Flask 静默**: 重定向 stdout/stderr 到 `io.StringIO()`，
关闭 werkzeug 日志。

---

### `_run_job(mode, ...) → None`

**任务执行器**（后台线程）。

```python
def _run_job(mode, db_dir=None, out_dir=None, no_cache=False, workers=None, export_opts=None):
    try:
        dirs = find_wechat_data_dirs()
        if mode == "keys":
            # 密钥库缓存 → 收割缺失 → 逐账号提取
            ...
        elif mode == "decrypt":
            # 并行解密
            ...
        elif mode == "auto":
            # 提取 + 解密
            ...
        elif mode == "sync":
            # 增量同步
            ...
        elif mode == "export":
            # 调用 exporter.run_export()
            ...
        _job["ok"] = True
        _job["report"] = report
    except Exception as e:
        _job["ok"] = False
    finally:
        _job["running"] = False
        _job["done"] = True
```

---

### `_log(msg) → None`

**日志写入**。

```python
def _log(msg):
    with _lock:
        ts = int(time.time() * 1000)
        _job["logs"].append([ts, msg])      # 任务日志
        _LOG_RING.append([ts, msg])         # 全局环形缓冲
        _siwx_logger.info(msg)              # 文件日志（logs/siwx.log）
```

**环形缓冲**: 最大 2000 条，超出时删除最早的。
**文件日志**: 同时写入 `logs/siwx.log`（10MB × 5 轮转）。

---

## API 路由

### `GET /api/status`

**状态总览**。

```json
{
  "wechat_running": true,
  "pids": [1234, 5678],
  "accounts": [
    {
      "wxid": "wxid_xxx",
      "db_dir": "C:/.../db_storage",
      "db_count": 32,
      "keys_cached": 30,
      "total_salts": 32,
      "manual": false
    }
  ],
  "stored_salts": 32,
  "conflicts": [{"wxid": "wxid_xxx", "dirs": ["C:/.../db_storage", "D:/.../db_storage"]}],
  "job": "空闲"
}
```

---

### `POST /api/run`

**启动任务**。

```json
// 请求
{
  "mode": "auto",
  "db_dir": null,        // 可选，指定单账号
  "out_dir": null,       // 可选，默认 ./output
  "no_cache": false,
  "workers": null,
  "export_opts": null    // 导出选项
}

// 响应
{"started": true}
// 或
{"error": "已有任务在运行"}, 409
```

---

### `GET /api/logs`

**返回文件日志 + 环形任务日志 + MCP 调用日志（合并按时间排序）**。

```json
{
  "logs": [
    [1725600000000, "[cipher] 扫描完成"],
    [1725600000100, "…"],
  ],
  "level": "rough"
}
```

**合并逻辑**: 文件日志（`_tail_app_log`）+ 环形日志（`_LOG_RING`）+ MCP 日志（`_tail_mcp_log`）+ 结构化日志（`log.get_logs()`），按时间戳排序后去重。

---

### `GET /api/job`

**返回当前任务状态（供前端轮询）**。

```json
{
  "running": true,
  "done": false,
  "ok": false,
  "mode": "auto",
  "logs": [[1725600000000, "任务开始"]],
  "report": null
}
```

---

### `POST /api/discover/validate`

**验证并保存手动输入的微信存储路径**。

```json
// 请求
{"path": "D:\\xwechat_files\\wxid_xxx\\db_storage"}
// 响应
{"ok": true, "wxid": "wxid_xxx", "db_dir": "...", "saved": true}
```

---

### `GET /api/logs/settings` / `POST /api/logs/settings`

**日志模式控制**。

```json
// GET 响应
{"level": "rough"}
// POST 请求
{"level": "detailed"}
```

---

### `GET /api/logs/export`

**导出脱敏日志**。

```
参数: start, end, desensitize=1
响应: text/plain（可直接下载）
```

---

## 日志合并

### `_tail_app_log(limit=800) → list`

**读取 siwx.log 文件末尾，转换为 `[ts_ms, message]` 格式**。

```
流程:
1. 找到 siwx.log 文件路径
2. 读取文件末尾 256KB
3. 解析每行时间戳 → ts_ms
4. 过滤 404 Not Found 噪音
5. 返回 [ts_ms, message] 列表
```

### `_tail_mcp_log(limit=500) → list`

**读取 MCP 日志文件末尾，转换为 `[ts_ms, message]` 格式**。

```
流程:
1. 找到 mcp.log 文件路径
2. 读取文件末尾 64KB
3. 解析每行时间戳 → ts_ms
4. 返回 [ts_ms, "[MCP] message"] 列表
```

---

## UI 目录结构

```
siwx/ui/
├── index.html          # 入口页
├── app.css             # 全局样式
├── app.js              # 全局 JS（路由 + 状态管理）
├── common.js           # 公共工具函数
└── pages/              # 模块化页面
    ├── guide.*         # 引导页（密钥提取 + 解密）
    ├── chat.*          # 聊天查看页
    ├── sns.*           # 朋友圈页
    ├── stats.*         # 聊天统计页
    ├── export.*        # 导出页
    ├── mcp.*           # MCP 配置页
    ├── settings.*      # 设置页
    ├── logs.*          # 日志页
    └── disclaimer.html # 免责声明弹层
```

### UI 路径解析

```python
def _ui_dir() -> Path:
    if getattr(sys, "frozen", False):
        # PyInstaller 打包后资源在 _MEIPASS
        return Path(sys._MEIPASS) / "ui"
    # 源码运行时在 siwx/ui
    return Path(__file__).resolve().parent / "ui"
```

---

## 状态栏

```python
def _status_getter() -> dict:
    return {
        "url": "http://127.0.0.1:8787",
        "wechat": "运行中(2)",
        "wxid": "wxid_xxx",
        "keys": "32",
        "job": "空闲",  // "auto…" / "✓完成" / "✗失败"
    }
```

**常驻显示**: 1 秒刷新，ANSI 转义码定位光标。

---

## 自动同步调度器

### `_start_auto_sync_scheduler() → None`

**后台增量同步调度器**（30 秒轮询）。

```
流程:
1. 每 30 秒检查一次
2. 条件: 开启 + 到间隔 + 微信在线 + 无运行中任务
3. 满足时启动 sync 任务
4. 记录 last_run / last_ok / last_message
```

**配置**: `auto_sync.json`（存于 `paths.app_root()`）
- `enabled`: 是否开启
- `interval_minutes`: 间隔（1~1440，默认 30）

---

## 崩溃钩子

### `_install_crash_hooks() → None`

**记录非 Flask/任务线程里的未捕获异常和 Python fatal traceback**。

```
功能:
1. sys.excepthook → 记录主线程未捕获异常
2. threading.excepthook → 记录线程未捕获异常
3. faulthandler.enable() → 记录 Python fatal traceback 到 crash.log
```

**日志位置**: `logs/crash.log`

---

## 文件日志

### `_setup_file_logger() → logging.Logger`

**详细文件日志**（10MB × 5 轮转）。

```
功能:
1. 创建 logs/ 目录
2. RotatingFileHandler: siwx.log, 10MB × 5
3. 同时输出到控制台（INFO+）
```

**日志位置**: `logs/siwx.log`

---

## 插件系统集成

### `_register_plugin_blueprints(flask_app) → None`

**把插件声明的 api_blueprints 挂到 Flask 应用上**。

```
流程:
1. 检查 registry.api_blueprints 是否为空
2. 遍历插件蓝图，逐个 try/except
3. 重名跳过，失败不影响宿主启动
```

### `_plugin_theme_links() → str`

**插件主题 → `<link>` 标签串**（按 priority 排序）。

```
流程:
1. 检查 registry.themes 是否为空
2. 遍历插件主题，生成 <link> 标签
3. 只允许页面资源目录内的相对 css 名（防路径穿越）
```

### `_emit_task_event(event: str, **ctx) → None`

**广播任务生命周期事件给插件监听器**（start / done）。

```
流程:
1. 检查 registry.task_listeners 是否为空
2. 遍历插件监听器，逐个 try/except
3. 插件异常只写日志，绝不影响任务本身
```

---

## 设计决策

### 为什么单任务槽？

- 微信内存扫描是独占资源
- 避免并发解密池竞争 IO
- 简化状态管理

### 为什么 Flask 静默？

serve 模式下 TUI 是主界面，Flask 横幅/日志会干扰 TUI 输出。

### 为什么环形日志缓冲？

日志页需要全局历史（跨任务），而任务日志只保留当前任务的。
环形缓冲限制内存使用。

---

## 使用示例

```bash
# 启动控制台
python run.py serve

# 指定端口
python run.py serve --port 9999

# 不自动打开浏览器
python run.py serve --no-open
```
