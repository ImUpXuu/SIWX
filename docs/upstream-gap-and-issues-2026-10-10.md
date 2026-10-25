# SIWX 上游差距与公开 Issue/PR 台账 —— 2026-10-10

> **⚠️ 已合并（2026-10-11）**：本文的台账职能已合并进 `docs/deferred-improvements-2026-10-11.md` 并按该日时点逐条复核更新——其中"上游领先 3 处"、PR #27/#28 对应项均已确认吸收进本地；"上游 tag 删除/main 回滚"类结论（后续版本的对比中出现）已被实时核验推翻。本文仅保留作 2026-10-10 时点底稿（Issue/PR 明细与 P0-2 串清单依据），**结论以合并版为准**。
>
> **报告日期**：2026-10-10
> **本地基准**：`main` HEAD = `be2c904`，`siwx/__init__.__version__ = "5.0.7"`，工作区干净（`git status --porcelain` 为空）
> **上游基准**：`ImUpXuu/SIWX` main = `14a9f4c`（v5.0.8 发版提交），最新 tag `v5.0.7` / `v5.0.8`，38 star / 9 fork，未开 Discussions
> **报告性质**：**只读**。本报告不包含任何代码改动；文中「建议方案」均为待评估设计，尚未实施。
> **方法**：① `git fetch https://github.com/ImUpXuu/SIWX.git main`（只写 FETCH_HEAD，未建 remote、未 merge）后做**树级内容比对**与目录树哈希比对；② `gh` API 抓取全量 issue（11 条）/ PR（18 条）/ 评论与 PR files；③ 每条结论回到本地源码逐行复核，所引 `文件:行号` 均与实际代码一致。
> **隐私声明**：本报告**不含**任何真实账号标识（微信号 / wxid / uin / 手机号 / 邮箱）、真实聊天内容原文、真实路径用户名段。涉及上游夹具数据泄漏（§三 ①-P0-2）只给**数量与文件定位**，不复现原文——公开原文本身即为二次泄漏。

---

## 一、结论摘要

1. **上游 v5.0.8 就是本地 5.0.7 线的整树快照**。两边内容只差 **15 个路径**（本地 308 文件 / 上游 309），`siwx/` 子树里唯一不同的是 `siwx/__init__.py` 的一行版本号。**前端（`siwx/ui`、`siwx/templates`、`siwx/assets`、`siwx/plugins`）逐字节一致**，「我们 UI 跟上游差很多」的印象已过期。
2. **上游比我们先进的项目只有 3 处**，都很小：打包 `datas` 缺 2 行、3 个环境敏感测试修复、5.0.8 的发版件。不存在「上游多了我们没有的功能」。
3. **不建议 merge，也不建议强推**。两条历史无共同祖先，`merge` 直接失败、`--allow-unrelated-histories` 会在共享文件上全量冲突；强推会覆盖对方 CI 基线、他人正在 review 的 PR #27/#28 与 9 个 fork 的历史。建议走「增量交付 + 吸收上游 3 处」（§二 ⑦ 路径 A）。
4. **最要紧的不是功能差距，是一条公开泄漏**：我们 `be2c904` 从 `tests/` 清掉的字符串里，有 **38 个「上游仍有、本地已无」**，随 10-07 的整树快照进了公开仓库，且 v5.0.8 tag 对象在内。清理**不能走公开 PR**（diff 的删除行 = 原文二次发布）。
5. GitHub 侧用户问题高度集中在**macOS**：密钥提取（#3 → #30）与媒体解密（PR #28）。核对结论是——**这些在本地代码里全部指得到具体行，且多数没有可用回退路径**（§三 ④）。

---

## 二、与上游的版本差距

### ① 时间线（解释 15 个路径的全部来源）

