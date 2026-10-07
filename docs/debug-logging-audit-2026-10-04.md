# SIWX Debug 日志埋点全面审计（2026-10-04）

> 审计方式：只读源码审阅（未运行任何代码，未触碰微信进程/内存）。
> 审计范围：`siwx/` 全部模块 + `siwx/strategies/` + `siwx/plugins/`，共 40+ 文件，逐文件过。
> 本文目的：列出"用户开启 Debug（详细）模式后，应该能看到但目前看不到"的所有位置，并给出每处的建议埋点方式。**本文只是审计报告，不含任何代码改动。**
> 修订（2026-10-04）：经逐条代码核查后修正——事实性错误（§3.3:135、§3.4 macos_lldb 四条、§4.1:223、§4.3、§4.4:64/196、§4.7 sns_export、§5.1:447/472、§5.2:706/410、§6.2 env_info/stats）、建议代码缺陷（§3.1 变量遮蔽、§4.5 缺 `as e` 等）与跨章节架构冲突（§2.1 迁移成本、§7 `_PATH_RE`）。核查明细见 `docs/debug-logging-audit-verification-2026-10-04.md`。

---

## 0. 结论摘要（TL;DR）

| 维度 | 现状评级 | 说明 |
|------|---------|------|
| 基础设施（落盘/轮转/崩溃钩子/脱敏） | ★★★★☆ | 合格甚至优秀 |
| Debug 埋点密度 | ★☆☆☆☆ | `log.detailed()` 全项目仅 **5 处**（extract.py:43,47、api_chat.py:603、sns_cdn.py:416、plugins/loader.py:201） |
| 渲染/导出问题可定位性 | ★★☆☆☆ | 只有聚合计数，无法定位到"哪条消息、什么原始类型" |
| 媒体（图片/语音）失败可定位性 | ★☆☆☆☆ | 只有原因分布 top3 聚合，local_id/md5 上下文在收集时就被丢弃 |
| Debug 开关本身 | ★★☆☆☆ | 不持久化、CLI/TUI/环境变量均无入口、重启后双通道级别脱节 |

**最严重的 5 个系统性盲区**（详见 §2）：

1. **双轨日志割裂**：提取/解密链（extract.py 及全部 strategies）的日志走 `log=print` 任务轨，完全不受 Debug 开关控制——开了详细模式，恰恰在最需要细节的"密钥为什么提不出来"路径上一条增量信息都没有。
2. **media.py 诊断信息走了 no-op 钩子**：`media.py:28` 的 `event = lambda msg: None`，图片三级来源诊断（attach 未命中/Bubble 未命中/命中哪家文件）在 CLI 下 100% 丢弃，serve 下也只进 tui.log，与 Debug 开关无关。
3. **媒体失败无逐条定位**：`exporter.py:152-162` 只输出原因分布 top3；`_try_decrypt` 还把 `media.get_image` 的细粒度 `last_err` 覆盖成笼统的"未找到源文件或解密为空"（exporter.py:250）。
4. **类型兜底文案产生时零日志**：`[链接]`/`[引用]`/`[类型N]` 出现时不记 local_id、原始 localType、rawContent 片段，`_ExportStats` 只给总数——"这条消息为什么显示不对"无法回答。
5. **30+ 处 `except: pass` 把"数据不完整"伪装成"正常为空"**：分片打不开→会话消失、config 损坏→设置重置、统计分片失败→数字无声偏低，全部无日志。

---

## 1. 现有日志架构盘点（哪些是达标的）

保留不动、无需埋点的部分：

- **结构化日志** `siwx/logger.py`：ROUGH/DETAILED 两档、环形缓冲（页面 2000/5000 条）+ 文件缓冲（5 万条）、毫秒时间戳、`desensitize_msg` 脱敏（密钥/wxid/gh_/chatroom/路径/账号键值对）。
- **标准 logging 通道**：`server.py:24-44` `"siwx"` logger → `logs/siwx.log`（RotatingFileHandler 10MB×5）。
- **崩溃兜底** `server.py:59-87`：`sys.excepthook` + `threading.excepthook` → siwx.log；`faulthandler` → `logs/crash.log`。
- **Flask 全局 errorhandler** `server.py:198-223`：未捕获异常记完整 traceback 并返回 JSON。
- **正面范本**（其他模块照抄即可）：
  - `sns_cdn.py`：失败 → 持久文件日志（`_log_media_failure`），成功 → `detailed("sns", ...)`；
  - `api_chat.py:597-606` `_safe_parse_refer`：解析异常兜底 + `detailed("parse", f"... raw={str(text)[:300]!r}")`——全项目唯一一处带原始数据的 DEBUG 日志，是消息级埋点的正确示范；
  - `sns_export.py:294-323`：媒体失败前 30 条带 `tid/idx/reason` 明细 warning + 聚合；
  - `exporter.py` `_ExportStats`：完整性统计进 manifest.json（但只有总数，缺逐条，见 §4）。

---

## 2. 跨模块系统性问题（P0，埋点前应先修的"地基"）

### 2.1 双轨日志割裂（最重要）

**现状**：项目存在两套互不相通的日志轨——

- **任务轨**：`extract.py` / `config_cipher.py` / `mmkv.py` / `memscan.py` / `macos_lldb.py` 等用 `log=print` 回调参数（`ctx["log"]`），进 `_job["logs"]`（前端任务面板）和 siwx.log；
- **结构化轨**：`siwx/logger.py` 的 `rough/info/warn/error/detailed`，受 `/api/logs/settings` 的 Debug 开关控制。

extract.py:108 的 `_d = lambda m: log(f"[extract] {m}")` 名义上是 detailed 语义，实际仍进 print 轨。**用户切到详细模式后，任务轨一条也不会多**。

**改法（模式 A：双写包装器，推荐）**——不动各策略的调用方式，只改回调注入点：

```python
# extract.py 中构造 ctx["log"] / log 参数处，统一换成：
def _dual_log(msg: str):
    print(f"[{time.strftime('%H:%M:%S')}] {msg}")            # 任务轨（保留）
    log.detailed("extract", log.desensitize_msg(msg))         # 结构化轨（受 Debug 开关控制）
```

策略文件内部的 `ctx["log"](…)` 全部无需改动，只需模块标签按策略区分（`strategy:cipher` / `strategy:mmkv` / `strategy:memscan` / `strategy:lldb`）。注意**所有策略日志的消息格式已天然脱敏**（salt 前 16 位 + key 前 8 位）。

**两个实施前提，原稿低估了**：

1. **extract.py 的形参遮蔽**：extract.py:13 已 `from siwx import logger as log`，但 `extract_keys_for_dir` / `global_harvest` / `_keystore_preset` / `decrypt_dir` / `extract_all` / `auto_all` 的形参全叫 `log`，函数体内模块级 logger 被遮蔽，`log.detailed(...)` 在里面不可调用。`_d` 改双写前必须先重命名模块级导入（如 `logger as _slog`）或改形参名，波及全部签名和 server.py 调用点——不是零成本。
2. **策略层绝不可直调 `logger` 模块**：所有策略只持有注入的 `ctx["log"]`，看不到 `siwx/logger.py`；插件策略同签名，更没有 logger 可用。若让策略直调 `logger.detailed(...)`，会破坏注入式设计/插件契约，且绕过任务日志（用户在前端看不到）。双写必须发生在**注入点**（server/extract 构造 ctx 回调处，本节模式 A），或把 ctx 扩展为双通道（`ctx["log"]` + `ctx["dbg"]`）。§3.4 中涉及策略文件的建议均以此为前提。

### 2.2 media.py 的 event 钩子是 no-op

**现状**：`media.py:28` `event = lambda msg: None`。`get_image` 内部 8+ 处（media.py:411,418,435,458,467,473,481,486）产出的三级来源诊断，`label` 已自带 `chat[:12] local_id md5[:8] bm[:8]`，信息量刚好。**CLI 模式下 100% 丢弃**；serve 模式下经 `server.py:877` 进 `tui.log` 实时可见，但不落结构化日志、不受 Debug 开关控制（全库仅此一处接线）。

**改法**：`event` 默认实现改为 `log.detailed("media", msg)`（media.py 直接 `from siwx import logger as log`），serve/CLI 两种模式都生效；tui.log 接线保留。

### 2.3 Debug 开关不持久化 + 入口单一 + 双通道脱节

已核实的事实（含行号）：

- 切换入口仅 `server.py:767-776`（POST /api/logs/settings）；`logger.set_level`（logger.py:35-41）只改内存全局变量，**重启回 ROUGH**（logger.py:28）。
- **CLI/TUI 无任何开关**：`cli.py:160-213` argparse 无 `--verbose/--debug`；tui.py 无级别概念。**无环境变量支持**（grep `os.environ|getenv` 于 server/logger/cli/tui/run.py 均无命中）。
- **重启脱节**：`server.py:28` 启动时无条件把 "siwx" logger 置为 DEBUG（首次启动并非"恢复"），而自研通道回 ROUGH——两通道口径不一致。

