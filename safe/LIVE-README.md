# SIWX × wexport 精简版：本人微信提取、网页、导出

本版按用户2026-11-02澄清制作：保留本人已登录Windows微信4.x自动提取、
本机网页查看与搜索、HTML/Markdown/TXT/CSV/JSON文本导出。
**聊天统计、加密备份、MCP已取消，相关模块不进入构建产物。**

## 使用

1. 保持本人Windows微信4.x登录，在微信中打开一段聊天。
2. 完整解压 `siwx-lite-windows-x64.zip`，双击 `siwx-lite.exe`，保留 `_internal` 目录。
3. 程序打开本机网页；选择账号，勾选本人数据确认，点击“提取并打开”。
4. 选择会话，按关键词或日期搜索；选择格式后“下载导出文件”。
5. 点击“退出并停止”，或回到程序窗口按Ctrl+C；单独关网页不会停止程序。

找不到移动到其他盘的账号时，在本人桌面终端运行：

```powershell
.\siwx-lite.exe --data 'D:\example\xwechat_files\user_example'
```

指定的账号目录须包含 `db_storage`。程序只查当前用户常用目录/当前用户注册表位置，
不全盘搜、不处理其他Windows用户运行的微信。不要用旧SYSTEM一键脚本启动此版。
默认端口8787，冲突时自动换空闲端口，不结束占用者；以窗口实际完整网址为准。
网址中的随机令牌只用于本次运行，不要转发。`--no-open`可改为手动打开网址。

## 保留和取消的边界

- 自动提取沿用wexport原有Windows只读实现，未修改提钥算法或扩大进程访问权限。
  只有在网页明确选择账号并确认后执行；密钥不显示、不提供密钥API、不缓存落盘。
- 解密数据库只放内存，关闭数据或退出释放连接；源库只读，不迁移或改写微信数据。
  程序不会生成密钥文件或明文数据库备份；系统分页/崩溃转储不在应用控制范围。
- 仅支持Windows微信4.x。微信3.x、macOS、Linux的自动提取不在此包内。
- 仅聊天文本与类型占位、引用和合并转发文本摘要；不提取图片密钥、不解码图片/语音/视频，
  不下载朋友圈或媒体。未成功读取的分库会提示部分可用，不保证全量记录完整。
- 网页只绑定127.0.0.1，每次启动随机令牌鉴权，校验Host/Origin，不支持局域网或公网。
- 无聊天统计、加密备份、MCP、插件、自动更新、LLDB、Hook、注入、盐锚点深扫、
  SYSTEM桥接、计划任务或开机常驻；导出不是加密备份，下载文件是明文，请妥善保存。
- 每个会话筛选最多十万条匹配消息，超限会拒绝导出，需缩小日期范围，不静默截断。

## Defender与McAfee

本包保留了读取本人微信进程的能力，这种行为仍可能被安全软件检测。
减少功能和保留只读权限不能保证零告警。本版不关闭防护、添加排除项、混淆绕过或隐藏行为。
未配置代码签名；杀软检测和真实微信设备验收必须针对准确构建/哈希独立记录。

## 开发与构建

源码入口 `safe/live_app.py`，浏览器页面在 `siwx_safe/live_ui/`；数据适配
`siwx_safe/live.py`，鉴权HTTP `siwx_safe/local_web.py`，被动导出复用`documents.py`。
厂商提取/读取模块是 `siwx_safe/vendor/wexport/` 的显式白名单，MIT署名保留。
Windows Python3.12：

```powershell
python -m pip install -r requirements-live.txt
python live_app.py
python -m unittest discover -s tests -v
```

完整构建走GitHub Actions **Build WeChat lite Windows**。推送
`codex/wechat-lite-*` 分支自动触发，产物为`siwx-lite-windows-x64`临时artifact；
`live.spec`只收集选定模块、网页、许可证，生成模块清单/SHA256SUMS，使用目录分发、
无UPX、无管理员清单。构建不会自动发布Release或替换Dufs。
模块清单允许现有只读内存模块，禁止旧SIWX/未选择wexport模块及统计/备份/MCP等。
源SQLCipher实现沿用上游页头校验/WAL读取，不作完整逐页HMAC校验，不能把解密成功等同完整性认证。

上一轮`siwx-safe.exe`仍是仅JSON离线版，不能自动提取；本版是`siwx-lite.exe`。
旧`run.py`、旧一键脚本和旧下载仍是旧版，没有随新分支自动替换。
改源码后重新构建才作用于EXE；回退正常revert重建，保留旧包，不回灌任何用户数据库。
