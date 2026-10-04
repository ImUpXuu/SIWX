# docs/debug-logging-audit-2026-10-04.md 核查报告（Kimi 会话存档）

> 来源：Kimi Code 会话 session_3c1913c0（2026-10-03），用户要求逐节核查审计文档与代码的一致性，不改代码。
> 该会话的 7 个核查子代理全部完成并返回报告，但主代理在汇总前因用量限额中断，未输出最终结论。
> 本文件为 7 份子代理报告的完整原样存档，行号均为核查时的实际代码行号。
> 总体结论：文档质量高，绝大多数断言属实、行号零偏移或 ±1；未发现"声称静默但实际已有日志"的漏判，唯一例外见 agent-6 报告 B 节（sns_export "成功无记录"）。问题集中在若干事实性错误与建议代码的可执行性。

===== agent-0 =====
# 核查报告：docs/debug-logging-audit-2026-10-04.md 断言核对

核查范围：§1、§2.3、§2.4、§2.5、§5.1、§5.4 及 §0 摘要中与本批文件相关的断言。所有行号均已与当前工作区代码逐一比对。

## A. 确认属实的断言

**§1 架构盘点** — 全部属实：
- logger.py 环形缓冲 2000（ROUGH）/5000（DETAILED）（`logger.py:30,40`）、文件缓冲 5 万条（`logger.py:32`）、毫秒时间戳、脱敏规则（密钥/wxid/gh_/chatroom/路径/账号键值对，`logger.py:148-181`）——全部精确命中。
- `server.py:24-44` "siwx" logger → `logs/siwx.log`，RotatingFileHandler 10MB×5 —— 精确命中（`server.py:32-33`）。
- `server.py:59-87` 崩溃钩子（sys/threading excepthook + faulthandler → crash.log）—— 精确命中。
- `server.py:198-223` Flask 全局 errorhandler —— 精确命中。
- `api_chat.py:597-606` `_safe_parse_refer`：异常兜底 + `detailed("parse", ... raw={str(text)[:300]!r})` —— 精确命中（`api_chat.py:603-605`）。
- `sns_cdn.py` 正面范本：失败 → `_log_media_failure` 持久文件日志（`sns_cdn.py:397-406`），成功 → `detailed("sns", ...)`（`sns_cdn.py:414-419`）—— 属实。
- `sns_export.py:294-323`：失败明细前 30 条带 tid/idx/reason 的 warning（`:310-315`）+ 聚合 warning（`:321-323`）—— 属实。
- §0 摘要"`detailed()` 全项目仅 5 处"—— 经全库 grep 确认恰为 5 处：extract.py:43,47、api_chat.py:603、sns_cdn.py:416、plugins/loader.py:201，与文档列举完全一致。

**§2.3 Debug 开关** — 全部属实：
- 唯一入口 `server.py:767-776`（POST /api/logs/settings）—— 精确命中。
- `logger.set_level` 在 `logger.py:35-41`，仅改内存；默认 ROUGH 在 `logger.py:28` —— 精确命中。
- `cli.py:160-213` argparse 无 `--verbose/--debug/--log-level` —— 属实。
- tui.py 无级别概念 —— 属实。
- 无环境变量支持 —— grep `os.environ|getenv` 于 server/logger/cli/tui/run.py 均无命中，属实。
- `server.py:28` 启动把 "siwx" logger 置 DEBUG —— 精确命中。
- CLI 不装崩溃钩子 —— 属实：全库 excepthook/faulthandler 仅 `server.py` 一处；`cli.py:119` 仅在 `cmd_serve` 内 import server，`keys/decrypt/auto/mcp/doctor` 模式均无 crash.log。

**§2.4 脱敏旁路** — 全部属实：
- `mcp_server.py:669` `_tool_call` 记录 args（`json.dumps(...)[:500]`）+ 耗时（`:670,685`）—— 精确命中。
- mcp.log 不过 `desensitize_msg` —— 属实：mcp_server.py 全文件无 desensitize 调用，mcp_log 是独立的 RotatingFileHandler（`mcp_server.py:43-63`），args 中 keyword/account/chat 明文落盘。
- `chat_bridge.py:44,87,112,132` 四处 `log.warn("plugin", f"...: {e}")` 直接内插异常文本 —— 四个行号全部精确命中。
- `report.py:62` 用 `log.desensitize_msg(...)` —— 精确命中，是正确写法。
- `macos_lldb.py:299-317`：OK: 行（`:300-301`）的 `line[3:].strip()` 即 passphrase —— 属实（当前代码对 OK 行是 return 而非 log，但作为"埋点时必须剔除"的预防性提示是准确的）。

**§2.5** — 属实：`server.py:880-882` werkzeug handlers 清空 + propagate=False + disabled=True，无替代 access log —— 精确命中。

**§5.1 server.py** — 行号全部精确命中：
- `:83-84` faulthandler 启用失败静默（`except Exception: _CRASH_FH = None`）—— 属实（try 起于 :79）。
- `:611-614` /api/status 内层 `except ValueError: pass`（:611-612）+ 外层 `except Exception: pass`（:613-614）—— 属实。
- `:632-644` /api/run：`get_json(silent=True)`（:634）、409 拒绝（:636-637）无日志 —— 属实。
- `:687-688,715-716` 两个 tail 函数读失败 `return []` —— 属实。
- `:908-909` TUI 状态栏 getter `except Exception: pass` —— 属实；配合 tui.py 循环每秒一次（`tui.py:172` sleep 1），"每秒吞一次"属实。
- 任务状态机：finally 块（`server.py:460-463`）置 done 无日志，409 无日志 —— 属实。

**§5.4 CLI/TUI** — 全部属实：
- `cli.py:154-218`：`main()` 的 try 仅捕 KeyboardInterrupt（`:214-218`）—— 属实。
- `cli.py:231-235` 插件 CLI 注册（ensure_loaded）整体失败静默 return —— 精确命中。
- `cli.py:290-293` 插件命令异常只记 `str(e)` 无堆栈 —— 精确命中。
- `tui.py:161-165` `_bar` 每秒吞异常无计数无日志 —— 精确命中。
- `tui.py:148-154` SetConsoleMode 纯体验项、明确排除埋点 —— 精确命中。

## B. 不准确/有瑕疵的断言

1. **§5.1 `server.py:472-475,498-501`「插件模块导入失败静默，任务事件不广播」— 描述不准确（轻度夸大/表述偏差）**。
   实际两处都是 `from siwx.plugins import registry` 失败（即 **siwx.plugins 包/注册表本身**导入失败，如插件系统损坏），而非"插件模块导入失败"（单个插件 import 失败的日志在 plugins/loader.py 另有处理）。且"任务事件不广播"只适用于 `:472-475`（`_emit_task_event`）；`:498-501`（`_plugin_theme_links`）影响的是主题 CSS 注入，与任务事件无关。建议文档把两处拆开描述，并修正为"插件系统注册表导入失败"。

2. **§5.1 `server.py:447-463` 行区间与断言粒度有轻微错位 — 行号偏移（无害）**。
   文档用 447-463 指"任务状态机转换无日志"，但该区间内的 **except 分支（:447-459）任务失败时是有日志的**（`server.py:448` `_siwx_logger.exception("任务执行失败")`）。无日志的仅是状态转换点（run() 内置 running 在 `:636-639`、finally 置 done 在 `:460-463`）和 409 拒绝。断言结论正确，但若实施者按"447-463 全无日志"理解会误读——该区间的失败路径已经是全项目日志较好的位置。

3. **§2.3「server.py:28 启动时把 'siwx' logger 恢复为 DEBUG」— 措辞小疵**。
   `:28` 是无条件 `setLevel(DEBUG)`（且 `_setup_file_logger` 有 handlers 去重），首次启动并非"恢复"。不影响结论。

4. **§0 摘要 "`log.detailed()` 全项目仅 5 处"的完整性 — 需补充说明**。
   grep `\.detailed\(` 确认恰好 5 处、列举无误；但 server.py 任务轨 `_log()`（`server.py:226-237`）和 api_chat.py 的 `_log(f"[msg] ...")`/`_log(f"[sessions] ...")` 走的是标准 logging 通道（不受 Debug 开关控制），文档 §2.1 双轨割裂的定性成立，不算错误。

## C. 文档建议中可优化之处

1. **§2.3 改法 1（开关持久化到 settings.json）有架构冲突风险，文档未提示**。
   `POST /api/logs/settings`（`server.py:773-775`）目前同步了 `logging.getLogger("siwx")` 的级别（detailed→DEBUG / rough→INFO），即"siwx.log 文件日志的级别"与自研 ring 的级别已联动。若实施者只按文档做"写 settings.json + 启动读回 + 同步 setLevel"，方向正确；但文档建议把 `:774-775` 逻辑"抽成公共函数两处共用"——实际该逻辑只有一处调用点（api_log_settings_save），"两处共用"指的是启动路径也要调，表述易误导成现有重复代码。另需提醒：启动时读回持久化级别必须在 `_setup_file_logger()`（`server.py:46` 模块导入时即执行）**之后**执行，否则 setLevel 会被 :28 的 DEBUG 覆盖回去——这个时序陷阱文档未提及。