**改法**：
1. `set_level` 时把选择写入 `data_dir()/settings.json`（或 api_settings 已有的持久化文件），启动时读回，并同步 `logging.getLogger("siwx").setLevel(...)`（把 server.py:774-775 现有逻辑抽成公共函数，供 api_log_settings_save 与启动路径两处调用）。**时序陷阱**：启动读回必须发生在 `_setup_file_logger()` 之后——它在 server.py:46 模块导入时即执行，且 :28 会无条件把 "siwx" logger 置回 DEBUG；读回放前面会被覆盖。
2. `cli.py` 加全局参数 `--detailed`（argparse 一行）+ 环境变量 `SIWX_LOG_LEVEL=detailed`（`logger.py` 模块加载时读一次）；
3. CLI 模式目前**完全不装崩溃钩子**（crash hooks 在 server.py:59-87 模块导入时安装，而 cli.py 只在 `cmd_serve` 里才 import server）——`keys/decrypt/auto/mcp/doctor` 模式崩了没有 crash.log。建议抽成**独立模块**（而非塞进 `siwx/logger.py`）：`_install_crash_hooks` 依赖 `_paths.app_root()`、标准 logging 的 `_siwx_logger`、`_flush_logs`，而 logger.py 目前是零业务依赖的纯缓冲设计，抽进去会引入 logger→paths 的新依赖方向，与其现状冲突。`run.py`/`cli.py main()` 入口统一安装。

### 2.4 脱敏旁路（埋点时必须避免的新坑）

- `mcp_server.py:669`：`_tool_call` 记录 args（截 500 字）与耗时——覆盖好，但 **mcp.log 不经 `desensitize_msg`**，args 里的 `keyword`（用户搜索词≈聊天内容片段）、`account`、`chat` 是明文。建议落盘前过 `logger.desensitize_msg`。
- `plugins/chat_bridge.py:44,87,112,132`：四处失败 warn 直接内插 `{e}`，异常文本可能携带消息片段，绕过脱敏（`report.py:62` 已是 `log.warn("plugin", log.desensitize_msg(...))` 的正确写法，chat_bridge 未对齐）。注意 `desensitize_msg` 只能处理路径/账号类模式，**对异常文本里携带的消息正文片段无能为力**——改法上除包 `desensitize_msg` 外，还需按 §7 规范 4 评估 `{e}` 是否可能含正文，必要时只记 `type(e).__name__`。
- **埋点规范**：新增日志统一用 `account=... chat=... talker=...` 键值格式（命中 logger.py:160 的 `_ACCOUNT_KV_RE` 脱敏规则）；路径直接写绝对路径即可（`_PATH_RE` 会压成文件名）；**密钥只允许 `mask_key`（6+4）或 salt 前 16 位**；lldb 的 `OK:` 行含 passphrase 明文，落日志前必须剔除（macos_lldb.py:299-317）。

### 2.5 请求级可观测性整体缺失

- `server.py:880-882`：werkzeug access log 被彻底禁用（handlers 清空 + disabled=True），又无替代——无法还原"谁在什么时间调了哪个 API、耗时多少"。
- **改法**：detailed 模式下挂一个仅写结构化轨的请求钩子：`log.detailed("http", f"{method} {path} {status} {ms}ms")`（**不含 query string**，防消息内容/关键词入日志）。
- 建议给每个导出/解密任务生成短 id（如 `job-3f2a`），任务轨与结构化轨的日志都带上，跨轨对账。

---

## 3. 提取 / 解密链路埋点清单

### 3.1 siwx/extract.py（438 行）

> 本文件所有日志走 `log=print` 任务轨（见 §2.1），下述"迁移"均指按模式 A 双写。

- `extract.py:全文件` | `_d` lambda（108 行）名义 detailed 实际 print 轨 | 开 Debug 后 119/131/139/144/153 行的详细埋点不出现在结构化日志 | 迁移：`_d` 改双写 `log.detailed("extract", m)`，module 标签 `"extract"`
- `extract.py:62-67` | `_keystore_preset` 吞 `parse_key` ValueError | 哪条 keystore 记录损坏不可见（表现为"覆盖率差一个"） | `detailed("keystore", f"密钥库记录解析失败 salt={en.salt_hex[:16]}…")`——**注意用条目变量（如 `en`），except 块里的 `e` 是 ValueError 异常对象，`e.salt_hex` 会 AttributeError**；存量损坏记录宜 `warn`
- `extract.py:87-88` | `global_harvest` 无待收割 salt 时静默 return | 看不到"收割被跳过"分支 | `detailed("harvest", "无待收割 salt，跳过内存扫描")`
- `extract.py:97-99` | 收割 0 命中时无输出 | "跑了完整内存扫描但 0 命中"这一关键失败不可见 | `detailed("harvest", f"收割 0 命中，目标 {len(page1_by_salt)} 个 salt")`（附扫描耗时——`global_harvest` 目前无计时，需新增 t0，非纯埋点）
- `extract.py:162-164` | 交叉验证中 `parse_key` 失败 continue 无日志 | 已知密钥集中有坏记录时悄悄少试一轮 | `detailed("extract", "交叉验证: 已知密钥解析失败")`
- `extract.py:174-179` | `keystore.save` 无异常保护 | OSError（磁盘满/权限）炸穿整个 extract_all 且无路径 | 包 try + `error("keystore", f"保存失败: {e}")`
- `extract.py:241-244` | `Path.resolve()` 失败静默降级 | 来源标签生成方式变化可能误触发 conflict 分支而不可见 | `detailed("decrypt", f"resolve 失败，来源标签用原始路径: {db_dir}")`
- `extract.py:255-271` | `_resolve_key`：262-263 行吞 ValueError；"keystore 有记录但 HMAC 不过"分支完全无日志 | 无法区分"无记录/记录损坏/HMAC 不过"——HMAC 不过=微信重装密钥轮换，是重要诊断信号 | `detailed("decrypt", f"salt={en.salt_hex[:16]}… keystore 记录 HMAC 未命中")`（同上，except ValueError 块内用条目变量，勿用 `e.salt_hex`）
- `extract.py:281-284` | `stat()` 失败静默 `mtime=0` | 缓存永不命中、该库每次全量重解，用户只觉得"慢" | `warn("decrypt", f"stat 失败 {e.rel}: {e}")`
- `extract.py:327-329` | 解密成功后二次 `stat()` 写 manifest 无保护 | 源库被微信删除的竞态会炸掉已成功的任务、缓存全丢 | 包 try + `warn("decrypt", f"{rel} manifest 记录失败: {e}")`
- `extract.py:364,400` | `collect_db_files(db)` 无 try | 单个账号目录损坏让 extract_all/auto_all 整体失败，其余账号全不跑 | 逐账号 try + `error("discover", f"收集 {db} 失败: {e}")` + continue
- `extract.py:371-379,409-417` | 两处重复的覆盖判定吞 ValueError | 损坏记录被当"未覆盖"触发不必要收割；且两段是复制粘贴 | `detailed("keystore", …)`；顺带下沉重复代码
- `extract.py:432-436` | 0 密钥时静默跳过解密 | Debug 看不出"解密被跳过及原因" | `detailed("extract", f"账号 {wxid} 无已验证密钥，跳过解密")`

### 3.2 siwx/pool.py（82 行）

- `pool.py:36-38` | `load_manifest` 吞一切 Exception 返回 `{}` | 缓存损坏被当首次运行，"全量重解"无原因 | `warn("pool", f"缓存清单加载失败 {p}: {type(e).__name__}")`
- `pool.py:41-47` | `save_manifest` 无异常处理 | 解密完成后清单写失败裸抛且无路径——产物在、清单丢、下次全量重解 | `error("pool", f"缓存清单写入失败 {out_root}: {e}")`
- `pool.py:54-58` | `_worker` 捕获 Exception 只返回 `str(e)` | 异常类型名丢失；子进程无法关联 logger | 返回 `f"{type(e).__name__}: {e}"`；Debug 下可带 `traceback.format_exc()` 尾部。**注意**：返回串会经 `_on_done`（extract.py:317）整串进任务面板日志，长堆栈会刷屏——若要"堆栈只进结构化轨"，需同步改 `_worker`/`_on_done` 的返回协议（如返回 `(short_err, long_tb)` 二元组，主进程分轨处理），不能只改 `_worker` 一侧
- `pool.py:63-82` | `decrypt_parallel` 无起止/并发数日志 | 看不到"用 N 进程解密 M 个任务、耗时 X" | `detailed("pool", f"并行解密启动 workers={n} tasks={len(tasks)}")` + 结束汇总
- `pool.py:77` | `mp.Pool(n)` 创建失败裸抛 | Windows spawn 环境问题只见崩溃无上下文 | try 后复用 66 行串行路径 + `warn("pool", …)`

