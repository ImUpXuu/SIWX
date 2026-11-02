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
