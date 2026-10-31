# Windows 一键命令行入口

在 **SYSTEM 或管理员 PowerShell 5.1+** 中粘贴（与 wexport1 的使用方式一致）：

```powershell
irm 'https://raw.githubusercontent.com/abxian/SIWX/codex/windows-console-20261031/scripts/Siwx-Console.ps1' | iex
```

无需 Git、Python 或手动安装依赖。脚本首次从 **ImUpXuu/SIWX 的 v5.2.0 官方 Release** 下载约 19 MB 的 Windows x64 EXE，按固定公开 SHA-256 校验，已有文件也校验；不匹配时停止并保留原文件，不自动覆盖。运行的是发布版 v5.2.0，并非本分支源码重新打包。下载需要 GitHub 可达；失败不改用其他来源、不降低 TLS 校验。

1. 确认目标 Windows 用户（回车使用检测到的桌面用户）。该用户需要保持登录，且只有一个 Explorer 桌面会话；微信 4.x 应在该用户会话内运行并登录。
2. 自动完成下载校验、用户身份自检、环境检查后进入菜单。
3. 选 `2` 完成账号发现、密钥提取、数据库解密；选 `6` 或 `7` 启动网页后，在该电脑打开 **http://127.0.0.1:8787/** 浏览和导出。

| 选项 | 操作 |
| --- | --- |
| 1 | 环境检查 `doctor` |
| 2 | 全自动提取并解密 `auto` |
| 3 | 仅提取密钥 `keys extract` |
| 4 | 查看打码密钥 `keys list` |
| 5 | 使用缓存密钥解密 `decrypt` |
| 6 | 启动网页并将日志显示在当前窗口 |
| 7 | 后台启动网页，HTTP 就绪后返回菜单 |
| 8 | 查看本脚本的后台任务、网址与最近日志 |
| 9 | 选择准确编号，停止并注销对应后台任务 |
| 0 | 退出菜单，保留已成功脱离的后台任务 |

命令执行中按 `Q` 停止并返回菜单，`Ctrl+C` 退出。后台启动成功后可以选 `0` 或关闭当前控制台；重新运行同一行命令后用 `8/9` 管理。没有开机/登录触发器，注销或重启后需重新启动。

## 运行方式与数据位置

- 实际 EXE 通过 Interactive/Limited 计划任务，以选定桌面用户身份运行，避免在 SYSTEM 的用户目录及 DPAPI 上下文中处理账号；不索取用户密码，不修改执行策略，不自动打开用户桌面的浏览器。
- 隐藏启动使用 Windows Task Scheduler + Windows Script Host/JScript + CMD，不依赖 Java、Python、VBScript。组织策略禁用 WSH 时明确报错，不尝试启用或绕过策略。
- EXE 与运行产物根：目标用户的 `%LOCALAPPDATA%\stories-in-wx-console`；数据库在其 `output`，导出在 `exports`，日志在 `logs`。CMD 显式设置 `SIWX_ROOT`，不跟随当前 SYSTEM 工作目录。密钥库仍使用目标用户原有 `%LOCALAPPDATA%\stories-in-wx`。
- 新建的运行目录与 `%SystemRoot%\Temp\SiwxBridge-随机编号` 诊断目录仅授权 SYSTEM、管理员、目标用户。已存在运行目录若允许其他主体读取则拒绝，保留原 ACL；拒绝经 reparse point 的路径。
- 临时诊断目录保留本次 CMD/JScript、身份核验结果、日志、真实退出码，可能含账号路径，不上传或提交。后台任务管理只接受匹配用户 SID、名称、元数据、诊断路径与启动动作的任务，不触碰其他计划任务。
- 网页只绑定 `127.0.0.1:8787`，不含 wexport1 的访问 token。默认不提供远程网址；已有程序占用端口时拒绝启动新网页。后台服务存在时，菜单拒绝再启动其他读写操作，改用网页或先停止后台任务。
- HTTP 首页返回 200 且匹配 SIWX 标识、任务仍 Running 才报告后台就绪。它不证明密钥提取、账号选择或解密已成功；这些以日志和实际数据验收为准。启动超时停止本次任务。

## 验证与维护记忆（2026-10-31，任务 305）

源码入口 `scripts/Siwx-Console.ps1`，测试 `scripts/tests/Verify-SiwxConsole.ps1`、`Verify-HiddenLauncher.js`。用户会话桥接来自 wexport1 的 v3.4，但仅使用 SIWX 已有 CLI 子命令，不传 wx 的账号编号、深扫或 data 开关。菜单不会执行需要交互密码/退出微信的 `backup`。

验证命令（开发环境需要 PowerShell 7 与 Node.js，Windows 使用者无需安装 Node.js）：

```powershell
pwsh -NoProfile -File scripts/tests/Verify-SiwxConsole.ps1
```

已在 macOS 的 PowerShell 7.6.9 下通过语法、精确 CLI 参数、CMD 注入拒绝、SID 自检顺序、UTF-8 分段与末行输出、模拟 WSH 成功/失败/缺结果/异常、后台任务归属及 HTTP 就绪正反例。固定 EXE digest 来源为官方 Release API；已完整下载核对 19,873,843 字节、SHA-256 和 PE x64 架构；下载校验通过/复用/已有文件不匹配/下载内容错误/网络失败清理均通过合成工件测试。尚未在 Windows 5.1/真实微信上验证 Task Scheduler、WSH、ACL、解密和后台持续行为，不能据此宣称真机成功。

更新固定版本时，同时核对官方 Release 的文件名、实际全文 SHA-256、CLI 参数兼容性，并同步 `scripts/privacy_scan.py` 的公开校验和白名单（此值不是私密密钥）。本入口使用独立目录和 `Siwx*` 任务名，不更改 wexport1。回滚：选 `9` 停止准确任务；保留聊天产物和密钥目录；Git 可回退本次脚本提交。若不再使用，确认数据已有备份后自行删除本工具创建的准确目录，不批量删除 Temp 或其他软件数据。
