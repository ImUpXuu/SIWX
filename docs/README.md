# stories-in-wx 文档中心

> **版本**: v5.0.5 | **日期**: 2026-10-02 | **平台**: Windows / macOS (依赖 DPAPI / 微信进程读取 / LLDB)

## 一句话定位

微信 4.x 数据库密钥提取、解密、聊天查看、媒体解密、多格式导出的 Python 自研工具。
一条命令全自动：`python run.py auto`，或启动 Web 控制台 `python run.py serve`。

---

## 快速上手

```bash
# 安装依赖
pip install -r requirements.txt

# 全自动（扫描 → 密钥提取 → DPAPI 保存 → 数据库解密）
python run.py auto

# 启动 Web 控制台（默认 http://127.0.0.1:8787）
python run.py serve

# 仅提取密钥
python run.py keys extract

# 查看密钥库（打码显示）
python run.py keys list

# 仅解密（使用已有密钥库）
python run.py decrypt --out ./output

# 打印环境信息（提 issue 时粘贴）
python run.py doctor
```

---

## 文档地图

### 架构与总览

| 文档 | 内容 |
|---|---|
| [architecture.md](./architecture.md) | 整体架构、数据流、设计哲学、两层缓存模型 |

### 核心模块

| 文档 | 模块 | 职责 |
|---|---|---|
| [module-sqlcipher.md](./module-sqlcipher.md) | `sqlcipher.py` | SQLCipher 4 HMAC 验证原语 + 页级流式解密 |
| [module-keystore.md](./module-keystore.md) | `keystore.py` | salt 索引密钥库（DPAPI 加密落盘） |
| [module-discover.md](./module-discover.md) | `discover.py` | 全盘目录发现 + 微信进程发现 |
| [module-winproc.md](./module-winproc.md) | `winproc.py` | 跨进程只读内存访问原语（ctypes） |
| [module-extract.md](./module-extract.md) | `extract.py` | 编排器：全局收割 → 策略链 → 交叉验证 → 解密 |
| [module-strategies.md](./module-strategies.md) | `strategies/` | 策略注册表插件系统（4 个内置策略） |
| [module-pool.md](./module-pool.md) | `pool.py` | 多进程解密池 + 输出缓存清单 |
| [module-media.md](./module-media.md) | `media.py` | 媒体解密（V0/V1/V2 + wxgf 转码 + 三级图片源） |
| [module-server.md](./module-server.md) | `server.py` | Flask Web 控制台 + 任务槽 + 日志流 |
| [module-exporter.md](./module-exporter.md) | `exporter.py` | 多格式导出引擎（8 种格式 + 多选会话批量导出） |
| [module-mcp.md](./module-mcp.md) | `mcp_server.py` + `api_mcp.py` | MCP 服务器（stdio, JSON-RPC 2.0, 11 个工具） + 配置 API |
| [module-sns.md](./module-sns.md) | `sns.py` + `sns_cdn.py` + `sns_isaac64.py` + `sns_export.py` | **朋友圈**：XML 解析、CDN 媒体获取、ISAAC64 解密、多格式导出 |

### 扩展与插件

| 文档 | 内容 |
|---|---|
| [plugin-development.md](./plugin-development.md) | **插件开发指南**：60 秒上手、PLUGIN 字典速查、17 类 hook 详解、节点树语法、隔离机制 |
| [module-plugins.md](./module-plugins.md) | 插件系统内部架构：中央注册表、契约解析、目录加载、显示条件、配置存储 |

### 辅助模块

| 文档 | 模块 | 职责 |
|---|---|---|
| [module-tui.md](./module-tui.md) | `tui.py` | rich 驱动的终端 UI 组件层 |
| [module-paths.md](./module-paths.md) | `paths.py` | 统一应用路径（与 cwd 解耦） |
| [module-html-template.md](./module-html-template.md) | `html_template.py` | HTML 导出模板（交互式查看器） |

> `voice.py`（SILK→WAV 转码）、`export_stream.py`（流式消息解析）、`stats.py`（聊天统计）、
> `logger.py`（双模式日志 + 脱敏导出）、`env_info.py`（环境信息）、`auto_update.py`（自动更新）
> 的接口说明见 [api-reference.md](./api-reference.md) 与 [cli-commands.md](./cli-commands.md)。

### 接口与运维