### 3.3 siwx/sqlcipher.py（201 行）

> 纯原语层。**本层建议不引入 logger 依赖**：下述 `detailed/warn` 埋点均落在调用方（extract.py 的 `collect_db_files` 调用处、pool 的 `_worker`），sqlcipher 自身只负责把上下文（reason/src/salt）通过返回值或异常消息带出去。若决定在本层直接打日志，需明确放弃"无 logger 依赖"立场并处理与 extract.py 的依赖方向，二者取其一，不能两头都要。

- `sqlcipher.py:75-76` | page1 临时复制分支吞 OSError | "微信占用且复制失败"（TEMP 满/杀软拦截）与"空文件"混为同一 None | 本层返回 `(None, reason)` 或调用方补记 `warn("sqlcipher", f"page1 读取失败 path={path} err={e}")`
- `sqlcipher.py:80-81` | page1 过短/全零 return None | 哪个文件、实际长度不可见 | collect_db_files 侧记 `detailed("sqlcipher", f"跳过 {p.name}: page1 无效 (len={len(page1 or b'')})")`
- `sqlcipher.py:96-101` | stat 失败 continue / size 不足一页静默跳过 | 库文件无声消失 | 调用方侧 `detailed("sqlcipher", f"跳过 {p}: {原因}")`
- `sqlcipher.py:124-138` | 打开源库失败无 src 路径/errno 上下文。**细节**：138 行的裸 `raise` 位于内层 `except OSError`（130-138，临时复制兜底）中，重抛的可能是复制/建临时文件阶段的异常，不一定是原始 `open(src)` 的 OSError | `_worker` 只能传回 "[Errno 13]"，且两段失败原因混同 | 分段包消息：open 失败 `raise OSError(f"打开 {src} 失败: {e}") from e`；复制兜底失败 `raise OSError(f"复制 {src} 到临时文件失败: {e}") from e`，两段区分
- `sqlcipher.py:145-146` | page1 HMAC 失败的 ValueError 无 src/salt/key 上下文 | 多账号场景无法定位是哪个库哪个密钥 | 消息补 `src={src.name} salt={page1[:16].hex()} key={mask_key(...)}`（严守脱敏）。**注意 `mask_key` 定义在 extract.py:28 而 extract 导入 sqlcipher——本层直接 import 会循环，需先把 `mask_key` 下沉到公共模块（sqlcipher.py 或 logger 旁）再引用**
- `sqlcipher.py:159` | 输出临时文件 `open` 无保护 | 权限/磁盘满时裸抛无 dst 上下文 | `error("sqlcipher", f"输出临时文件创建失败 dst={dst}: {e}")`
- `sqlcipher.py:196-201` | finally 清理临时文件若 unlink 失败会掩盖原始解密异常 | Windows 文件占用时真实错误被顶掉 | **finally 块里两处都要包**：`tmp_copy.unlink`（198-199）与 `tmp_out.unlink`（200-201），各包 try + `detailed("sqlcipher", f"临时文件清理失败 …")`——只包后者会漏掉同样风险

### 3.4 siwx/strategies/（6 个文件）

- `strategies/__init__.py:29-34` | 策略链循环无日志：早停、use_memory 跳过均静默 | 看不到链上走了哪几个策略、哪个被跳过——排"为什么没跑内存扫描"的第一现场 | `detailed("strategy", f"执行策略 {mod.__name__}")` / `f"跳过 {mod.__name__} (use_memory=False)"` / 早停记"已全覆盖"
- `strategies/__init__.py:35-38` | 内置策略异常只进 print 轨单行（无堆栈） | "单策略失败不影响整链"的容错让失败完全隐形 | 双写 `warn("strategy", f"{name} 异常: {type(e).__name__}: {e}")` + detailed 带 traceback 尾部（DETAILED 模式下两者会有一次重复，可接受；或 detailed 内带 traceback、warn 只发不含堆栈的单行）
- `strategies/__init__.py:49-54` | 插件策略加载 `except Exception: return` 彻底静默 | 插件策略层无声消失 | `warn("strategy", f"插件策略加载失败: {type(e).__name__}: {e}")`
- `strategies/__init__.py:65-68` | 插件策略**运行期**异常同样只记单行无堆栈（原稿只覆盖了 49-54 的加载期） | 插件策略失败与内置策略同样隐形 | 同 35-38 的双写方案

**config_cipher.py（主力策略，全部日志走 `ctx["log"]` 任务轨）**：

- `config_cipher.py:全文件` | 全部日志走 `ctx["log"]` | Debug 开关管不到主力策略 | **按 §2.1 模式 A 在注入点双写**（标签 `strategy:cipher`），本文件不直接 import logger。"验证成功 N 个"（319 行）与"未检测到微信进程"（218 行）保留双写 info/print。**降级取舍**：中间的进度类输出（每 PID 区域数/MB、needle 数、282 行"未取得配置 blob"等）是用户排障的首要看点，保留任务轨 info，只把**失败点**（掩码求解失败、候选 0 产出、指针链计数）降为 detailed，避免一刀切削弱 ROUGH 模式反馈
- `config_cipher.py:227-230` | `open_process` 失败仅猜测"权限不足?" | 真实失败码（5=拒绝访问 vs 87=参数错）不可见 | 配合 winproc 错误码改造（见 §3.6），记 `detailed("strategy:cipher", f"PID={pid} 打开失败 err={code}")`
- `config_cipher.py:287-289` | 每个 blob 候选产出数不记日志 | "blob 在但解不出候选"（内置掩码失效第一信号）只能靠兜底日志间接推断 | `detailed("strategy:cipher", f"blob#{i} len={len(blob)} 候选={len(cands)}")`（blob 内容绝不落日志）
- `config_cipher.py:292-311` | crib 与打分两条掩码求解路都失败时无日志 | 微信升级后掩码彻底失效，用户只见"验证 0 个" | `detailed("strategy:cipher", "掩码求解失败: crib 与 ASCII 打分均未命中")`
- `config_cipher.py:305` | 掩码求解只用 `blobs[0]`（字面属实；验证闭包 `check`（295-303）仍会遍历全部 blobs，只是掩码推导不用它们） | 其余 blob 不参与掩码推导不可见 | `detailed("strategy:cipher", f"掩码求解仅用 blob[0]（共 {len(blobs)} 个）")`
- `config_cipher.py:63-66,94-97,106-109` | `bytes.fromhex` ValueError 静默 continue | 候选因非法 hex 被丢弃的次数不可见（掩码错乱时大量触发，是诊断线索） | `_try_candidates` 入口记 `f"候选 {len(cands)} 个, 非法 {n_bad} 个"`
- `config_cipher.py:114-121` | 逐候选 HMAC 失败无计数 | "试了多少候选、多少 HMAC 失败"决定扫描是否正常 | `_try_candidates` 返回 `(found, tried)`，调用方记汇总
- `config_cipher.py:264-279` | 指针链每层 read_mem 返回 None 全静默 | 结构体偏移因微信版本失效时，失败发生在哪一层无从判断——排"升级后策略失效"最需要的信息 | 循环外加计数器（node 命中 X / config 命中 Y / blob 命中 Z），结束 detailed 一条

**keystore_source.py**：`14-17` parse_key 失败静默 continue → `detailed("strategy:keystore", f"salt={salt[:16]}… 记录解析失败")`；`10-23` store 为空/全部未命中无输出 → 无条件记 `f"密钥库 {len(store)} 条, 命中 {found} 个"`（区分"库空"与"全过期"）。

**memscan.py（对外完全黑盒的兜底策略）**：`28-30` open_process 失败 continue **无任何日志** → `detailed("strategy:memscan", f"PID={pid} 打开失败")`；全文件无 regions/MB/候选/HMAC 计数 → 循环外加计数，结束时 detailed 一条汇总（含 found==0 的情况）。

**mmkv.py**：`58-60` MMKV 目录不存在静默 return 0 → `detailed("strategy:mmkv", f"MMKV 目录不存在: {mmkv_dir}")`；`73-76` 枚举 OSError → `warn`；`85-88` 单文件读失败 → detailed；`89-93` 头部校验失败（len/total_size 是格式变化的唯一线索）→ detailed；`100-107` **全部候选密钥 GCM 解密失败静默**——这是"文件存在但密钥未命中"的最重要细分原因（派生密钥不对 vs 文件不是 tinfo 格式），必须记 `f"{name}: {len(candidates)} 个候选派生密钥全部 GCM 校验失败"` + 候选 label 列表；`115-117` 明文中找不到 rel 路径 → detailed。

