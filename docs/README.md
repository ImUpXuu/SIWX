# stories-in-wx 文档中心

> **版本**: v0.2.5 | **日期**: 2026-09-06 | **平台**: Windows (依赖 DPAPI / 微信进程读取)

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
| [module-mcp.md](./module-mcp.md) | `mcp_server.py` + `api_mcp.py` | MCP 服务器（stdio, JSON-RPC 2.0, 6 个工具） + 配置 API |

### 辅助模块

| 文档 | 模块 | 职责 |
|---|---|---|
| [module-tui.md](./module-tui.md) | `tui.py` | rich 驱动的终端 UI 组件层 |
| [module-paths.md](./module-paths.md) | `paths.py` | 统一应用路径（与 cwd 解耦） |
| [module-html-template.md](./module-html-template.md) | `html_template.py` | HTML 导出模板（交互式查看器） |

### 接口与运维

| 文档 | 内容 |
|---|---|
| [cli-commands.md](./cli-commands.md) | CLI 命令完整参考（auto / keys / decrypt / serve） |
| [api-reference.md](./api-reference.md) | Web API 参考（全部端点 + 参数 + 返回值） |
| [packaging.md](./packaging.md) | PyInstaller 打包 / GitHub Releases 发布流程 |

### 技术专题

| 文档 | 内容 |
|---|---|
| [media-decryption-principles.md](./media-decryption-principles.md) | V2 文件格式逐字节解析 + 账号级密钥派生 |
| [media-research.md](./media-research.md) | 媒体解密研究笔记 + 实测数据 |

---

## 项目结构

```
stories-in-wx-py/
├── run.py                  # CLI 入口（multiprocessing.freeze_support）
├── requirements.txt        # pycryptodome / flask / psutil / openpyxl / rich / zstandard
├── version.json            # 版本信息 + 发布资产 URL
├── diag_*.py               # 诊断脚本（blob / media / verify）
├── packaging/              # PyInstaller spec（win / mac）
│   ├── siwx-win.spec
│   └── siwx-mac.spec
├── .github/workflows/      # CI/CD
├── docs/                   # 本文档中心
├── output/                 # 解密产物（运行后生成）
├── exports/                # 导出产物（运行后生成）
│
└── siwx/                   # 核心包
    ├── __init__.py         # __version__ = "0.2.0"
    ├── cli.py              # argparse 子命令 + 流程编排
    ├── extract.py          # 编排器（全局收割 + 两层缓存）
    ├── sqlcipher.py        # SQLCipher 4 原语 + 流式解密
    ├── keystore.py         # DPAPI 密钥库
    ├── discover.py         # 目录发现 + 进程发现
    ├── winproc.py          # 跨进程内存访问
    ├── pool.py             # 多进程解密池
    ├── paths.py            # 统一路径
    ├── tui.py              # rich 终端 UI
    ├── media.py            # 媒体解密
    ├── server.py           # Flask 控制台
    ├── api_chat.py         # 聊天 API 蓝图
    ├── api_export.py       # 导出 API 蓝图
    ├── api_settings.py     # 设置 API 蓝图
    ├── exporter.py         # 导出引擎
    ├── html_template.py    # HTML 模板
    │
    ├── strategies/         # 策略插件
    │   ├── __init__.py     # STRATEGY_REGISTRY
    │   ├── keystore_source.py  # 密钥库缓存
    │   ├── mmkv.py         # MMKV 离线提取
    │   ├── config_cipher.py    # WCDB Config.Cipher 扫描
    │   └── memscan.py      # 内存字面量兜底
    │
    └── ui/                 # Web 前端（无构建步骤）
        ├── index.html
        ├── app.css / app.js
        ├── common.js
        └── pages/          # 模块化页面
            ├── guide.*     # 引导页（密钥提取 + 解密）
            ├── chat.*      # 聊天查看页
            ├── export.*    # 导出页
            ├── settings.*  # 设置页
            └── logs.*      # 日志页
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
