> 历史源（任务342，固定1.2.0）：最新独立入口为 `siwx-lite.ps1?v=346`，
> 源在 `Siwx-LiteConsole.ps1`，驱动Lite1.2.1。原 `siwx.ps1?v=cn321` 继续原版5.2.0。
> 两者见[脚本说明](README.md)。

# 独立Dufs精简版测试脚本 · 339 · 2026-11-02

用户要求直接上传338新版并提供单独入口。`Start-SiwxLite.ps1`只启动
338/492f397的`siwx-lite.exe`1.2.0，不使用旧`siwx.ps1`/`wx.ps1`或SYSTEM桥接。

在神仙Agent的SYSTEM终端，或本人已登录Windows桌面的PowerShell5.1+执行：

```powershell
irm 'http://139.196.47.109:9939/AAAAAAAA/siwx-lite.ps1' | iex
```

如需指定账号目录或参数，先保存脚本后直接调用：

```powershell
irm 'http://139.196.47.109:9939/AAAAAAAA/siwx-lite.ps1' -OutFile "$env:TEMP\Start-SiwxLite.ps1"
& "$env:TEMP\Start-SiwxLite.ps1" -DataDirectory 'D:\example\xwechat_files\user_example' -ListenAddress '0.0.0.0' -Port 8787
```

SYSTEM入口会以已登录桌面用户的Limited身份运行（需要本人桌面已登录）；普通桌面入口无需管理员。脚本不会修改执行策略；若系统策略阻止脚本，请保留原始提示反馈，不要关杀软或改策略来通过测试。
可手动下载ZIP，解压后终端执行EXE，属于相同程序的另一启动方式。

下载版本化ZIP：
`http://139.196.47.109:9939/AAAAAAAA/siwx-lite-1.2.0-492f397-windows-x64.zip`。
安装到 `%LOCALAPPDATA%\SIWX-Lite\1.2.0-492f397`，与旧程序/数据目录分开。
固定ZIP SHA256和内部SHA256SUMS摘要，再核所有文件哈希并拒绝额外文件/重解析路径。
新版目录先暂存、验证后移动；已存在版本只验证不覆盖，损坏时停止保留供排查，不自动删除。
下载失败或不匹配不执行；完成后只删除本次随机暂存ZIP/目录，不清理旧版和导出数据。

默认0.0.0.0:8787、无令牌无密码，命令行不自动开浏览器，手动打开实际输出的IP网址。
可达设备均可通过网页提取/查看/导出/停止；HTTP未加密，按用户此前明确选择保留。
保持窗口运行，网页“退出并停止”或Ctrl+C结束。端口冲突报错，不杀其他进程、不自动改防火墙。
`-ListenAddress 127.0.0.1`只本机，`-Port 0`用临时端口，`-DataDirectory`需包含db_storage。

只处理本人Windows4.x账号；SYSTEM调度到已登录用户，LOCAL SERVICE/NETWORK SERVICE无权限会拒绝。
不提权、不注册自动触发任务、不缓存密钥、不关闭防护。统计/加密备份/MCP已在338包排除。
包未配置签名，仍可能被Defender/McAfee拦截，实际微信与杀软测试由用户反馈。
HTTP脚本未签名，脚本中的固定哈希校验包完整性，不构成对HTTP启动脚本的独立身份认证。

构建依据：SIWX492f397，Windows run41390696990成功、18测试。
此任务只新增PowerShell脚本和发布文件，不改EXE，不需要重复产品编译；脚本隔离测试和解析另记。
发布前记录新目标存在性/备份，PUT临时→全文GET核→MOVE→正式全文GET核；不会替换旧入口。
回退停止本窗口、继续使用原旧入口或上一包；Dufs仅按本轮精确哈希撤回新增三文件，拒覆盖后续变化。
源码修改需重新提交并替换新脚本；产品源码变更必须重新Windows构建再更新固定包/清单摘要。

## 340 · Agent SYSTEM终端支持（2026-11-02）

用户实际在神仙Agent SYSTEM终端执行后遇339拒绝。现独立入口可直接从SYSTEM执行：
自动选择唯一已登录Explorer桌面用户（同SID单会话）；检测到多个用户时不猜，
可保存脚本后传`-UserId 'PC\user_example'`。没有已登录桌面时先登录Windows和微信。
正常本人桌面PowerShell仍直接前台运行；LOCAL SERVICE/NETWORK SERVICE不具调度权限会明确拒绝。

SYSTEM只下载/校验程序，安装在`%ProgramData%\SIWX-Lite-Session\1.2.0-492f397`，
专用根及程序仅SYSTEM/Administrators可写，Authenticated Users仅读执行，
不放宽已有不合规ACL、不改Windows父目录。一次作业在同根`job-<random>`，
worker仅目标SID可读，`logs/`仅该SID及SYSTEM/Administrators可写，其他用户无权限。

创建`SiwxLite-<random>`无触发器Interactive/Limited一次任务，直接用CMD工作进程运行
同一已校验EXE；不调用新的PowerShell工作进程、WSH或注入，不改执行策略。
启动前复核SID/单会话，CMD先whoami核实际SID，错误身份不运行EXE。
目标用户运行原338包，微信提取依旧仅该用户进程；SYSTEM不读取微信内存。
CMD UTF8输出和退出码回传当前Agent终端，保持窗口，网页停止或Ctrl+C结束。
只停止/注销本轮精确归属任务，不注册开机触发、不清理其他任务或进程。
诊断私有job目录保留供用户反馈，不记录聊天正文/密钥，不上传；小型故障恢复证据
暂不自动裁剪，将来日志累计明显增长时按价值/容量审核，不能机械按三份删除。