**macos_lldb.py**（外层函数全部已走 `ctx["log"]`——经 34 行注入，39-99/303-321 各处均是；**print 仅存在于 lldb 子进程的内嵌脚本字符串（125-280）里，那是 lldb Python 环境唯一的回传通道，无法也不必"迁移"**）：
- `87` passphrase 抓到但派生 HMAC 未命中 | 逐 salt 失败无日志（95 行已有无条件汇总 `派生 {derived} 个密钥`，派生 0 个时可见；缺的是逐 salt 细分） | detailed
- `289-296` | 超时时临时脚本文件残留。**机制**：`os.unlink`（296 行）在 `subprocess.run`（293-295）**之后**，超时异常直接从 run 抛出（318 行捕获），unlink 根本没执行——后果是脚本残留，而非"unlink 吞掉异常上下文" | try/finally 保证清理（修复方向不变，归因已更正）
- `299-317` | **并非"原始输出全部丢弃"**：`BP:`/`FAIL:`/`SYM:`/`HIT:`/`PROCESS_DEAD:`/`Traceback` 各前缀行及 stderr（截 4000 字符）已逐条转入日志；`OK:` 行被识别并 return（不落日志）。真正丢弃的只有不匹配任何已知前缀的行 | Debug 下记未识别行尾部（截断），**必须先剔除 `OK:` 行（含 passphrase 明文）**
- `168` 内嵌脚本符号扫描失败 → 补 `SYMERR:` 前缀供外层解析（外层 306-307 已预留解析钩子，只需改内嵌脚本）
- `186-188,267-271` Detach 失败静默（微信可能卡在断点暂停，用户可感知的副作用）→ 脚本内 print `WARN:DetachFail`，外层 `warn`

### 3.5 siwx/keystore.py（165 行）

- `keystore.py:136-139` | **主路径 `load()`**：DPAPI `_unprotect` 或 JSON 解析失败 → `return {}` | 整个密钥库静默清零，下次必然全量重收割，用户以为缓存"丢了"而日志毫无痕迹。**本链路最值得修的一处** | `warn("keystore", f"密钥库读取失败 path={p} err={e}（可尝试备份该文件后重跑）")`
- `keystore.py:127-130` | 旧位置读取失败静默 | 存量密钥迁移无声放弃 | 同上 `warn`
- `keystore.py:131-134` | 迁移 `save` 失败 `except OSError: pass` | 此后每次启动都走旧库慢路径 | `detailed("keystore", f"迁移到 {p} 失败: {e}")`
- `keystore.py:92-96,109-113` | `store_path` 失败静默回退 legacy / `_safe_root` 兜底 TEMP | 密钥库实际写在哪个路径不可见（排"密钥库为什么总为空"的第一问题）；落 TEMP 重启即丢 | `detailed` / `warn("keystore", "数据目录不可用，密钥库回退到 TEMP（重启后丢失）")`
- `keystore.py:142-147` | `save()` 无异常保护，os.replace 失败裸抛无路径 | .tmp 残留 | `error("keystore", f"保存失败 path={p}: {e}")`。**注意**：只补日志、不改异常传播语义（记完仍抛出）——不要顺手吞掉异常而掩盖写入失败
- `keystore.py:150-153` | chmod 0600 失败静默（非 Windows 明文兜底路径，安全相关） | `detailed("keystore", f"chmod 0600 失败 {p}: {e}")`

### 3.6 siwx/discover.py / siwx/winproc.py / siwx/env_info.py

**discover.py**：`26-29` 手动路径配置损坏静默返回空（引导页直接回空，无从排查）→ `warn`；`33-35` 手动路径逐条被拒的 error 文案被丢弃 → `detailed`；`121-122` 进程枚举 AccessDenied → detailed（NoSuchProcess 不必记）；`149-150,170-171,195-196` 目录枚举失败静默削减候选集 → detailed + 循环后记 `f"扫描 {len(roots)} 个根, 命中 {len(out)} 账号"`；`303-312` 注册表读取失败 → detailed；`104-124` `find_wechat_pids` 从不记录结果 → Debug 下随时记 `f"微信进程: {pids}"`。

**winproc.py（整条链唯一的"错误码黑洞"，仅静态审阅未运行）**：

- `winproc.py:31-34` | `open_process` 失败返回 None 不调 GetLastError（winproc.py:10 的 `ctypes.windll.kernel32` 未用 `use_last_error=True`） | 所有策略只能写"权限不足?"猜测 | `ctypes.WinDLL("kernel32", use_last_error=True)`。**注意兼容性**：`open_process`/`read_mem` 有 7 个调用点，改返回元组需全部同步改；更稳的做法是保持签名不变，在函数内部失败时调 `ctypes.get_last_error()` 走模块级计数器/一次性 detailed，由策略层汇总
- `winproc.py:42-47` | ReadProcessMemory 失败与空读混同（`read_mem(...) or b""`），无错误码 | 区域中途失效时扫描结果悄悄缩水 | **热路径勿逐块打日志**：失败计数器由策略层汇总，或加节流
- `winproc.py:57-73` | VirtualQueryEx 返回 0 即 break 无记录（实际 break 在 63-65） | 枚举提前终止（句柄失效）与正常走完不可分 | `detailed("winproc", f"区域枚举终止于 {hex(addr)}")`
- `winproc.py:66-68` | ≥500MB 区域被静默过滤 | needle 恰在大堆里时扫描必然失败而无线索 | 过滤计数 + `f"跳过 {n} 个超大区域(≥500MB)"`

**env_info.py**（定位是"失败可接受"，但失败会让 bug 报告缺关键行）：`104-111` paths 段失败→报告里目录四行整块消失（path 问题恰是最常见故障）→ `warn` + 报告里填 `"不可读"` 占位（对齐 116-117 行密钥库的正确做法）；`53-55,92-95` 插件 import 失败整行消失/frozen 误判 → detailed。**注意**：`63-64` 并非静默——它返回"开启（状态不可读）"占位行，报告里可见；问题只是无法区分"插件系统坏了"与"计数失败"。另 frozen 误判（92-95）建议考虑 except 分支填 `"未知"` 而非回退 `False`——否则 bug 报告会把打包产物写成"源码运行"，这是报告正确性问题，光打日志救不回来。

---

## 4. 渲染 / 导出 / 媒体链路埋点清单（用户最关心的部分）

> 核心修法集中在三件事：**① 媒体失败元组带上 local_id/ts/md5；② 兜底文案产生点加逐条 detailed（含 rawContent 截断）；③ media.event 改道进 logger.detailed**。做到这三件，"哪条消息、什么原始类型"就能从日志直接回答。

### 4.1 siwx/exporter.py（823 行）

