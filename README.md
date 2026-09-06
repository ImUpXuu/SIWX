# stories-in-wx (Python)

微信 4.x（Weixin 4.1.x，已在 4.1.13.63 实测）数据库密钥提取、解密、聊天查看、媒体解密、多格式导出、MCP 接管的 Python 自研工具。

## 特性

- **全自动**：`python run.py auto` 一条命令完成全盘目录扫描（A-Z 盘符 + 用户目录）→ 全局收割密钥 → DPAPI 加密保存 → 数据库解密
- **全局收割**：微信内存只扫一次，用全部账号 salt 的联合集做 HMAC 验证，密钥再分发给各账号
- **只读提取**：WCDB Config.Cipher 两遍扫描（4.1.10+ 免管理员、免重启），自研 crib-drag 掩码恢复兜底
- **安全**：密钥按 salt 索引、DPAPI 加密落盘（含熵绑定）、日志只输出打码密钥、进程句柄只读（VM_READ，无注入）
- **现代化轻量 UI**：Flask + 原生 HTML/CSS/JS，明暗双主题、适度圆角，仅绑定 127.0.0.1
- **多格式导出**：支持 8 种格式（JSON/HTML/TXT/CSV/Markdown/TOML/SQLite/XLSX），可多选会话批量导出到一个目录 / 每会话子文件夹 / 单个或每会话独立压缩包
- **MCP 服务器**：`python run.py mcp` 启动 stdio MCP 服务器，Claude / ZCode 等 AI 客户端可直接查询聊天记录、搜索消息、导出数据（零依赖 JSON-RPC 2.0）
- **插件缝**：`siwx/strategies/STRATEGY_REGISTRY`，向列表追加同签名 `extract(ctx)` 函数即可接入新策略

## 使用

```bash
pip install -r requirements.txt

python run.py auto                     # 全自动（推荐）
python run.py keys extract [--json]    # 仅提取密钥（全局收割）
python run.py keys list                # 查看密钥库（打码）
python run.py decrypt [--db-dir X] [--out DIR]
python run.py serve [--port 8787]      # Web 控制台
python run.py mcp                      # MCP 服务器（stdio，供 AI 客户端接入）
```

## MCP 配置

启动 Web 控制台 → 进入 MCP 页 → 复制客户端配置 JSON 到你的 AI 客户端配置文件（如 Claude Desktop 的 `claude_desktop_config.json`）：

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

MCP 提供 6 个工具：`get_status` / `list_accounts` / `list_sessions` / `get_messages` / `search_messages` / `export_chat`。可在 MCP 页按需禁用指定工具。

## 实测记录（2026-09-06，WeChat 4.1.13.63）

- 全局收割：79 个唯一 salt，一次内存扫描联合验证，32 个密钥 HMAC 验证通过
- 当前登录账号 32/32 全覆盖，密钥存入 DPAPI 密钥库
- 解密：32/32 数据库成功（556MB，19.8s），contact.db 读出 3864 个真实联系人

## 架构

```
siwx/
├─ sqlcipher.py            # HMAC 验证原语（全系统咽喉）+ 页级解密
├─ keystore.py             # salt 索引密钥库（DPAPI 加密）
├─ discover.py             # 全盘自动目录发现 + 进程发现（psutil）
├─ winproc.py              # 只读跨进程内存原语（ctypes）
├─ strategies/             # 插件缝：keystore / mmkv / config_cipher / memscan
├─ extract.py              # 编排：全局收割 → 策略链 → 交叉验证 → 解密
├─ server.py               # Flask 控制台（/api/status /api/run /api/logs /api/job）
├─ mcp_server.py           # MCP 服务器（stdio, JSON-RPC 2.0, 6 个工具）
├─ api_mcp.py              # MCP 配置 API 蓝图
├─ api_chat.py             # 聊天查看 API 蓝图
├─ api_export.py           # 导出 API 蓝图
├─ api_settings.py         # 设置 API 蓝图
├─ exporter.py             # 多格式导出引擎（8 种格式 + 多选批量导出）
├─ html_template.py        # HTML 导出模板（交互式查看器）
├─ paths.py                # 统一路径（防弹版：SIWX_ROOT / sys.frozen / __main__ / __file__）
├─ media.py                # 媒体解密（V0/V1/V2 + wxgf）
├─ tui.py                  # rich 终端 UI
└─ ui/                     # 现代化前端（无构建步骤）
    ├─ index.html / app.js / common.js / app.css
    └─ pages/              # guide / chat / export / mcp / logs / settings
```

## 文档

详见 [docs/README.md](./docs/README.md)，包含每个模块的详细说明、架构设计、CLI/API 参考。

## 说明

- 未登录账号的密钥不在微信内存中（微信按需懒加载数据库），切换登录后重新运行即可提取——这是微信本身的密钥生命周期，原项目同样存在。
- 仅供个人数据备份与研究使用，严禁用于侵犯他人隐私。