| 时间（+08:00） | 侧 | 事件 |
|---|---|---|
| 10-06 23:17–23:36 | 本地 | 5 个提交：UI 修复、发版闸门、`perf(sqlcipher)` strxor、macOS Apple Silicon 修复移植、隐私红线与中性化、null 安全加固 |
| **10-07 10:50** | 上游 | `f05b118`「feat: 合入 5.0.7 优化线」——**把本地整棵树当快照吃进去**（含 UI、docs、scripts、.github），非挑选式移植 |
| 10-07 10:52 | 上游 | `6788647` 打包 `datas` 补 `siwx/assets`、`siwx/templates` |
| 10-07 11:20 | 上游 | `f160d9d` 3 个环境敏感测试修复（UTC 时区、干净密钥库自给自足） |
| 10-07 11:20–11:25 | 上游 | `25e271c` / `14a9f4c` 发版 v5.0.8（版本号四处 + `version.json`） |
| **10-07 18:18** | 本地 | `be2c904` `tests/_base.py` + 夹具真实串清理 + 冗余检测清理 + AGENTS/CLAUDE 一行 |

上游 main 的最后两个提交都晚于 `f05b118`，而本地最后一次提交 `be2c904` 晚于他们全部提交 —— 所以差异是「双向各领先一小段」，不是谁改造了谁。

### ② 逐路径差异清单

`git diff --name-status HEAD FETCH_HEAD` 的完整结果与处置建议：

| 路径 | 状态 | 差异内容 | 建议处置 |
|---|---|---|---|
| `siwx/__init__.py` | M（1 行） | `5.0.7` vs `5.0.8` | 随发版统一到新版本号 |
| `version.json` | M（12 行） | 版本/日期/notes/assets URL 全套 5.0.7 vs 5.0.8 | 吸收上游 5.0.8 条目，再写下一版 |
| `README.md` | M（1 行） | 版本徽章 5.0.7 vs 5.0.8 | 同上（四处同步之一） |
| `docs/README.md` | M（1 行） | 文档头版本 | 同上 |
| `AGENTS.md` / `CLAUDE.md` | M（各 1 行） | 发版清单第 1 条：本地已改为「CI 的 test job 会跑 pytest，漏了会被拦」；上游仍是旧的「CI 不跑测试，漏了不会被拦（踩过坑）」 | **保留本地版**。`.github/workflows/release.yml` 两树相同，其中确有 `全量测试: python -m pytest -q -rs`，故上游该句为事实错误 |
| `packaging/siwx-win.spec` | M（+2） | 上游多 `datas`：`siwx/assets`、`siwx/templates` | **吸收上游**（见 ③-1） |
| `packaging/siwx-mac.spec` | M（+2） | 同上 | 吸收上游 |
| `tests/test_regressions.py` | M（188 行） | 双向都改：本地基类化 + 夹具清理；上游 `f160d9d` 环境修复 | **唯一需要手工三方合并的文件** |
| `tests/test_sns.py` | M（194 行） | 本地清理夹具；上游保留原样 | 以本地为主，核对未覆盖断言 |
| `tests/test_plugins.py` / `test_macos_lldb.py` | M（8 / 9 行） | 本地移除冗余检测 | 以本地为主 |
| `tests/_base.py` | **D（仅本地有）** | 本地 `be2c904` 新增的 62 行公共基类 | 交出去 |
| `PR_DESCRIPTION.md` | **A（仅上游有）** | issue #3 的一次性 PR 描述，100 行中文 | 按我方审计删除（见 ⑥） |
| `push_and_pr.sh` | **A（仅上游有）** | 一次性推分支建 PR 脚本，46 行 | 按我方审计删除（见 ⑥，含凭据残留雷） |

**唯一真实的能力缺口在打包**：同一份前端源码，上游打的包有表情图与模板，本地打的包缺文件。

### ③ 上游领先我们的 3 处

1. **`packaging/*.spec` 的 `datas`（各 +2 行）**
   `(str(ROOT / "siwx" / "assets"), "siwx/assets")`、`(... "siwx" / "templates" ...)`。缺失后果：v5.0.8 的「小黄脸表情内嵌渲染（146 张）」与「自定义 HTML 导出模板」在**打包版**里因资源未入包而失效——源码跑 `python run.py` 正常，一发 exe/dmg 就坏，属于最容易漏测的一类。