- `exporter.py:152-162` `_log_media_failures` | 只输出原因分布 top3；`failures` 列表只有 reason 字符串 | **哪条消息的图/语音丢了、当时用的哪个 md5 不可见** | `_decrypt_media_parallel:193-197` / `_decrypt_media_serial:208-216` / `_export_voice_media:260-266` 改收集 `(local_id, ts, md5, reason)` 元组；逐条 `detailed("export", f"图片失败 local_id={local_id} ts={ts} md5={(md5 or '')[:8]}… {reason}")`；warn 聚合行保留
- `exporter.py:223-229` `_decrypt_one`（多进程子进程） | 子进程不能直接用 logger，失败只返回 reason | 上下文要在主进程补齐 | **问题在收集端而非子进程**：`_decrypt_one` 返回的 `key`（228 行，`_media_key(md5, bubble_md5, local_id, ts)`）已携带定位所需的 md5/local_id/ts，是主进程 `imap_unordered` 循环（192-197）只把 `reason` 追加进 failures、丢弃了 key。改收集循环保留 `(key, reason)` 即可逐条 `detailed("media", ...)`；**无需改子进程返回协议**（Windows spawn 模式下多进程协议改动有兼容性成本）
- `exporter.py:250` | `return None, "未找到源文件或解密为空"` **覆盖了** `media.get_image` 的细粒度 `last_err`（attach 解密失败/V2 密钥未命中/Bubble 未知格式/本地无原图） | "没这个文件"与"有文件解不开"分不清 | `get_image` 失败时第二返回值就是 `last_err` 字符串，直接透传 `return None, info` 即可，几乎零成本（现实现丢弃了 info，见 exporter.py:240-250）
- `exporter.py:128-149` `_attach_media` | media_map 未命中（L145-146）或槽位不匹配（L147-148）时静默 return | 前端渲染"📷 图片未下载"后无从对应到日志 | 对 `t in (3,47,34)` 且 `mf` 为空的分支：`detailed("media", f"媒体未回填 localType={t} local_id={local_id} ts={ts} md5={(md5 or '')[:8]}… bm={(bubble_md5 or '')[:8]}…")`（这正是失败集合，量可控）
- `exporter.py:312-330` `_ExportStats.observe` | `quote_fail`（L319）、`fallback_labels`（L327-328）只累加计数 | "这条引用没解析出来"只有总数没有个体 | 计数同一分支加 `detailed("export", f"quote解析失败 local_id={msg.get('localId')} ts={msg.get('createTime')} raw_head={…hex 头…} len={len(rawContent)}")`；fallback 文案记 `content[:120]`。**隐私与量级口径**：`_FILE_LOG` 存的是未脱敏原文（脱敏只发生在 get_logs/export_logs），rawContent 即聊天正文——**正文只记 hex 头 + 长度（对齐 §5.2 `_decode_content` 的做法），必要时至多前 16~32 字符**；且 3 万条导出逐条打会刷爆 5 万条环形缓冲，逐条明细用 detailed + 评估采样
- `exporter.py:68-87` `collect_avatars` | head_image.db 不存在/查无/write 失败均无日志；**且 81 行 `(dest/fn).write_bytes(row[0])` 没有任何 try——写失败是未处理异常，会让 run_export 的头像阶段整体崩溃**（不只是"静默无日志"） | 头像空白无从排查；写失败直接炸导出 | 逐条 write 包 try + 计数；汇总一条 `detailed("export", f"头像缺失 db_exists={} 用户数={} 命中={} 写失败={}")`
- `exporter.py:345-362` `run_export` 开始/两遍扫描 | 只有 progress（不进日志） | 看不到导出参数与第一遍扫描结论 | `detailed("export", f"开始导出 chat={chat} fmt={fmt} range=[{start_ts},{end_ts}] media={want_media}")`；第一遍后 `f"扫描: msgs={} images={} voices={} senders={}"`
- `exporter.py:396-410,457-461` | 媒体完成数/写出条数只有 progress | — | `detailed("export", f"媒体完成 img={}/{} voice={}/{}")`、`f"写出完成 written={} collected={}"`
- `exporter.py:479-489` manifest 写入 | 成功有 info（好） | 日志侧无 type_counts 对照 | `detailed("html", f"type_counts={stats.type_counts}")`
- `exporter.py:542-549` `_plugin_export_format` | `except Exception: return None` | 插件格式失败被当未知格式走到兜底 ext | `warn("plugin", f"导出格式查询失败: {e}")`
- `exporter.py:588-595` `_run_after_export` | ensure_loaded 失败时**所有** after_export 钩子静默跳过 | `warn("plugin", f"after_export 钩子加载失败: {e}")`
- `exporter.py:799-801` `run_export_multi` 单会话失败 | 异常只进 results/progress 不进 logger | 批量导出后翻日志看不到哪个会话挂了 | `error("export", f"会话导出失败 chat={chat}: {type(e).__name__}: {e}")`

### 4.2 siwx/export_stream.py（260 行）

- `export_stream.py:70-72` | `_enrich_row` 未知类型渲染成 `类型N`/`[类型N]`、quote/link 解析失败直接出兜底文案，均无日志 | **这是"显示不对但查不到原始类型"的核心盲区**（注意：引用解析因**异常**失败的路径已有日志——api_chat.py:603-605 `_safe_parse_refer` 打 detailed；缺日志的是"无异常但返回 None"的兜底，如 t==57 未匹配到 refermsg 时 `_fmt` 出 `[引用]`，实施时勿重复埋点） | 尾部判定：`if t not in TYPE_NAMES: detailed("parse", f"未知类型 t={t} local_id={local_id} ts={ts} raw_head={…} len={…}")`；quote 为 None 且 t==57（或 content 以兜底前缀开头）时 `detailed("parse", ...)` 记 `local_id/ts/md5`（正文按 §4.1 的 hex 头口径）
- `export_stream.py:61` + `api_chat.py:662-669` | type 3/47 消息 XML 无 md5 且 packed_info 无 32 位 hex 时 `md5=bubble_md5=None` | 该图三级来源全靠 local_id+ts，成功率骤降但无日志 | `detailed("media", f"图片无md5可定位 local_id={local_id} ts={ts}")`
- `export_stream.py:124-131` `message_stream` | 分片打开/查询无 try（对比 `count_messages:98-100` 有 warn，行为不一致） | 表缺失/库损坏直接中断整个导出 | 包 try：`warn("export", f"流式读取分片失败，消息可能缺失: {Path(db).name}: {e}")`。**行为变化提示**：从"中断导出"改为"部分导出"后，缺失靠 run_export 的 count-vs-written 对账 warn（458-461）和 manifest.json 兜底——更完整的做法是把失败分片计数写进 manifest，否则批量导出（run_export_multi）下逐会话仍只靠一条 warn 定位
- `export_stream.py:104-136` | 参与归并的分片数/总条数无记录 | `detailed("export", f"message_stream: shards={len(iterators)}")`

### 4.3 兜底文案产源地：siwx/api_chat.py `_fmt`（354-392 行）

- `api_chat.py:354-392` | `[引用]`/`[链接]`/`[类型X]` 文案在此产生，无逐条日志 | 全链路只有 `_ExportStats` 总数 | **埋点落 `parse_quote_or_link`，不要落 `_enrich_row`**：`_enrich_row` 只被导出流（export_stream）使用，API `/messages` 端点是内联手写循环（api_chat.py:845-885）不经过它——"API 与导出共用"对 `_enrich_row` 不成立（api_chat.py:682 的 docstring 是过时描述）。三处真正共用的收束点是 `parse_quote_or_link`（build_messages:745、`_enrich_row`:63、`/messages`:872 均调用），落这里才能同时覆盖导出流、/messages API 和 MCP；若在 `_fmt` 内做，需传入 local_id 上下文

### 4.4 siwx/media.py（504 行）

- `media.py:28` | `event = lambda msg: None`（根问题，见 §2.2） | 开 Debug 也看不到图片为什么没解出来 | 默认实现改 `log.detailed("media", msg)`；label 已自带截断与脱敏，直接可用
- `media.py:64-65` `_load_key_cache` | 缓存文件损坏静默当无缓存。**后果修正**：损坏 ≠ 必然报"V2 密钥未命中"——`candidate_keys`（203-223）随后会走 kvcomm 离线派生兜底，派生也失败才报该错；但缓存损坏确实少了一条密钥来源，且日志无从区分"缓存坏了"与"缓存没坏但密钥不对" | 非 FileNotFoundError 时 `detailed("media", f"密钥缓存读取失败: {type(e).__name__}")`
- `media.py:210-212` | 缓存坏 key 记录静默跳过 | `detailed("media", f"缓存密钥格式非法 wx={wx_clean}")`
- `media.py:146-147,173-174` | VoipEngine.dll 加载失败、wxgf 转码失败静默——wxgf 原样落成浏览器打不开的 .jpg | `_get_voip_fn` 失败 `warn("media", f"VoipEngine.dll 加载失败，wxgf 将无法转码: {e}")`（每进程一次——**现状无节流机制**，失败时 `_VOIP_FN` 保持 None，每次 convert_wxgf 都会重试重报，需新增 `_VOIP_TRIED` 之类的标记位，否则每张 wxgf 图都刷一条 warn）；`convert_wxgf` 失败 detailed
- `media.py:187-200` `decrypt_v2_body` | 三处 `return None, None`（189 过短 / 192 aes_size 非法 / 196 解密后头部非已知图像魔数）不区分原因，上层统一报"V2 密钥未命中" | 密钥错 vs 文件结构坏分不清。**注意 196 行并非"签名不匹配"**：V2 格式没有签名字段，它是"密钥错或密文损坏"的混合信号，reason 码应用 `head-not-image`/`bad-head-sig` 而非 `sig-miss` | 二选一：①返回第三个元素 reason（`too-short`/`bad-aes-size`/`head-not-image`）拼进 `last_err`——可透出 API 层，但返回签名变化需同步改全部 5 个解包调用点（media.py:361,367,432,439,464）；②在 `decrypt_v2_body` 内部直接打 detailed（它本就拿到全部上下文）——零调用方迁移成本，但 reason 不透出。按是否需要 API 层可见来取舍
- `media.py:231-232` | hardlink.db 不存在或 md5 长度不对 → `return []` | `detailed("media", f"hardlink不可用 db={hl.is_file()} md5_len={len(md5 or '')}")`
- 正面项：`get_image` 的 `last_err` 逐级覆盖（420/437/442/451/469/475/488）逻辑合理，缺的只是两个出口（exporter.py:250 覆盖 + event() 丢失）——修出口即可，内部不用动

### 4.5 siwx/voice.py

- `voice.py:374-375` | media_*.db 损坏静默跳过，最终统一报"语音数据不存在或尚未同步" | "真没有"与"库打不开"分不清 | `warn("voice", f"media分片打开失败: {db.name}: {e}")`——**现状是 `except sqlite3.Error:` 未绑定 `e`，实施时必须改成 `except sqlite3.Error as e:`，否则 NameError**
- `voice.py:302-309` `_chat_id` | 映射失败静默，后续查询命中率下降无人知晓 | `detailed("voice", f"Name2Id查询失败 err={e}")`
- `voice.py:221-224` | pilk 导入失败 err 为空串，掩盖"默认依赖本身挂了" | `detailed("voice", f"pilk 导入失败: {e}")` 并让 err 非空
- `voice.py:52-56` | voicelength 等属性非法回退 0（时长显示 0） | `detailed("voice", f"voicemsg属性非法 {name}={attrs.get(name)!r}")`（量小）

