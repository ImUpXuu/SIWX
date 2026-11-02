# 原版 SIWX 与精简版使用独立脚本（2026-11-02，任务356）

用户澄清：原脚本继续启动原版程序，新修改的程序使用新脚本。任务344将原入口
切换到Lite属于误解，已恢复；以下两个入口分开维护。

| 版本 | 源脚本 | 程序 | 下载入口 |
|---|---|---|---|
| 原版 | `Siwx-Console.ps1` v1.7 | 原 SIWX 5.2.0 EXE | `siwx.ps1?v=cn321` |
| 精简版 | `Siwx-LiteConsole.ps1` v1.16 | 原生 1.6.0 目录包 | `siwx-lite.ps1?v=356` |

原来的命令保持原版：

```powershell
irm 'http://139.196.47.109:9939/AAAAAAAA/siwx.ps1?v=cn321' | iex
```

新修改的精简程序使用：

```powershell
irm 'http://139.196.47.109:9939/AAAAAAAA/siwx-lite.ps1?v=356' | iex
```

新版1.6.0直接使用原SIWX网页与后端，保留四步运行向导、使用导览、设置、运行日志和朋友圈浏览/媒体/导出：账号卡/聊天气泡/时间轴/筛选/多选复制/独立导出页面；
移除已取消功能及GitHub/无用介绍外链。程序仅127.0.0.1监听，应用无客户端鉴权，新脚本v1.16固定新包。

两者都沿用原WScript隐藏启动→CMD→本人已登录桌面用户→日志回传方式；
在原SYSTEM/管理员PowerShell运行，选择微信所在的本人桌面用户。
原脚本恢复任务321原字节，不改其5.2.0下载固定摘要/旧功能/监听与反代参数。
`?v=`只是缓存标记，不能将已被替换的文件变回旧版；原入口本次已实际恢复。

精简版菜单1版本，2/6网页前台，7后台，8状态/网址/日志，9停止，12/13只读目录搜索，
14账号目录(含`db_storage`)，15首选端口，16检测端口并释放确认属于本人的SIWX占用。仅监听`127.0.0.1:8787`；占用只停确认归属的当前版本或全清单验证通过的已知旧版本（1.5.0/1.5.1/1.5.2）
进程，否则在8787–8887换空闲端口。本机访问回环地址；同一电脑上的反代/穿透客户端连接127.0.0.1实际端口，不能用其他机器直接连电脑局域网IP。
精简程序没有IP/Host/Origin/Sec-Fetch-Site白名单或token/密码/Cookie鉴权；
不自动修改外部反代认证。JSON类型/大小/路径与本地进程归属校验保留。
新版安装`%SystemRoot%\SIWX\lite-1.6.0-9419f05`，固定原Windows云构建ZIP、
内部清单与全部文件SHA，先stage验证再移动；已有版本不覆盖。旧5.2.0 EXE、数据和旧包保留。
`Start-SiwxLite.ps1`为任务342历史源，独立新分发入口现对应`Siwx-LiteConsole.ps1`。

新脚本验证：`Verify-SiwxLiteConsole.ps1`、`Verify-LiteConsolePorts.ps1`、
`Verify-LiteConsolePackage.ps1`；原脚本验证保留`Verify-SiwxConsole.ps1`、`Verify-PortOwner.ps1`。
Windows5.1云检查为`Verify separate SIWX launchers`。真实Windows微信/任务与杀软仍须实机验收。
恢复四个分发目标须核当前本次新SHA后恢复before字节；无DB回灌、无旧包删除或备份裁剪。

以下为历史维护记录；与上文冲突时以任务344的当前入口为准。

---

# Windows 一键命令行入口

在 **SYSTEM 或管理员 PowerShell 5.1+** 中粘贴（与 wexport1 的使用方式一致）：

```powershell
irm 'https://raw.githubusercontent.com/abxian/SIWX/codex/windows-console-20261031/scripts/Siwx-Console.ps1' | iex
```

无需 Git、Python 或手动安装依赖。脚本首次从指定 Dufs 下载 **ImUpXuu/SIWX v5.2.0 官方 Release 的原件副本**（约 19 MB Windows x64 EXE），按固定公开 SHA-256 校验，已有文件也校验；不匹配时停止并保留原文件，不自动覆盖。运行的是发布版 v5.2.0，并非本分支源码重新打包。下载需指定 Dufs 可达；失败不改用其他来源。

