# SIWX 全量代码审查报告 v2（前端 + 导出链路 + 服务层）

> 基于 personal 分支 commit 5680959（上游 v5.0.7 合入后）静态分析。
> 未改动任何运行时代码。本报告分三部分：
> 一、对上一轮 N1~N6 的逐条验证结论；二、本轮全量审查新发现；三、根因归纳与修复优先级建议。

---

## 一、上一轮 N1~N6 验证结论：全部成立

| # | 结论 | 核验证据 |
|---|------|----------|
| N1 | ✅ 成立（中危） | `count_messages`（export_stream.py:27）全仓仅 tests/test_regressions.py:317 引用，生产零调用；真正在用的 `_collect_metadata` 在 exporter.py:138 仍是 `except sqlite3.Error: pass`，坏分片消息无声消失 |
| N2 | ✅ 成立（中危） | `message_stream`（export_stream.py:67-74）对 `sqlite3.connect`/`execute` 无任何容错；且异常发生在 `heapq.merge` 归并内部，整个导出上抛失败，而第一遍 `_collect_metadata` 是跳过继续——同一坏分片两遍行为不一致，`written != count` 对账 warning 虽会触发但用户看到的是整体失败而非缺片提示 |
| N3 | ✅ 成立（中危，仅 E-2） | 裸 local_id 过滤在 export_stream.py:81、exporter.py:112；前端证据链完整：export.js:404 `useIds = !!chatRange && ids.length>0 && selected.has(chatRange.chat)` → server.py:436 → 两遍扫描均按裸 local_id 匹配。同会话跨分片撞号时多导、错导、meta messageCount 虚高 |
| N4 | ✅ 成立（低危） | export_stream.py:96-97 把 SELECT 取出的 server_id 丢弃，:87 回填 `None`；api_chat.py:625 `platformMessageId: str(server_id or "")` 恒为空串。Web API 路径（api_chat.py:608,625）是填的，两线不一致 |
| N5 | ✅ 成立（低危） | `_KEEP_FIELDS`（export_stream.py:19）把 quote/link 保留进 JSON；HTML 有 `.msg-quote`；TXT/CSV/MD（及 TOML/SQLite/XLSX 的 content 字段）只有回复自身 title，无"引用了谁"上下文 |
| N6 | ✅ 全部成立 | heapq.merge 按 (ts, local_id) 归并（export_stream.py:79）；export.js:406 排除后 `chats` 为空时 `if (!chats.length) return;` 无任何提示；pack="zip" 时 `export_result["file"]=None`（exporter.py:458-459）；count_messages 死代码同 N1 |

---

## 二、本轮新发现

### P0

#### A-1. 自动更新链路构成远程代码执行入口（信任客户端 JSON + SHA 校验 fail-open + 无 Origin 校验）

完整链条，每环均已核实：

1. `/api/update/do`（api_update.py:31-40）接受请求体里的任意 `remote` JSON 直接传给 `run_update(remote)`；
2. `_asset_urls`（auto_update.py:131-143）把 `remote.assets.windows` 作为首选下载地址，**无域名白名单**（仅 GitHub 直链兜底）；
3. `_verify_sha256`（auto_update.py:178-179）`if not expected_sha: return True` —— 校验缺失即放行；
4. `_get_asset_sha`（auto_update.py:194-209）任何网络异常 `return ""`，与上条组合即"SHA 拉取失败 = 不校验"；且 SHA 文件本身也来自同一个不可信 `remote`；
5. `run_update`（auto_update.py:239）`if expected_sha and not _verify_sha256(...)` —— expected_sha 为空时整段校验被跳过；
6. 下载的 exe 最终经 `subprocess.Popen([str(target), "serve"])` 执行（auto_update.py:316）。

触发前提是服务无 CSRF/Origin/Host 校验（server.py:196 注释自己承认存在 DNS rebinding 风险 M-1 但未修），恶意网页可 POST `/api/update/do` + 伪造 remote。限制：`is_frozen()` 挡住源码运行模式，仅打包版受影响。

**建议**：`/do` 忽略请求体、服务端强制重新拉取版本信息；`expected_sha` 为空时拒绝更新（fail-closed）；SHA 源与下载源分离；给本地 API 加 Origin/Host 校验（一并消除 M-1）。

### P1（后端）

#### A-2. `/api/settings/clear` 的 `wxid` 无校验 → 路径穿越后 `shutil.rmtree` 任意目录

api_settings.py:140-144：`root / wxid` 直接拼接后整目录递归删除，`ignore_errors=True` 还吞掉部分失败。对比同模块 api_sns.py:31-42 已有 `_ACCOUNT_RE` 防护，这里是遗漏。