### 4.6 siwx/html_template.py（前端渲染侧）

- `html_template.py:20-68` `build_chat_data` | 整个文件不 import logger；渲染参数无服务端日志 | 函数尾部 `detailed("html", f"HTML数据构建: msgs={} media={} quote={} link={} unknown_types={...}")`。**架构取舍**：html_template.py 有意保持零业务依赖（便于模板独立），直接 import logger 引入新依赖方向；更省的做法是在调用方（exporter）拿到返回值后打 detailed，二者取一
- `html_template.py:342`（JS） | 未知类型+mediaPath 回退纯文本，服务端不知道 | JS 分支加 `console.warn('[render] unknown type '+t, msg.timestamp)`（导出页 F12 可见）；服务端靠 4.1 的 `_attach_media` miss 埋点间接覆盖
- `html_template.py:419-424`（JS） `__imgErr` | `<img onerror>` 静默处理。**描述修正**：并非"换占位块"——它原地修改 `<img>`（加 `.broken` class、删 src、设宽高、设 textContent），而 `<img>` 是替换元素、textContent 不会渲染出文字；若 CSS 未对 `.broken` 定义样式，用户看到的只是空白（疑似另一处小 bug，建议核查 `.broken` 的 CSS） | `console.warn('[img-err]', img.src, ts)`。**改动量提示**：`__imgErr(this)` 的调用点（337/339 onerror）只传 img 元素、拿不到 msg 对象，要打时间戳必须先由 renderMsg 把 `msg.timestamp` 存进 `data-ts`（需穿参改模板拼接，成本中等）；更根本的是导出打包前校验 `media_map` 引用的文件存在

### 4.7 朋友圈：siwx/sns.py / sns_cdn.py / sns_export.py / api_export.py

- `sns.py:458-461 + 977-979` | `parse_timeline` XML 解析失败 return None，`iter_timeline` 静默 continue——注释自认实测 5684 条有 17 条失败，**朋友圈导出无声缺帖** | `iter_timeline` 统计：逐条 `detailed("sns", f"XML解析失败 tid={tid} content={content[:200]!r}")`。**注意 tid 也是 bytes**：iter_timeline 里 `con.text_factory = bytes`（sns.py:971），tid/user/content 都要 `errors="replace"` 解码，否则 f-string 打出 `b'...'`；content 片段可能含他人 wxid/昵称，建议过 `desensitize_msg`。循环外 `warn(f"共 {n} 条动态解析失败被跳过")`
- `sns.py:680-693` / `928-957` | 缓存图/图池导出三段失败（读/解密/写）只计数无文件名无日志 | `detailed("sns", f"…失败 file={f.name} stage=…")` + 结束汇总
- `sns.py:749-751` | 媒体无尺寸→match 直接 none | `detailed("sns", f"媒体无尺寸 tid={tid} idx={idx}")`
- `sns_cdn.py:206-220` `_decompress` | 解压失败 return raw 静默，之后被上报成 `undecodable`，**错误方向被带偏** | 两种打标方式成本不同，需先选：①只加 `detailed("sns", f"解压失败 enc={encoding}")`——最省，但 `_decompress` 在 `fetch()` 内不持有 url/out 上下文，日志无 url 归属，排查要靠时间戳对照；②让 reason 变 `decompress-error`——需改 `fetch` 返回结构或在 `fetch_media` 侧二次探测（如 gzip 魔数检查），成本高于一句话
- `sns_cdn.py:328-341,364-365` | 缓存写失败（fetch_media:504 不检查返回值）/读失败静默 | `detailed("sns", …)`
- `sns_export.py:117-126` | media_map 无 `(tid, idx)` 键时导出页静默显示死图（CDN URL 常已过期） | `detailed("sns", f"媒体未落盘 tid={tid} idx={idx}")`
- `sns_export.py:314-323` | 失败明细 warn 只覆盖前 30 条（设计如此，310 行硬上限）。**成功并非"无记录"**：链路级成功已由 `fetch_media` 经 `_log_media_ok`（sns_cdn.py:458 缓存命中 / 506 下载成功）打 `detailed("sns", ...)`（含 ext/字节数/url）——缺的是 **tid/idx 归属维度**，而非成功记录本身 | 不要在 sns_export 再写一条成功 detailed（会与 `_log_media_ok` 重复）；正确做法是给 `fetch_media`/`_log_media_ok` 增传 tid/idx 上下文（改签名或写入返回的 out 字典）。超出 30 条的失败走 `detailed("sns", ...)`
- `api_export.py:43-44,56-57,70-71,109-110` | 插件格式列举失败/下载拒绝/预览失败均无日志 | 插件列举失败 `warn`；`/download`、`/open` 的 404 拒绝是安全边界行为，可能被扫描/误触频繁触发，用 `detailed`（warn 进文件日志有刷量风险）；`/render` 的 109-110 是真异常，记 `warn` 合理

---

## 5. 服务 / API 层埋点清单

### 5.1 siwx/server.py

- `server.py:83-84` | faulthandler 启用失败静默 | 用户以为有崩溃记录实际没有 | `warn("server", f"crash.log 初始化失败: {e}")`
- `server.py:447-463` | 任务状态机转换（/api/run 置 running:636-639、finally 置 done:460-463）无日志；409 拒绝无日志。**该区间内 except 分支的失败路径是有日志的**（448 行 `_siwx_logger.exception("任务执行失败")`），无日志的仅是状态转换点与 409——勿按"447-463 全无日志"理解 | 状态时序、任务排队/拒绝历史 | `detailed("server", f"job 状态: running={} mode={} done={}")` ×2；409 时 `warn("server", "任务被拒绝: 已有任务在运行")`。**补全**：置 running 的入口还有 auto-sync 调度（server.py:840-841），其"已有任务"等价分支（838-839 continue）也应一并埋点
- `server.py:632-644` | `/api/run` 不记请求参数；`get_json(silent=True)` 吞坏 JSON | 无法还原用户提交的任务参数 | `detailed("server", f"/api/run mode={mode} workers={} db_dir={}")`；坏 JSON 时 `warn`
- `server.py:611-614` `/api/status` | 内层 ValueError + 外层 Exception 双双 pass | 账号 db_count=0 无原因 | `detailed("api", f"[status] 账号={wxid} db 扫描失败: {type(e).__name__}: {e}")`
- `server.py:687-688,715-716` | `_tail_app_log`/`_tail_mcp_log` 读失败返回 [] | 日志页空白无解释 | `error("server", f"读取 siwx.log 失败: {e}")`（注意勿自写自递归，可用 print 或一次性标志）
- `server.py:908-909` | TUI 状态栏 getter 每秒吞一次异常 | 状态栏长期"…"的根因不可见 | 失败计数，连续 N 次 `warn`（节流）
- `server.py:472-475,498-501` | **`from siwx.plugins import registry`（插件系统注册表本身）导入失败**静默（单个插件的 import 失败在 plugins/loader.py 另有处理）。两处影响不同，勿混为一谈：472-475 使 `_emit_task_event` 失效（任务事件不广播），498-501 使 `_plugin_theme_links` 失效（主题 CSS 注入失败，与任务事件无关） | `warn("plugin", f"插件系统注册表导入失败: {e}")` ×2（各自注明影响面）
- `server.py:880-882` | werkzeug 禁用且无替代 access log | 见 §2.5

### 5.2 siwx/api_chat.py

