# 贡献指南

感谢你愿意为 stories-in-wx（SIWX）出力 🙌

这是一个**本地运行的个人微信数据自备份工具**：提取微信 4.x 数据库密钥 → 解密 → 浏览聊天 → 恢复媒体 → 多格式导出 → 通过 MCP 交给 AI 检索。所有核心流程都在本机完成，**不上传任何数据到第三方服务器**。

请先花两分钟读完本文，能让你的 PR 更快被合并。

---

## 一、红线（务必遵守）

1. **绝不提交个人隐私数据。** 包括但不限于：密钥、`wxid` 明文、真实聊天记录、未脱敏日志、真实数据库文件、导出产物。
   - 日志请用「设置 → 📥 导出日志」的**脱敏导出**。
   - 环境信息请用「设置 → 📋 复制环境信息」或 `python run.py doctor`（路径中的用户名已打码）。
2. **不引入绕过授权的能力。** 本项目面向「处理自己有权访问的数据」，不接受任何用于未经授权读取他人数据的改动。
3. **遵守许可证。** 项目使用 AGPL-3.0 且**不可商用**，贡献即视为同意以相同许可证发布。

---

## 二、环境准备

| 项目 | 要求 |
|---|---|
| Python | 3.10+（CI 使用 3.12；本地建议 3.11 / 3.12） |
| 操作系统 | Windows（完整支持）或 macOS（4.1.80+） |
| 依赖 | `pip install -r requirements.txt` |

```bash
git clone https://github.com/ImUpXuu/SIWX.git
cd SIWX
pip install -r requirements.txt

# 启动 Web 控制台（开发时最常用）
python run.py serve
# 浏览器打开 http://127.0.0.1:8787
```

依赖只有 7 个，均为纯 Python 或有成熟预编译包：`pycryptodome`、`flask`、`psutil`、`openpyxl`、`rich`、`zstandard`、`pilk`。
**不要引入需要编译工具链或外部二进制的依赖**（例如 ffmpeg）——这会破坏打包产物的开箱可用性。

---

## 三、项目结构速览

```
siwx/
├─ sqlcipher.py            # SQLCipher 4 原语 + 流式页级解密
├─ keystore.py             # 密钥库（DPAPI 加密缓存）
├─ discover.py             # 跨平台微信目录 / 账号发现
├─ strategies/             # 密钥提取策略链
├─ extract.py              # 编排：全局收割 → 策略链 → 解密
├─ media.py                # 图片 / 表情 / 头像解密
├─ voice.py                # 语音（SILK）元数据与数据读取
├─ export_stream.py        # 流式消息解析
├─ exporter.py             # 多格式导出引擎
├─ api_*.py                # Flask 蓝图（chat / settings / update / plugins ...）
├─ mcp_server.py           # MCP Server（stdio + JSON-RPC 2.0）
├─ env_info.py             # 环境信息采集（bug 报告用）
├─ paths.py                # 跨平台路径（data_dir / app_root / out_root）
├─ plugins/                # 插件框架
└─ ui/                     # 原生 HTML / CSS / JS 前端
```

改代码前建议先读 `docs/architecture.md`；涉及媒体解密请以 `docs/media-decryption-principles.md` 为准。

---

## 四、开发工作流

### 分支

- 从 `main` 切出，分支名**不要带 `/`**（历史上曾因此导致 HEAD 异常）。
- 推荐命名：`fix-macos-lldb`、`feat-csv-export`、`docs-plugin-guide`。

### 提交信息

使用 Conventional Commits 风格，中文描述也可以：

```
fix(macos): 修复 LLDB 密钥提取始终 0/N
feat(export): 支持按日期范围导出
docs(readme): 补充常见问题
```

- 一个提交只做一件事，避免把格式化改动和新功能混在一起。
- 提交正文里写清**为什么改**，以及你**怎么验证的**。

### 测试

```bash
# 完整回归（提交前必须全绿）
python -m unittest discover -s tests

# 单个测试文件 / 单个用例
python -m unittest tests.test_regressions
python -m unittest tests.test_regressions.TestVersionSource
```

前端改动请顺带做语法检查：

```bash
node --check siwx/ui/pages/<改动的文件>.js
```

写测试时注意：

- 测试要**轻量、自包含**，不依赖本机真实微信数据。
- 涉及路径的测试请用临时目录，别写进用户真实数据目录。
- 涉及插件的测试要保证 `SIWX_NO_PLUGINS=1` 下宿主行为不变。

---

## 五、平台注意事项

### Windows

- 完整支持。密钥库使用 DPAPI 加密，**与当前用户绑定**。
- 注意杀毒软件可能拦截进程内存读取，排查时请说明。

### macOS

- 需要微信 4.1.80+，密钥通过 LLDB 断点捕获。
- 提取时微信需处于登录状态；密钥未缓存时建议在 60 秒内重新登录微信。
- 详见 [MACOS_SUPPORT.md](./MACOS_SUPPORT.md)。

### 跨平台通用要求

- **持久化数据一律走 `paths.data_dir()`**，不要用工作目录（cwd）或程序安装目录。
  历史教训：macOS 双击 `.app` 时 cwd 是受 SIP 保护的只读 `/`，在 cwd 下建目录会直接导致应用秒退。
- 不要在启动路径上做可能抛异常的重操作；初始化失败要能**降级**而不是阻塞启动。

---

## 六、插件开发

插件是本项目主要的扩展方式，通常**不需要改宿主代码**。支持的扩展点包括页面、设置项、消息渲染、导出格式、MCP 工具、CLI 命令、主题、密钥策略等。

- 写插件看 [`docs/plugin-development.md`](./docs/plugin-development.md)
- 改插件框架看 [`docs/module-plugins.md`](./docs/module-plugins.md)
- 示例见 [`examples/plugins/demo_stats/`](./examples/plugins/demo_stats/)

**红线**：`SIWX_NO_PLUGINS=1`（零插件模式）下，宿主行为必须与不引入插件系统时完全一致。

---

## 七、提交 PR

- PR 描述请按模板填写，重点写清**验证方式**（实际命令 + 结果），不要只写「已测试」。
- 一个 PR 聚焦一个主题。大改动建议先开 issue 讨论设计。
- 界面改动请附前后对比截图；行为改动请附相关日志。
- CI 会在推送 tag 时构建发布产物，日常 PR 不需要关心发布流程。

---

## 八、版本发布（维护者）

版本号唯一来源是 `siwx/__init__.py` 的 `__version__`，发布时需同步 README 徽章与 `docs/README.md`。

发布说明写在 **annotated tag** 上，CI 会用它作为 Release 正文并生成 `version.json`。注意必须使用 `--cleanup=whitespace`：

```bash
git tag -a v5.0.3 --cleanup=whitespace -F release-notes.md
git push origin v5.0.3
```

> 默认的 `--cleanup=strip` 会把**整行以 `#` 开头的 Markdown 标题当注释删掉**，导致 Release 正文丢失全部标题结构。

---

## 九、遇到问题？

- 使用问题 → 提 issue 选「❓ 使用咨询」
- 疑似 Bug → 提 issue 选「🐛 Bug 反馈」，并附上环境信息与脱敏日志
- 技术原理 → 参考 [upxuu.com](https://upxuu.com) 的相关文章

再次感谢你的贡献 ❤️