2. **§2.3 改法 3（崩溃钩子抽到 logger.py）与 logger.py 现状有耦合，未完全确认无坑**。
   `_install_crash_hooks` 依赖 `_paths.app_root()`、`_siwx_logger`（标准 logging）、`_flush_logs`，而 `logger.py`（自研结构化轨）目前完全不依赖 paths/logging。若抽进 logger.py，会引入新的模块依赖方向（logger→paths/logging），与 logger.py 当前"零依赖纯缓冲"的设计冲突。更稳妥的是按文档第二选项"独立模块"。文档把它列为第一推荐（"抽到 siwx/logger.py 或独立模块"）但未讨论该耦合，属于建议本身的瑕疵。

3. **§2.4 对 chat_bridge.py 的建议（统一包 `desensitize_msg`）方向正确，但有一处遗漏**。
   文档建议把 `chat_bridge.py:44,87,112,132` 四处 warn 改为 `log.warn("plugin", log.desensitize_msg(f"...: {e}"))`。可行，但 `logger.py` 的 `_PATH_RE` 会把 Windows 绝对路径压成文件名、`{e}` 中常见的文件路径因此已部分脱敏；真正漏网的是 `{e}` 里可能携带的消息正文片段（来自插件自己的异常文本），这一点文档 §7 规范 4 有覆盖，建议实施时以规范 4 为准而非仅机械包 desensitize_msg（desensitize_msg 对正文片段无能为力）。此为建议粒度问题，非错误。

4. **§5.1 建议 `error("server", f"读取 siwx.log 失败: {e}")` 对 `_tail_app_log` 有递归风险，文档已自行提示但方案不完整**。
   文档注明"勿自写自递归，可用 print 或一次性标志"——提示到位。但 `_tail_mcp_log` 失败不存在该递归问题（mcp.log 与 siwx.log 是不同文件），两处用同一方案即可，无需为 mcp 侧做特殊处理；文档未区分，实施时照抄可能过度设计。轻微。

5. **§5.1 任务状态机建议缺 auto-sync 路径**。
   置 `_job["running"]=True` 的入口其实有**三处**：`/api/run`（`server.py:638`）、auto-sync 调度（`server.py:840-841`）、以及 `_run_job` 的 finally（`:461-463`）。文档只提了前两类的代表（/api/run 与 finally），auto-sync 的转换（`:840`）和它的 409 等价分支（`:838-839` 已有任务时 continue）未列入清单。属遗漏，不算错误。

## "声称静默但实际已有日志"的漏判排查

重点核查了文档声称静默/无日志的位置，**未发现实质性漏判**（即没有"文档说完全无日志、实际已有该场景日志"的硬错误）：

- `server.py:447-463` 任务失败路径已有 `exception` 日志（`:448`），但文档的断言限定在"状态转换"和"409"，不矛盾（见 B.2）。
- 反向验证 §5.2 末尾"`/sessions` 与 `/messages` 已有入口日志、值得肯定"：属实且行号精确——`_log(f"[sessions] 账号=...")` 在 `api_chat.py:513`，`_log(f"[msg] 查询消息: ...")` 在 `api_chat.py:803`（另 `:829,837,899` 还有分片级日志）。这条是"反向漏判"的正面确认：文档没有把这些误报为静默。
- auto-sync 调度循环的失败是有日志的（`server.py:853` `_siwx_logger.exception`）、启动也有 `_log`（`:842`）——文档未声称此处静默，无冲突。

**总结**：本批次约 30 余条断言中，28 条完全属实（含全部行号级引用，精确度很高）；2 条有描述性瑕疵（B.1 插件导入失败的对象描述错误、B.2 区间内含已有日志的失败分支）；2 条为措辞小疵（B.3/B.4 不影响结论）；文档修改建议整体合理，主要优化点是 C.1 的持久化时序、C.2 的模块依赖方向、C.5 的 auto-sync 遗漏。

===== agent-1 =====
核对完毕。以下是逐条核查报告。

# 核查报告：docs/debug-logging-audit-2026-10-04.md §2.1 / §3.1 / §3.2 / §3.3

## A. 确认属实的断言

以下断言与代码完全一致（行号全部对得上，事实描述准确）：

**§2.1 双轨日志割裂**
- extract.py:108 `_d = lambda m: log(f"[extract] {m}") if log else None` 名义 detailed 实际任务轨——属实（`siwx/extract.py:108`）。
- config_cipher / mmkv / memscan / macos_lldb / keystore_source 均用 `ctx["log"]` 回调（`siwx/strategies/config_cipher.py:214`、`mmkv.py:56`、`memscan.py:17`、`macos_lldb.py:34`、`keystore_source.py:23`）——属实。
- serve 模式下该回调是 `server._log`（`siwx/server.py:226-237`），确实同时进 `_job["logs"]` 和 siwx.log（经 `_siwx_logger.info`）；CLI 模式为默认 `print`——属实。
- "所有策略日志的消息格式已天然脱敏（salt 前 16 位 + key 前 8 位）"——抽查属实（`config_cipher.py:116`、`macos_lldb.py:89`、`memscan.py:41,48`、`mmkv.py:128` 均为 `salt[:16]` / `key_hex[:8]` 打码）。

**§3.1 extract.py（438 行，行数准确）**
- 62-67 `_keystore_preset` 吞 `parse_key` ValueError（`except ValueError: pass` 在 66-67）——属实。
- 87-88 `global_harvest` 空 `page1_by_salt` 静默 `return {}, {}`——属实，无日志。
- 97-99 收割 0 命中无输出（`if key_map:` 才 log）——属实。
- 108/119/131/139/144/153 `_d` 埋点全部走任务轨——属实。
- 162-164 交叉验证 `parse_key` 失败 `continue` 无日志——属实。
- 174-179 `keystore.save` 无 try——属实（`keystore.save(store)` 在 178 行裸调）。
- 241-244 `Path.resolve()` 失败静默降级 casefold 原始路径——属实。
- 255-271 `_resolve_key`：262-263 吞 ValueError；"keystore 有记录但 HMAC 不过"无任何日志（整体返回 None 时调用方 276 行只打"无密钥"，无法区分无记录/损坏/HMAC 不过）——属实。
- 281-284 `stat()` 失败静默 `mtime=0`（缓存永不命中）——属实。
- 327-329 解密成功后 manifest 记录里的二次 `e.path.stat()` 无保护——属实；异常会跳出结果循环、跳过 340 行 `save_manifest`，"已成功的任务被炸掉、缓存全丢"的后果推断成立。
- 364/400 `collect_db_files` 在字典推导里裸调，单账号损坏全灭——属实。
- 371-379 与 409-417 两段覆盖判定代码重复且都吞 ValueError——属实（复制粘贴确认）。
- 432-436 `rep["verified"] > 0` 才解密，否则 `dec=None` 静默——属实（extract.py 自身无日志；server.py:370 有聚合计数，但那不在本文件断言范围内，不算漏判）。

**§3.2 pool.py（82 行，准确）**
- 36-38 `load_manifest` 吞一切 `Exception` 返回 `{}`——属实。
- 41-47 `save_manifest` 无异常处理——属实。
- 54-58 `_worker` 只返回 `str(e)`，异常类型丢失——属实。
- 63-82 `decrypt_parallel` 无任何起止/并发日志——属实（整个文件零 logging）。
- 77 `mp.Pool(n)` 裸抛——属实。

**§3.3 sqlcipher.py（201 行，准确）**
- 75-76 page1 临时复制分支吞 OSError 返回 None——属实。
- 80-81 page1 过短/全零 return None——属实。
- 96-101 `stat()` 失败 `continue` / `size < PAGE_SZ` 静默跳过——属实。
- 145-146 page1 HMAC 失败 `ValueError("page1 HMAC 验证失败（密钥不匹配）")` 无 src/salt/key 上下文——属实。
- 159 输出临时文件 `open` 无保护——属实（虽然 154 行 mkstemp 刚建过该文件，实际触发概率极低，但事实断言没错）。
- 196-201 finally 中 `tmp_out.unlink` 失败会掩盖原始解密异常——属实。

**"静默/无日志"漏判检查**：以上所有被标"静默/无日志"的位置，我逐一复核，确认均无日志——未发现漏判。

## B. 不准确/错误的断言

仅发现 1 处事实性偏差（轻微）+ 若干建议代码本身的问题（归入 C）：

1. **§3.3（135 行）"打开源库失败裸 raise 原始 OSError"** —— 描述不够精确。
   - 文档说法：124-138 打开源库失败"裸 raise 原始 OSError"。
   - 实际：`sqlcipher.py:124-125` 首次 `open(src)` 失败后进入临时复制兜底；138 行的裸 `raise` 位于**内层** `except OSError`（130-138）中，重抛的是**复制或打开临时文件**时捕获的异常，不一定是"原始"的 `open(src)` OSError。
   - 错误类型：描述轻微夸大/不精确（不影响"无 src 路径上下文"这一核心结论，也不影响建议方向）。

其余所有行号、行为描述、后果推断均与代码一致，无事实错误、无行号偏移。

## C. 文档建议中可优化之处