2. **`f160d9d` 3 个环境敏感测试修复**
   把 `TestManifestSourceGuard` 改为密钥自给自足（原文注：「此前用的是开发者本机真实密钥库，干净环境…」）。**这解释了 PR #27/#28 报告者在 Mac 上报的 2 个失败**：他们的分支基线停在 v5.0.7，没带这个修复。
3. **v5.0.8 的发版件**：`version.json` 的 notes 与四处版本号。不算技术领先，但意味着对方已经占用了一个版本号，我们下一次交付要从 5.0.9 起算。

### ④ 我们领先上游的 1 处

`be2c904` 整体：`tests/_base.py`（62 行公共基类）、`tests/` 夹具中 **98 个字符串字面量的清理动作**（其中 38 个是「上游仍有、本地已无」，见 §三 ①-P0-2）、`test_plugins.py` / `test_macos_lldb.py` 冗余检测移除、AGENTS/CLAUDE 的 CI 描述纠错。

### ⑤ 前端一致性证据（目录树哈希）

| 目录 | 本地 | 上游 | 结论 |
|---|---|---|---|
| `siwx/ui/` | `3b1fc447` | `3b1fc447` | SAME |
| `siwx/templates/` | `8a0d6512` | `8a0d6512` | SAME |
| `siwx/assets/` | `7dcc7aed` | `7dcc7aed` | SAME |
| `siwx/plugins/` | `6554c861` | `6554c861` | SAME |
| `scripts/`、`.github/`、`examples/` | — | — | SAME |

上游 5.0.8 发布说明中的「**UI**：首启改为全屏引导向导（同意条款 → 选号 → 提取解密），账号可随时'＋添加'」即我方改造；我方有意删除的 `siwx/ui/pages/guide.*`（并入 onboarding/settings）在上游同样为 0 个文件——那次快照连删除一起带走了。

### ⑥ 「移了没移完」的准确落点

方向与直觉相反：**不是上游删了我们的东西没删净，而是我们删掉的东西上游还留着**。

- 本地按 `docs/full-code-audit-2026-09-28.md:508-509`、`:577`（P3 仓库整洁）删除了根目录 `push_and_pr.sh` 与 `PR_DESCRIPTION.md`；上游 main 仍在发布这两个文件。
- `push_and_pr.sh:28` 为 `git remote set-url origin "https://oauth2:${GITHUB_TOKEN}@github.com/..."` —— 把运行者的 token 明文写入 `.git/config`（同文档 `:433-443` §5.2 判为中危·凭据泄漏）。全文扫描**未发现硬编码 token 字面量**，风险面是「谁跑谁残留」。
- 该脚本还硬编码分支名 `fix/macos-key-extraction-0n`（带 `/`），与 `CONTRIBUTING.md`「分支名不要带 `/`」的约定自相矛盾——该约定当初正是为修它的副作用而写的。
- **文档与现实不符的两处**（两树都有）：`docs/full-code-audit-2026-09-28.md:43` 与 `docs/review-2026-10-02.md:312` 仍把这两个文件描述为「留在仓库根目录」；`docs/audit-siwx-issues-2026-10-03.md`、`docs/full-code-audit-2026-09-28.md` 这类审计报告本身也已被上游一并公开，其中实测数字与样本描述建议按红线再复核一遍。

### ⑦ 是否直接 merge：明确建议

**不要 merge**（无共同祖先：本地根提交 `2a75375`、远端根提交 `7f97783`，`git merge-base` 为空）。也**不建议强推**：`ImUpXuu/SIWX` 是 public、已有 38 star / 9 fork 与真实用户流量，强推 main 会同时打断对方 CI、他人正在 review 的 PR #27/#28 基线，并把 §三-P0-2 的公开泄漏面接过我们的历史。

