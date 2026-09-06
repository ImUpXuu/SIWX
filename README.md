# stories-in-wx (Python)

微信 4.x（Weixin 4.1.x，已在 4.1.13.63 实测）数据库密钥提取、解密、聊天查看、媒体解密、多格式导出、MCP 接管的 Python 自研工具。

> **跨平台**: Windows（完整支持） + macOS（4.1.80+，LLDB 密钥提取）

## 特性

- **全自动**：`python run.py auto` 一条命令完成全盘目录扫描 → 全局收割密钥 → DPAPI 加密保存 → 数据库解密
- **全局收割**：微信内存只扫一次，用全部账号 salt 的联合集做 HMAC 验证，密钥再分发给各账号
- **只读提取**：WCDB Config.Cipher 两遍扫描（4.1.10+ 免管理员、免重启），自研 crib-drag 掩码恢复兜底
- **macOS 支持**：4.1.80+ 通过 LLDB 断点捕获 passphrase + PBKDF2 派生密钥
- **流式导出**：万条聊天不 OOM，增量写入 + 并行媒体解密
- **安全**：密钥按 salt 索引、DPAPI 加密落盘（含熵绑定）、日志只输出打码密钥、进程句柄只读（VM_READ，无注入）
- **现代化轻量 UI**：Flask + 原生 HTML/CSS/JS，明暗双主题，仅绑定 127.0.0.1
- **多格式导出**：8 种格式（JSON/HTML/TXT/CSV/Markdown/TOML/SQLite/XLSX），多选会话批量导出
- **MCP 服务器**：`python run.py mcp` 启动 stdio MCP 服务器，AI 客户端直接查询聊天记录
- **自动更新**：打包产物启动时检查 GitHub Release，一键更新
- **插件缝**：`siwx/strategies/STRATEGY_REGISTRY`，追加同签名 `extract(ctx)` 即可接入

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

启动 Web 控制台 → 进入 MCP 页 → 复制客户端配置 JSON 到你的 AI 客户端配置文件：

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

MCP 提供 6 个工具：`get_status` / `list_accounts` / `list_sessions` / `get_messages` / `search_messages` / `export_chat`。

## 实测记录（2026-09-06，WeChat 4.1.13.63）

- 全局收割：79 个唯一 salt，一次内存扫描联合验证，32 个密钥 HMAC 验证通过
- 解密：32/32 数据库成功（556MB，19.8s），contact.db 读出 3864 个真实联系人
- 流式导出：万条聊天内存 <50MB，4939 条仅需 422ms
- 解密速度：195MB/s（流式，inline XOR 优化）

## 架构

```
siwx/
├─ sqlcipher.py            # HMAC 验证原语 + 流式页级解密（195MB/s）
├─ keystore.py             # salt 索引密钥库（DPAPI 加密）
├─ discover.py             # 跨平台目录发现 + 进程发现（psutil）
├─ winproc.py              # 只读跨进程内存原语（ctypes, Windows）
├─ strategies/             # 插件缝：keystore / mmkv / config_cipher / memscan / macos_lldb
├─ extract.py              # 编排：全局收割 → 策略链 → 交叉验证 → 解密
├─ server.py               # Flask 控制台
├─ mcp_server.py           # MCP 服务器（stdio, JSON-RPC 2.0, 6 个工具）
├─ export_stream.py        # 流式导出管线（heapq.merge + 增量写入）
├─ exporter.py             # 多格式导出引擎（8 种格式 + 多选批量 + 并行媒体）
├─ auto_update.py          # 自动更新（版本检查 + 平台检测 + 增量更新）
├─ api_*.py                # API 蓝图（chat / export / settings / mcp / update）
├─ html_template.py        # HTML 导出模板（交互式查看器）
├─ paths.py                # 统一路径（防弹版）
├─ media.py                # 媒体解密（V0/V1/V2 + wxgf）
├─ tui.py                  # rich 终端 UI
└─ ui/                     # 现代化前端（无构建步骤）
    ├─ index.html / app.js / common.js / app.css
    └─ pages/              # guide / chat / export / mcp / logs / settings
```

## 文档

详见 [docs/README.md](./docs/README.md)，包含每个模块的详细说明、架构设计、CLI/API 参考。

## 贡献者

| 贡献者 | 说明 |
|---|---|
| [ImUpXuu](https://github.com/ImUpXuu) | 项目作者，核心架构与 Windows 端实现 |
| [lcrworld-jtl](https://github.com/lcrworld-jtl) | macOS 端密钥提取支持（LLDB 断点 + PBKDF2 派生策略） |

## 许可证

本项目采用 **GNU Affero General Public License v3.0 (AGPL-3.0)** 许可证。

- ✅ 个人使用、研究、学习
- ✅ 修改源码（需开源修改版本）
- ❌ **不可用于商业目的**（包括销售、付费服务、集成到商业产品）
- ❌ 闭源分发

完整许可证文本：[LICENSE](./LICENSE) | [AGPL-3.0 官方文本](https://www.gnu.org/licenses/agpl-3.0.html)

## 免责声明

1. **个人责任**：使用本项目产生的任何法律责任由使用者自行承担。项目作者不对任何直接或间接损失负责。

2. **合法使用**：本项目仅供个人数据备份与研究使用。使用者应遵守所在国家/地区的法律法规。**严禁用于未经授权访问他人数据、侵犯隐私、窃取商业机密等非法目的**。

3. **数据安全**：解密后的聊天记录包含敏感个人信息。使用者有责任妥善保管解密产物，防止数据泄露。建议在使用完成后及时删除不必要的解密文件。

4. **无担保**：本项目按"原样"提供，不保证适用性、可靠性或正确性。使用本项目可能导致微信账号封禁等风险，使用者需自行承担。

5. **微信条款**：本项目可能违反微信/WeChat 服务条款。使用者需自行评估风险。

6. **商业禁止**：依据 AGPL-3.0 许可证，本项目及其衍生作品**不得用于商业目的**。任何商业使用行为均违反许可证条款。

## 说明

- 未登录账号的密钥不在微信内存中（微信按需懒加载数据库），切换登录后重新运行即可提取。
- macOS 端需要微信已登录且安装 Xcode Command Line Tools（提供 lldb）。