1. **§3.1（107 行）建议代码有变量遮蔽 bug**：`detailed("keystore", f"密钥库记录解析失败 salt={e.salt_hex[:16]}…")`——在 `except ValueError:` 块内 `e` 是 ValueError 异常对象，`e.salt_hex` 会抛 AttributeError。应改用条目变量名（如 `en.salt_hex`）。按原文照抄会引入新 bug。

2. **§2.1 模式 A 低估了迁移成本（"几乎为零"不准确）**：`extract.py:13` 已 `from siwx import logger as log`，但 `extract_keys_for_dir` / `global_harvest` / `_keystore_preset` / `decrypt_dir` / `extract_all` / `auto_all` 的形参全部叫 `log`，**在函数体内模块级 logger 被参数遮蔽**，`log.detailed(...)` 在里面根本不可调用。`_d` 改双写前必须先重命名导入（如 `logger as _slog`）或改形参名，波及全部签名和 server.py 调用点——不是零成本。

3. **§3.3 内部自相矛盾**：小节开头声明"纯原语层，可保持无 logger 依赖"，但 133/134/137 行的建议分别在 `collect_db_files`（就在 sqlcipher.py 内）和输出文件创建处直接记 `detailed/error` 日志，等于在该层引入 logger。要么把 `collect_db_files` 移出 sqlcipher.py，要么放弃"无 logger 依赖"的立场，二选一。

4. **§3.3（136 行）`mask_key` 不可直接用**：`mask_key` 定义在 `extract.py:28`，而 `extract.py` 导入 `sqlcipher`——sqlcipher.py 反向 import 会循环。需要把 `mask_key` 下沉到公共模块（如 sqlcipher.py 或 logger 旁）再引用。建议未提及这一点。

5. **§3.3（138 行）建议覆盖面不全**：只提议给 `tmp_out.unlink`（200-201）包 try，但同 finally 块里 `tmp_copy.unlink`（198-199）有同样的"掩盖原始异常"风险，应一并处理。

6. **§3.3（135 行）建议的错误消息会误导**：`raise OSError(f"打开 {src} 失败: {e}") from e` 中的 `e` 在复制失败场景下是 copy2 的异常，消息却写成"打开 src 失败"。建议区分两段失败原因。

7. **§3.2（124 行）traceback 建议缺少落地路径**：`_worker` 返回串带 traceback 后，会经 `_on_done`（extract.py:317）整串进任务面板日志——长堆栈刷屏。文档说"由主进程写 detailed"，但没有说明 `_on_done` / `_worker` 的返回协议要同步改（如返回 5 元组区分 short_err/long_tb），否则做不到"只在结构化轨里"。

8. **小遗漏（非错误）**：§3.1（109 行）建议"附扫描耗时"，但 `global_harvest` 目前没有计时，需新增 `t0`——建议本身合理，只是不是纯埋点。

## 总结

该审计文档质量很高：§3.1/§3.2/§3.3 的全部行号引用**零偏移**，"静默/无日志"断言**无一漏判**（被点名处确实都没有日志）。核心事实框架（双轨割裂、吞异常清单）完全成立。主要问题集中在**建议代码的可执行性**：一处变量遮蔽 bug（§3.1-107）、`log` 参数遮蔽导致的迁移成本低估（§2.1）、`mask_key` 循环导入（§3.3-136）、以及 sqlcipher.py"无 logger"立场与具体建议的自相矛盾（§3.3）。这些建议按原样实施时会踩坑，需要先解决命名/归属问题。唯一的事实性偏差是 §3.3-135 对"裸 raise 原始 OSError"的轻微夸大（重抛的可能是复制失败的异常而非原始 open 失败）。

===== agent-2 =====
核对完成。以下为逐条核查报告。

# 核查报告：docs/debug-logging-audit-2026-10-04.md §3.4 / §3.5 断言验证

## A. 确认属实的断言

**strategies/__init__.py**
- `:29-34` 策略链循环（早停 break、use_memory 跳过 continue）无任何日志 — 属实（代码 29-34 行，循环体内确实零日志）。
- `:35-38` 内置策略异常只进 `ctx["log"]` 单行、无堆栈 — 属实（38 行单行 `f"... 异常: {e}"`）。
- `:49-54` 插件加载 `except Exception: return` 彻底静默 — 属实（53-54 行）。

**config_cipher.py**
- 全文件走 `ctx["log"]` — 属实（214 行 `log = ctx["log"]`）。且我追查了 `ctx["log"]` 来源：server 路径走 `_siwx_logger.info` + 任务日志，不经过 `logger.py` 的 `detailed()` 门控，故文档"Debug 开关管不到主力策略"的说法**成立**。
- `:227-230` open_process 失败仅猜测"权限不足?" — 属实（229 行原文如此）。
- `:287-289` 每个 blob 候选数不记日志 — 属实（286-289，无 `len(cands)` 输出）。
- `:292-311` crib 与打分两路失败无日志 — 属实（有 293 行"启用…求解"的起始日志，但 `mask is None` 时无任何失败记录）。
- `:305` 掩码求解只用 `blobs[0]` — 属实（305/307 行均为 `blobs[0]`）。
- `:64-66,94-97,107-109` `bytes.fromhex` ValueError 静默 continue — 属实（实际在 63-66、94-97、106-109，行号偏 1）。
- `:114-121` 逐候选 HMAC 失败无计数 — 属实（115 行 verify 失败仅落入循环下一轮）。
- `:264-279` 指针链每层 `read_mem` 返回 None 全静默 — 属实（264/270/276 三处，`if node and...` 失败即跳过）。
- `:319` "验证 N 个"、`:218` "未检测到微信进程" — 属实（218、319 行原文存在；319 行原文是"扫描完成： 验证 {found} 个密钥"，文档为合理转述）。

**keystore_source.py**
- `:14-17` parse_key 失败静默 continue — 属实。
- `:10-23` store 为空/全部未命中无输出 — 属实（22-23 行 `if found:` 才记日志）。

**memscan.py**
- `:28-30` open_process 失败 continue 无任何日志 — 属实。
- 全文件无 regions/候选/HMAC 计数 — 属实（仅 41/48 行命中日志与 58-59 行 found>0 时的汇总；21 行有"未检测到进程"，文档未误称）。

**mmkv.py**
- `:58-60` 目录不存在静默 return 0、`:73-76` 枚举 OSError、`:85-88` 单文件读失败、`:89-93` 头部校验失败、`:100-107` 全部候选 GCM 失败静默（106-107 `if plaintext is None: continue`）、`:115-117` 找不到 rel 路径 — 全部属实。

**macos_lldb.py**
- `:168` 内嵌脚本符号扫描 `except Exception: continue` 静默 — 属实（166-169）。
- `:186-188` Detach 失败静默 — 属实（185-188 `except Exception: pass`）。
- 第二处 Detach 静默 — 属实（实际在 267-271，文档写 260-271 偏前几行，内容为 `Continue()` 的 try/except）。

**keystore.py**（§3.5 全部属实，行号均有 ±1 偏移）
- `:137-139` 主路径 load() 失败 `return {}`（实际 136-139）；`:128-130` 旧位置读取失败静默（127-130）；`:132-134` 迁移 save 失败 `except OSError: pass`（131-134）；`:95-96,112-113` store_path 静默回退（92-96、109-113）；`:142-147` save() 无异常保护、os.replace 裸抛（142-147 确认无 try）；`:151-153` chmod 失败静默（150-153）。

## B. 不准确 / 错误的断言

1. **"macos_lldb.py 全文件 print 轨"** → **错误**。外层函数全部走 `ctx["log"]`（`macos_lldb.py:34` 取出，经 `log` 参数传入 `_capture_passphrase_via_lldb`，39/41/47/54/58/61/67/70/72/89/95/99/303/305/309/311/313/317/319/321 全是 `log(...)`）。只有 lldb 子进程内嵌脚本字符串（125-280）用 print——那是 lldb Python 环境里唯一的回传通道，无法避免。文档据此建议"迁移 strategy:lldb"对外层是多余的。

2. **"`:87` passphrase 抓到但派生 HMAC 未命中静默（无中间信号）"** → **夸大**。逐 salt 失败确实无日志，但 95 行有**无条件**汇总日志 `PID={pid}: 派生 {derived} 个密钥`，派生 0 个时用户能看到。不是"完全无信号"，而是"无逐 salt 细分"。

3. **"`:299-317` lldb 原始输出全部丢弃"** → **错误**。299-317 已将 `BP:`/`FAIL:`/`SYM:`/`HIT:`/`PROCESS_DEAD:`/`Traceback` 各前缀行及 stderr（截 4000 字符）逐条转入日志；`OK:` 行被正确识别并 return（不落日志）。真正被丢弃的只有**不匹配任何已知前缀的行**。安全风险判断（OK: 含 passphrase 明文）是对的，但前提描述失真。

4. **"`:289-296` unlink 吞掉 TimeoutExpired 上下文"** → **机制描述错误**（修复方向正确）。`os.unlink`（296 行）在 `subprocess.run`（293-295）**之后**；超时发生时异常直接从 run 抛出（318 行捕获），unlink **根本没执行**——结果是临时脚本文件残留，而不是 unlink 吞掉异常上下文。建议的 try/finally 正确，但给出的原因不对。