1. 确认目标 Windows 用户（回车使用检测到的桌面用户）。该用户需要保持登录，且只有一个 Explorer 桌面会话；微信 4.x 应在该用户会话内运行并登录。
2. 自动完成下载校验、用户身份自检、环境检查后进入菜单。
3. 选 `2` 完成账号发现、密钥提取、数据库解密；选 `6` 或 `7` 启动网页后，在该电脑打开 脚本显示的实际网址（默认 **http://127.0.0.1:8787/**）浏览和导出。

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
| 12 | 在所有文件系统盘搜索 `xwechat_files` 文件夹 |
| 13 | 仅在 C盘搜索 `xwechat_files` 文件夹 |

命令执行中按 `Q` 停止并返回菜单，`Ctrl+C` 退出。后台启动成功后可以选 `0` 或关闭当前控制台；重新运行同一行命令后用 `8/9` 管理。没有开机/登录触发器，注销或重启后需重新启动。

## 运行方式与数据位置

- 实际 EXE 通过 Interactive/Limited 计划任务，以选定桌面用户身份运行，避免在 SYSTEM 的用户目录及 DPAPI 上下文中处理账号；不索取用户密码，不修改执行策略，不自动打开用户桌面的浏览器。
- 隐藏启动使用 Windows Task Scheduler + Windows Script Host/JScript + CMD，不依赖 Java、Python、VBScript。组织策略禁用 WSH 时明确报错，不尝试启用或绕过策略。
- v1.7起EXE安装在`%SystemRoot%\SIWX\siwx-v5.2.0.exe`（通常`C:\Windows\SIWX`）。运行产物根：目标用户的 `%LOCALAPPDATA%\stories-in-wx-console`；数据库在其 `output`，导出在 `exports`，日志在 `logs`。CMD 显式设置 `SIWX_ROOT`，不跟随当前 SYSTEM 工作目录。密钥库仍使用目标用户原有 `%LOCALAPPDATA%\stories-in-wx`。
- 新建的运行目录与 `%SystemRoot%\Temp\SiwxBridge-随机编号` 诊断目录仅授权 SYSTEM、管理员、目标用户。已存在运行目录若允许其他主体读取则拒绝，保留原 ACL；拒绝经 reparse point 的路径。
- 临时诊断目录保留本次 CMD/JScript、身份核验结果、日志、真实退出码，可能含账号路径，不上传或提交。后台任务管理只接受匹配用户 SID、名称、元数据、诊断路径与启动动作的任务，不触碰其他计划任务。
- 网页只绑定本机 `127.0.0.1`，默认端口8787，不含 wexport1 的访问 token。v1.5遇到当前用户本工具占用时先结束再重启，其他程序占用时保留并尝试8788、8789至8887；无可用端口时报错。默认不提供远程网址。后台服务存在时，菜单拒绝再启动其他读写操作，改用网页或先停止后台任务。
- HTTP 首页返回 200 且匹配 SIWX 标识、任务仍 Running 才报告后台就绪。它不证明密钥提取、账号选择或解密已成功；这些以日志和实际数据验收为准。启动超时停止本次任务。

## 验证与维护记忆（2026-10-31，任务 305）

源码入口 `scripts/Siwx-Console.ps1`，测试 `scripts/tests/Verify-SiwxConsole.ps1`、`Verify-HiddenLauncher.js`。用户会话桥接来自 wexport1 的 v3.4，但仅使用 SIWX 已有 CLI 子命令，不传 wx 的账号编号、深扫或 data 开关。菜单不会执行需要交互密码/退出微信的 `backup`。

验证命令（开发环境需要 PowerShell 7 与 Node.js，Windows 使用者无需安装 Node.js）：

```powershell
pwsh -NoProfile -File scripts/tests/Verify-SiwxConsole.ps1
```

