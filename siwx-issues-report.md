# SIWX 导出逻辑静态排查报告（5.0.7 合并后复查）

> 说明：本文档基于 **personal 分支合并上游 v5.0.7 之后**的代码（commit 5680959）静态分析得出，
> 未改动任何运行时行为。旧版报告（基于 5.0.6）中的问题在此逐一标注现状，并补充合并后新发现。
> 行号以当前源码为准；全量测试 303 通过 / 1 跳过。

---

## 一、旧问题现状（5.0.6 报告 → 合并后）

| # | 旧问题 | 现状 |
|---|--------|------|
| 1 | 引用消息（外层 49）丢 quote | ✅ 已修。上游 5.0.7 与本地 `_enrich_row` 均按 `<refermsg>` 探测（api_chat.py:583），并有 TestQuoteInType49 回归测试覆盖 |
| 2 | 渲染器读 `quote.sender` 而解析器给 `displayname` | ✅ 已修。html_template.py:357 兼容两种字段名 |
| 3 | 媒体按 localId 回填导致跨分片撞号错挂 | ⚠️ 大头已修：`_attach_media`（exporter.py:145）按类型（3/47/34）+ 图/语互斥回填，文本/链接/系统消息不再错挂。**残留**：同为图片的两条消息跨分片撞号仍会共享同一张图（上游注释中已承认，需 (md5, localId, ts) 组合键才能根治） |
| 4 | type 49 content 泄漏 CDATA | ✅ 已修。api_chat.py:317-329 截取 title 后经 `_xml_text` 剥离 |
| 日志1 | 分片打开失败静默吞掉 | ❌ **基本未修**（详见新发现 N1/N2） |
| 日志2 | 三套日志互不相通 | ⚠️ 部分改善：日志页统一脱敏、UI 级别切换现在同时作用于 siwx.log（logger.py + server.py）。三套通道本身仍未合并 |
| 日志3 | 日志不脱敏 | ✅ 已修。`_desensitize_item` 对日志页所有条目脱敏，`export_logs` 保留 desensitize=0 通道 |
| 日志4 | logger 无条件 print() | ⚠️ 仍在。logger.py:64-68 `_cprint` 直接 print，打包成无控制台运行时仍有风险（低危） |

上游 5.0.7 还顺带修了：zip 死链（与本地 E-1 相同修复，取一）、媒体失败原因聚合日志
（`_log_media_failures`）、条数对账 warning、HTML 渲染器 type 49 走引用分支 + 复用预解析 link 字段。

---

## 二、新发现（合并后代码）

### N1. 分片失败日志加在了死代码里，真正使用的路径仍然静默（中危）

`export_stream.count_messages`（export_stream.py:39-41）是上游唯一加了分片失败日志的地方，
但 **生产代码没有任何调用方**（仅测试引用）。实际导出链路用的是：

- `_collect_metadata`（exporter.py:104-141）：`except sqlite3.Error: pass` —— **坏分片的消息无声消失，
  采集数直接变少**，与 5.0.6 时期完全一样；
- `message_stream`（export_stream.py:67-74）：**连 try/except 都没有** —— 分片被锁/损坏时
  sqlite3.Error 直接炸掉整个导出（见 N2）。

**建议**：把 count_messages 的日志口径复制到 `_collect_metadata`，并给 `message_stream`
补一致的容错；同时清理或接回 count_messages。

### N2. `message_stream` 无分片容错，一个坏分片炸掉整个导出（中危）

`_collect_metadata`（元数据遍）遇到坏分片是"跳过继续"，`message_stream`（写出遍）却是
"异常上抛 → heapq.merge 处炸 → 整个会话导出失败"。同一个坏分片，两遍行为不一致：
用户看到的是导出中途报错，而进度/计数来自跳过了坏分片的第一遍，数字对不上。
另外 `sqlite3.connect(db)` 在 `shards_for` 预过滤后仍可能因文件锁/损坏失败。

**建议**：`message_stream` 的 connect/execute 包 try/except sqlite3.Error，记 logger.warn
后跳过该分片，与 `_collect_metadata` 对齐；两遍跳过的分片数若 >0 应在对账 warning 中体现。

### N3. E-2「导出所选」跨分片 localId 撞号会多导出错误消息（中危，仅 E-2 场景）

`only_local_ids` 过滤（export_stream.py:81、exporter.py:112）是"任意分片内 local_id 命中即产出"。
localId 是**分片内**序号，同一会话跨多分片时各分片独立编号——聊天页多选的某条消息 localId
可能在另一个分片里命中一条完全不同的消息，导致：

- 导出条数比所选多、混入错误消息（media 同理被挂上）；
- `_collect_metadata` 的 count 同步虚高，HTML meta messageCount 不准。

E-2 的文档注释只声明了"跨会话无意义"，未覆盖**同会话跨分片**这一层。
触发条件：所选会话的消息分布在多个分片、且撞号（大会话较容易命中）。

**建议**：E-2 请求带上所选消息的 (ts, localId) 对（聊天页手里都有），过滤条件改为
localId + ts 双重匹配；或 E-2 场景走单分片定位。

### N4. 导出 JSON 的 platformMessageId 恒为空串（低危）

`message_stream` 的 SELECT 取了 server_id（export_stream.py:71），但 `_shard_iter`
yield 时丢弃（export_stream.py:96-97），随后 row 固定填 `None`（export_stream.py:87）→
`platformMessageId: ""`。Web API 路径（api_chat.build_messages）是填的，两线不一致。
上游 v5.0.7 同样如此（非合并引入，但排查中确认）。

**建议**：`_shard_iter` 把 server_id 带进 row 元组即可。

### N5. 非 HTML 格式不携带引用/链接结构，引用消息在 TXT/CSV/MD/SQLite/XLSX 中丢上下文（低危）

问题 1 修复后，HTML 有 `.msg-quote` 引用块；但其余格式的 content 只有引用回复自身的
title 文本，"引用了谁、原文是什么"不在任何字段里（`quote` 结构只进 JSON/HTML）。
属既有设计缺口，修复引用渲染后被凸显。

**建议**：至少在 TXT/CSV 的 content 后追加 `[引用 <displayname>: <content 前 N 字>]`。

### N6. 小问题

- `heapq.merge` 按 (ts, local_id) 归并（export_stream.py:79），跨分片同秒消息按分片局部 id
  排序，与真实先后可能不符（纯展示层，无功能影响）；
- 导出页 `run()` 中排除规则过滤后 `chats` 为空时静默 return（export.js），无提示；
- `count_messages` 为死代码（同 N1）；
- 打包 zip 后 `export_result["file"] = None` 只在 pack="zip" 分支置空，`export_dir` 键在
  `after_zip` 插件回调中传 None——插件若假设 export_dir 存在需自行判空（上游引入，留意即可）。

---

## 三、结论

合并后导出链路主体健康：引用渲染、媒体错挂、CDATA、脱敏等 5.0.6 的主要问题均已收敛，
个人功能（E-2/E-4/E-8/E-9/E-11/S-3/F-2~F-4/J-1~J-4/V-2/V-3、排除公众号+正则+名单）全部保留，
全量测试 303 通过。当前最值得修的是 **N1+N2（分片容错不一致）** 和 **N3（E-2 跨分片撞号）**，
两者都源于"localId 只在分片内唯一"这一底层约束，建议一并做组合键/双重匹配改造。