5. **"`:305` 掩码求解只用 `blobs[0]`"** → 字面属实但**易误导**。求解（crib/scoring）确实只用 `blobs[0]`，但验证闭包 `check`（295-303）对每个候选掩码会遍历**全部** blobs。其余 blob 只是不参与掩码推导，并非完全闲置。建议的日志文案本身无误。

6. **行号系统性偏移**（文档已声明"以内容为准"，仅提示）：config_cipher 的 fromhex 块实际 63-66/106-109、HMAC 循环 114-123；keystore.py 各项均偏前 1 行。内容全部对得上，不构成事实错误。

**漏判检查**（文档称"静默"处实际已有日志）：除 B-2（95 行汇总）外，mmkv.py:134-135 在 `found==0` 时有兜底日志"MMKV 文件存在但密钥未命中"——文档未否认它（建议补的是逐文件 GCM 失败的细分），不算漏判；config_cipher 掩码恢复有 293 行起始日志，文档措辞为"失败时无日志"，准确。其余"静默"断言处均核实无日志。

## C. 文档建议中可优化之处

1. **策略层统一"迁移 logger.detailed/warn"存在架构冲突（最大问题）**：所有策略只持有注入的 `ctx["log"]`，看不到 `siwx/logger.py` 模块。让 config_cipher/keystore_source/memscan/mmkv 直接调 `logger.detailed("strategy:cipher", …)` 会：破坏 propose-verify 的注入式设计与插件契约（插件策略同签名，却无 logger 可用）；绕过任务日志/日志页，用户看不到。更合理的做法是把 ctx 扩展为双通道（如 `ctx["log"]` + `ctx["dbg"]`）或在 server/extract 层把 `log` 做成同时写两轨的适配器。keystore.py 的建议无此问题（核心基建，直接 import logger 即可）。

2. **config_cipher"全量迁移 detailed"有副作用**：该策略当前向任务日志输出用户可感的进度（每 PID 区域数/MB、needle 数、blob 数、最终验证数）。文档虽要求 319/218 两行双写保留，但中间的"未取得配置 blob"（282 行）等也是用户排障首要看点；一刀切降为 detailed 会削弱 ROUGH 模式反馈。建议只把**失败点**（掩码求解失败、候选 0 产出、指针链计数）降为 detailed，进度保留 info。

3. **macos_lldb 输出建议基本正确且安全意识到位的两点值得肯定**：必须先剔除 `OK:` 行的要求是必要的——passphrase 是 64 位 hex，虽 `logger.desensitize_msg` 的正则能遮，但 `_FILE_LOG` 里存的是原文，脱敏只在展示/导出时发生；try/finally 修复也正确（只是文档对 bug 机制的归因写反了，见 B-4）。另可补充：脚本已无 `SYM:` 输出但外层 306-307 已预留解析钩子，加 `SYMERR:` 前缀确实只需改内嵌脚本。

4. **mmkv"候选 label 列表"日志**：labels 是静态派生名（"code+wxid" 等），无密，安全；但实现时需确保只记 label、绝不记 `aes_key` 本身。

5. **小遗漏**：`strategies/__init__.py:65-68` 插件策略**运行期**异常同样只记单行无堆栈，文档 49-54 条只覆盖加载期；`:35-38` 建议"warn + detailed 双写"在 DETAILED 模式下会产生重复条目（可接受，但建议改为 detailed 带 traceback 级别内区分）。keystore.py 各条建议（load 失败 warn、save 失败 error）方向均正确；`save()` 建议只补日志、不改变异常传播语义，是最安全的改法，文档未明说"记日志后仍需抛出/或明确吞掉"，实现时需注意不要顺手把异常吞掉而掩盖写入失败。

**总结**：事实性断言共 30 余条，其中 §3.4 strategies/__init__、keystore_source、memscan、mmkv、config_cipher 以及 §3.5 keystore.py 的全部"静默/无日志"断言均与代码一致（行号普遍偏 1，文档已声明以内容为准）；错误集中在 macos_lldb.py 的 4 条——"全文件 print 轨"、"派生未命中完全静默"、"lldb 原始输出全部丢弃"、"unlink 吞掉 TimeoutExpired"，均把"部分丢弃/有汇总日志"说成了"全部/彻底"，且对超时清理 bug 的机制归因写反。建议层面最大隐患是策略层直调 `logger` 模块与注入式 `ctx["log"]` 架构的冲突，实施前需要先解决通道问题。

===== agent-3 =====
核查完毕。以下报告以代码为唯一标准，行号均标注实际位置。

# 审计文档 `docs/debug-logging-audit-2026-10-04.md` §3.6 / §6.2 / §7 断言核对报告

## A. 确认属实的断言

**§3.6 discover.py**（该文件全文无 logger 引用，无漏判）：

- **27-29** 手动配置 JSON 损坏静默 `return []` —— 实际 `discover.py:26-29`，`except (OSError, json.JSONDecodeError): return []`，属实。
- **33-35** 逐条被拒的 error 文案丢弃 —— 实际 `discover.py:33-35`，`validate_db_path` 返回的 `r["error"]`（如"目录不存在"）被 `continue` 丢弃，属实。
- **121-122** AccessDenied 静默 —— 实际 `discover.py:121-122`，`except (psutil.NoSuchProcess, psutil.AccessDenied): continue`，属实（NoSuchProcess 属于正常竞态，文档在 §7.6 已正确排除）。
- **149-150,170-171,187-196** 目录枚举失败静默削减候选集 —— 实际 `discover.py:149-150`（Windows Users 枚举）、`170-171`（macOS）、`195-196`（主 root 循环 `except OSError: continue`），全部属实。
- **299-313** 注册表读取失败 —— 实际 `discover.py:303-312`，`except Exception: pass` 返回 `""`，属实。
- **104-124** `find_wechat_pids` 从不记录结果 —— 函数 `discover.py:104-124` 无任何日志，属实。

**§3.6 winproc.py**（全文无 logger 引用，无漏判）：

- **31-34** `open_process` 失败返回 None 不调 GetLastError —— 实际 `winproc.py:31-34`；且 `winproc.py:10` 为 `ctypes.windll.kernel32`，未用 `use_last_error=True`，属实。
- **42-47 + 84** ReadProcessMemory 失败与空读混同 —— `winproc.py:42-47` 失败返回 `None`，`winproc.py:84` `read_mem(...) or b""` 把失败块当空块处理，无错误码，属实（`or b""` 实际在 84 行，42-47 是 `return None` 的定义处，文档引号位置略有偏差但行为描述准确）。
- **57-73** VirtualQueryEx 返回 0 即 break 无记录 —— 实际 `winproc.py:63-65`，属实。
- **66-68** ≥500MB 区域静默过滤 —— 实际 `winproc.py:66-68`，过滤条件 `0 < mbi.RegionSize < REGION_LIMIT`，`REGION_LIMIT = 500*1024*1024`（`winproc.py:15`），属实。

**§6.2 paths.py**（全文无 logger 引用，无漏判）：

- **22-26** SIWX_ROOT 无效时静默忽略 —— 实际 `paths.py:22-26`，`if p.is_dir(): return p`，否则无声落入后续分支，属实。
- **97-102** 主路径不可写静默回退 `%USERPROFILE%` —— 实际 `paths.py:97-102`，`except OSError: pass` 后回退 `%USERPROFILE%\stories-in-wx\<subdir>`，属实。
- **63** 数据目录兜底临时目录 —— 实际 `paths.py:60-64`，`LOCALAPPDATA or USERPROFILE or tempfile.gettempdir()`，属实。

**§6.2 stats.py**（全文无 logger 引用，无漏判）：

- **102-103** `signature()` 失败 → 缓存永不命中 —— 实际 `stats.py:102-103` 返回 None；`compute_stats`（`stats.py:575-576`）`sig is not None` 才查缓存，且 `_save_disk_cache`（`stats.py:559-560`）`sig is None` 直接 return，既不命中也不写入。"约 2.4s"出自模块 docstring `stats.py:7` 的实测口径（8 万条），可溯源，属实。
- **146-147,186-187,232-233** 分片打不开/TEMP 建表失败/主聚合兜底 —— 实际分别在 `stats.py:145-147`、`186-187`、`232-233`，三处静默，属实。
- **160-161,433-443,482-493** Name2Id/公众号识别（`_contact_flags`）/昵称表（`_contact_name_map`）静默降级 —— 实际 `stats.py:160-161`、`433-443`、`483-493`，属实。
- **548-549,566-567** 缓存 JSON 损坏静默重算并覆盖 / 缓存写失败 —— 实际 `stats.py:548-549`（损坏 → return None → 重扫后 `_save_disk_cache` 覆盖）与 `stats.py:566-567`（`except OSError: pass`），属实。
- **303-311,325-329** 无耗时、无缓存来源标记 —— `compute_stats`（`stats.py:570-592`）无计时、无 mem/disk/miss 标记，属实。但注意 `_scan` 已有 `log` 回调通道并会输出分片数/总条数/私聊发送者数（`stats.py:325,329,335`）——只是全部 API 调用方（`api_stats.py:73,89,105`）都**没有传 log 参数**，该通道实际是死的。这不构成文档漏判（建议的"缓存来源+耗时"确实没有），但见 C 节。