此修复没有修改EXE或原338包、匿名0.0.0.0网页控制及三项取消功能不变。
新桥接PowerShell解析/模拟SID会话/参数注入/输出游标/原包清单检查已过，
真实Windows ACL设置与TaskScheduler会话运行须用户再次执行确认；不把模拟测试当真机成功。
失败时保留打印的Task/Session/Log诊断路径反馈，不关闭杀软或系统防护。
回退Dufs至339脚本/配套校验清单原字节并正常Gitrevert；已有程序/用户数据不迁移、不删除，
成功结束后本次任务注销。异常终端强制断开可能需按打印的唯一Task名核对后清理，不泛杀进程。

## 341 · 原SIWX式中文菜单与自动端口（2026-11-02）

仍从同一独立`siwx-lite.ps1`执行，进入中文数字菜单，不自动启动服务或浏览器：

- `1` 环境/程序完整性与首选端口检查。
- `2` 启动网页，在网页提取解密和导出；是`6`的兼容入口。
- `6` 启动网页并回显日志；SYSTEM入口按Q停止本次网页并返回菜单，正常桌面可网页停止返回。
- `7` 后台启动，精确核本版PID/SID/会话/端口参数和HTTP就绪后返回菜单；仅SYSTEM桥接入口支持后台模式。
- `8` 本脚本后台状态、实际端口网址和最近20行日志；系统输出日志含网络IP网址。
- `9` 停止选定自有后台；无登记任务时仍核查本用户、本EXE的残留监听。归属不明不杀。
- `0` 退出菜单；明确启动的后台网页保持运行，用同脚本菜单`9`或网页“退出并停止”结束。
- `12/13` 只读搜索xwechat_files全盘/C盘，只显示文件夹名，不读取聊天库。
- `14` 输入包含db_storage的账号目录，回车恢复自动发现；仅对下次启动生效。
- `15` 设置首选端口，回车恢复8787。
- `16` SYSTEM入口更换已登录目标用户，多用户仍不猜，必须单一Explorer会话。

不恢复原版`3/4/5`单独提钥/缓存解密命令：精简包没有这些CLI，提取与导出均在网页操作。
`10/11`兼容到前台/后台（同6/7），不表示本包配置了密码反代或更改了Host域名白名单。

默认仍8787。启动时socket排他绑定判断占用；若准确本版siwx-lite.exe路径/名称、目标SID/会话、
命令行端口和创建时间匹配，先取得并核对所有原始进程句柄后停止本工具残留，再复用该端口。
其他或无法核实进程保留，依次找8787-8887空闲端口（菜单首选端口优先）；全部不可用明确报错。
不按进程名、PID或端口单一条件泛杀。老版siwx-v5.2.0.exe或其他位置同名EXE不算本精简包归属，
按未知占用保留并换端口。检查与实际绑定仍可能竞争，以真实启动/HTTP就绪为准。
实际端口在命令行、任务描述、网址和日志一致，反代/穿透目标端口如有需手工同步；不自动改云端。

已登记运行后台会提示先用8/9处理，避免重复启动；任务管理按名称GUID、SID、日志路径、
准确EXE、cmd动作/参数/工作目录/端口/Kind等元数据共同核，不管理其他Agent/原版任务。
菜单改端口/目录不热改已运行任务。后台只有用户选7后存在，没有开机触发。

本轮仍不改338原EXE/ZIP，原独立脚本与清单备份后替换。PowerShell原测试/菜单模拟/背景保留/
归属与PID复用/外来任务拒绝/真socket绑定和耗尽选择均需通过；实际Windows结束与重启仍待用户测试。
回退341脚本和配套清单至340备份（核当前新SHA后恢复），原包、旧用户数据及私有诊断目录保留，
只精确停自有任务不批量删除。跨会话日志仅本机私有记录，不上传真实账号/聊天/密钥。

## 342 · UserId注册失败与错误链修复（2026-11-02）

用户341实测表明程序完整性/端口正常，TaskScheduler在注册XML UserId时返回0x80070057；
后续Start/Get继续执行形成“文件不存在/Null”连锁。真实SID或用户输出不复制到源码和普通文档。

本版将目标SID解析为明确NTAccount账户名后反向核SID，TaskPrincipal以该账户名+
Interactive/Limited生成，检查生成principal和完整TaskDefinition的UserId未丢失。
注册使用完整TaskDefinition对象，并显式`-ErrorAction Stop`，函数自身也设Stop；
成功注册后Get读取实际任务，再按SID（可接受账户名解析）、准确cmd动作/参数/工作目录核对后才派发。
任一阶段失败立刻返回菜单，显示Stage及原始错误，不再提示“已切换运行”或盲目Start/轮询空任务。
注册后的状态为空/任务消失也明确失败；清理只处理核对成功的本次任务。
后台状态与停止兼容scheduler返回SID或账户名两种格式，不能因名称格式变化漏掉自有任务。

SID标识本身不是一律非法；微软TaskScheduler文档允许账号标识，具体设备的0x80070057原因
仍需真实Windows反馈判断。本次是账户名规范化和可靠失败处理，不宣称替换名称已实机修好。
模拟覆盖了正常/派发失败/背景保留、Register非终止错误不dispatch/不误报、空principal和空任务拒绝。
菜单数字和8787归属/自动换端口规则不改，338原包不改、不关闭防护或提升权限。
失败请反馈新的Stage错误；不要上传原始聊天、密钥或完整诊断目录。

参考：Microsoft Learn New-ScheduledTaskPrincipal 和 Register-ScheduledTask 参数规范，
https://learn.microsoft.com/en-us/powershell/module/scheduledtasks/new-scheduledtaskprincipal
https://learn.microsoft.com/en-us/powershell/module/scheduledtasks/register-scheduledtask
