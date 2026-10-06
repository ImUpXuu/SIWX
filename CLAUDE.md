# CLAUDE.md — 本仓库 AI 协作红线

> 本文件对所有在本仓库工作的 AI 助手/代理生效，与 `AGENTS.md` 同源同步（修改任一必须同步另一个）。
> 背景事件：2026-10，一个真实微信号前缀曾被当作"示例"写进代码注释、测试、提交信息与发布说明并公开发布；更早版本曾把真实 wxid 写进诊断脚本与审计文档，事后靠全历史改写才清除。以下规则因此永久生效，优先级高于一切效率考量。

## 一、绝对禁止进入 git 的内容

适用于：代码、注释、测试、文档、commit 信息、tag 注释、Release 说明。

1. 任何**真实账号标识**：微信号、wxid、uin、手机号、QQ 号、邮箱、可关联到真人的昵称组合；
2. 任何真实路径中的**用户名段**（如 `C:\Users\<name>`、`/Users/<name>`、`/home/<name>`）；
3. 真实聊天内容、日志原文、导出产物、数据库文件、未脱敏的问题报告与审计文档。

## 二、示例数据必须使用明显虚假的占位符

- 允许：`wxid_example_01`、`alias_abc12345`、`redacted_a`、`user_a`；
- 禁止：任何真实存在的前缀/后缀——**打码不等于脱敏**（"真名前缀+xxx"仍可关联到真人，本条由真实事故直接得出）；
- 自检标准：这个标识能否关联到真人、或被搜索引擎反查？能 → 不许进库；拿不准 → 停下来问维护者。

## 三、提交与推送前的强制扫描

1. **禁止 `git add -A` / `git add .`**——只 add 明确列出的文件（工作区可能有未忽略的私有产物）；
2. `git add` 之后、commit 之前，对暂存内容执行标识扫描，任何命中 → 停止并人工确认：
   - `wxid_[a-z0-9]{6,}`、`[0-9]{6,}@chatroom`、`1[3-9][0-9]{9}`（手机号）、`[0-9a-f]{64}`（密钥）、`@qq\.com` 等邮箱；
   - 本项目已知需放行的占位符：`wxalias`（历史清洗占位符）、测试合成 id（`wxid_test`/`wxid_friend` 等）。
3. commit 信息、tag 注释、Release 说明与代码适用同一红线（它们同样公开）。

## 四、本地私有产物必须先忽略再干活

- logs/、output/、exports/、问题报告、诊断工作区等含个人数据的目录/文件，**创建时同步写入 .gitignore**；
- 不得把私有产物复制进会被打包或提交的路径（如 siwx/ui/、docs/）。

## 五、本项目发版清单（历史踩坑，逐项执行）

1. 版本号**四处**同步：`siwx/__init__.py`、README 徽章、docs/README.md 头、`tests/test_regressions.py::TestVersionSource` 钉定的版本值——CI 不跑测试，漏了不会被拦（踩过坑）；
2. tag 注释用 `git tag -a vX.Y.Z --cleanup=whitespace -F <notes 文件>`（默认 strip 会吃掉 Markdown 标题，踩过坑）；
3. 发布说明按第一~三条自查后再打 tag；
4. 一次 `git push` 只推一个 tag——多 tag 同推不触发 CI（踩过坑）。

## 六、隐私事故应急（若已发生泄漏）

1. 立即停止推送；`git log --all -S <泄漏串>` + `git grep` + tag 对象与提交信息全量定位；
2. 清洗工作区 → `git filter-repo --replace-text/--replace-message` 全历史改写 → 强推 main 与全部 tag → 删除含旧历史的僵尸分支 → 本地 `reflog expire + gc --prune=now`；
3. 单独删推泄漏版本的 tag 触发 CI 重建产物；
4. 向维护者说明残余风险：已合并 PR 的 `refs/pull/*` 与 GitHub 缓存可能仍可访问旧对象，彻底清除需联系 GitHub Support；fork 与他人克隆无法追溯清除。