已在 macOS 的 PowerShell 7.6.9 下通过语法、精确 CLI 参数、CMD 注入拒绝、SID 自检顺序、UTF-8 分段与末行输出、模拟 WSH 成功/失败/缺结果/异常、后台任务归属及 HTTP 就绪正反例。固定 EXE digest 来源为官方 Release API；已完整下载核对 19,873,843 字节、SHA-256 和 PE x64 架构；下载校验通过/复用/已有文件不匹配/下载内容错误/网络失败清理均通过合成工件测试。尚未在 Windows 5.1/真实微信上验证 Task Scheduler、WSH、ACL、解密和后台持续行为，不能据此宣称真机成功。

更新固定版本时，同时核对官方 Release 的文件名、实际全文 SHA-256、CLI 参数兼容性，并同步 `scripts/privacy_scan.py` 的公开校验和白名单（此值不是私密密钥）。本入口使用独立目录和 `Siwx*` 任务名，不更改 wexport1。回滚：选 `9` 停止准确任务；保留聊天产物和密钥目录；Git 可回退本次脚本提交。若不再使用，确认数据已有备份后自行删除本工具创建的准确目录，不批量删除 Temp 或其他软件数据。

## 中文交互更新（2026-10-31，任务 306）

一键入口已改为中文菜单、输入提示、状态说明及常见错误；菜单数字和实际命令参数保持不变。底层 EXE 的原始输出、CMD/WSH 的技术诊断及系统返回的任务状态保留原文，便于定位错误。SIWX 横幅为 v1.1，wx 横幅为 v3.5；wx 配套下载器和旧用户会话启动器也已中文化。

PowerShell 源文件保持 ASCII，中文以 Unicode 转义存储，由脚本内 `ConvertFrom-ConsoleText` 解码，兼容旧版 Windows PowerShell 5.1 的本地文件及 `irm | iex` 编码路径，无需调整系统语言或执行策略。修改文案时沿用此编码方式。测试新增真实菜单字串的中文解码和入口 ASCII 校验，并复验既有用户会话/命令参数/日志/后台管理用例。验证环境为 macOS PowerShell 7.6.9，Windows 5.1 真机仍待验证。

GitHub 一键入口使用原分支地址，重新运行会获取新版；已运行的控制台需退出后重新执行入口命令。自托管复制的脚本不会自动更新，需同步相应文件。回滚可恢复本次提交前的脚本，诊断及聊天数据目录无需修改。

## Dufs 一键分发（2026-10-31，任务 307）

用户指定的无密码 Dufs 为 `http://139.196.47.109:9939/AAAAAAAA/`，本轮使用以下入口：

```powershell
irm 'http://139.196.47.109:9939/AAAAAAAA/siwx.ps1?v=cn308' | iex
```

SIWX 的脚本与 EXE 为 `siwx.ps1` / `siwx.exe`；脚本直接从该 Dufs 下载固定官方 v5.2.0 EXE，仍核对官方 SHA-256，不依赖 GitHub 下载运行环境。wx 的脚本为 `wx.ps1`（`1wx.ps1` 同内容兼容入口），EXE 为 `wx.exe`（`wexport.exe` 同原件）；本轮复用既有 EXE，不宣称重新编译。另同步原文件名的脚本及 wx 配套下载器/用户启动器。HTTP 沿用用户指定服务，SIWX 固定摘要只校验 EXE 字节，不等于 HTTP 脚本具发布者认证。

发布工具与恢复证据在本地工作区 `ops/dufs307-20261031/`：`publish-dufs.py prepare` 完整备份现网目标；`publish` 先核未漂移，EXE先行、临时 PUT/完整GET校验/MOVE覆盖/再次全文GET；`rollback` 仅回退仍匹配本轮发布内容的目标，拒绝覆盖后续改动。`before.json` / `before/` 是原件，`manifest.json` / `verified.json` 为最终大小及 SHA-256；目录仅本机授权用户可访问，不入 Git。本地备份前约122GB空闲；旧工具备份约20MB且为明确回退点，全保留不裁剪。远端容量无法通过现有API读取，约20MB有界新增以暂存上传成功为门禁，不清理其他文件。

发布验收以最终 `verified.json` 与 Trilium 实际记录为准：源代码推送、Dufs文件发布、Windows真机提取/后台运行是独立状态。Windows真机仍待设备测试。后续替换 EXE 要同步固定摘要和版本；只改菜单无需重编译 EXE。恢复时优先使用同一工具精确恢复原字节，不批量覆盖目录。