| 路径 | 做法 | 代价 / 前置 | 建议 |
|---|---|---|---|
| **A 增量交付**（推荐） | 先吸收上游 3 处（spec×2、`f160d9d`），再把 `be2c904` 的内容交出去；版本号交由对方 bump 或我们提 5.0.9 | 只需手工合并 `tests/test_regressions.py`（188 行）；其余是「选谁的版本」单选题 | ✅ 风险最低、可逆、不动对方历史 |
| B 以本地为主版本 | 整树快照替换上游 main | ①无共同祖先只能整树替换；②本地 `5c4fd41` 之前的提交仍含真实账号标识，push 前**必须** `git filter-repo` 全历史改写（破坏性，需明确授权）；③四处版本号同步 + tag 规范（`--cleanup=whitespace`、一次只推一个 tag）；④强推影响他人 fork | ⚠️ 除非确定要接盘主线，否则不划算 |

---

## 三、GitHub 公开 Issue / PR 台账

### ① Issue 全量（11 条，#3–#30）与代码落点

| # | 状态 | 用户提出的问题 | 本地代码落点与结论 |
|---|---|---|---|
| 3 | CLOSED `bug` | 密钥提取一直 0/24，界面提示无解密产物；评论追加 3 位 macOS 复现（Sequoia 15.1 / 15.7.9、Tahoe 26.5.2），日志恒 `module importing failed` → `未捕获到 passphrase` | 根因已由 PR #7/#10 定为「提取脚本加载阶段即中断，与登录状态无关」；PR #26 的 Apple Silicon 加固**已在本地**（`siwx/strategies/macos_lldb.py:165-167`、`siwx/extract.py:75-82`、`:93-94`） |
| 4 | CLOSED | win 无法解密；评论追加「下载最新 4.0 打开后版本仍显示 v0.3.3」 | 版本一致性现由测试闸门守护：`tests/test_regressions.py:1171` `TestVersionSource` + CI `python -m pytest`；UI 显示口径本轮**未复核** |
| 5 | CLOSED | 「无法下一步」，微信明确在运行仍检测不到；评论给出定性「只会检测默认存储路径，换回默认目录就能检测到」（2 人复现） | 现状已扩：`siwx/discover.py:141-173` 扫全盘符 + 多用户 `Documents/xwechat_files`（macOS 走 Containers 分支），且提供手动入口 `/api/discover/validate`（`siwx/server.py:900-906`，UI 在 `siwx/ui/onboarding.js:212`、`siwx/ui/pages/settings.js:404`）→ 判为**已闭环，建议做一次非默认目录回归** |
| 6 | CLOSED | 能不能导出语音 | 5.0.0 已实现；导出侧见 `siwx/exporter.py:300-332` |
| 9 | CLOSED | Win11 + 5.0 第一步仍「未找到微信数据目录」 | 同 #5 路径；维护者当时还主动编辑 issue 遮挡截图隐私 |
| 11 | CLOSED | macOS 双击 .app 秒退，`OSError: [Errno 30] Read-only file system` | 数据目录已迁 `paths.data_dir()`（`docs/full-code-audit-2026-09-28.md:298` 记录该修复）；报告者随后追问「可以运行但解析不了密钥」→ 回到 #3 那条线 |
| 13 | CLOSED `enhancement` | 想在删除前完整导出朋友圈（日期/图片/评论/评论者名字） | 5.0.4 已实现 |
| 14 | OPEN `notice` | 维护者公告：在 insight 中看到来自腾讯内部的 refer 项目，**随时可能 DMCA 下架**，号召 fork，承诺数周内改 MIT | 非缺陷，但是「主线归属」决策的现实背景（§二 ⑦） |
| 24 | CLOSED | 我方改进提案（10 项中量级） | 已被维护者回评「已合并👍」；实际消化情况见 ④-未复核项 |
| 29 | OPEN `question` | 从 WEFLOW 迁来的用户致谢：「保住了我好多年的聊天记录」 | 非缺陷；说明真实用户在依赖本项目，处置泄漏与 macOS 问题的紧迫性更高 |
| 30 | OPEN `question` | **最新且零回复**：`[macos_lldb] PID=…: 未捕获到 passphrase`、`已验证 0/20`，siwx 5.0.8 / macOS；已按提示重登录无效，卡点是「我的微信版本没到 4.1.80+」 | 代码层面确实无解，见 ③-P1-4 |

