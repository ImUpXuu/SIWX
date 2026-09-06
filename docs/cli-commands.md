# CLI 命令参考

> **入口**: `run.py` → `siwx/cli.py` | **依赖**: `argparse` + `rich`

---

## 命令总览

```
python run.py auto                     # 全自动（推荐）
python run.py keys extract [--json]    # 仅提取密钥
python run.py keys list                # 查看密钥库（打码）
python run.py decrypt [--db-dir X] [--out DIR]
python run.py serve [--port 8787]      # Web 控制台
python run.py mcp                      # MCP 服务器（stdio, 供 AI 客户端接入）
```

**裸跑（双击 exe）**: 默认启动 Web 控制台。

---

## 全局参数

| 参数 | 说明 | 默认值 |
|---|---|---|
| `--workers N` | 并行解密进程数 | `min(8, cpu_count)` |
| `--no-cache` | 忽略缓存强制重跑 | false |

---

## auto — 全自动

```
python run.py auto [--out DIR] [--workers N] [--no-cache]
```

**流程**: 扫描 → 密钥提取 → DPAPI 保存 → 数据库解密

**参数**:
| 参数 | 说明 | 默认值 |
|---|---|---|
| `--out DIR` | 解密输出根目录 | `./output` |

**退出码**:
| 码 | 含义 |
|---|---|
| 0 | 全部成功 |
| 1 | 未找到微信数据目录 |
| 2 | 部分账号未完成（未登录账号密钥不在内存中） |

**示例**:
```bash
python run.py auto
python run.py auto --out D:\decrypted --workers 4
python run.py auto --no-cache  # 强制重跑
```

---

## keys extract — 提取密钥

```
python run.py keys extract [--db-dir X] [--json] [--no-cache]
```

**流程**: 全局收割 → 策略链 → 交叉验证 → 保存到密钥库

**参数**:
| 参数 | 说明 | 默认值 |
|---|---|---|
| `--db-dir X` | 指定单账号数据目录 | 自动扫描全部 |
| `--json` | JSON 格式输出 | false |

**退出码**:
| 码 | 含义 |
|---|---|
| 0 | 全部 salt 已验证 |
| 2 | 部分 salt 未验证 |

**示例**:
```bash
python run.py keys extract
python run.py keys extract --json
python run.py keys extract --db-dir "C:/.../wxid_xxx/db_storage"
```

---

## keys list — 查看密钥库

```
python run.py keys list
```

**输出**: 打码显示的密钥库表格。

```
密钥库 · 32 条 · DPAPI 加密
路径: C:\Users\xxx\AppData\Local\stories-in-wx\keystore.bin

salt           来源      更新时间           密钥
abcdef12…      cipher    2026-09-06 20:00   abcdef…7890
fedcba98…      mmkv      2026-09-06 20:00   fedcba…0123
```

---

## decrypt — 解密数据库

```
python run.py decrypt [--db-dir X] [--out DIR] [--workers N] [--no-cache]
```

**流程**: 加载密钥库 → 缓存判定 → 并行解密

**参数**:
| 参数 | 说明 | 默认值 |
|---|---|---|
| `--db-dir X` | 指定单账号数据目录 | 自动扫描全部 |
| `--out DIR` | 解密输出根目录 | `./output` |

**退出码**:
| 码 | 含义 |
|---|---|
| 0 | 全部成功 |
| 2 | 全部失败 |

**示例**:
```bash
python run.py decrypt
python run.py decrypt --out D:\decrypted --workers 8
```

---

## serve — Web 控制台

```
python run.py serve [--host HOST] [--port N] [--no-open]
```

**流程**: 启动 Flask → 打开浏览器 → 常驻状态栏

**参数**:
| 参数 | 说明 | 默认值 |
|---|---|---|
| `--host HOST` | 绑定地址 | `127.0.0.1` |
| `--port N` | 端口 | `8787` |
| `--no-open` | 不自动打开浏览器 | false |

**示例**:
```bash
python run.py serve
python run.py serve --port 9999
python run.py serve --no-open
```

---

## mcp — MCP 服务器

```
python run.py mcp
```

**流程**: 启动 stdio MCP 服务器，等待 AI 客户端通过 stdin/stdout 连接。

**参数**: 无。

**说明**:
- 协议: newline-delimited JSON-RPC 2.0（MCP 2024-11-05）
- 工具: `get_status` / `list_accounts` / `list_sessions` / `get_messages` / `search_messages` / `export_chat`
- 客户端配置: 启动 Web 控制台 → MCP 页 → 复制配置 JSON

**示例**:
```bash
# 手动测试（Ctrl+C 退出）
python run.py mcp

# 验证 JSON-RPC 握手
echo '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2024-11-05","capabilities":{},"clientInfo":{"name":"test","version":"1.0"}}}' | python run.py mcp
```

**验证 MCP 工具列表**:
```bash
echo '{"jsonrpc":"2.0","id":2,"method":"tools/list","params":{}}' | python run.py mcp
```

---

## 平台检查

```python
if os.name != "nt":
    print("stories-in-wx 依赖 Windows 平台接口...")
    return 1
```

非 Windows 平台直接退出（macOS 版本仅为构建产物占位）。

---

## 中断处理

```python
try:
    return args.fn(args)
except KeyboardInterrupt:
    tui.log("\n中断")
    return 130
```

Ctrl+C 优雅退出，返回码 130。