## EXE 日志编码修复（2026-10-31，任务 308，脚本 v1.2）

菜单中文正常而 EXE 回显乱码时，是原生输出字节与读取方式不一致：冻结的 Windows Python 在重定向时可能输出系统 GBK，旧脚本固定按 UTF-8 解码，且按解码后重新编码的长度推进游标，替换字符会导致字节计数错误和后续跳读。此修复仅改脚本，不重编译 EXE、不读取或上传真实聊天数据。

`ConvertFrom-SiwxLogBytes` 按完整行严格尝试 UTF-8，不合法则用 GB18030（兼容 GBK）解码，支持同一日志内 CMD UTF-8 与 EXE GBK 混合；`Show-SiwxOutput` 只按实际读取的原始字节数推进，保留未完成的 UTF-8/GBK 字符与半行。后台选项 8 也通过相同解码函数显示最后 20 行（最多读取末尾 64KiB）。原始日志不重写，旧日志也可由新脚本读取。

自动识别无法区分所有同时合法的字节序列。必要时可在运行一键入口前设置 `$env:SIWX_CONSOLE_LOG_ENCODING='GB18030'`，也支持 `UTF8` 或 `Auto`；默认 `Auto`。此变量只控制当前脚本日志显示，不改变微信或 EXE 的运行编码。

新脚本更新到原 Dufs `siwx.ps1` 与 `Siwx-Console.ps1`，重新获取以 `?v=cn308` 避免旧缓存。原后台服务可保留，退出旧菜单后运行新版用 8 查看；若重新启动网页，先用 9 停止旧后台，避免端口冲突。前台/后台/截图中的乱码并不代表解密数据库损坏；业务成功仍以实际数据验收为准。

新增验证：合成中文复现原 GBK→UTF-8 乱码、混合编码、真实字节游标、重复读取无重放、拆分 GBK 字符、后台尾部及64KiB边界、末行、强制编码；保留全部旧测试。用户真实账号和粘贴日志未复制到源码、测试、提交或公开记录。Windows 真机修复效果等待用户重新运行确认。回退点为任务307发布的脚本原字节，EXE摘要保持不变。

## 已加密码的反代模式（2026-10-31，任务309，脚本v1.3）

菜单10：反代模式启动并显示日志；11：反代模式后台启动。仅在公网反代已配置独立认证时使用。普通6/7仍使用本机Host保护。两种模式均只绑定127.0.0.1，默认8787（v1.5可自动选择空闲端口）；反代模式显式追加现有`--trust-lan`兼容外部Host，保留SIWX Origin和Sec-Fetch-Site写请求校验，不改EXE。

wx.sxnn.de:5443链路：OpenResty认证→神仙面板18181按完整Host命中NAT7→server140本机8787。不能在OpenResty改Host为localhost，否则面板无法选中原隧道。所有IP可到登录入口，无IP白名单；只允许认证后进入应用，公网请求的Authorization在反代认证后清除，不转交Windows。凭据仅保存在Trilium私密凭据下wx.sxnn.de专用条目，不写脚本、Git或普通项目笔记。

运行：`irm 'http://139.196.47.109:9939/AAAAAAAA/siwx.ps1?v=cn309' | iex`，先停止旧8787网页，再选11。现有后台服务可用8查看、9停止。反代配置更新只需nginx -t/reload；Windows启动参数变更只重启本工具任务，不能重启微信或其他用户程序。Dufs分发仅两个脚本，EXE固定官方5.2.0不变。

NAS可编辑配置：/vol1/@appdata/1Panel/1panel/www/conf.d/wx.sxnn.de.conf 与 sites/wx.sxnn.de/proxy/root.conf；认证哈希在sites/wx.sxnn.de/.auth/htpasswd。恢复点为vol2/docker-projects-backups/wx-proxy309-日期，含旧两配置/.keep；ops/proxy309-20261031保存脱敏工具及验证。先撤销Windows反代模式，再恢复旧NAS配置并-t/reload，避免产生未认证的Host放行窗口。验证匿名/错误口令401、认证后首页和静态文件200、同源不存在POST404及跨源POST403；不查询聊天数据。完整真机聊天浏览/解密导出仍由用户使用验收。

