# SIWX 原生功能删减版

以仓库中的原 SIWX 5.2.0 页面和后端为基准，直接保留原生聊天、消息媒体、朋友圈、导出、首启向导、设置、日志与提取解密缓存。不再使用 `safe/` 的页面或数据适配器。新版版本为1.5.0，原版脚本和5.2.0原始分发入口保持独立。

`python native/prepare.py` 生成 `_build/siwx`；原 `siwx/` 不改动。`native-source-manifest.json` 逐文件记录原SHA、生成SHA及删除。密码算法、提取、导出、朋友圈、媒体与向导文件与源文件逐字节相同；聊天页只去统计按钮、弹层和处理代码，其余会话/分页消息/富文本/媒体逻辑保持原生。移除聊天统计、备份、MCP、在线更新和推广信息。许可证与原作者法律条款保留。插件机制保留，但不能重新注册移除功能的URL。

原生服务默认监听0.0.0.0，不校验客户端IP、Host、Origin、Sec-Fetch、token或密码。保留原始路径和请求参数校验。微信进程范围只限启动它的同一Windows用户。

构建：安装requirements，执行prepare，再运行 `python -m PyInstaller --noconfirm --clean native/native.spec`。启动：`python native/app.py --host 0.0.0.0 --port 8787 --no-open`；脚本仍采用原WSH/CMD与选定桌面用户链路。`--data` 将目录交给原数据源管理保存。

存储：仅新版 `%LOCALAPPDATA%/stories-in-wx-console/native`，其中 `data` 存原生DPAPI密钥缓存/设置，`output` 存原生解密数据库；不使用旧原版默认 `stories-in-wx` 目录，不自动迁移旧原版数据。`SIWX_NATIVE_HOME` 可显式指定新版本地绝对根目录（也供隔离合成测试使用）。微信源库只读；清理缓存仍需原页面用户操作确认。

验证必须通过原生Flask API和真实页面模块读取合成SQLite、点击会话显示消息、筛选、分页、时间线与导出；FakeSource成功不能作为本版本聊天正常的证明。真实设备提取/媒体可用性仍需实际运行确认。1.4.2的适配修补构建未发布，不作为本版本实现。

保留AGPL-3.0许可证及源代码，发行时同时附完整对应源、差分生成器和构建说明；设置页提供本机对应源码下载，不依赖外部推广链接。