#### A-3. `/api/sns/emoji` 是未加白名单的任意 URL 抓取（可读 SSRF）

api_sns.py:269-274 未校验 URL 即调 `sns_cdn.fetch_emoji`；sns_cdn.py:559 直接 `fetch(plain)`。对比同文件 `/api/sns/media`（api_sns.py:422）明确有 `is_wechat_cdn()` 拒绝外链——同一类参数在 A 端点有校验、B 端点没有。响应体前若干字节会返回给调用方，可探测内网。

#### A-4. 导出 HTML 属性上下文 XSS：`esc()` 不转义引号，`link.url` 直拼 href

- html_template.py:323：`esc()` 用 `textContent→innerHTML` 实现，文本节点序列化只转义 `& < >`，**不转义 `"` `'`**；
- html_template.py:407：`'<a href="'+esc(url)+'" ...>'` —— url 取自消息 XML `<url>` 元素（`_parse_appmsg` 用正则解析，攻击者=发链接卡片的聊天对象可控），含 `"` 的 URL 可闭合属性注入事件处理器；`javascript:` 伪协议也无拦截。

正文/联系人名走元素内容上下文（安全），问题只在属性上下文。修复：`esc()` 补 `.replace(/"/g,'&quot;').replace(/'/g,'&#39;')`，URL 加 http/https 白名单。

### P1（前端）

#### A-5. `/api/job` 不返回 `error` 字段，三处前端把失败当成功

server.py:844-857 的 payload 只有 `running/done/ok/mode/pct/log_total/log_trimmed/report/logs`。而 guide.js:231、export.js:503、common.js 都在读 `job.error`（恒 undefined）：
- **guide.js:231 最危险**：密钥提取/解密失败时 `job.error` 为假 → 跳过错误徽章 → 直接执行 `gFinish.classList.remove('hidden')` 显示"完成"按钮，用户误以为成功进入聊天页；
- export.js:515 失败导出只显示"⚠️ 无导出结果"，真实报错藏在日志流里。

失败信息实际只在 `_run_job` 里追加成日志行 `[错误] ...`（server.py:456-468），从未结构化暴露。

#### A-6. `startJob` 轮询遇单次 fetch 失败即永久停止

common.js:71：`catch (e) { return; }` 不再调度 `setTimeout`。后果：guide 页 `setBusy(true)` 永不复位（按钮永久禁用）；chat.js:61-80 增量同步的 Promise 永不 resolve。一次后端重启/网络抖动即锁死。

### P2（正确性）

#### A-7. 导出 HTML 类型筛选逻辑反转：勾选任一类型 → 除系统消息外全部隐藏

html_template.py:521-522 存入 `activeTypes` 的是 `dataset.type`（**字符串**），:466 却用数值 `m.type` 去 `has()`，SameValueZero 永不相等 → `!has(t)` 恒真 → 消息被过滤。筛选功能完全不可用。修复：`activeTypes.has(String(t))`。

#### A-8. E-2 多选模式下 local_ids 被静默丢弃，实际导出与 UI 承诺不符

export.js:404 的 `useIds` 只要求范围会话在勾选集中，不要求**只**勾选了它。多会话时后端 `run_export_multi`（exporter.py:719）`ids = only_local_ids if (only_local_ids and len(chats)==1) else None` 静默忽略 → 导出全部历史，而 export.js:99 横幅仍写"将精确导出所选 N 条"。修复：`useIds` 加 `selected.size === 1`。

#### A-9. `voice.get_voice` 兜底查询无会话约束，可能返回别的会话的语音

voice.py:322-325：`svr_id=?`、`local_id=?` 等不带 chat 约束的条件作为兜底；voice.py:339-348 外层按 media_*.db 逐个轮询、内层先到先得——当前会话在某个 db 里查不到（cid=None）时会用裸 local_id 命中**其它会话**的语音并直接 return。与 N3 同根：local_id 只在会话/分片内唯一。

#### A-10. `extract_md5_from_xml` 无左边界，误匹配 `cdn*md5` 属性 → 图片静默降级缩略图

media.py:502：`re.search(r'md5\s*=\s*["\']([0-9a-fA-F]{32})["\']')` 会在 `cdnthumbmd5="…"` 的子串处命中。CDN md5 若先于真实 md5 出现，attach 原图目录与 hardlink 两级来源全部 miss，导出变缩略图且因"成功"而无日志。修复：`(?<![a-zA-Z0-9_])md5\s*=`。

#### A-11. `get_image` 内存缓存键缺 `ts`（media.py:385）