| 文档 | 内容 |
|---|---|
| [cli-commands.md](./cli-commands.md) | CLI 命令完整参考（auto / keys / decrypt / serve / mcp / doctor） |
| [api-reference.md](./api-reference.md) | Web API 参考（全部端点 + 参数 + 返回值，含朋友圈 / 统计 / 更新） |
| [packaging.md](./packaging.md) | PyInstaller 打包 / GitHub Releases 发布流程 |
| [wal-support-plan.md](./wal-support-plan.md) | WAL 增量读取评估（结论：不需要实施，含实测依据） |

### 审计报告

| 文档 | 内容 |
|---|---|
| [security-audit-2026-09-19.md](./security-audit-2026-09-19.md) | 安全审计（路径穿越 / 无鉴权等发现与风险约束） |
| [data-safety-audit-2026-09-19.md](./data-safety-audit-2026-09-19.md) | 数据安全审计（并发缓存 / 撞名等数据正确性问题） |
| [full-code-audit-2026-09-28.md](./full-code-audit-2026-09-28.md) | 全量代码审计（≈26,500 行，4 类 11 项新识别问题） |

### 技术专题

| 文档 | 内容 |
|---|---|
| [media-decryption-principles.md](./media-decryption-principles.md) | V2 文件格式逐字节解析 + 账号级密钥派生 |
| [media-research.md](./media-research.md) | 媒体解密研究笔记 + 实测数据 |
| [sns-implementation-guide.md](./sns-implementation-guide.md) | **朋友圈实施指南**：参考项目、已完成资产、待办清单、风险红线 |
| [sns-research-2026-09-29.md](./sns-research-2026-09-29.md) | 朋友圈研究记录（30 轮实验原始数据、排除的假设、未解之谜） |
| [sns-todo.md](./sns-todo.md) | **朋友圈待办与接入指南**：未完成项清单（含真实数量、待提取字段、接入点、测试建议、优先级） |

---

## 项目结构

