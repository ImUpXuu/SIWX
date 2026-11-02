# SIWX × wexport 安全离线版

两项目的安全功能合并到此独立目录。保留聊天文本解析、中文桌面浏览、搜索、
HTML / Markdown / TXT / CSV / JSON 导出。普通用户双击 `siwx-safe.exe` 即可选择文件。
离线版是聊天资料整理工具，不是防病毒产品，也不能保证所有杀软均不告警。

## 数据来源

- 支持 SIWX 已有会话 JSON：`{"session":{"displayName":"示例"},"messages":[...]}`。
- 支持按 wexport Message 模型组织的 JSON 数组：`[{"type":1,"raw":"示例文本"}]`。
  wexport 原版并不原生导出此 JSON；这里合并的是其消息解析器，不能把原版 Markdown
  或加密数据库当作 JSON 导入。
- 可读取本版导出的 JSON。单次一个文件，最大64 MiB、十万条；大文件需先拆分。
- 只处理主动选中的导出文件；不直接读取微信加密数据库，不提取密钥。
  已有 JSON 可以使用；只有加密库时，本版无法导入，勿误认为聊天记录已丢失。
- 本版仅导出文本，不恢复图片、语音、视频或附件；JSON 内的媒体路径不会被打开。

## 使用

源码运行需 Python 3.10+（桌面需安装 Tk 支持），无第三方运行依赖：

```sh
cd safe
python app.py
python app.py conversation.json --out result.html --format html --search 关键词
python -m unittest discover -s tests -v
```

桌面搜索按发送者、时间、类型、正文匹配；导出全部匹配消息，列表最多显示5000条。
HTML 可直接打开，Ctrl+F搜索，无脚本、网络图片或外部样式。CSV 会转义公式起始字符。
所有导出拒绝覆盖已有文件，请使用新的文件名；源 JSON 保持原样。

## 安全边界

运行入口 `safe/app.py` 与旧版 `run.py` 独立。新产物不含旧 SIWX / wexport 的
进程内存读取、密钥自动提取、LLDB、注入、Hook、自动更新、服务监听、MCP、
后台计划任务或 SYSTEM 用户桥接。无需管理员、关闭杀软、添加排除项或更改执行策略。
保留旧源码仅用于历史与恢复，不会复制到安全版构建目录。旧版下载入口仍是旧版，
不会因本分支变更自动变成安全版；两者不能混称。

导出的文件包含聊天明文，请自行选择私有保存目录。本版不会自动上传、打开浏览器、
执行附件或访问输入中的链接。HTML/Markdown中的用户内容按被动文本输出。

## 构建与验证

推送 `codex/safe-offline-*` 分支的安全版源码会自动触发 **Build safe offline Windows**。
该工作流注册后也可在 GitHub Actions 手动选择安全版分支并 Run workflow。
产物是 `siwx-safe-windows-x64` 文件夹包，下载解压后运行，保留 `_internal` 目录。
构建用 `safe.spec`，关闭 UPX、采用 onedir，生成模块清单与SHA256SUMS；仅临时 artifact，
不创建Release、不替换现网。构建工具依赖固定 PyInstaller 6.16.0。

本版未配置代码签名。实际杀软检测应针对准确构建文件、哈希、杀软版本和检测名记录；
未签名/新文件的信誉提示仍可能存在。合成数据测试不能替代Windows桌面和杀软实测。
功能验证以主动打开测试JSON、搜索、五种格式导出、退出后无监听服务为准。

维护入口：`siwx_safe/documents.py` 是格式适配与被动导出，`siwx_safe/app.py` 是桌面/CLI；
第三方来源及许可见 THIRD-PARTY.md。修改后重新Actions构建才作用于EXE。
回退用Git正常revert后重建，旧版与既有导出保留，无数据迁移、数据库回灌或备份裁剪。

来源：用户合并与只留安全功能请求，任务336，2026-11-02。