**§7 logger.py**：

- `_ACCOUNT_KV_RE` 在 `logger.py:160`，`_PATH_RE` 在 `logger.py:157`，`desensitize_msg` 在 `logger.py:163-182`，行号与行为描述全部属实。`_ACCOUNT_KV_RE` 的幂等掩码逻辑（`logger.py:177-181`）确实如 §7.3 所述。
- "现有调用已用 discover/parse/sns/plugin 标签"属实（如 `extract.py:43,47` 用 `discover`，plugin 标签 40+ 处）。

## B. 不准确/错误的断言

1. **env_info.py `63-64` 被归入"插件行消失/静默"范畴 —— 部分不准确。**
   - 文档说法：`53-55,63-64,92-95` 插件行消失/计数不可读/frozen 误判。
   - 实际代码：`env_info.py:63-64` 的 `except Exception: return "开启（状态不可读）"` **并非静默**——报告里会显示"开启（状态不可读）"占位行。真正静默的是 `53-55`（import 失败 → return None → `env_info.py:119-121` 整行不写入报告）和 `92-95`（frozen 误判为"源码运行"）。
   - 错误类型：过度归因 / 表述不精确。"状态不可读"占位虽不理想（无法区分"插件系统坏了"与"计数失败"），但不能称为"缺关键行"。

2. **stats.py `566-567` "缓存写失败 → 每次全量重扫" —— 轻微夸大。**
   - 文档说法：缓存写失败 → 每次全量重扫。
   - 实际代码：`_save_disk_cache` 失败（`stats.py:566-567`）不影响 `stats.py:590-591` 的 `_MEM_CACHE` 写入——**同进程内后续请求仍命中内存缓存**，不会每次重扫。只有跨进程（每次 CLI 运行 / 服务重启）才会重复全量重扫。
   - 错误类型：影响范围描述失准（跨进程才成立）。结论方向正确（磁盘缓存形同虚设），建议文案可保留但应注明"重启后"。

3. **§6.2 stats.py 整节未提及 `_scan` 已有的 `log` 回调机制 —— 遗漏（非事实错误，但影响建议质量）。**
   - 实际代码：`stats.py:303-311` 定义了 `log` 回调并在 `325,329,335` 输出并行回退、分片数/总条数、私聊发送者数；`compute_stats` 也透传 `log`（`stats.py:570,587`）。文档把 `303-311,325-329` 列为"无耗时、无缓存来源"的静默点，却没说明这里**已有一条现成的日志通道**、只是调用方从不传参。
   - 错误类型：上下文遗漏。不算事实错误，但读者会以为需要从零搭埋点。

其余本批断言（§3.6 discover/winproc 全部、§6.2 paths 全部、§7 logger 定位）经逐行核对均属实；未发现"实际已有日志却被文档说成静默"的漏判（这 6 个文件经 grep 确认均无 logger 引用）。

## C. 文档建议中可优化之处

1. **`_PATH_RE` 会吞噬建议消息里的比率/分数 —— 与 §4/§6 建议存在直接冲突（最重要）。**
   `logger.py:157` 的 `[/][^\s]+` 分支会把日志中的 `17/5684`、`img=3/5` 这类比率匹配成路径并替换为末段（`Path("17/5684").name` → `5684`，输出变成 `175684`）。文档自己在 §4.1 建议 `detailed("export", f"媒体完成 img={}/{} voice={}/{}")`、§3.6 建议 `f"扫描 {len(roots)} 个根, 命中 {len(out)} 账号"`（这个无斜线，安全），但只要消息里出现 `a/b` 形式的数字比就会被脱敏规则破坏，日志页与导出均受影响。实施前应先修 `_PATH_RE`（如要求路径含至少一个路径分隔符+扩展名/目录名特征，或改用 `(?:[A-Za-z]:[\\/]|~\/|/Users/|/home/|\\\\)` 等更严格的锚点），否则新埋点产出的是损坏文本。

2. **stats.py 建议应优先复用/接通现有 `log` 通道，而非另起 `detailed`。**
   `_scan` 已有 `log` 回调且带 try 保护（`stats.py:305-310`），`api_stats.py` 三处调用均未传参。最小改动是让 `api_stats.py` 传入 `log=lambda m: log.detailed("stats", m)` 并给 `compute_stats` 补计时与缓存来源标记；直接在内层塞 `detailed` 会与现有回调双轨并行，同一条信息可能打两遍（`325,329` 与建议的新行内容重叠）。

3. **`paths.py:97-102` 建议的 warn 与脱敏假设需修正。**
   建议写 `warn("paths", log.desensitize_msg(f"{subdir} 主路径不可写，回退: {fallback}"))`。但 `warn` 输出的 `_cprint` 路径不经过 `desensitize_msg`（脱敏只作用于 `get_logs`/`export_logs`，见 `logger.py:110-121,124-143`）——预先 `desensitize_msg` 只会让控制台输出也变成脱敏文本，而文档 §7.3 的约定是"路径直接写（脱敏规则会处理）"。这里包 `desensitize_msg` 属于双重脱敏，与文档自己的规范不一致；另外 `_writable_fallback` 是热路径且有 `_PATH_CACHE`，回退分支每进程只触发一次，warn 不会刷屏，但建议未指出"函数内无法 import logger 会引入循环依赖风险"——`paths.py` 被 `logger` 之外的几乎所有模块依赖，`paths` 内 import `siwx.logger` 需确认无环（logger 不 import paths，实测无环，可行，但文档未论证）。

4. **winproc.py 建议"失败返回 `(None, get_last_error())`"有 API 兼容性副作用。**
   `open_process` 现有 7 个调用点（`winproc.py:105` 及策略层），改返回元组需全部同步改；更稳妥是先只把 `kernel32` 换成 `use_last_error=True` 并在 `open_process`/`read_mem` 内部失败时调 `ctypes.get_last_error()` 走模块级计数器/一次性 detailed，保持签名不变。文档 §7.5 已提到"热路径用计数器+汇总"，但 §3.6 的改造建议本身没提签名兼容性，实施时需注意。

5. **env_info.py 建议"`warn` + 填'不可读'占位"方向正确，但缺一点： frozen 误判（`92-95`）无法事后从日志区分。**
   `is_frozen` 失败时建议只给 `detailed`，但 frozen 误判会让 bug 报告把打包产物写成"源码运行"，这是报告正确性问题而非日志问题——更彻底的做法是 `except` 分支填 `"未知"` 而非回退 `False`，文档未覆盖此选项。

6. **§7.3 键值脱敏对 `chat=` 的副作用未讨论。** `_ACCOUNT_KV_RE`（`logger.py:160`）会掩掉 `chat=` 后的值前 4 字符保留，但群聊名 `12345@chatroom` 已被 `_CHATROOM_RE` 先掩成 `***@chatroom`，随后 KV 规则命中 `len(v)<=4` 或含 `***` 跳过（`logger.py:179`），幂等性设计正确——这一条文档说得对，但建议消息中若写 `chat=xxx@chatroom` 会只剩 `chat=***@chatroom`，定位会话能力受限，实施时建议 `chat=` 值优先用 md5 表名或 local_id 辅助定位，文档未提示。

**总结**：本批 20 余条事实断言中 18 条完全属实；2 条不准确（env_info 63-64 并非静默、stats 566-567 "每次重扫"夸大）；1 条重大上下文遗漏（stats `_scan` 已有 log 通道）；未发现漏判"已有日志"的情况。主要实施风险集中在 `_PATH_RE` 对含斜线文本的过度脱敏与 winproc API 签名变更的兼容性。

===== agent-4 =====
核查完成。以下为逐条核对报告（以代码为准，行号为实际代码行号）。

# 审计文档 `docs/debug-logging-audit-2026-10-04.md` §4.1–§4.3、§5.2 核查报告

## A. 确认属实的断言

**§4.1 exporter.py**（823 行，与文档一致）：

| # | 断言 | 核对结果 |
|---|------|---------|
| 1 | `152-162` `_log_media_failures` 只输出 top3 原因分布、failures 只有 reason 字符串 | ✅ 属实（`[:3]` 截断在 160 行；三个收集点 197/216/266 行均为 `failures.append(reason)`） |
| 3 | `250` 覆盖 `media.get_image` 的细粒度 `last_err` | ✅ 属实。`media.get_image` 在 media.py:393-489 逐级赋值 `last_err`（"attach 解密失败"/"V2 密钥未命中"/"Bubble 未知格式"/"本地无原图"等）并于 489 行 `return None, last_err`；exporter.py:250 丢弃之，统一报 "未找到源文件或解密为空" |
| 4 | `128-149` `_attach_media` 未命中（145-146）/槽位不匹配（147-148）静默 return、门禁 `t in (3,47,34)`（137 行） | ✅ 属实，无任何日志 |
| 5 | `312-330` `observe` 的 `quote_fail`（319）/`fallback_labels`（327-328）只计数 | ✅ 属实 |
| 7 | `345-362` `run_export` 开始/扫描只有 progress | ✅ 属实（353/359/362 行仅 progress） |
| 8 | `396-410,457-461` 媒体完成数/写出条数只有 progress | ✅ 属实（410、457 行仅 progress；458-461 的 warn 仅在 count≠written 时触发） |
| 9 | `479-489` manifest 成功有 info、日志无 type_counts | ✅ 属实（483-487 info 只含 quote/link/record/fallback；type_counts 只进 manifest.json） |
| 10 | `542-549` `_plugin_export_format` `except Exception: return None` | ✅ 属实（548-549） |
| 11 | `588-595` `_run_after_export` ensure_loaded 失败静默跳过所有钩子 | ✅ 属实（590-595 try 包住 ensure_loaded，失败直接 return） |
| 12 | `799-801` `run_export_multi` 单会话失败只进 results/progress | ✅ 属实（799-801，无 logger 调用） |