`f"{account}:{chat}:{local_id}:{md5}:{bubble_md5}:{hq}"` —— 跨分片撞号且 md5/bubble_md5 均为 None 时，第二条消息直接命中第一条的缓存图。与 N3 同根（见第三节）。

#### A-12. `media_key.json` 并发写竞态：固定 `.tmp` 文件名

media.py:71：tmp 无 pid/uuid 后缀；Flask 多线程 + 导出多进程 Pool（exporter.py:200-205 的 8 个子进程各自 `_remember_key`）同时写同一文件。Windows 上 `os.replace` 撞车抛 `PermissionError`，异常沿 `_remember_key → get_image` 上抛，被 `_try_decrypt` 捕获后**已成功解密的图片被记为失败**。修复：tmp 加 pid，或整体 try/except 吞掉（key 缓存失败不应影响导出结果）。

#### A-13. stats.js 数字滚动动画破坏非纯数字卡片

stats.js:319-327 对所有 `.st-card .v` 执行 `raw.replace(/[^\d.]/g,'')` 后 countUp 回写："最活跃时段" `"14:00"` → 解析 1400 → 定格为 **"1,400"**；"时间跨度" `"123 天"` → 变 **"123"** 单位丢失。

#### A-14. export.js 导出进度轮询两处缺陷

- export.js:449-486：`catch` 无限吞错且无超时，后端中途崩溃后 `running` 恒 true，"开始导出"永久禁用；
- 未校验 `job.mode`：全局单槽任务（`/api/run`），导出中若在聊天页触发增量同步，导出页会把 sync 的完成当导出完成，渲染出"⚠️ 无导出结果"（sns.js:735 反而有 `rep.kind==='sns_export'` 校验，export.js 缺对称处理）。

#### A-15. `/api/run` 通道的 `account`/`db_dir`/`out_dir` 完全未校验

server.py:397（export）、246-247（sns_export，绕过 api_sns.py:346 入口处的正则校验）、296、337。`account="../.."` 可越出 out_root；`db_dir` 可指定任意目录触发密钥提取/内存扫描。

#### A-16. 前端零散正确性问题

- chat.js:157-169：滚动加载无 try/finally，一次请求失败 `loading` 永久 true，向上翻页死掉；
- export.js:526：`onclick="SX.openPath('${esc(r.total_dir)}')"` —— `esc` 不处理反斜杠，Windows 路径经内联 JS 转义后 `\t`→TAB 等，"打开目录"对含这些序列的路径必然失败；应改 data-* 属性 + addEventListener；
- chat.js:34-38：无账号时 init 提前 return，整页事件从未绑定，完成引导后回到聊天页全部无响应（须手动刷新）；
- stats.js/sns.js：busy 守卫"丢新保旧"，切换账号后展示旧账号数据（请求代数缺失；chat.js 的 loadSeq 是对的，这两页没抄）。

### P3（择要）

| # | 位置 | 问题 |
|---|------|------|
| 1 | api_chat.py:298-301 | `_decode_content` 遇 UnicodeDecodeError 整条消息置空；应 `errors="replace"` |
| 2 | exporter.py:118-122 | `_collect_metadata` 的 first_ts/last_ts 按分片遍历顺序取首尾，分片间无时序保证时 HTML dateRange/TXT 头部时间跨度会错；应 min/max（第二遍 heapq.merge 是全局有序的，两遍口径不一致） |
| 3 | api_chat.py:518-519 | `_parse_refer` 图片引用判定失效：refermsg 的 `<type>` 是元素形态，正则 `type=\"?3\"?` 永不命中 → 整段 `<msg><img ...>` XML 漏进 quote.content（不可读，esc 过无 XSS） |
| 4 | media.py:368-371 | `_decrypt_any` 对 0 字节 .dat 抛 IndexError（`data[0]`），Web 端 500 |
| 5 | api_chat.py:1021 | `bubble_md5` 查询参数未校验（md5 有 len==32 校验，它没有）直接进 glob 模式 |
| 6 | logger.py:64-107 | `_cprint` 无条件 print；MCP 进程走 stdio JSON-RPC，任何人将来在 MCP 路径调一次 `logger.info` 就破坏协议流（结构性隐患） |
| 7 | server.py:690-731 vs export_logs | 日志"查看"tail siwx.log 文件，"导出"只导进程内 `_FILE_LOG`（重启清零）——日志页看到的记录导不出来 |
| 8 | stats.py:558-567 | 统计磁盘缓存写入非原子（无 tmp+replace），并发可损坏缓存致每次全量重扫 |
| 9 | server.py:880 | sns_export 模式不可取消，且 `_job` 复位缺 `pct/log_trimmed`（api_sns.py:380），前一个导出任务裁剪过的日志计数会串到朋友圈导出 |
| 10 | paths.py:99 | 非 Windows 回退落到 `.`（cwd），与模块"绝不使用 cwd"的设计声明矛盾，macOS 双击启动时 cwd 为 `/` |
| 11 | mcp_server.py:104-171 | MCP 工具的 `account` 未校验，与同文件 `_sns_db` 的正则防护不一致 |
| 12 | auto_update.py:334-352 | `_mount_dmg` 失败路径泄漏 `siwx_mount_*` 临时目录 |
| 13 | api_sns.py:221-223 | `has_more: len(rows)>=limit` 口径过宽，恰好整页时多一次空请求 |
| 14 | html_template.py:324-326 vs export_stream | HTML 用浏览器本地时区、TXT/CSV/MD 用服务器本地时区，HTML 发到别的时区打开与同包 CSV 对不上（建议 HTML 头部标注导出时区） |
| 15 | exporter.py:337-338 | 导出中途异常时 export_dir 残留且出现在 /api/export/list，无清理；取消仅在会话边界生效，单会话内图片并行解密一旦开跑不可中断 |
| 16 | logs.js:27-57 | "清屏"后 1 秒内环形缓冲原样画回，按钮实际无效 |