- `api_chat.py:706-707` | **`build_messages` 的 `except sqlite3.Error: pass`** | Msg_ 表查询失败→该分片 0 条消息。**调用方已核实：全仓生产调用方只有 MCP（mcp_server.py:172,193）**——导出走 `export_stream.message_stream`（exporter.py:97 等），API `/messages` 是内联手写循环（845-885），都不经过它；api_chat.py:682 docstring 的"导出与 API 共用"是过时描述。故爆炸半径是 **MCP 工具结果缺段**，不是导出缺会话 | `error("api", f"[build] 表查询失败 chat={chat} db={db.name}: {e}")`（warn 以上，直接影响数据完整性）。导出侧等价且更严重的风险在 §4.2 `message_stream:124-131`（连 try 都没有，直接中断整个导出）
- `api_chat.py:80-81` `shard_index` | 分片打不开即被剔除索引，整会话"消失" | `detailed("api", f"[shard] 分片读取失败 db={db.name}: {e}")`
- `api_chat.py:266-278,329-330` `_contact_names` | schema 降级/contact.db 打不开均静默，且**错误空结果会进缓存** | `detailed`（降级）/ `warn`（打不开）
- `api_chat.py:343-351` `_decode_content` | zstd 解压失败、UTF-8 解码失败 → **消息正文静默变空串** | `detailed("parse", f"[content] 解压失败 local_id={} len={} head={raw[:16]!r}")`（只记 hex 头，不透传正文，脱敏安全）
- `api_chat.py:410-411,426-427,832-833,947-948,1001-1002,1066-1071` | 会话/联系人相关的 sqlite3.Error 静默六连。**其中两处与"pass"不符**：410-411（`_sender_map`）实为 `except sqlite3.Error: return {}`；1066-1071（/avatar）实为降级到插件头像解析器再 404——有明确降级处理，问题是无日志而非无处理。其余四处为 pass | 各自静默归零或缺数据 | 逐处 `detailed("api", …)`；sessions 的三级降级链（462-481）用 `warn`
- `api_chat.py:1092-1097,1137-1141` | local_id/ts 参数非法静默归零 → 后续误导性 404 | `detailed("api", "[voice/image] local_id/ts 参数非法，已归零")`
- 请求入口 | `/accounts`、`/timeline`、`/stats`、`/avatar`、`/media/*` 无入口日志（`/sessions`:513 与 `/messages`:803 已有，值得肯定） | account/chat 用 `account=... chat=...` 键值形式（命中脱敏）。**通道要先选**：现有 `_log` 走 `logging.getLogger("siwx")` → siwx.log 文件，**不进环形缓冲**（日志页看不到，server.py:651-655 自己也注明）；`logger.detailed` 进环形缓冲但仅详细模式可见。给新端点补入口日志前先定用哪条通道，否则修复后"日志页依然空白"；`/media/*` 高频端点用 detailed 节流是对的

### 5.3 设置 / 更新 / MCP / 统计 / SNS API

- `api_settings.py:40-41` | auto_sync.json 损坏静默回默认——**用户的 enabled=true 被无声丢弃，定时同步悄悄停摆** | `warn("settings", f"auto_sync.json 解析失败，已回默认: {e}")`
- `api_settings.py:132-153` | `/api/settings/clear`：`shutil.rmtree(ignore_errors=True)` 删失败不可见且目录仍进 removed 列表（假成功）；**破坏性操作全程无日志** | `info("settings", f"[clear] kind={} wxid={} removed={}")` + rmtree 失败 `warn`
- `auto_update.py:110-111` | 两个版本源的网络错误全部丢弃——"无更新"与"检查失败"对前端不可区分 | `detailed("update", f"[check] {base_url} 拉取失败: {e}")`
- `auto_update.py:179-183,234-235,212-213,347-350` | 下载重试/sha 拉取/sha 校验异常/新版拉起失败全静默或误导（IO 错误被报成"文件损坏"） | 各点 `warn("update", …)`；`run_update`（239-279）全流程零日志，各阶段补 `info`
- `api_stats.py:76-77,109-110` | `except Exception: return 500 {...}`——**绕过了全局 errorhandler，traceback 不落任何日志** | `error("stats", f"[overview] 账号={account} 统计失败: {e}")`
- `api_sns.py:102-103,136-139,207-209,61-74,334-386` | 头像查询失败/账号统计失败伪装 0 条/**解析失败的动态静默消失**/非法时间参数被静默忽略（以为在筛范围实际全量）/409 拒绝无日志 | 逐点 `detailed`/`warn`；timeline 循环外记 `scanned/parsed/skipped` 三计数
- `mcp_server.py:74-76` | mcp_config.json 损坏→**全部工具静默恢复默认启用**（安全相关开关失效） | `warn`（走 mcp_log）
- `mcp_server.py:118-119,147-148,225-226,245-246,371-374,486-487,623-625,643-645` | 会话列表/状态检测/全库搜索分片/sns 统计/插件工具加载的静默失败 | 各点 `mcp_log.debug/warning`；**全库搜索静默不完整**（scanned 截断无失败标记）建议 warn
- `mcp_server.py:724-726` | 畸形 JSON-RPC 行零记录 | `mcp_log.debug(f"非法 JSON-RPC 行: {line[:120]!r}")`

### 5.4 CLI / TUI

- `cli.py:154-218` | 无 `--verbose/--debug/--log-level`；`main()` 只捕 KeyboardInterrupt；**CLI 模式不装崩溃钩子** | 事实清单见 §2.3 的三项改法（`--detailed` 参数 + `SIWX_LOG_LEVEL` 环境变量 + 崩溃钩子抽公共模块）
- `cli.py:231-235` | 插件 CLI 注册整体失败静默 | `warn("plugin", f"插件 CLI 命令注册失败: {e}")`
- `cli.py:290-293` | 插件命令异常只有 str(e) 无堆栈 | detailed 补 `traceback.format_exc(limit=3)`
- `tui.py:161-165` `_bar` | 每秒吞一次状态栏异常，无计数无日志 | 失败计数，连续 N 次 `warn`（节流）
- `tui.py:148-154` `SetConsoleMode` | 纯体验项 | 不埋点（明确排除，避免过度埋点）

---

## 6. 插件系统与基础设施埋点清单

### 6.1 siwx/plugins/

- `loader.py:52-55` | `ensure_root` 目录创建失败静默 → 表象是"装了插件但一个都没加载" | `warn("plugin", f"插件目录创建失败: {root} ({type(e).__name__})")`
- `loader.py:64-67` | 目录列举失败与真为空不可区分 | `warn("plugin", f"插件目录读取失败: {root}")`
- `loader.py:205-217` | 逐插件加载**无计时、无成功埋点** | 循环内计时：`detailed("plugin:<name>", f"加载成功 version={} 耗时={ms:.1f}ms hooks={}")`；import/register 分段计时（import 慢常见于插件顶层做重活）
- `loader.py:208-211` | import 失败只有单行 error 无堆栈 | detailed 补 `traceback.format_exc(limit=3)`
- `loader.py:241-264` | `plugin_ui_dir` 返回 None 静默 | 插件页面 404 时无从查起 | `detailed("plugin", f"plugin_ui_dir({name}) 未找到页面目录")`
- `contract.py:126,154,179,203,238,268,289,310,333,353,375` | 全部 `_build_*` 构造器 `if not isinstance(e, dict): continue`——**最高频静默容错点**（十余处） | 逐处 `detailed("plugin:<name>", f"{hook_name}[{i}] 条目不是 dict（{type(e).__name__}），已跳过")`
- `contract.py:161-170` 等 | 单字段强转失败牵连整组条目被弃（异常上抛到 register_plugin L459 才被 warn） | 建议改逐条目 try/except；过渡期在 warn 里补 `{hook_name}` 上下文
- `contract.py:322-323` | `input_schema` 非 dict 静默置空——MCP 工具无法正确传参 | `warn("plugin:<name>", f"mcp_tools[{name}].inputSchema 非 dict，已置空")`
- `contract.py:456-461` | 未声明的 hook 键（拼错名）被静默无视 | `detailed("plugin:<name>", f"未知键: {set(data) - 已知键集}")`
- `registry.py:280-309` | 装饰器热路径：成功调用/熔断跳过均零日志 | `detailed("plugin:<name>", f"decorate({dec.name}) 耗时={ms:.1f}ms patch_keys={sorted(patch)}")`——**只记 patch 的键名不记值**（值可能含消息正文）；熔断跳过按插件节流记 detailed
- `registry.py:322-324` | 哨兵 future 提交失败静默——卡住线程永久占用名额（占满 8 个后整体熔断） | `detailed("plugin", f"_mark_stuck 哨兵提交失败: {e}")`
- `registry.py:226-232` | 多插件声明同一 local_type 时低优先级者被永久遮蔽无痕迹 | 加载期记 `detailed("plugin", f"local_type={t} 渲染器被 X 独占，Y 永不生效")`
- `config.py:110-117` | 配置 JSON 损坏与文件不存在同等静默——**损坏文件会被下次 save 直接覆盖，用户设置永久丢失** | 区分：缺文件 `detailed`；JSON 损坏 `warn`
- `config.py:95-105` | 存储值校验失败静默回默认（只记 key 不记值，防用户数据入日志） | `detailed("plugin", f"{plugin}.{key} 存储值非法，回退默认")`
- `chat_bridge.py:42-53,89-114,130-135` | 渲染器/转换器/过滤器/头像解析的成功与类型不符均静默；插件以为生效的字段被白名单丢弃 | 各点 `detailed("plugin:<name>", …)`——**只记返回类型/键名/字节数，严禁记 username 明文与消息正文**；四处 warn 统一改 `log.warn("plugin", log.desensitize_msg(f"...: {e}"))`（对齐 report.py:62）
- `report.py:76-81` | 同名插件第二次记录静默丢弃（import 成功但 register 失败时后者信息丢失） | `detailed("plugin", f"报告重复条目 {st.name}({st.status}) 被忽略")`
- `conditions.py:38-61,122-123,160-161` | 条件上下文采集失败静默（插件页**被静默隐藏**，用户以为插件消失）；条件非 dict 被宽放为"显示"（与异常时"隐藏"方向不一致）；evaluate 兜底"交由上层记录"但上层没有记录点 | 三段分别 detailed；evaluate 异常 `warn(f"页面条件求值异常，页面隐藏: {sorted(condition.keys())}")`（只记键名）