### ② PR 全量（18 条）与本地关系

| # | 状态 | 主题 | 与本地的关系 |
|---|---|---|---|
| 1 / 2 / 7 / 8 / 10 / 12 | MERGED | macOS 支持落地、keystore 跨平台、LLDB 0/N 修复、命名与分片索引、数据落盘迁移 | 已在本地（同一批内容） |
| 15–23 | MERGED | 我方 9 个 fix PR（时间轴、分页游标、`SX.esc`、HTML 转义、pack=each 死链、图片缓存加锁、`is_me`、TXT 头部计数、路由代数守卫） | 已在本地并已被上游快照带走 |
| 26 | MERGED | Apple Silicon 密钥提取（4 个叠加问题） | **已移植进本地**（`4573277`） |
| **27** | **OPEN**（0 review / 0 comment） | `keys extract --db-dir` 未生效 + SIP 文档纠偏 + 断点捕获窗口说明 | 未吸收；缺陷在本地逐条成立，见 ③-P1-3 / P1-5 |
| **28** | **OPEN**（0 review / 0 comment） | macOS 媒体链路缺口 + 全量媒体备份；已 rebase 到 v5.0.8 并「剔除已被上游独立修复的部分」 | 未吸收；核心缺口在本地成立，见 ③-P0-1 |

> 两个 OPEN PR 的作者都端到端验证过（其中一位报出 300/300 抽样解密成功、7.8 万张图片全量备份跑通），卡点是**没有 Mac 设备可供 maintainer 复核**——这正是它们挂着的现实原因。

### ③ 未闭环问题 → 代码定位 → 建议方案

> 每条均为 2026-10-10 回本地源码复核后的结论。行号以本地 HEAD 为准。

**P0-1 macOS 媒体链路整体断裂（issue #11 尾部 + PR #28）**
- 现象：macOS 上聊天图片/朋友圈图片**全部**解不开；已解密的 wxgf 在导出里消失。
- 根因 a：`siwx/media.py:129-143` `find_kvcomm_codes()` 的三条 glob 全为 `C:/Users/*/AppData/Roaming/Tencent/...`，无 macOS 容器路径；其返回值喂给 `siwx/media.py:312` 的账号级媒体密钥派生循环 → macOS 恒空集。
- 根因 b：wxgf 解码只有 Windows 路径——`siwx/media.py:198-209` 只找 `C:\Program Files\Tencent\...\VoipEngine.dll`，worker 用 `os.add_dll_directory` + `ctypes.WinDLL`（`siwx/media.py:160-179`）→ macOS `convert_wxgf`（`:212-256`）恒 None → `_finalize`（`:465-480`）返回 None → `get_image` 记 `last_err="wxgf 转码失败"`（`:581/:609/:653`）并**当整体失败处理**：导出侧不写文件、不进 `media_map`，已解密的原始 wxgf 也不留档。
- 建议方案：① `find_kvcomm_codes()` 按 `platform.system() == "Darwin"` 追加 `~/Library/Containers/*wechat*/.../app_data/{net,ilink}/kvcomm/` 类路径（容器名含 wechat 匹配，兼容改名/马甲包）；② 引入 macOS 侧 wxgf 解码路径，**并区分「解出来了但本平台不能预览」与「本地没有原图」**——前者应在导出中保留 `.wxgf` 原样留档而非丢弃；③ 失败原因需要一路透传到导出报告与前端，不能坍缩成 404。
- 影响面：`media.py` / `exporter.py` / 前端图片回退；不动 Windows 逻辑。