### 审查确认无问题的重点项

- 静态文件路由（/pages、/vendor、/plugin-pages）路径遍历防护完整（safe_join + resolve().relative_to 双重校验）；
- 默认绑定 127.0.0.1，`0.0.0.0` 需显式传参；
- 导出并发由 `/api/run` 全局单槽互斥，重复点导出得 409；进度回调经 `_lock` 线程安全；
- html_template 的 `_script_safe_json`（html_template.py:77-86）同时处理 `</` 与 `<!--`，CHAT_DATA 注入无 `</script>` 逃逸；
- `_enrich_row` 除 N4 外字段契约（quote/link/md5/bubbleMd5/voice/typeName/rawContent）三端一致；
- 前端 XSS 面整体收口好：`esc` 补齐引号、renderNodes 白名单、safeUrl/safeStyle、聊天页 loadSeq 请求代数；
- 插件契约（contract.py/report.py）hook 隔离、first-wins，整体健壮；
- subprocess 全部列表参数无 shell=True；日志脱敏 `/api/logs` 出口覆盖完整。

---

## 三、根因归纳与修复优先级建议

三个系统性根因，建议按主题修而非逐点打补丁：

1. **localId 只在分片/会话内唯一** —— N1/N2/N3、A-9（voice 兜底）、A-11（图片缓存键）、既有 `_attach_media` 残留，全部同一根源。一次「(shard 标识或 ts, local_id) 组合键」改造可同时解决六处；N1+N2 顺带把 `message_stream` 容错与 `_collect_metadata` 对齐、接回或删除 count_messages 即可（原报告建议成立，且本轮 A-9/A-11 强化了"组合键值得做"的判断）。
2. **信任边界不一致** —— A-1（update/do 的 remote）、A-2（clear 的 wxid）、A-3（emoji 的 URL）、A-15（/api/run 的 account/db_dir）全是"同类参数在别的端点有校验、这个入口漏了"。抽 `validate_account()` 与本地请求 Origin/Host 断言两个基础设施即可消除惯性，其中 A-1+M-1 组合是全应用唯一 RCE 入口，最优先。
3. **失败路径契约缺失** —— A-5（/api/job 无 error，guide 页失败仍显示"完成"）、A-6（轮询零容错锁死按钮）、A-14（导出轮询同病）、A-1 里的 SHA fail-open。统一"任务失败必须结构化可见"的约定。

修复顺序建议：A-1 → A-2/A-3/A-4 → A-5/A-6 → N1+N2+N3+A-9+A-11 组合键改造 → A-7/A-8/A-10/A-12/A-13 → P3 择要。

---

## 四、v2.1 复核订正（2026-10-03，逐条回源码验证后并入）

> 对 v2 全部断言的二次复核结论：P0/P1/P2（A-1~A-16）16 条全部成立，其中 5 条机制或影响
> 需按本节订正；N1~N6 中 5 条成立、N6 含 1 项误判；"确认无问题"清单 9 条中 4 条不实或过头；
> 另确认 4 个 v2 漏项。以下订正均已亲自回代码核实行号。

### 4.1 结论订正