**§4.2 export_stream.py**（260 行，与文档一致）：

- ✅ `70-72`：`70` 行 `TYPE_NAMES.get(t, f"类型{t}")` 兜底；content 由 `_fmt` 产出（72 行），quote/link 解析失败时 quote=None、content 为兜底文案，均无日志。
- ✅ `61` + `api_chat.py:662-669`：`enrich_message_row` 定义在 662-670 行（文档写 662-669，差一行，无关紧要）；type 3/47 无 XML md5 且 packed_info 无 32 位 hex 时 `md5=bubble_md5=None` 属实（664-669）。
- ✅ `124-131` `message_stream` 分片打开/`conn.execute` 无 try；对比 `count_messages` 98-100 有 warn 属实。Msg_ 查询失败会向上抛出、中断整个导出。
- ✅ `104-136` 参与归并的分片数/总条数无任何记录。

**§4.3**：`_fmt`（354-392）产生 `[引用]`（362/363/372）、`[链接]`（387/391）、`[类型X]`（392），无逐条日志 ✅。

**§5.2 api_chat.py**：

- ✅ `80-81` `shard_index` 分片打不开即 `continue` 剔除索引，且该索引会进缓存（86-87）。
- ✅ `266-278,329-330` `_contact_names`：schema 降级内层 pass（272-273）、打不开 pass（276-278）、**空结果进缓存**（279-280）；`_contact_names_for` 329-330 pass 且缓存（331-332）。
- ✅ `343-351` `_decode_content`：zstd 失败（344-347）与 UTF-8 失败（348-351）均静默返回空串。
- ✅ `426-427,832-833,947-948,1001-1002` 四处 `except sqlite3.Error: pass` 属实。
- ✅ `1092-1097,1137-1141` local_id/ts 非法时 `ValueError` → 静默归零，随后按 0 参数查询，大概率产生误导性 404。
- ✅ 请求入口日志：`/accounts`（414）、`/timeline`（904）、`/stats`（959）、`/avatar`（1021）、`/media/voice`（1082）、`/media/image`（1129）均无入口日志；`/sessions:513` 与 `/messages:803` 已有 `_log`（值得肯定的判断也属实）。

## B. 不准确/错误的断言

**B1. §5.2 `706-707`："build_messages 的 except sqlite3.Error: pass（导出/API/MCP 三处共用）" —— 调用方事实错误**

- 文档说法：`build_messages` 被导出/API/MCP 三处共用，因此是"本层最严重静默点，导出缺整段会话"。
- 实际：全仓 grep 确认 `build_messages` 的生产调用方**只有 MCP**（`siwx/mcp_server.py:172`、`:193`）。导出链路用的是 `export_stream.message_stream`（`exporter.py:97` 等），API `/messages` 端点是内联手写循环（`api_chat.py:845-885`），都不经过 `build_messages`。docstring（682 行"导出与 API 共用"）是历史遗留描述，与现行代码不符。
- 错误类型：过时/未核实的调用关系断言。静默点本身（706-707 pass）存在，但爆炸半径是 MCP 工具，而非导出。导出路线上等价且更严重的风险是 §4.2 的 `message_stream:124-131`（连 try 都没有，直接中断整个导出）——文档在两处之间的严重度定性自相矛盾。

**B2. §5.2 "六连 except sqlite3.Error: pass" 中含两处非 pass**

- `410-411`（`_sender_map`）：实际是 `except sqlite3.Error: return {}`，不是 pass。结论方向（静默）仍成立，但"pass"字样与"六连"的修辞不准确。
- `1066-1071`（`/avatar`）：实际是 `except sqlite3.Error:` 后回退插件头像解析器、再 404——有明确的降级处理，不是 pass、也不是"静默归零"。它的问题是**无日志**，不是无处理。文档把它列入"pass 六连"属于错误归类。
- 错误类型：行为描述失实（行号对，定性错）。

**B3. §4.1 `223-229`："_decrypt_one 子进程失败只返回 reason" —— 不精确**

- 实际：`_decrypt_one`（223-229）返回 `(key, rel_path, ok, reason)` 四元组，其中 `key = _media_key(md5, bubble_md5, local_id, ts)`（228 行）**本身已携带 md5（或 bubble_md5）、local_id、ts**。上下文丢失发生在主进程的 `imap_unordered` 循环（192-197）：只把 `reason` 追加进 failures，丢弃了 key。
- 文档建议"子进程返回值带全 `(md5, bubble_md5, local_id, ts, reason)`"——子进程侧改动并非必需（key 已够定位消息；唯一额外信息是 md5 与 bubble_md5 并存时的原始 bubble_md5）。改收集循环即可达到目标。
- 错误类型：问题定位偏差（把收集点的丢弃说成子进程返回缺失）。现象层面（failures 只有 reason）属实。

**B4. §4.3 建议理由错误："在 `_enrich_row` 层做，API 与导出共用"**

- 实际：`_enrich_row` 只被 `export_stream._enrich_row`（即导出/message_stream 路径）使用；API `/messages` 端点不经过它（内联重复逻辑，845-885 行）。"API 与导出共用"对 `_enrich_row` 不成立。真正三处共用的收束点是 `parse_quote_or_link`（`build_messages:745`、`_enrich_row:63`、`/messages:872` 都调用）和 `_fmt`。
- 错误类型：架构关系断言错误，导致推荐落点覆盖不全。

**B5. §4.1 `68-87` collect_avatars "write 失败均无日志" —— 半属实**

- `db` 不存在（72-73）静默、`write` 无日志，均属实；但 `(dest/fn).write_bytes(row[0])`（81 行）**没有任何 try**，写失败会直接异常向上传播、使 `run_export` 的头像阶段整体崩溃——不是"静默无日志"，而是"未处理异常"。文档的修法（汇总 detailed 一条）解决不了崩溃问题。
- 错误类型：遗漏并存的更严重问题（未捕获异常），把崩溃描述成静默。

## C. 文档建议中可优化之处

1. **建议落点修正（最重要）**：兜底文案埋点应落在 `parse_quote_or_link`（或 `_fmt`），而非 `_enrich_row`——前者才同时覆盖导出流、`/messages` API 和 MCP（见 B4）。在 `_enrich_row` 做会漏掉 API 路径，与文档自己"显示不对但查不到原始类型"的目标相悖。

2. **日志建议本身的隐私口径不一致**：§5.2 `_decode_content` 建议明确"只记 hex 头，不透传正文，脱敏安全"（很好）；但 §4.1 建议记 `rawContent[:200]` 全文、`content[:120]`。注意 `logger._FILE_LOG` 存的是**未脱敏原文**（脱敏只发生在 `get_logs`/`export_logs` 时，logger.py:118-121、140），rawContent 即聊天正文，落盘量又大（3 万条导出逐条打会刷爆 5 万条环形缓冲）。建议统一为：正文只记 hex 头 + 长度 + 类型，必要时记前 16~32 字符。

3. **`detailed` 级别的适用性**：logger.detailed 只在 DETAILED 模式记录（logger.py:81-86），默认 ROUGH 模式不生效。媒体失败、分片打不开这类**数据完整性事件**用 detailed 意味着默认模式下用户依然看不到——文档自己在 §5.2 build_messages 建议"warn 以上"，但 §4.2 分片打开失败只建议 warn、§4.1 大量失败明细建议 detailed。建议：影响数据完整性的（分片失败、表查询失败、媒体解密失败汇总）至少 warn；逐条明细才用 detailed。

4. **入口日志通道差异**：`/sessions`、`/messages` 现有 `_log` 走 `logging.getLogger("siwx")` → RotatingFileHandler（server.py:27-42），**不进 `_LOG_RING`**（server.py:651-655 自己承认这一点），即应用内日志页看不到。若按文档给 `/accounts` 等端点补 `detailed("api", ...)`，会进环形缓冲但只在详细模式可见。两套通道并存，建议文档明确：入口日志用 `_log`（文件）还是 `logger.detailed`（环形缓冲），避免修复后"日志页依然空白"的落差。`/media/*` 属高频端点，入口日志用 detailed 节流是对的。

5. **`exporter.py:250` 透传建议可行但需小改**：get_image 失败时第二返回值就是 `last_err` 字符串，建议直接 `return None, ctype or "未找到源文件或解密为空"` 即可，无需更大改动。

6. **§4.2 给 `message_stream` 包 try 的副作用**：把"中断导出"改成"部分导出"会改变现有行为——好的一面是 `run_export` 的 count-vs-written 对账 warn（458-461）和 manifest.json 会让缺失可见；但更完整做法是同时把失败分片计数写进 manifest，否则批量导出（`run_export_multi`）下逐会话仍只靠一条 warn 定位。