**P0-2 上游公开仓库仍带 38 条真实夹具串（隐私红线）**
- 事实：`be2c904` 清理了 `tests/` 98 个字符串字面量，其中 **38 个「上游 main 仍有、本地已无」**：`tests/test_sns.py` 28、`tests/test_regressions.py` 8、`tests/test_macos_lldb.py` 1、`tests/test_plugins.py` 1。形态属真实朋友圈标题/歌名/群名一类中文正文（AGENTS.md §一.2 禁止项），随 10-07 整树快照进入公开仓库，**v5.0.8 tag 对象也在内**（tag 指向 `14a9f4c`，晚于快照）。对方此前那轮全历史改写早于我们的清理，故这批串是改写之后重新进去的。
- 本地自查结果：干净。`tests/` 里 47 种 wxid 类 token 中不符合放行清单的只剩短合成串；按 §三 口径 `wxid_[a-z0-9]{6,}` 且含数字的 4 处命中，全部是 `tests/test_regressions.py:2219-2240`（`TestLogDesensitize`）为验证脱敏规则而刻意构造的合成串（形如 `wxalias_…` / `wxid_…`，非真实标识）。
- **处置约束（关键）**：不能提公开 PR——PR/commit 的删除行本身就是原文的第二次发布。可选：① 把「已清理状态的整体树」交给维护者（非公开渠道），由其改写历史并重打 tag；② 由维护者侧按 AGENTS.md §六 走 `filter-repo --replace-text` → 重推 main 与全部 tag → 单独删推 v5.0.8 tag 触发 CI 重建产物；③ 同时告知残余风险：`refs/pull/*`、GitHub 缓存、他人 fork 与 9 个 fork 无法追溯清除。
- 建议同时复核：`docs/audit-siwx-issues-2026-10-03.md`、`docs/full-code-audit-2026-09-28.md`、`docs/debug-logging-audit-2026-10-04.md`、`docs/data-safety-audit-2026-09-19.md` 这些已随快照公开的审计文档中，是否存在可关联真人的昵称组合或真实路径段。

**P1-3 `keys extract --db-dir` 被静默忽略（PR #27）**
- 定位：`siwx/cli.py:20-41` 两个分支都只调 `extract.extract_all(log=…, use_cache=…)`，`args.db_dir` 从未参与；`_resolve_dirs()`（`siwx/cli.py:14-17`）只在 `cmd_decrypt` 等处使用（`siwx/cli.py:69`）；底层 `siwx/extract.py:441 def extract_all(log=print, use_cache=True)` 根本没有接收目录列表的参数。
- 后果：多账号机器上无法把「LLDB 一生只命中一次」的捕获窗口留给目标账号；`--json` 分支同样返回全部账号。
- 建议方案：`extract_all()` 增加可选 `dirs=None`（为 None 时保持全盘自动发现，行为不变），`cmd_keys_extract` 在有 `--db-dir` 时经 `_resolve_dirs()` 传入；补 CLI 回归用例。

**P1-4 微信版本低于 4.1.80 的 Mac 用户没有任何可用路径（issue #30，零回复）**
- 定位：macOS 策略注册表只有 `keystore_source + mmkv + macos_lldb`（`siwx/strategies/__init__.py:23-32`）；**吃内存字面量的 `memscan` 依赖 `winproc`，非 Windows 不加载**（同文件 `:17-22`、`siwx/strategies/memscan.py:7`）。而 <4.1.80 的 macOS 微信 raw key 仍在内存里，本应正是 memscan 的场景。
- 二次门槛：`macos_lldb.py:254` 的命中条件是硬编码 `rounds == 256000 and plen == 32`（常量 `PBKDF2_ITERATIONS`，`:20`），KDF 参数一变即恒 0 命中；`siwx/extract.py:239` 只把「需要 4.1.80+」打给用户，**没有任何版本探测、也没有回退**。
- 建议方案：① 加 macOS 微信版本探测，把「你的版本不支持」从提示升级为前置判定 + 明确下一步（升级到 4.1.80+，或走原始密钥路线）；② 评估为 Darwin 增加一个不依赖 winproc 的内存/离线策略；③ 放宽 KDF 参数匹配（记录实际 `rounds/plen` 到日志，便于按版本适配）。

