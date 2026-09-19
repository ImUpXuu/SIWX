<div align="center">

# Stories in WX · SIWX

**把微信 4.x 本地聊天记录，变成可浏览、可搜索、可导出、可交给 AI 的个人资料库。**

[![License](https://img.shields.io/badge/license-AGPL--3.0-blue.svg)](./LICENSE)
[![Python](https://img.shields.io/badge/python-3.10%2B-3776AB.svg)](https://www.python.org/)
[![Version](https://img.shields.io/badge/version-5.0.3-success.svg)](./version.json)
[![Platform](https://img.shields.io/badge/platform-Windows%20%7C%20macOS-lightgrey.svg)](#平台支持)

[快速开始](#快速开始) · [核心功能](#核心功能) · [MCP 接入](#让-ai-读取你的聊天记录mcp) · [免责声明](#免责声明)

</div>

## 核心功能

- **一键全流程** — 发现账号 → 提取密钥 → 解密数据库 → 浏览聊天 → 恢复媒体 → 转码语音 → 多格式导出，全部在 Web 控制台内闭环，无需拼装零散脚本。
- **适配微信 4.x 真实结构** — 支持多账号、多盘符、`message_*.db` 多分片与 `media_*.db` 资源库；按文件签名建立分片索引，按会话精准定位。
- **8 种导出格式** — JSON / HTML / TXT / CSV / Markdown / TOML / SQLite / XLSX，媒体与语音引用全格式支持，可打包 ZIP。
- **语音直出 WAV** — 微信语音（SILK）默认由纯 Python 的 [pilk](https://pypi.org/project/pilk/) 解码，配合标准库封装为 WAV，**无需 ffmpeg 或任何外部二进制**。
- **流式处理，不炸内存** — SQLCipher 4 逐页 4KB 流式解密；导出链路 K 路归并 + 增量写入，万级消息导出内存占用保持低位。
- **两级缓存，重跑秒回** — 密钥按 salt 索引缓存（DPAPI 加密），解密产物按 `mtime + size + key` 命中缓存；源库未变时直接复用。
- **AI 可直接接管** — 内置 MCP Server（stdio + JSON-RPC 2.0），6 个工具供任意 MCP 客户端检索与导出聊天记录。
- **自动增量刷新** — 可选开启，微信在线时按设定间隔自动重新解密变更的数据库。
- **完全本地运行** — Web 控制台仅监听 `127.0.0.1`，核心流程不上传任何数据到第三方服务器。

> ⚠️ 本项目仅限处理**你本人拥有合法访问权限**的数据，且禁止商用。使用前请阅读文末[免责声明](#免责声明)。

## 它是怎么工作的

```
发现数据目录 → 提取密钥 → 解密数据库 → 浏览聊天 → 恢复媒体 → 转码语音 → 导出 / MCP
   discover      策略链      SQLCipher     Web UI      media.py    voice.py    exporter
```

密钥提取采用 **propose-verify 分离**的策略链（密钥库 → MMKV → WCDB Config.Cipher → 内存扫描），策略只产出候选，由编排器统一做 HMAC 校验后入库；密钥落盘时经 Windows DPAPI 加密，不存明文。

## 适合哪些场景

- **个人聊天备份**：导出为长期可读的 HTML / Markdown / SQLite，留档不留遗憾。
- **项目资料整理**：把和客户、同事、群聊里的关键决策沉淀下来。
- **图片表情找回**：从微信资源库里恢复聊天中的图片、表情、头像。
- **AI 检索聊天**：通过 MCP 让 AI 帮你查"之前谁说过什么""这个需求什么时候定的"。
- **数据分析**：导出 JSON / CSV / XLSX / SQLite 后，用脚本、Excel 或 BI 工具继续分析。
- **本地研究与学习**：了解微信 4.x 的数据库结构、媒体加密方案与解密流程。

---

## 平台支持

| 平台      | 支持情况         | 说明                            |
| ------- | ------------ | ----------------------------- |
| Windows | 完整支持（推荐）     | 密钥提取、数据库解密、Web 控制台、导出、MCP 全功能 |
| macOS   | 支持微信 4.1.80+ | 通过 LLDB 断点捕获密钥，提取时需微信处于登录状态   |

### macOS 重要说明

macOS 版使用 LLDB 捕获微信内存中的密钥：

- 密钥已缓存时通常可直接复用，速度很快。
- 密钥未缓存时，点击"提取并解密"后请在 **60 秒内重新登录微信**，确保密钥出现在内存中。

详见 [MACOS\_SUPPORT.md](./MACOS_SUPPORT.md)。

---

## 快速开始

### 方式一：下载 Release（推荐）

1. 打开 [Releases](https://github.com/ImUpXuu/SIWX/releases)。
2. 下载对应平台产物：
   - Windows：`stories-in-wx-v*-windows-x64.exe`
   - macOS：`stories-in-wx-v*-macos.dmg`
3. 双击运行，程序会自动打开浏览器进入 Web 控制台。
4. 按引导页完成：欢迎检查 → 选择账号 → 提取密钥并解密。

### 方式二：源码运行

```bash
# 1. 克隆仓库
git clone https://github.com/ImUpXuu/SIWX.git
cd SIWX

# 2. 安装依赖（Python 3.10+）
pip install -r requirements.txt

# 3. 启动 Web 控制台
python run.py serve
# 浏览器自动打开 http://127.0.0.1:8787

# 4. 或使用命令行全自动模式
python run.py auto
```

### 命令行速查

```bash
python run.py auto                    # 全自动：发现 → 提密钥 → 解密
python run.py keys extract [--json]   # 仅提取密钥（--json 便于脚本消费）
python run.py keys list               # 查看密钥库（密钥打码显示）
python run.py decrypt [--db-dir X]    # 仅解密数据库
python run.py serve [--port 8787]     # 启动 Web 控制台
python run.py mcp                     # 启动 MCP Server（stdio）
```

---

## Web 控制台怎么用

### 引导流程

| 步骤       | 做什么                   |
| -------- | --------------------- |
| 1. 欢迎检查  | 检测微信状态、账号数量、密钥缓存与运行环境 |
| 2. 选择账号  | 从本机识别到的微信账号中选择要处理的账号  |
| 3. 提取并解密 | 自动提取密钥、解密数据库、生成可浏览数据  |

### 功能页面

| 页面   | 说明                              |
| ---- | ------------------------------- |
| 聊天查看 | 浏览会话列表，分页查看消息，预览图片、表情、头像、语音     |
| 导出   | 多选会话批量导出，勾选是否导出媒体 / 语音，可打包 ZIP  |
| MCP  | 生成 AI 客户端接入配置，管理各工具开关，查看调用日志    |
| 日志   | 查看任务进度、错误栈、运行日志，支持脱敏导出          |
| 设置   | 版本更新、日志模式、缓存状态、**自动刷新数据库**、清除缓存 |

---

## 导出能力

SIWX 的导出目标是：**既适合人阅读，也适合机器继续处理**。

| 格式       | 适合用途                         |
| -------- | ---------------------------- |
| JSON     | 保留结构化字段，适合程序处理、二次开发、AI 管道    |
| HTML     | 自包含聊天查看页，适合长期归档和直接阅读         |
| TXT      | 最朴素的纯文本备份，方便全文搜索             |
| CSV      | Excel、Numbers、WPS、数据库工具都容易打开 |
| Markdown | 写作、笔记、知识库沉淀友好，按日期组织          |
| TOML     | 配置风格结构化文本，适合可读性强的归档          |
| SQLite   | 适合 SQL 查询、统计分析、长期结构化保存       |
| XLSX     | 直接作为 Excel 工作簿查看和筛选          |

**8 种格式全部支持媒体与语音引用**：勾选后，图片以文件路径/`mediaFile` 字段落盘引用，语音会转成 WAV 一并输出（转码失败则保留原始 `.silk` 并在导出结果中说明）。

### 媒体与语音导出说明

- 图片、表情、头像等资源会尽量从微信本地资源库按需恢复。
- 语音从 `VoiceInfo.voice_data` 读取明文 SILK，默认用 **pilk** 转 WAV；无需 ffmpeg 等外部依赖。
- 若本机确实无法转码，则保留原始 SILK 文件，不会静默丢数据。

---

## 让 AI 读取你的聊天记录（MCP）

在 Web 控制台的 MCP 页面复制配置到支持 MCP 的客户端即可：

```json
{
  "mcpServers": {
    "stories-in-wx": {
      "command": "python",
      "args": ["G:/path/to/run.py", "mcp"]
    }
  }
}
```

当前提供 6 个 MCP 工具（可在 MCP 页面按需禁用）：

| 工具                | 作用           |
| ----------------- | ------------ |
| `get_status`      | 查看 SIWX 当前状态 |
| `list_accounts`   | 列出已解密账号      |
| `list_sessions`   | 列出某个账号下的会话   |
| `get_messages`    | 读取指定会话消息     |
| `search_messages` | 搜索聊天记录       |
| `export_chat`     | 导出指定聊天（可含语音） |

于是你可以问 AI：

- "帮我找一下和某某有关的聊天记录。"
- "总结一下这个群最近一个月讨论了什么。"
- "把某个会话导出成 Markdown。"

---

## 自动刷新数据库

在 **设置 → 自动刷新数据库** 开启后：

- 后台调度器每隔 30 秒轮询一次；
- 满足「已开启 + 到达设定间隔 + 微信在线 + 当前无运行中任务」时，自动执行一次增量解密；
- 间隔可设为 1 ~ 1440 分钟，默认 30 分钟；
- 结果（上次运行时间、成功与否、消息）会回写到设置页，随时可见。

适合"微信长期挂着、想保持本地备份一直最新"的使用方式。

---

## 性能表现

| 指标      | 参考结果                    |
| ------- | ----------------------- |
| 数据库解密速度 | 约 175 MB/s              |
| 消息导出速度  | 约 6000 - 8000 条/s       |
| 万级消息导出  | 11992 条约 2 秒            |
| 内存占用    | 流式处理，万级消息导出保持低占用，避免 OOM |

> 实际速度受硬盘、数据库大小、媒体数量、安全软件与系统权限影响。

---

## 项目结构

```text
siwx/
├─ sqlcipher.py            # SQLCipher HMAC 验证 + 流式页级解密
├─ keystore.py             # salt 索引密钥库，Windows 使用 DPAPI 加密缓存
├─ discover.py             # 跨平台目录、账号、进程发现
├─ strategies/             # 密钥提取策略链：keystore/mmkv/config_cipher/memscan/macos_lldb
├─ extract.py              # 编排入口：全局收割 → 策略链 → 解密输出
├─ media.py                # 图片、表情、头像等媒体解析（V0/V1/V2）
├─ voice.py                # 语音元数据解析、SILK 读取与 pilk→WAV 转码
├─ export_stream.py        # 流式消息解析与导出数据构造
├─ exporter.py             # JSON/HTML/TXT/CSV/MD/TOML/SQLite/XLSX 导出引擎
├─ api_*.py                # Web API：chat / export / settings / mcp / update
├─ server.py               # Flask 应用与任务调度
├─ mcp_server.py           # MCP Server，stdio + JSON-RPC 2.0
├─ auto_update.py          # 自动更新检测与应用
└─ ui/                     # Flask + 原生 HTML/CSS/JS Web 控制台
```

更深入的原理与模块说明见 [docs/](./docs)。

---

## 构建发布

```bash
# Windows
pip install pyinstaller
pyinstaller packaging/siwx-win.spec

# macOS
pyinstaller packaging/siwx-mac.spec

# 推送 tag 触发 GitHub Actions 自动构建 + Release
git tag -a v5.0.0 -m "更新说明……"
git push origin v5.0.0
```

版本号唯一来源是 `siwx/__init__.py` 的 `__version__`；CI 会校验 tag 与之一致，并从 tag 注释生成 `version.json` 与 Release 说明。

---

## 常见问题

**1. 为什么必须在本机运行？**

微信数据库、密钥和聊天记录都是高度敏感的个人数据。SIWX 设计为本地运行，核心流程不需要把数据上传到任何第三方服务器，Web 控制台只绑定 `127.0.0.1`。

**2. 解密失败怎么办？**

依次检查：微信是否正在运行 / 是否刚重新登录过 → 是否选中了正确的账号 → 数据目录是否完整 → 是否被安全软件拦截 →（macOS）是否允许 LLDB 调试且微信版本满足要求。然后打开 Web 控制台的"日志"页面看详细错误。

**3. HTML 导出能离线打开吗？**

可以。HTML 导出面向归档场景；若同时导出媒体，请保留导出目录结构或使用 ZIP 包，避免资源路径丢失。

**4. 语音能导出成 MP3 吗？**

当前默认输出 **WAV**（pilk 解 SILK → PCM → 标准库封装），WAV 是无损 PCM 容器，通用播放器和剪辑软件都支持。MP3 属于有损压缩，需要额外编码器，暂未内置。

**5. 会不会占用大量 CPU / 磁盘？**

解密是短时高吞吐但可控的任务；媒体按需解密；缓存可随时在设置页清除。全自动增量刷新默认关闭，开启后也可自行调间隔。

---

## 参与贡献

欢迎提交 Issue / PR，一起完善更多微信版本、更多导出格式和更稳定的媒体恢复能力。

动手前请先读 [CONTRIBUTING.md](./CONTRIBUTING.md)，里面有开发环境、测试要求、跨平台注意事项和几条红线。

反馈问题时请带上**环境信息**，能大幅加快定位：

- Web 控制台：**设置 → 环境信息 → 📋 复制环境信息**（路径中的用户名已打码）
- 命令行：`python run.py doctor`

提 issue 请使用仓库内置模板：🐛 Bug 反馈 / ✨ 功能建议 / ❓ 使用咨询。

---

## 贡献者

| 贡献者                                             | 说明                        |
| ----------------------------------------------- | ------------------------- |
| [ImUpXuu](https://github.com/ImUpXuu)           | 项目作者，核心架构与 Windows 端      |
| [lcrworld-jtl](https://github.com/lcrworld-jtl) | macOS 端密钥提取，LLDB + PBKDF2 |

---

## 许可证

本项目采用 **AGPL-3.0** 许可证，详见 [LICENSE](./LICENSE)。

**本项目不可商用。** 如需特殊授权，请联系作者。

---

## 免责声明 · 法律声明

> **请在使用本项目前，完整阅读并理解以下声明。下载、安装、运行或以任何方式使用本项目，即视为你已阅读、理解并同意本声明的全部内容。如不同意，请立即停止使用并删除本项目及其产物。**

### 一、项目定位与用途限定

本项目是一个**技术研究与个人数据管理工具**，其设计目的仅限于：

1. 对**使用者本人设备上、本人微信账号**的本地数据进行备份、迁移、归档与整理；
2. 对**使用者本人拥有合法访问权限**的数据进行格式转换与本地检索；
3. 信息安全、数据取证、软件工程等相关领域的**合法学习与研究**活动。

本项目**不是**、也不得被用作任何形式的入侵工具、监控工具、取证工具或数据窃取工具。

### 二、使用者义务

使用者在使用本项目时，必须自行确保：

1. **数据权属合法**：仅处理自己拥有合法访问权与处分权的数据，不得处理他人数据；
2. **取得必要授权**：如涉及他人信息（例如聊天记录中的对方），应确保已取得相应合法授权或具备其他合法处理依据；
3. **遵守法律法规**：严格遵守所在国家或地区的全部适用法律法规与平台服务协议；
4. **自行评估风险**：使用者应自行评估并承担使用本项目可能带来的全部风险。

### 三、明确禁止的用途

**严禁**将本项目用于以下任何用途：

- 未经授权访问、读取、解密、复制、传播**他人**的数据或设备数据；
- 侵犯他人**隐私权、个人信息权益、通信秘密**或商业秘密；
- 用于敲诈勒索、跟踪骚扰、非法监控、恶意取证、商业爬取或数据倒卖；
- 规避、破坏任何安全保护措施或计算机信息系统访问控制；
- 任何**商业用途**（本项目采用 AGPL-3.0，明确不可商用）；
- 其他任何违反法律法规或公序良俗的用途。

### 四、法律风险提示

使用者须特别注意：未经授权访问、获取、解密、复制或传播他人数据，以及侵犯他人隐私、个人信息权益或通信秘密的行为，在**世界多数国家和地区**都可能构成**刑事犯罪、行政违法或民事侵权**，行为人将承担相应的刑事、行政与民事法律责任，具体认定与处罚依使用者所在地法律为准。

> 上述提示**仅为一般性风险说明，不构成法律意见，亦非穷尽列举**。各国/地区法律规定存在差异，使用者应自行咨询专业法律意见，并对自身行为的合法性独立负责。

### 五、免责范围

在适用法律允许的最大范围内：

1. 本项目作者及贡献者**不对本项目作任何明示或默示的担保**，包括但不限于适销性、特定用途适用性及不侵权的担保；
2. 本项目**按"现状"（AS IS）提供**，不保证功能完整、无缺陷、稳定或适用于任何特定目的；
3. 因使用或无法使用本项目所导致的**任何直接、间接、附带、特殊、惩罚性或后果性损失**（包括但不限于数据丢失、设备损坏、业务中断、利润损失、法律纠纷、行政处罚或刑事追责），作者及贡献者**概不承担任何责任**；
4. 使用本项目产生的**全部风险与全部法律责任，由使用者自行独立承担**。

### 六、数据安全提示

- 解密产物、导出文件、密钥缓存均以**明文或不完全加密**形式存在于你本机，请自行做好访问控制与介质加密；
- 请勿在公共或不受信任的设备上运行本项目；
- 请勿将解密后的数据、导出文件或密钥上传至任何不受信任的第三方服务；
- 建议在操作前对重要数据进行完整备份。

### 七、开源许可与免责的关系

本免责声明是对 AGPL-3.0 许可证的补充说明，不改变、也不替代 AGPL-3.0 的条款。若本声明与 AGPL-3.0 存在冲突，以 AGPL-3.0 为准；但本声明的**用途限定与责任免除**部分，应被视为作者在许可之外另行作出的、更严格的使用条件说明。

---
## Star History

<a href="https://www.star-history.com/?repos=imupxuu%2Fsiwx&type=date&legend=top-left">
 <picture>
   <source media="(prefers-color-scheme: dark)" srcset="https://api.star-history.com/chart?repos=imupxuu/siwx&type=date&theme=dark&legend=top-left" />
   <source media="(prefers-color-scheme: light)" srcset="https://api.star-history.com/chart?repos=imupxuu/siwx&type=date&legend=top-left" />
   <img alt="Star History Chart" src="https://api.star-history.com/chart?repos=imupxuu/siwx&type=date&legend=top-left" />
 </picture>
</a>

<div align="center">

**开发者**: [UPXUU](https://upxuu.com) · **博客**: [upxuu.com](https://upxuu.com)

**License**: AGPL-3.0（不可商用） · **Copyright**: © 2026 UPXUU

<sub>本项目为独立开源项目，与任何其他软件、产品、服务或组织均无关联，亦未获得任何第三方的授权、认可或支持。</sub>

</div>