```
stories-in-wx-py/
├── run.py                  # CLI 入口（multiprocessing.freeze_support）
├── requirements.txt        # pycryptodome / flask / psutil / openpyxl / rich / zstandard / pilk
├── version.json            # 版本信息 + 发布资产 URL（CI 从 tag 生成）
├── diag_*.py               # 诊断脚本（blob / media / verify / sns_api / sns_cdn）
├── scripts/                # 研究与运维脚本
│   ├── wal_probe.py        # WAL 帧解析探针（评估用，结论见 wal-support-plan.md）
│   ├── sns_card_probe.py   # 朋友圈卡片 XML 探针（dump / --tags / --sub）
│   └── generate_version_json.py
├── packaging/              # PyInstaller spec（win / mac）
│   ├── siwx-win.spec
│   └── siwx-mac.spec
├── examples/               # 示例插件（可直接拷进用户插件目录）
│   └── plugins/demo_stats/
├── tests/                  # 回归测试 + 插件测试
│   ├── test_regressions.py # 全量回归（250+ 例）
│   ├── test_plugins.py
│   ├── test_sns.py         # 朋友圈 49 例
│   └── test_macos_lldb.py
├── .github/workflows/      # CI/CD
├── docs/                   # 本文档中心
├── video/                  # HTML → MP4 渲染（研究用，非核心链路）
├── output/                 # 解密产物（运行后生成）
├── exports/                # 导出产物（运行后生成）
│
└── siwx/                   # 核心包
    ├── __init__.py         # __version__ = "5.0.5"（版本唯一来源）
    ├── cli.py              # argparse 子命令 + 流程编排
    ├── extract.py          # 编排器（全局收割 + 两层缓存）
    ├── sqlcipher.py        # SQLCipher 4 原语 + 流式解密
    ├── keystore.py         # DPAPI 密钥库
    ├── discover.py         # 目录发现 + 进程发现
    ├── winproc.py          # 跨进程内存访问
    ├── pool.py             # 多进程解密池
    ├── paths.py            # 统一路径
    ├── tui.py              # rich 终端 UI
    ├── media.py            # 媒体解密（V0/V1/V2 + wxgf 转码）
    ├── voice.py            # 语音元数据 + SILK 读取 + pilk→WAV 转码
    ├── logger.py           # 双模式日志（粗略/详细）+ 脱敏导出
    ├── export_stream.py    # 流式消息解析与导出数据构造
    ├── exporter.py         # 导出引擎（8 种格式）
    ├── html_template.py    # HTML 导出模板
    ├── stats.py            # 聊天统计（跨分片聚合）
    ├── env_info.py         # 环境信息采集（供 bug 报告）
    ├── auto_update.py      # 自动更新检测与应用
    │
    ├── server.py           # Flask 控制台 + 任务槽（_run_job）+ 日志流
    ├── mcp_server.py       # MCP Server（stdio, JSON-RPC 2.0, 11 个工具）
    ├── api_chat.py         # 聊天 API 蓝图（含会话时间轴 / 单会话统计）
    ├── api_export.py       # 导出 API 蓝图
    ├── api_settings.py     # 设置 API 蓝图（含环境信息）
    ├── api_mcp.py          # MCP 配置 API 蓝图
    ├── api_plugins.py      # 插件 API 蓝图
    ├── api_stats.py        # 统计 API 蓝图
    ├── api_sns.py          # 朋友圈 API 蓝图（11 个端点）
    ├── api_update.py       # 更新 API 蓝图
    │
    ├── sns.py              # 朋友圈：XML 解析 + snsId 时间还原 + 卡片
    ├── sns_cdn.py          # 朋友圈：CDN URL 构造 / 下载 / ISAAC64 解密 / 缓存
    ├── sns_isaac64.py      # 朋友圈：ISAAC64 流密码（纯 Python）
    ├── sns_export.py       # 朋友圈：多格式导出（独立于 exporter.py）
    │
    ├── plugins/            # 插件系统（中央注册表 + 17 类 hook）
    │   ├── registry.py     # PluginRegistry 单例 + 命名空间
    │   ├── contract.py     # PLUGIN 字典校验 + 字符串函数名解析
    │   ├── loader.py       # 用户级目录扫描 + importlib 装载
    │   ├── conditions.py   # 声明式显示条件求值
    │   ├── config.py       # 每插件配置存储
    │   ├── report.py       # 加载报告
    │   └── chat_bridge.py  # registry ↔ api_chat 桥接
    │
    ├── strategies/         # 密钥提取策略链（平台条件加载）
    │   ├── __init__.py     # STRATEGY_REGISTRY
    │   ├── keystore_source.py  # 密钥库缓存
    │   ├── mmkv.py         # MMKV 离线提取
    │   ├── config_cipher.py    # WCDB Config.Cipher 扫描（Windows 主力）
    │   ├── memscan.py      # 内存字面量兜底
    │   └── macos_lldb.py   # LLDB 断点捕获（macOS 专属）
    │
    └── ui/                 # Web 前端（Flask 托管，原生 HTML/CSS/JS + Vue 3）
        ├── vendor/         # 内置 Vue 3 生产版（不依赖 CDN）
        ├── index.html
        ├── app.css / app.js / common.js / widgets.js
        └── pages/          # 8 个页面三件套 + 免责声明
            ├── guide.*     # 引导设置
            ├── chat.*      # 聊天查看
            ├── sns.*       # 朋友圈
            ├── stats.*     # 聊天统计
            ├── export.*    # 导出
            ├── mcp.*       # MCP
            ├── logs.*      # 日志
            ├── settings.*  # 设置
            └── disclaimer.html  # 免责声明（首次启动强制确认）
```

---

## 关键设计红线

1. **静止状态加密**：密钥库用 DPAPI + 项目熵加密，不落明文
2. **只读提取**：进程句柄仅 `PROCESS_VM_READ | PROCESS_QUERY_INFORMATION`，无写入/注入
3. **日志脱敏**：只输出 salt 与打码密钥（`xxxxxx…xxxx`），永不出明文
4. **按需解密媒体**：全量解密可能达数 GB，按需 + 内存 LRU 缓存（200 张，程序关闭释放）
5. **salt 才是身份**：密钥按 salt 索引（数据库的真实身份），不是按文件路径

---

## 实测环境

- **微信版本**: WeChat (Weixin.exe) 4.1.13.63 / Windows 11
- **全局收割**: 79 个唯一 salt，一次内存扫描联合验证，32 个密钥通过
- **解密**: 32/32 数据库成功（556MB，19.8s），contact.db 读出 3864 个联系人
- **媒体**: 朋友圈 40/40、聊天图片 15/15 用同一把账号级密钥解密成功
- **朋友圈**: 真实库 5684 条动态；四格式导出全通过；SNS 测试 49/49
- **测试**: 全量回归 266 通过 / 1 跳过（2026-10-01）
