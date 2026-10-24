# Linux 支持说明

Linux 端的密钥提取走的是**进程内断点**，和 Windows / macOS 都不一样，原因和操作方式如下。

## 为什么不用 ptrace

Linux 上抓微信密钥的常见办法是外部 `ptrace` tracer：在登录时附加进程、读寄存器。
但在微信 4.1.2x 上这条路会直接撞死：

```text
[ERROR:scoped_ptrace_attach.cc(27)] ptrace: Operation not permitted (1)
```

微信内置 Chromium crashpad，崩溃采样时会自己 `scoped_ptrace_attach` 附加线程。
**一个进程只能有一个 tracer**，外部 tracer 抢先附加后 crashpad 附加失败，进程直接
`abort()`（`SIGABRT`）。这不是权限问题，把 `kernel.yama.ptrace_scope` 设成 0 也一样；
也不是断点打歪 —— 只 attach 不打断点时进程能活，一旦保持被 trace 就死。

## 现在的做法

把断点打在**微信自己的进程内**，全程没有 ptrace，也不需要 root：

1. `siwx.linux_hook.elfhook` 在微信主二进制的 `.rodata` 里找
   `com.Tencent.WCDB.Config.Cipher` 锚点，顺着 RIP 相对 `lea` 交叉引用定位
   cipher-config 消费函数，得到相对镜像基址的偏移（本机 4.1.13.23 为 `0x87ac370`）。
2. `siwx/linux_hook/wxhook.c` 现场编译成 `.so` 注入。构造函数用 `dl_iterate_phdr`
   取 PIE 基址，把 `base+偏移` 首字节改成 `INT3`，注册自己的 `SIGTRAP` 处理器
   （并包一层 `sigaction`，防止被微信覆盖）。
3. 命中后从 `ucontext` 取 `REG_RSI` / `REG_RDX`，按三种可能的入参布局各读 32 字节
   （`hit-raw` 直传、`hit-flat0` / `hit-flat8` 结构体首字段或次字段），全部写进日志
   当候选。复原原字节、把 RIP 拨回断点让原指令照常执行，随即拆掉断点——**一次性
   布点**，不再重布。（早期版本置 TF 单步后重新布点，实测与其它线程执行同一地址
   产生竞态，捕获后必崩。）
3. 用捕获到的 32 字节 passphrase 走 PBKDF2-SHA512(256000) 派生每个库的密钥，
   并逐库用 page-1 HMAC 校验；**校验不通过就不入库**。

## 注入方式（关键）

桌面环境（DDE / UOS / GNOME）通过 systemd 用户管理器拉起微信，父进程是 systemd，
`LD_PRELOAD=` 环境变量传不进去。所以走用户管理器环境：

```bash
systemctl --user set-environment LD_PRELOAD=/path/to/wxhook.so
# 之后再启动的瞬态单元会继承
systemctl --user unset-environment LD_PRELOAD     # 捕获结束立刻还原
```

策略在这一步做了三件事：保存原有 `LD_PRELOAD`、注入、在 `finally` 里精确还原
（原来有值就恢复原值，没有就删除）。没有 systemd 用户会话时退化为带环境变量
直接执行二进制。

## 前置条件

| 条件 | 说明 |
|---|---|
| Linux x86-64 | 目前只支持 x86-64 构建 |
| 系统编译器 | `cc` 或 `gcc`，用于现场构建 shim；不新增 Python 依赖 |
| 登录一次 | 扫码登录期间触发；微信已登录时由策略重启微信，建连即触发 |

**不需要 root。**

## 操作步骤

```bash
python run.py keys extract
```

**运行前必须知道的三件事：**

1. **微信会被自动关闭并重新拉起。** 这是方案的一部分（钩子只能在微信启动时注入），
   不是故障，不要自己再去点微信。
2. **重新拉起后微信停在扫码界面，必须手动登录一次。** 不登录就不会建立数据库连接、
   不会触发断点，工具只能一直等到超时。默认等 180 秒；手慢的话先设
   `WXS_HOOK_TIMEOUT=300` 再运行。
3. 微信若是已登录状态重启，通常会自己恢复；但要按"可能需要扫一次码"来准备。

跑完以后：systemd 用户环境还原，临时 shim 与日志一并删除，断点已拆；
密钥入库后，后续解密/导出不再需要介入，也不需要微信在线。

## 注意事项

- **偏移是构建相关的。** 微信升级后用 `find_hook_offset()` 重算即可，不需要改代码；
  定位失败会明确报 `HookNotFoundError`，不会静默 0 命中。
- **不要直接运行 `/opt/apps/com.tencent.wechat/files/wechat`。** 官方 deb 布局下
  直启会 `SIGABRT`（`exit 134`），必须经桌面启动器（`dde-am com.tencent.wechat`）
  拉起；策略已优先走启动器。
- shim 构造函数的第一件事是校验 `/proc/self/exe`：不是目标微信进程立刻返回，
  不做任何内存改动（万一环境变量残留，也不会影响其它程序）。
- 该方案只在你自己机器上、处理你自己有权访问的数据时使用。

## 实测记录

- 环境：UOS/Deepin，微信 4.1.13.23（构建 4067695895），25 个数据库。
- 结果：passphrase 捕获后 **25/25** 个数据库 page-1 HMAC 校验通过，
  含 `sns.db`（朋友圈）、`message_0..4`、`biz_message_0`、`contact.db`。
- 解密产物 247 MB，消息正文可正常读取。