7. **遗漏的既有日志（文档未提，不算错误但应补充）**：`api_chat.py:603-605` `_safe_parse_refer` 已对引用解析**异常**打 detailed——即 quote 解析因异常失败的路径已有日志，真正缺日志的是"无异常但返回 None"的兜底（如 t==57 未匹配到 refermsg 时 `_fmt` 出 `[引用]`）。文档 §4.3 应区分这两类，避免修复时重复埋点。

8. **§4.1 `_decrypt_one` 建议减负**（见 B3）：不必改子进程返回值协议（Windows spawn 模式下多进程协议改动有兼容性成本），主进程收集循环保留 `(key, reason)` 即可，key 已含定位所需的 md5/local_id/ts。

## 汇总

- 文档行号引用整体非常准（三份文件总行数 823/260/1151 全部吻合，绝大多数行号误差 ≤1 行）。
- 事实性错误 4 处：B1（build_messages 调用方，最严重，直接动摇"本层最严重静默点"的定性）、B4（`_enrich_row` 共用性）、B2（两处非 pass 混入"pass 六连"）、B3（子进程返回描述偏差）；另有 B5 一处遗漏并存的崩溃问题。
- 修改建议方向整体合理（媒体失败元组、兜底埋点、分片 try、入口日志、透传 last_err 都是对的），主要问题是落点选择（应选 `parse_quote_or_link`）、日志级别混用（数据完整性事件应 warn+）、以及 rawContent 全文落盘与文档自身脱敏原则冲突。

===== agent-5 =====
核查完成。以下为报告。

# 核查报告：debug-logging-audit-2026-10-04.md 中 media/voice/html_template/server 相关断言

## A. 确认属实的断言

- **§2.2 / §4.4 `media.py:28` `event = lambda msg: None`**：属实（media.py:28，注释自称"CLI 下默认静默"）。`get_image` 中 8 处 `event(...)` 调用行号 411/418/435/458/467/473/481/486 全部精确命中；`label`（media.py:394）确为 `chat[:12] local_id md5[:8] bm[:8]` 截断格式。
- **§2.2 `server.py:877` 接线**：属实，server.py:877 `media.event = tui.log`（位于 `run_server` 内，仅 serve 模式生效）。全库仅此一处接线（grep 确认）。
- **§4.4 `media.py:210-212`**：`candidate_keys` 中缓存记录 `try: bytes.fromhex(...) except (KeyError, ValueError): pass` 静默跳过，属实（实际为 209-212）。
- **§4.4 `media.py:146-147,173-174`**：`_get_voip_fn` 的 `except Exception: return None`（146-147）与 `convert_wxgf` 的 `except Exception: return None`（173-174）均属实，且均无任何日志。"wxgf 原样落成 .jpg"链条也属实：`_finalize`（media.py:348-353）转码失败时保留 `ext="wxgf"`，exporter.py:246 按 ctype 判后缀落到 else 分支写成 `.jpg`。
- **§4.4 `media.py:187-200` `decrypt_v2_body` 三处 `return None, None`**：属实（189 过短 / 192 aes_size 非法 / 196 头部签名不识），三处均不区分原因，上层统一落到 `last_err = "V2 密钥未命中"`（437/469）。
- **§4.4 `media.py:231-232`**：`resolve_image_path` 对 hardlink.db 不存在或 md5 非 32 位直接 `return []`，属实（230-232）。
- **§4.4 正面项**：`last_err` 逐级覆盖行号 420/437/442/451/469/475/488 全部命中；exporter.py:250 `return None, "未找到源文件或解密为空"` 确实覆盖了 `get_image` 返回的具体 reason，属实。
- **§4.5 voice.py 全部五条**：374-375 `except sqlite3.Error: continue`（media_*.db 损坏静默跳过，最终统一报 376 行"语音数据不存在或尚未同步"）；302-309 `_chat_id` 静默返回 None（含 row 为 None 的情况）；221-224 pilk 导入失败 `return None, "", ""`（err 为空串）；52-56 `as_int` 异常回退 default 0。全部属实。
- **§4.6 html_template.py**：文件确实不 import logger（仅 json/re/datetime，grep 确认）；JS 342 行未知类型+mediaPath 回退纯文本属实（341 行注释正是审计 D6 的修复说明）；419-424 `__imgErr` 无日志属实。
- **§7.6 排除项 `voice.py:256-258`**：属实，该处是 `_decode_silk_to_pcm_with_command` 内 `pcm_path.unlink()` 的临时文件清理，列入不埋点清单合理。

## B. 不准确/错误的断言

1. **§4.4（222 行）"`_load_key_cache` 缓存文件损坏……之后表现为'V2 密钥未命中'"** —— 过度简化。实际：media.py:61-65 损坏静默当无缓存属实，但 `candidate_keys`（203-223）随后会走 kvcomm 离线派生兜底，只有派生也全部失败时才会出现"V2 密钥未命中"（media.py:437/469）。缓存损坏 ≠ 必然报该错；且即使缓存完好，失败也报同样的错（这正是该行改法想解决的"分不清"问题，但现状描述把条件说死了）。行号 64-65 本身命中。

2. **§4.4（225 行）`decrypt_v2_body` 三处返回的原因命名为"过短/aes_size 非法/签名不匹配"** —— 第三处命名不准。media.py:196 的 `return None, None` 发生在 `_image_sig(head)` 未识别出图像魔数（即解密后的头部不是 jpeg/png/gif/webp/wxgf），这是"密钥错或密文损坏"的混合信号，并非密码学意义上的"签名不匹配"（V2 格式根本没有签名字段，只有 AES 头 + XOR 尾）。改法给出的 reason 码 `sig-miss` 沿用了这个不精确命名，建议改为 `bad-head-sig`/`head-not-image` 之类。

3. **§2.2（69 行）"默认全丢"与 §4.4（221 行）"开 Debug 也看不到"** —— 需限定场景。`event` 默认 no-op 属实，且 event 通道不受 logger Debug 开关控制也属实；但在 **serve 模式下**事件经 tui.log 实时显示在 TUI 日志流里（server.py:877），并非"全丢"。准确说法应是"CLI 模式全丢；serve 模式可见但不受 Debug 开关控制、不落结构化日志"。

4. **§4.6（240 行）"`__imgErr` 静默换占位块"** —— 措辞不准。html_template.py:419-424 并未插入占位块，而是原地修改 `<img>` 元素（加 `.broken` class、删 src、设宽高、`textContent`）。且 `<img>` 是替换元素，`textContent` 不会渲染出"📷 图片"文字——若 CSS 未对 `.broken` 定义样式，用户看到的只是空白区。埋点建议不受影响，但"换占位块"描述与实际行为有出入，且这里本身可能是另一个小 bug（未完全确认其视觉表现，取决于模板中 `.broken` 的 CSS）。

5. **§4.5（231 行）改法 `warn("voice", f"media分片打开失败: {db.name}: {e}")`** —— 可行性细节：voice.py:374 现为 `except sqlite3.Error:`（未绑 `e`），实施时必须改成 `except sqlite3.Error as e:`，文档改法列照抄会 NameError。属改法未写全，非事实错误。

## C. 文档建议中可优化之处

- **`event` 默认实现改 `log.detailed("media", msg)`（§2.2）**：方向合理，logger.detailed 默认关闭不刷屏，serve 下 tui.log 接线保留也无冲突。两点注意：① `event` 在 get_image 热路径中每次 miss 都调用，若未来在 serve 模式下调试会经 tui.log 全量弹出（现状已是如此，非新增问题）；② 注意循环 import——media.py 现不依赖 siwx.logger，而 logger.py 不 import 任何业务模块，实际无环，安全。
- **decrypt_v2_body 返回第三元素 reason（§4.4）**：可行但改动面比文档暗示的大：返回签名变化需同步改全部 5 个解包调用点（media.py:361、367、432、439、464）。更省的做法是直接在 `decrypt_v2_body` 内部打 `detailed`（它本就拿到全部上下文），不改签名、无调用方迁移成本；返回 reason 的唯一优势是可拼进 `last_err` 透出到 API 层。建议实施时二选一，文档未给出取舍依据。
- **`_get_voip_fn` 失败 warn"每进程一次"（§4.4）**：需要新增 `_VOIP_TRIED` 之类的标记位。现状是失败时 `_VOIP_FN` 保持 None、每次 `convert_wxgf` 都重试重报（media.py:128-148），若不加节流，warn 会随每张 wxgf 图重复刷日志——文档写了"每进程一次"但未指出现状无此机制。
- **`exporter.py:250` 覆盖出口（§4.4 正面项）**：文档只说"修出口即可"，未给具体改法。注意 `get_image` 返回 `(None, last_err)`，而 `_try_decrypt` 签名是 `(Path, "")`/`(None, reason)`，直接把 `get_image` 的 `info`（即 last_err）透传即可，几乎零成本；建议文档明确这一点（现实现丢掉了 info，见 exporter.py:240-250）。
- **§4.6 服务端 `build_chat_data` 尾部埋点**：合理，但 `build_chat_data` 是纯函数且 html_template.py 有意保持零业务依赖（便于模板文件独立），加 logger import 会引入新依赖方向；替代做法是调用方（exporter）在拿到返回值后打 detailed，文档未考虑这层架构取舍（不构成反对，仅提示）。
- **`renderMsg` 把 `msg.timestamp` 存进 `data-ts`（§4.6）**：`timestamp` 已在 CHAT_DATA 里，但 renderMsg 以字符串拼 innerHTML，加 data-ts 需穿参改模板拼接，成本中等；而 `__imgErr(img)` 的调用点（337/339 行 onerror）拿不到 msg 对象，只能拿 DOM——除非存 data-ts，否则 console.warn 确实打不出时间戳。文档的逻辑链是自洽的，但未说明 `__imgErr(this)` 只传了 img 元素这一约束，读者可能低估改动量。