309线上补充：隧道向Flask转发普通HTTP时，沿用面板的`Connection: upgrade`曾导致读取响应头前断开、502；精确原始HTTP对照中`Connection: close`返回200。现wx根反代清空Upgrade并显式Connection close，保持其他站点不变。NAS本地SNI严格TLS经完整Windows隧道验收：匿名首页/API401、错密码401、正确密码首页200/6139字节、同源不存在POST404、跨源POST403。Windows原生本机Host请求200，监听仍127.0.0.1。没有查询聊天API或读取实际内容。

## 残留端口清理（2026-10-31，任务313，脚本v1.4）

用户明确要求旧软件删除/旧任务消失后结束本工具残留8787并重新启动。菜单6/7/10/11遇到占用时不再直接返回：按监听PID、准确EXE路径、进程名、serve固定参数、选定Windows用户SID/会话与创建时间确认身份，先持有全部原生进程句柄再结束PyInstaller同身份父进程及监听子进程，等端口释放再继续启动。选9没有可管理后台任务时也尝试这一有界清理；有后台任务时停止后若残留8787同样处理。原日志、密钥缓存、数据库和导出均不删除。

仅监听端口不足以判定身份。其他程序/其他用户/其他路径/无法读取命令或用户SID的进程一律保留，显示PID并拒绝启动新服务；父进程不符合身份也拒绝。复核创建时间及原生句柄防PID复用，不使用按端口直接taskkill或通配符进程清理。软件已删但CIM仍返回原执行路径和命令时可以确认；系统无法提供这些字段时仍须人工确认，脚本不会猜。

验证：`pwsh -NoProfile -File scripts/tests/Verify-PortOwner.ps1`，合成CIM/句柄测试覆盖准确身份六条件、复用PID、父进程先停、混合监听拒绝、其他用户父进程拒绝、取得第二句柄失败时不杀任何进程、孤儿单进程与空端口。另复验完整`Verify-SiwxConsole.ps1`。Windows目标机真实停止/重启仍待用户重跑，不把隔离测试说成真机成功。当前EXE加载9020等原生加载错误是独立问题，本修复不关闭Defender/AppLocker或系统策略。

入口使用Dufs `siwx.ps1?v=cn313` 获取v1.4。先退出旧菜单，再重新运行入口；启动反代选11（后台），10为当前窗口持续日志。源只改脚本/测试/文档，无EXE重编译；Dufs仅两个脚本覆盖，官方5.2.0 EXE不变。恢复点在`ops/port313-20261031/before`，按记录SHA精确回退两个脚本；恢复源码提交可撤销本次变更。进程停止本身不可撤回，但可用原参数重新启动本工具，数据不变。

## 自动选择空闲端口（2026-11-01，任务315，脚本v1.5，当前行为）

菜单6/7/10/11先尝试8787；精确确认是当前用户的本工具SIWX时，结束旧父/子进程后原端口重启；其他程序、其他用户或无法确认归属的占用不结束，依次尝试8788至8887。若已有本脚本后台运行，优先其实际端口，重复启动会替换已确认的旧服务。端口检测通过独占绑定回环地址，能识别尚未接受连接的占用；端口探测和EXE绑定间仍有竞争，启动失败不会报告就绪。

实际端口记录在后台任务Description的Port字段；旧任务没有此字段时默认8787，超出范围或异常值拒绝管理。菜单8、HTTP就绪检查、菜单9停止后的残留清理均使用实际端口。无后台任务的菜单9只检查上述范围监听，并精确校验身份后清理。所有端口仍绑定127.0.0.1，所有启动参数保持白名单与拒绝注入。

反代/内网穿透需要将服务端口填写为脚本显示的实际端口；脚本会提示，不自动修改NAS站点或面板NAT。入口：`irm 'http://139.196.47.109:9939/AAAAAAAA/siwx.ps1?v=cn315' | iex`，先0退出旧菜单再加载。反代后台选11、状态8、停止9。不自动打开浏览器。

验证：Verify-PortOwner.ps1覆盖真实回环socket绑定/释放、动态端口身份、父子清理、默认空闲/本工具占用/其他程序连续占用/优先旧端口/全部占用；Verify-SiwxConsole.ps1覆盖端口白名单、旧新任务元数据、非法端口拒绝及动态HTTP网址，完整既有编码和启动器回归。实际Windows5.1及用户电脑停止/重新启动仍需重新运行确认。