| 项 | 订正 |
|----|------|
| N2 | 机制描述修正：`sqlite3.connect` 在两遍扫描中都位于 try 之外（exporter.py:104-105、export_stream.py:68），connect 失败两遍都炸；"跳过继续 vs 上抛"的不一致仅对 execute/游标中途错误成立 |
| N5 | 证据链修正：quote/link 进 JSON 与 `_KEEP_FIELDS` 无关——该常量全仓零引用，是死代码。quote 进 JSON 是因为 `_enrich_row` 的 export 形状直接含它 |
| N6 第3项 | **撤回**：pack="zip" 置 `file=None` 是上游有意设计（exporter.py:456-458 注释防死链，export.js 已按空值不渲染链接），从待修清单划掉，动它反而制造死链 |
| A-1 | 触发机制修正：跨站 POST `application/json` 会触发 CORS 预检被挡，恶意网页需经 DNS rebinding 或本机进程才可达。P0 定性不变（fail-open 与信任客户端 remote 独立成立） |
| A-2 | 影响上调：Windows 上 `wxid="C:/x"` 这类绝对路径经 `root / wxid` 直接整体替换，无需穿越即可删任意目录 |
| A-3 | **降级 P1→P2**：`detect_mime` 魔数不识别就不返回响应体（sns_cdn.py:562-564），可读面仅限命中图片魔数的载荷——实际是异常/耗时盲打探测口，非可读 SSRF |
| A-9 | 影响上调：`get_voice` 由 Web 播放端点（api_chat.py:986）与导出（exporter.py:290）共用，串音两处皆中 |
| A-16② | 影响上调：JS 字符串未知转义丢反斜杠，任何含 `\` 的 Windows 绝对路径经内联 onclick 都失真，非仅含 `\t \n \u` 等序列者 |
| A-16③ | **撤回**：app.js:169-171 每次导航都重跑 `mod.init(view)`，引导完成后重进聊天页即正常绑定，"必须手动刷新"不成立 |
| P3#6 | **升级 P3→P2**：MCP 导出工具直接调 `exporter.run_export`（mcp_server.py:254/261），`logger.warn` 无条件 `_cprint`（logger.py:99-103）→ exporter.py:174/428 的 warning 已在污染 MCP stdio JSON-RPC 帧，既成事实而非假设 |
| P3#2 | 范围修正：时间跨度错乱只影响 JSON/HTML/SQLite 的 meta，TXT 头部（export_stream.py:149-153）不打印 first/last ts |
| P3#8 | 措辞降级："每次全量重扫"过头——缓存损坏偶发且可自愈 |

### 4.2 "确认无问题"清单勘误

- C5 撤回：frontend 形状（api_chat.py:605-621）无 rawContent/typeName，"三端字段一致（除 N4）"不成立；
- C1 依据不实：显式 resolve 校验仅在 /vendor（server.py:569-579）与 /plugin-pages（582-596）；/pages（563-566）纯靠 send_from_directory 兜底，安全结论不变；
- C7 部分撤回：插件 writer 异常在 exporter.py:522-524 是 log 后**重抛**（整次导出失败），registry.py:12-13 宣称的隔离在该调用点不成立；first-wins 碰撞守卫未覆盖 renderers/avatar_resolvers（此条待验）；
- C9 补充：`_PATH_RE`（logger.py:157）漏 UNC 路径；`?desensitize=0`（server.py:827）在同一无鉴权 socket 直供原文。

### 4.3 v2 漏项（本轮确认并入）

1. **`_KEEP_FIELDS` 死代码 → JSON 体积翻倍**：export 形状含 rawContent（api_chat.py:629），`stream_export_json` 原样 dumps，模块头宣称的"去掉 rawContent 重复字段减 50% 内存"未实现。与 N1/N5 同源，进根因 1 一起修。
2. **api_chat.py 七个端点的 account 同样零校验**（如 :974→:986 `_out_root() / account` 仅判非空）——A-15/根因 2 的面比 v2 所写更宽。
3. **sns_cdn.py:225-228 关闭 TLS 证书校验**（`check_hostname=False` + `CERT_NONE`），且被 /api/sns/media 正常链路共用。v2 未提。
4. **chat.js:69-77 同步失败静默**：/api/job 无 error 字段的第四个落点（409/超时是 startJob 本地合成故正常，真实任务失败无提示）。

### 4.4 修订后的修复优先级

A-1（fail-closed + 服务端取数 + Origin/Host 校验）→ A-2/A-4 → A-5/A-6（含 chat.js 第四落点）→ **P2#6 MCP stdio 污染** → N1+N2+N3+A-9+A-11+漏项1 组合键改造 → A-7/A-8/A-10/A-12/A-13/漏项2/漏项3 → P3 择要（A-3 已降入此档）。