**漏判检查**：文档声称"静默/无日志"的 16 处（media 6、voice 5、html 2、§7.6 1 及其他相关）逐一核对，均确认无现有日志，无漏判。voice.py 整个文件同样不 import logger（与 media.py 一样，加埋点前都需先建立依赖）。

总体结论：文档事实断言准确率很高（行号全部命中或 ±2 行内），主要问题集中在 §B-1 的条件说死、§B-2 的"签名不匹配"命名不准、§B-3 的场景限定缺失；改法层面的问题均为实施细节（缺 `as e`、缺节流标记、未评估签名变更的调用方迁移面），无方向性错误。

===== agent-6 =====
核查完成。以下为逐条核对报告。

# 核查报告：docs/debug-logging-audit-2026-10-04.md §1 / §4.7

## A. 确认属实的断言

1. **§1 正面范本 `sns_cdn.py`**：失败 → `_log_media_failure` 持久文件日志——属实。`_log_media_failure` 在 `siwx/sns_cdn.py:397-406`，内部 `_file_log.warning(...)`（`_file_log = logging.getLogger("siwx")` 在 `sns_cdn.py:58`）；成功 → `detailed("sns", ...)`——属实，`_log_media_ok` 在 `sns_cdn.py:409-421`，detailed 调用在 416 行。
2. **§1 `sns_export.py:294-323`**：媒体失败前 30 条带 tid/idx/reason 明细 warning + 聚合——属实。明细 warn 在 `sns_export.py:314-315`（含 tid/index/live/reason/err），聚合 warn 在 321-323 行，30 条上限在 310 行（`if len(stat["fail_samples"]) < 30`）。
3. **§4.7 `sns.py:458-461 + 977-979`**：parse_timeline XML 解析失败 return None、iter_timeline 静默 continue——行号完全吻合（`try/except ET.ParseError: return None` 恰在 458-461；`if feed is None: continue` 恰在 977-979）。
4. **"5684 条有 17 条失败"注释真实存在**：`siwx/sns.py:455` docstring 原文"容错：实测 5684 条中有 17 条无法解析（疑为特殊字符/版本差异），调用方应处理 None。"
5. **§4.7 `sns.py:680-693` / `928-957`**：缓存图扫描（读失败 682-684、解密失败 686-688、无尺寸 691-693）与图池导出（读 930-933、解密 934-944、写 952-957）三段失败均只 `stat["fail"] += 1` 后 continue，无任何日志。`sns.py` 全文无 logger/detailed/warn 引用（grep 零命中），不存在漏判的既有日志。
6. **§4.7 `sns.py:749-751`**：媒体无尺寸 → `Match(idx, "none", method="no-size")` 直接 continue——属实（749-751）。
7. **§4.7 `sns_cdn.py:206-220`**：`_decompress` 解压失败 `return raw` 静默——属实。
8. **"解压失败被上报成 undecodable"因果链成立**：`_decompress` 失败返回压缩态 raw（219）→ `fetch` 返回该 body（233）→ `fetch_media` 中 `detect_mime(body)` 得 `ext=None`（483-484）→ 走 494-501 分支置 `undecodable` → 509-510 `out.update(reason="undecodable")` → 516 `_log_media_failure`。全项目仅 `sns_cdn.py` 一处上报 undecodable（grep 确认）。"错误方向被带偏"的定性成立——用户看到的是"已下载 N 字节但认不出格式/密文解不开"，而非解压失败。（注：链路由代码静态验证；实际触发依赖服务端返回损坏压缩流，未运行验证，属"未完全确认"的实际发生率，但代码因果本身确认无误。）
9. **§4.7 `sns_cdn.py:328-341,364-365` + `fetch_media:504` 不检查返回值**：属实。`_atomic_write` 失败返回 False 被 504 行忽略（行号恰好 504）；`read_cached` 读失败 `except OSError: return None` 在 362-365（doc 写 364-365，无实质偏差）。补充：读缓存失败被当作 miss 会触发重新下载，影响是性能而非数据丢失，但"静默"断言本身准确。
10. **§4.7 `sns_export.py:117-126`**：media_map 无 `(tid, idx)` 键时不设 `localFile`，无日志——属实（117-118 主图、125-126 实况图）。
11. **§4.7 `sns_export.py:314-323` 前 30 条上限**：属实（310 行硬上限，注释 307 行明言"前 30 条进日志（避免刷爆）"，确为设计使然）。
12. **§4.7 `api_export.py:43-44,56-57,70-71,109-110` 均无日志**：属实。43-44 插件列举 `except Exception: pass`；56-57 `/download` 越界/不存在 404；70-71 `/open` 路径无效 404；109-110 `/render` 兜底 except。`api_export.py` 全文无 log/warn 引用，无漏判。

## B. 不准确/错误的断言

**仅 1 条实质错误：**

- **§4.7 `sns_export.py:314-323` 行的"且成功无记录"** —— **不准确（漏判已有埋点）**。
  - 文档原说法：sns_export 媒体导出"成功无记录"，建议"成功……全量走 detailed"。
  - 实际代码：`download_media` 的成功路径全部经由 `sns_cdn.fetch_media`，而 fetch_media 成功时在 `sns_cdn.py:458`（缓存命中）和 506 行（下载成功）调用 `_log_media_ok`，即成功**已有** `detailed("sns", "[媒体] 成功 ext=... url=...")` 记录。链路级成功并非"无记录"。
  - 准确的表述应为：成功记录**缺 tid/idx 归属维度**（`_log_media_ok` 只记 ext/字节数/url，不记属于哪条动态哪张图），而非"成功无记录"。错误类型：对现有埋点覆盖范围的低估。

其余断言行号虽有 ±1 级偏移（如 read_cached 的 362 vs 364），均以内容定位后确认实质正确，不构成错误。

## C. 文档建议中可优化之处

1. **§4.7 建议 1（iter_timeline 逐条 detailed）漏了一个细节**：`iter_timeline` 里 `con.text_factory = bytes`（`sns.py:971`），所以循环里 `tid` 也是 **bytes**。建议的 f-string `f"XML解析失败 tid={tid} content=..."` 会打出 `b'...'`。文档只提了 content 需 `errors="replace"` 解码，漏了 tid（及 user）同样要解码。另外逐条 detailed 内含 `content[:200]` 原始片段，需注意 sns 内容可能含他人 wxid/昵称，与 §1 引用的脱敏惯例（`desensitize_msg`）有张力，建议复用脱敏。
2. **§4.7 建议 7（成功全量走 detailed）与 `_log_media_ok` 重复**：如上 B 节，成功已有 detailed 记录。更优做法是给 `fetch_media`/`_log_media_ok` 增传 `tid/idx` 上下文（改签名或写入 `out` 字典），而不是在 sns_export 再写一条，避免同一次成功出现两条 detailed。
3. **§4.7 建议 4（_decompress 打标 reason=decompress-error）有轻微架构摩擦**：`_decompress` 在 `fetch()` 内被调用（`sns_cdn.py:233`），不持有 url/out 上下文；要让 reason 变成 `decompress-error` 需改 `fetch` 返回结构或在 `fetch_media` 侧二次探测（如对 body 做 gzip 魔数检查），成本高于文档一句话。仅加 `detailed("sns", f"解压失败 enc={encoding}")` 则没有 url 归属，排查时仍要对照时间戳。建议文档明确选哪种。
4. **§4.7 建议 8（api_export 拒绝路径记 warn）注意量级与性质**：`/download`、`/open` 的 404 拒绝是安全边界行为，可能被扫描/误触频繁触发，用 `warn` 进文件日志有刷量风险，建议用 `detailed`（或至少注明量级预期）；`/render` 的 109-110 记日志则完全合理（是真异常）。
5. **未遗漏的更好做法**：§4.7 建议 5（检查 `_atomic_write` 返回值）正确且必要；§4.7 建议 2/3/6 均无副作用，与现有架构兼容。另可补充：`read_cached` 读失败可考虑 `detailed` 而非 warn（失败后会自动重下载，属可恢复事件）。

**总结**：文档本批次断言质量高——24 项具体断言中 23 项完全属实，唯一错误是 sns_export"成功无记录"一条（低估 `_log_media_ok` 覆盖）；建议部分有 3 处可改进（tid bytes 解码遗漏、与既有成功日志重复、decompress 打标的架构成本），1 处需注明日志量级风险。