发布仅两个Dufs脚本别名，不改EXE。备份ops/port315-20261101/before与before.json可精确恢复旧字节，Git撤销本次提交可恢复源码。小型历史恢复点全部保留，不裁剪；本轮不结束真实Windows进程、不改业务数据/认证/NAS映射。

## 查找微信数据目录（2026-11-01，任务320，脚本v1.6）

菜单12使用`Get-PSDrive -PSProvider FileSystem`枚举文件系统盘，再用`Get-ChildItem -LiteralPath <根目录> -Directory -Filter xwechat_files -Recurse -Force -ErrorAction SilentlyContinue`搜索；菜单13只搜索C盘。逐条显示完整路径和最终数量；无权限/不可达目录跳过，找到0项不表示电脑一定不存在目录。扫描可能耗时，映射网络盘也可能参与；Ctrl+C沿用退出脚本行为。

这是发起管理员/SYSTEM PowerShell看到的盘符及权限，不保证包含目标桌面用户独有映射盘。只搜索目录名称，不读取聊天文件内容、不自动解密、不改目录/数据库，也不把路径写入本工具日志或上传。后台网页运行时仍可使用12/13。用户可从输出选择路径，本轮不自动修改SIWX配置。

更新：先0退出旧菜单，再执行`irm 'http://139.196.47.109:9939/AAAAAAAA/siwx.ps1?v=cn320' | iex`；看到v1.6，选12全盘或13仅C盘。仅发布两个脚本别名，EXE不变；恢复点`ops/search320-20261101/before`与before.json可按旧字节恢复，源码撤销本次Git提交。语法/完整隔离回归及合成文件夹实际递归检查通过，Windows真实全盘搜索待用户重跑确认。

## EXE系统目录安装（2026-11-01，任务321，脚本v1.7）

用户授权EXE放Windows/SIWX：按`$env:SystemRoot`定位Windows目录，程序固定`%SystemRoot%\SIWX\siwx-v5.2.0.exe`；标准C盘Windows即`C:\Windows\SIWX\siwx-v5.2.0.exe`。新建专用目录禁继承，Administrators/SYSTEM完全控制、Authenticated Users仅读执行；不改Windows父目录ACL。现有安装目录/EXE要求所有者是管理员或SYSTEM且其他主体无写入/删除/改权限/所有权，否则停止并保留，不自动放宽原ACL。路径拒绝重解析点。每次启动前重查目录/EXE ACL和固定官方SHA。

数据根和密钥缓存不移动：目标用户LocalAppData/stories-in-wx-console及stories-in-wx。CMD工作目录改数据根，显式SIWX_ROOT不变，Limited桌面用户只读执行系统目录EXE。旧用户目录EXE保留不删除；残留清理在原SID/会话/创建时间/严格参数校验之外，仅额外接受准确旧EXE路径，命令行须匹配实际那一路径，其他位置同名程序不接受。

生效：0退出旧菜单，再`irm 'http://139.196.47.109:9939/AAAAAAAA/siwx.ps1?v=cn321' | iex`，管理员/SYSTEM运行，显示v1.7和程序实际路径，首次下载校验官方EXE。不关闭安全策略、不保证解决原9020。完整PowerShell隔离回归和旧新路径识别通过；Windows ACL实际设置/普通用户启动需用户重新运行验证，本机macOS不能替代Windows ACL验收。Dufs仅两个脚本替换，EXE分发原件不变。回退ops/install321-20261101/before旧两脚本及Git撤销，旧EXE/数据保留，不自动删除系统目录。小备份全部保留无裁剪。


任务356：导出页“已导出文件”从本机持久exports读取，刷新/重启/升级保留，支持搜索/分页/下载；删除原文件后不再显示。菜单16探测首选端口TCP，再经固定安装位置、全文件SHA、ACL、用户SID/会话/精确命令/创建时间/原句柄核验才能停止进程。自动启动沿用同样判断。未知占用不结束，继续寻找8787–8887备用端口。不是通过同进程名或网页标题判定；不修改反代或防火墙。