**P1-5 macOS 文档与实际约束相反（PR #27，且是 #30 用户反复重试的成因）**
- 定位：`MACOS_SUPPORT.md:36` 仍写「可能需要 `sudo` 或关闭 SIP」；注意事项总共 3 条（`:33-37`），测试状态表 `:50-51` 的 Apple Silicon 行仍未勾。
- 缺失的三条实操事实：SIP 开启时 AMFI 拒绝 attach 无 `get-task-allow` 的 hardened-runtime 进程，**root 也不行**，只能关 SIP；断点只在数据库连接**新建**时命中一次，微信跑久了不再触发，需完整退出重开并在窗口内立即提取，且 `killall` 优于 `osascript … quit`（后者需要未必存在的「自动化」权限，未授权会静默失败，表现为「看着重启了其实还是旧 PID」）；多账号要靠 `--db-dir`（依赖 P1-3 修复才真正生效）。
- 建议方案：按上述改写 `MACOS_SUPPORT.md` 注意事项与测试状态表，并在 `keys extract` 失败提示里加一条「断点窗口只在连接新建时打开」。

**P1-6 导出扩展名靠字符串猜测，WebP 被写成 `.jpg`**
- 定位：`siwx/exporter.py:291` `ext = "png" if "png" in info else ("gif" if "gif" in info else "jpg")`；而 `siwx/media.py:44-50` 的签名表含 `webp`，`_emit` 返回的 ctype 就是 `image/webp`（`siwx/media.py:546-557`）→ WebP 字节配 `.jpg` 文件名，导出产物「看似一张图」实则命名错误。上游 PR #28 改为直接从 ctype 取子类型。
- 说明：PR #28 描述里的「wxgf 被误标 .jpg」在**本地不成立**（本地 `_finalize` 已拦住 wxgf 透传），但同一行代码的 webp/jpeg 分支成立——是同一家族的缺陷，不是同一个 case。
- 建议方案：`ext = info.rsplit("/", 1)[-1]`，并对无法识别的 ctype 显式失败而非默认 `.jpg`。

**P2-7 前端把「本平台无法预览」显示成「原图未下载」**
- 定位：`siwx/ui/common.js:113-128` `imgFallback()` 只有一种文案 + 重试按钮，不区分原因；后端 `siwx/api_chat.py:1576-1577` 把一切失败坍缩成 `404 JSON`。叠加 P0-1，macOS 用户会反复点重试而永不生效。
- 建议方案：失败响应带结构化原因（`missing_local` / `no_decoder_on_platform` / `decrypt_failed`），前端按原因选择文案与是否挂重试。（PR #28 最终没走前端路线，而是后端保留 `.wxgf` 留档解决，两种方案需二选一并保持一致。）

**未复核项（本轮没有回代码验证，不作结论）**：#24 提案中 10 项中量级改进（富化三处重复、按天取整的「导出所选」、两遍扫描首遍、HTML 真流式与 XLSX write_only、`/api/job` 增量拉取、`/api/status` 快照缓存、协作式取消、auto-sync TOCTOU、traceback 回显、页面模块缓存击穿）；#4 的 UI 版本显示口径。

### ④ 上游/贡献者描述里已被我们修掉的（勿重复劳动）