### 6.2 siwx/paths.py / siwx/stats.py

- `paths.py:22-26` | SIWX_ROOT 环境变量无效时静默忽略 | `warn("paths", f"SIWX_ROOT 不存在，已忽略")`（路径过脱敏）
- `paths.py:97-102` | **主路径不可写静默回退 `%USERPROFILE%`——导出产物位置悄然改变，用户"找不到导出的文件"却无任何日志**。本文件最重要的缺失埋点 | `warn("paths", f"{subdir} 主路径不可写，回退: {fallback}")`——**不要包 `desensitize_msg`**：`warn` 的 `_cprint` 路径本就不经脱敏（脱敏只发生在 get_logs/export_logs，logger.py:110-121,124-143），预先包裹只会让控制台输出也变成脱敏文本，与 §7.3"路径直接写"的约定一致即可。依赖方向已验证无环（logger 不 import paths），paths 内 import siwx.logger 可行；回退分支有 `_PATH_CACHE`、每进程只触发一次，warn 不会刷屏
- `paths.py:63` | 数据目录极端环境兜底临时目录（重启即失） | `detailed("paths", …)`
- `stats.py:102-103` | `signature()` 失败→缓存永不命中（每次请求全量重扫约 2.4s）无痕迹 | `detailed("stats", f"{account} 分片签名不可用")`
- `stats.py:146-147,186-187,232-233` | 分片打不开/TEMP 建表失败/主聚合兜底——**统计总量无声偏低或残缺** | `warn("stats", f"分片失败: {db.name}: {e}（结果可能不完整）")`（空分片先短路避免误报）
- `stats.py:160-161,433-443,482-493` | Name2Id/公众号识别/昵称表静默降级——排行里全是 wxid 而无昵称的原因不可见 | `detailed("stats", …)`
- `stats.py:548-549,566-567` | 缓存 JSON 损坏静默重算并覆盖；缓存写失败 → **跨进程**重复全量重扫（写失败不影响 `_MEM_CACHE`（stats.py:590-591），同进程内后续请求仍命中内存缓存；只有 CLI 每次运行/服务重启后才重复全量重扫——磁盘缓存形同虚设，但"每次"仅跨进程成立） | `warn` / `detailed`
- `stats.py:303-311,325-329` | `compute_stats` 无耗时、无缓存来源（mem/disk/miss）标记 | **优先接通现有通道而非另起 detailed**：`_scan` 已有 `log` 回调（stats.py:303-311 定义，325/329/335 输出并行回退、分片数/总条数/私聊发送者数，compute_stats:570,587 透传），但所有调用方（api_stats.py:73,89,105）都没传参——通道实际是死的。最小改动是让 api_stats 传 `log=lambda m: logger.detailed("stats", m)` 并补计时与缓存来源标记；直接在 `_scan` 内塞新 detailed 会与现有回调双轨并行，同一条信息打两遍

---

## 7. 埋点规范（实施时统一遵守）

1. **module 标签约定**：`extract` / `decrypt` / `harvest` / `keystore` / `pool` / `cipher` / `sqlcipher` / `strategy:<name>`（cipher|memscan|mmkv|lldb|keystore） / `discover` / `winproc` / `export` / `media` / `voice` / `parse` / `html` / `api` / `http` / `server` / `settings` / `mcp` / `update` / `stats` / `paths` / `sns` / `plugin` / `plugin:<name>` / `env`。现有 5 处调用已用的标签（`discover`/`parse`/`sns`/`plugin`）保持一致。
2. **级别选择标准**：
   - `detailed`：正常路径的关键分支、决策点、成功指标、高频路径（防止刷屏）；
   - `warn`：会改变用户可见结果或性能的降级（数据缺失、缓存失效、路径回退、配置损坏、进程打开失败）；
   - `error`：任务失败、数据完整性受损（如 build_messages 表查询失败）。
   - **数据完整性事件至少 warn**：分片打不开、表查询失败、媒体解密失败汇总这类"结果已残缺"的事件，用 detailed 意味着默认 ROUGH 模式下用户依然看不到（logger.detailed 只在 DETAILED 模式记录，logger.py:81-86）；逐条明细才用 detailed。
3. **消息字段格式**：定位四件套 `local_id= ts= localType= md5[:8]=`；**正文类内容（rawContent/content）只记 hex 头 + 长度，必要时至多前 16~32 字符**（`_FILE_LOG` 存未脱敏原文，脱敏只发生在展示/导出时，正文截断落盘=明文落盘）；账号/会话用 `account= chat= talker=` 键值形式；路径直接写（脱敏规则会处理）。`chat=` 的值经脱敏后只剩 `***@chatroom`，定位会话能力受限——建议辅以 md5 表名或 local_id。
4. **脱敏红线**：明文密钥绝不入日志（只允许 `mask_key` 或 salt 前 16 位）；lldb `OK:` 行（passphrase）剔除后再落日志；插件 patch/消息正文只记键名/长度不记值；异常内插 `{e}` 前考虑是否可能携带用户数据，必要时包 `desensitize_msg`（但其对消息正文片段无能为力，正文一律按规范 3 的截断口径处理）。
5. **热路径纪律**：winproc 的 RPM 失败、stats 的缓存写入等高频点用**计数器 + 结束时一条汇总**，不逐次打日志；TUI 状态栏等每秒轮询点用**连续失败 N 次才 warn** 的节流。
6. **不埋点清单**（明确排除，避免过度工程）：`tui.py:148-154` SetConsoleMode、`voice.py:256-258` 临时文件清理、`logger.py` 自身的 `_cprint`、进程枚举的 NoSuchProcess（正常竞态）。
7. **实施前先修 `_PATH_RE`（否则新埋点产出损坏文本）**：logger.py:157 的路径规则 `[/][^\s]+` 会把 `17/5684`、`img=3/5` 这类比率/分数当路径匹配，替换为末段后输出变成 `175684` 这样的损坏文本。本文多处建议的消息（如 §4.1 的 `img={}/{}`、`voice={}/{}`）正含此形式。应先收紧规则（要求盘符/`~/`/`/Users/` 等明确锚点，或分隔符+扩展名特征），再落含斜线的埋点。

## 8. 建议实施顺序

| 批次 | 内容 | 理由 |
|------|------|------|
| 第〇批 | §7 规范 7 的 `_PATH_RE` 收紧 | 前置依赖：不先修，后续含斜线的埋点（`img={}/{}` 等）会被脱敏规则破坏成乱码 |
| 第一批 | §2.1 双轨合并（模式 A 双写，注意 extract.py 形参遮蔽与策略层注入架构）、§2.2 media.event 改道、§2.3 开关持久化 + CLI/环境变量入口 | 地基；做完后现有日志立即受 Debug 控制，埋点才有意义 |
| 第二批 | §4 渲染/导出/媒体链路全部（尤其 4.1 媒体失败元组、4.2 未知类型、4.4 V2 reason） | 用户报障最高频的"图丢了/消息显示不对" |
| 第三批 | §3 提取/解密链路（尤其 keystore.py:137、strategies/__init__.py 链路日志、winproc 错误码） | "密钥提不出来"排障 |
| 第四批 | §5 + §6 API 层与插件/stats（大量 `except: pass` 补日志，机械但量大） | 数据完整性类静默问题 |
| 随时可做 | §2.5 access log / 任务 id / mcp.log 脱敏 | 增强项 |

---

*审计覆盖文件：logger.py、server.py、api_chat.py、api_settings.py、api_export.py、api_mcp.py、api_plugins.py、api_stats.py、api_sns.py、api_update.py、auto_update.py、mcp_server.py、cli.py、tui.py、extract.py、pool.py、sqlcipher.py、keystore.py、discover.py、env_info.py、winproc.py、exporter.py、export_stream.py、html_template.py、media.py、voice.py、sns.py、sns_cdn.py、sns_export.py、paths.py、stats.py、plugins/loader.py、plugins/contract.py、plugins/registry.py、plugins/config.py、plugins/chat_bridge.py、plugins/report.py、plugins/conditions.py、strategies/__init__.py、strategies/config_cipher.py、strategies/keystore_source.py、strategies/macos_lldb.py、strategies/memscan.py、strategies/mmkv.py（strategies 目录下 6 个文件全部覆盖）。*
