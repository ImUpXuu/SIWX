# 独立Dufs精简版测试脚本 · 339 · 2026-11-02

用户要求直接上传338新版并提供单独入口。`Start-SiwxLite.ps1`只启动
338/492f397的`siwx-lite.exe`1.2.0，不使用旧`siwx.ps1`/`wx.ps1`或SYSTEM桥接。

在**本人已登录Windows桌面**的PowerShell5.1+执行，无需管理员：

```powershell
irm 'http://139.196.47.109:9939/AAAAAAAA/siwx-lite.ps1' | iex
```

如需指定账号目录或参数，先保存脚本后直接调用：

```powershell
irm 'http://139.196.47.109:9939/AAAAAAAA/siwx-lite.ps1' -OutFile "$env:TEMP\Start-SiwxLite.ps1"
& "$env:TEMP\Start-SiwxLite.ps1" -DataDirectory 'D:\example\xwechat_files\user_example' -ListenAddress '0.0.0.0' -Port 8787
```

脚本不会修改执行策略；若系统策略阻止脚本，请保留原始提示反馈，不要关杀软或改策略来通过测试。
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

只处理本人Windows4.x账号，用户服务账户SYSTEM/LOCAL SERVICE/NETWORK SERVICE直接拒绝，
不提权、不注册后台任务、不缓存密钥、不关闭防护。统计/加密备份/MCP已在338包排除。
包未配置签名，仍可能被Defender/McAfee拦截，实际微信与杀软测试由用户反馈。
HTTP脚本未签名，脚本中的固定哈希校验包完整性，不构成对HTTP启动脚本的独立身份认证。

构建依据：SIWX492f397，Windows run41390696990成功、18测试。
此任务只新增PowerShell脚本和发布文件，不改EXE，不需要重复产品编译；脚本隔离测试和解析另记。
发布前记录新目标存在性/备份，PUT临时→全文GET核→MOVE→正式全文GET核；不会替换旧入口。
回退停止本窗口、继续使用原旧入口或上一包；Dufs仅按本轮精确哈希撤回新增三文件，拒覆盖后续变化。
源码修改需重新提交并替换新脚本；产品源码变更必须重新Windows构建再更新固定包/清单摘要。