- `api_chat.py:735-769` `_parse_refer` 已先 `html.unescape`、并用 `_refer_type_label`（`:772-779`）按 `type="N"` 与 `<type>N</type>` 两种写法全等判定，嵌套 XML 一律剥标签不透传（`:761-764`）→ PR #28 描述里的「引用消息转义 XML 泄漏」不成立；其 rebase 提交说明亦写明「剔除已被上游独立修复的部分」，现 diff 中已无 `api_chat.py`。
- wxgf 原样透传成 `200 + image/wxgf` 本地不存在（`media.py:465-480`）。
- PR #26 的四处（`discover` 权限中断、`global_harvest` 非 Windows 守卫、macOS 逐账号内存策略、arm64 LLDB + `CCKeyDerivationPBKDF` 断点）均已在本地。

---

## 四、建议执行顺序（均需授权后再动，本轮未实施任何改动）

1. **P0-2 隐私**：确定处置渠道（联系维护者给非公开 patch，还是我方做全历史改写），并复核四份已公开的审计文档 —— 优先级高于一切功能项。
2. **吸收上游 3 处**：`packaging/*.spec` 各 2 行 + `f160d9d` 的测试修复（可逆、影响小，先让本地包能跑对）。
3. **手工合并 `tests/test_regressions.py`**（188 行双向改动，唯一真正的合并工作）。
4. **P1-3 + P1-5**：`--db-dir` 生效 + macOS 文档纠偏（PR #27 的价值，直接回应 #30）。
5. **P0-1 + P2-7**：macOS 媒体链路（kvcomm 路径、wxgf 平台解码/留档、失败原因结构化）。
6. **P1-6**：导出扩展名取自 ctype。
7. **P1-4**：macOS 微信版本探测与 Darwin 内存策略可行性评估。
8. 发版：版本号四处同步、`tag -a --cleanup=whitespace -F notes`、一次只推一个 tag。

---

## 五、附录

**复现命令（只读）**

```bash
git fetch --no-tags https://github.com/ImUpXuu/SIWX.git main      # 只写 FETCH_HEAD，不建 remote、不 merge
git diff --name-status HEAD FETCH_HEAD                             # 15 个路径
git diff --stat HEAD FETCH_HEAD                                    # 403 insertions / 230 deletions
git rev-parse "HEAD:siwx/ui" "FETCH_HEAD:siwx/ui"                  # 目录树哈希逐字节比对
git log FETCH_HEAD -8 --pretty="%h %ad %s"
gh issue list --repo ImUpXuu/SIWX --state all --limit 200 --json number,title,state,labels,body
gh pr list --repo ImUpXuu/SIWX --state all --limit 200 --json number,title,state,author,body
gh api repos/ImUpXuu/SIWX/pulls/28/files --jq '.[].filename'
```

**本地测试现状（2026-10-10 实跑）**：`python -X utf8 -m unittest tests.test_regressions.TestManifestSourceGuard tests.test_regressions.TestTempFileUniqueness -v` → **Ran 9 tests, OK**。上游报出的 2 个失败在本地不复现，且 PR #27 分支基线停在 v5.0.7（未含 `f160d9d`）；但这两个用例仍带 Windows 假设（`tests/test_regressions.py:1776` 用 `str(...).casefold()` 比来源、`:1778` 用反斜杠 `"contact\\contact.db"` 造条目；`:1594-1597` 靠抛 `OSError(13)` 强制走复制回退分支），**是否因 macOS 符号链接（`/var → /private/var`）与大小写敏感文件系统而误判，需真机跑当前 main 才能定论**。

**上游仓库统计**：38 star / 9 fork / 磁盘 2986 KB / Discussions 0 条；11 个 issue（3 OPEN：#14 公告、#29 致谢、#30 提问）、18 个 PR（2 OPEN：#27、#28，均 0 review 0 comment）。

---

**关联文档**：`docs/full-code-audit-2026-09-28.md`、`docs/audit-siwx-issues-2026-10-03.md`、`docs/review-2026-10-02.md`、`docs/debug-logging-audit-2026-10-04.md`、`MACOS_SUPPORT.md`、`AGENTS.md`、`KNOWN_LIMITATIONS.md`。
