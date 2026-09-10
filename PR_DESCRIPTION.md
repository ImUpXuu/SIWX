# PR: 修复 macOS 端密钥提取失败（0/N）

Closes #3

## 问题

macOS 端提取密钥时始终显示 `0/N salt 已验证`，日志中出现 `error: module importing failed`。
用户按指引在窗口期内重新登录微信也无法解决（见 issue #3）。

## 原因

macOS 端的密钥捕获脚本（`siwx/strategies/macos_lldb.py`）存在几处缺陷，叠加导致提取必然失败：

- 脚本在启动加载阶段即中断，后续附加进程、设断点的流程根本没有执行——这是日志中
  `module importing failed` 的直接来源；
- 断点命中后的参数读取未覆盖 `sqlite3_key_v2` 的调用形态，部分命中情况下读不到密钥；
- 附加微信进程的时序不够稳健，存在竞争，等待窗口也可能被外层超时中途打断；
- 提取结束后直接结束了微信进程，用户被迫反复重新登录，进一步增加了复现难度。

## 修复内容

1. 修正脚本加载流程，确保每次都能正常启动并携带目标进程信息；
2. 按"先完整附加、再设断点、后进入等待"的顺序理顺时序，消除竞争；
3. 补全 `sqlite3_key` / `sqlite3_key_v2` 两种调用形态下的参数读取，同时兼容 Intel 与 Apple Silicon；
4. 提取完成后分离调试器而非结束微信进程，微信保持登录状态；
5. 延长等待与超时时间，正常流程不再被中途截断；
6. 全部诊断输出（断点位置数、失败原因、等待结果）走统一的 `ctx["log"]` 通道，
   自动汇入运行日志：Web 控制台「日志」页实时可见，同时落盘 `logs/siwx.log`（轮转保留 5×10MB）；
7. 日志新增 `断点位置数` 一行，后续如再出问题可直接定位失败环节
   （无断点 / 附加失败 / 等待超时分别对应不同输出）。

## 验证

- 构造符合 SQLCipher 4 校验规则的测试数据，从策略入口到密钥验证入库做了全链路验证：
  覆盖两种调用形态命中、符号缺失、附加权限不足、等待超时共 6 个场景、17 项断言，全部通过；
- 各异常情形均能安全退出且保持微信运行；
- issue 中报告的 `error: module importing failed` 已消除。

**关于实机测试的说明**：以上验证在 Linux 环境通过模拟 LLDB 完整链路完成；我手头没有
Apple Silicon（ARM）架构的 Mac，无法完成 ARM 实机测试，Intel 实机也仅能覆盖我手头的
设备环境。请有条件的维护者或用户在合并后于 Apple Silicon 实机回归一次；若仍有异常，
请附上 `logs/siwx.log` 中完整 `[macos_lldb]` 段落与 macOS 版本号，新增的日志行可以直接
区分失败类别（无断点 / 附加失败 / 等待超时）。

## macOS 使用教程

### 1. 安装

- **方式一（推荐）**：前往 [Releases](https://github.com/ImUpXuu/SIWX/releases) 下载
  `stories-in-wx-v*-macos.dmg`，双击运行，浏览器会自动打开 Web 控制台；
- **方式二（源码）**：
  ```bash
  git clone https://github.com/ImUpXuu/SIWX.git && cd SIWX
  pip install -r requirements.txt
  python run.py serve        # Web 控制台，默认 http://127.0.0.1:8787
  ```

### 2. 提取前准备

- 安装 Xcode Command Line Tools（自带 LLDB）：`xcode-select --install`；
- 微信保持登录状态（密钥只存在于运行中的进程里）；
- 若此前提取失败过，建议先在设置里清空密钥缓存，避免旧缓存干扰判断；
- 附加微信进程可能需要权限：终端方式可用 `sudo python run.py serve`，
  仍提示权限不足时按 `MACOS_SUPPORT.md` 的指引处理。

### 3. 提取与解密（Web 控制台三步）

1. **欢迎页**：确认检测到微信与账号；
2. **选号**：选择要提取的微信号；
3. **提取并解密**：点击后若密钥未缓存，请在点击前 **60 秒内退出并重新登录微信**
   （issue #3 中已验证这一步必须做），保持微信在线等待捕获完成即可。
   首次提取成功后密钥会缓存，之后秒回，无需再重新登录。

### 4. 命令行方式（可选）

```bash
python run.py keys extract                 # 仅提取密钥（推荐先跑这一步验证）
python run.py keys list                    # 查看已缓存密钥（打码显示）
python run.py decrypt --db-dir ~/Library/Containers/com.tencent.xinWeChat/Data/Documents/xwechat_files/<wxid>/db_storage
python run.py auto                         # 提取 + 解密一条龙
```

### 5. 查看运行日志

- Web 控制台「📋 日志」页实时展示；文件日志在程序目录 `logs/siwx.log`；
- 提取环节以 `[macos_lldb]` 为前缀：`断点位置数: N`、`FAIL:NoSymbol`、`FAIL:Attach:…`、
  `FAIL:Timeout` 分别对应"没找到符号 / 附加被拒 / 等待超时"三类情况，反馈问题时请带上这一段。

### 6. 常见问题

| 现象 | 处理 |
|------|------|
| 密钥 0/N，日志 `断点位置数: 0` | 微信版本较新或符号被裁剪，请反馈日志与微信版本号 |
| 密钥 0/N，日志 `FAIL:Attach` | 附加权限不足，用 `sudo` 重跑或参考 `MACOS_SUPPORT.md` |
| 密钥 0/N，日志 `FAIL:Timeout` | 捕获窗口内微信没有打开数据库：先退出微信，在 60 秒内重新登录并保持在线，再点提取 |
| 提取成功但微信退出 | 旧版本行为，本版已改为提取后自动分离，微信不受影响 |
