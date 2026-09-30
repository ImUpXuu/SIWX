# SIWX v5.0.3 全量代码审计报告

> **审计日期**：2026-09-28
> **审计范围**：仓库全量逐文件通读（源码 / 前端 / 测试 / 文档 / 构建 / CI）
> **审计性质**：**只读**。本报告不包含任何代码改动，所有发现仅供决策参考。
> **审计基准**：`siwx/__init__.py::__version__ = "5.0.3"`

---

## 摘要

| 项目 | 数值 |
|---|---|
| Python 源码（`siwx/`，含子包） | **11,189 行 / 42 个模块** |
| 前端（`siwx/ui/`） | **4,094 行 / 25 个文件** |
| 测试（`tests/`） | **3,467 行 / 3 个套件** |
| 文档（`docs/`） | **6,578 行 / 28 篇** |
| 根目录文档与配置 | 1,146 行 |
| **合计受审代码与文档** | **≈ 26,500 行** |

**总体结论**：这是一份**工程质量显著高于同类个人工具平均水平**的代码库。密码学实现正确且经过与标准库的交叉验证，数据损坏防线（原子写、唯一临时文件、来源标记）到位，插件系统设计克制且向后兼容红线有测试守护，文档密度极高（每千行代码约 250 行文档）。

本次审计**新识别 11 项问题**，其中 1 项属数据正确性、1 项属凭据泄漏、其余为健壮性与一致性；另有 **8 项安全问题**与 **5 项数据安全问题**已在仓库既有审计文档中记录但**尚未修复**，本报告一并汇总并标注现状。

---

## 一、审计范围与方法

### 1.1 覆盖清单

| 类别 | 文件 | 状态 |
|---|---|---|
| `siwx/*.py`（顶层 28 个） | sqlcipher / keystore / discover / winproc / extract / pool / media / voice / export_stream / exporter / html_template / stats / server / cli / tui / logger / paths / env_info / auto_update / mcp_server / api_chat / api_export / api_mcp / api_plugins / api_settings / api_stats / api_update / `__init__` | ✅ 全部逐行 |
| `siwx/strategies/`（6 个） | `__init__` / keystore_source / mmkv / config_cipher / memscan / macos_lldb | ✅ 全部逐行 |
| `siwx/plugins/`（8 个） | `__init__` / contract / registry / loader / chat_bridge / conditions / config / report | ✅ 全部逐行 |
| `siwx/ui/`（25 个） | index.html / app.js / app.css / common.js + 8 个页面的 html/js/css（disclaimer 仅 html） | ✅ 全部逐行 |
| `docs/`（28 篇） | architecture / module-* × 15 / plugin-development / media-* × 2 / *-audit-2026-09-19 × 2 / api-reference / cli-commands / packaging / wal-support-plan / README | ✅ 全部通读 |
| `tests/`（3 个） | test_regressions（1,696 行）/ test_plugins（1,393 行）/ test_macos_lldb（378 行） | ✅ 全部逐行 |
| `examples/plugins/demo_stats/`（5 个） | `__init__` + pages/{index.html,index.js,index.css,theme.css} | ✅ 全部逐行 |
| `scripts/`（4 个） | generate_version_json.py / wal_probe.py / update_win.bat / update_mac.sh | ✅ 全部逐行 |
| `packaging/`（2 个） | siwx-win.spec / siwx-mac.spec | ✅ 全部逐行 |
| `.github/`（6 个） | workflows/release.yml + 3 个 issue 模板 + config.yml + PR 模板 | ✅ 全部逐行 |
| 根目录 | README / CONTRIBUTING / MACOS_SUPPORT / PR_DESCRIPTION / version.json / requirements.txt / auto_sync.json / push_and_pr.sh / LICENSE / run.py / diag_{blobs,media,verify}.py | ✅ 全部逐行 |
| `video/` | render_html_to_mp4.py + 宣传素材 | ✅ 脚本逐行 |

### 1.2 未纳入范围（有意排除）

- `output/`、`exports/`、`logs/` —— 运行期产物与用户真实数据，不读取内容
- `__pycache__/`、`*.pyc` —— 编译产物
- `siwx/vendor/silk-decoder/` —— 第三方二进制
- `LICENSE` —— 标准 AGPL-3.0 全文，仅确认版本

### 1.3 方法说明

- 以「数据流」为主线（发现 → 提密钥 → 解密 → 存储 → 消费），逐模块核对实现与文档声明是否一致。
- 对关键不变量（HMAC 覆盖范围、临时文件唯一性、缓存键构成、白名单边界）做**源码级交叉验证**，而非仅采信注释与文档。
- 对本报告标注为「新发现」的条目，均已在源码中定位到具体文件与行号。

---

## 二、项目全貌

### 2.1 定位与设计哲学

SIWX 是一个**本地运行的个人微信 4.x 数据自备份工具**，完整链路为：

```
发现数据目录 → 提取密钥 → 解密数据库 → 浏览聊天 → 恢复媒体 → 转码语音 → 多格式导出 / MCP
   discover      策略链      SQLCipher     Web UI      media.py    voice.py    exporter
```

三条贯穿全库的设计哲学（与 `docs/architecture.md` 声明一致，实测吻合）：

1. **propose-verify 分离**：所有密钥策略只产出**候选**，绝不自行判定正确性；由编排器统一用 SQLCipher 的 page-1 HMAC 验证后才入库。这是全库最重要的架构约束。
2. **零外部二进制**：语音用纯 Python 的 `pilk` 解 SILK，WAV 用标准库 `wave` 封装；不依赖 ffmpeg。打包产物因此开箱即用。
3. **缓存优先但绝不静默覆盖**：密钥按 salt 索引缓存，解密产物按 `(mtime, size, key)` 命中；同时用 `@source` 标记保证换副本时**跳过**而非覆盖。

### 2.2 模块地图与依赖方向

```
                        ┌──────────────────────────────┐
   入口层               │ run.py → cli.py / server.py   │
                        │         / mcp_server.py       │
                        └───────────────┬──────────────┘
                                        │
   ┌────────────────────────────────────┼────────────────────────────────────┐
   │  API 层（Flask 蓝图，7 个）         │                                    │
   │  api_chat / api_export / api_mcp / api_plugins / api_settings /        │
   │  api_stats / api_update                                                │
   └────────────────────────────────────┼────────────────────────────────────┘
                                        │
   ┌────────────────────────────────────┼────────────────────────────────────┐
   │  领域层（编排与消费）               │                                    │
   │  extract（编排） pool（并行） export_stream（流式解析）                 │
   │  exporter（8 格式） html_template stats（聚合） media voice            │
   └────────────────────────────────────┼────────────────────────────────────┘
                                        │
   ┌────────────────────────────────────┼────────────────────────────────────┐
   │  基础层                             │                                    │
   │  sqlcipher（密码学） keystore（密钥库） discover（发现）                │
   │  winproc（内存读） paths（路径） logger（日志） env_info tui            │
   └────────────────────────────────────┼────────────────────────────────────┘
                                        │
   ┌────────────────────────────────────┴────────────────────────────────────┐
   │  策略层 strategies/：keystore_source → mmkv → config_cipher → memscan    │
   │                    （macOS 另有 macos_lldb）                            │
   └─────────────────────────────────────────────────────────────────────────┘

   横切层 plugins/：契约(contract) → 注册表(registry) → 加载(loader)
                    → 桥接(chat_bridge) / 条件(conditions) / 配置(config) / 报告(report)
```

**依赖方向纪律**：`plugins/loader.py` 不在模块顶层 import 任何业务模块（避免插件加载污染宿主启动路径），`docs/module-plugins.md` 明确将此列为设计目标，实测代码遵守。

### 2.3 核心抽象

| 抽象 | 位置 | 语义 |
|---|---|---|
| `DbEntry` | `sqlcipher.py` | 一个待解密数据库的完整描述：`rel`（相对路径）/ `path` / `size` / `salt_hex` / `page1` |
| `ctx`（策略上下文） | `strategies/__init__.py` | 策略与宿主之间唯一的通信契约：`log` / `key_map` / `pids` / `db_dir` 等 |
| `CHAT_DATA` | `html_template.py` | HTML 导出的自包含数据契约：`meta` / `members` / `avatarFiles` / `messages` |
| `PLUGIN` 字典 | `plugins/contract.py` | 插件的**唯一声明入口**，纯数据、函数用字符串名引用 |
| 节点树 | `ui/common.js::renderNodes` | 插件渲染器的输出格式（结构化数据，非 HTML） |

---

## 三、核心链路逐层剖析

### 3.1 SQLCipher 4 解密内核（`sqlcipher.py`，201 行）

**参数表（实测与文档一致）**：

| 参数 | 值 |
|---|---|
| PAGE_SZ | 4096 |
| KEY_SZ | 32（AES-256） |
| SALT_SZ | 16 |
| RESERVE_SZ | 80（= IV 16 + HMAC 64） |
| IV_SZ | 16 |
| HMAC_SZ | 64（SHA-512） |
| KDF | PBKDF2-SHA512，iter=2，dklen=32 |
| mac_salt | `salt XOR 0x3A`（逐字节） |
| 页 1 HMAC 覆盖 | `page1[16:]`（跳过 salt）+ `pack("<I", 1)` |

**关键实现质量点**：

1. **HMAC 前置校验**：`verify_enc_key()` 在解密任何内容前先验 HMAC，错误密钥**立即失败**，不会产出垃圾数据。这是「不污染密钥库」的第一道防线。
2. **手写 CBC 优化**：复用单个 ECB cipher + 大整数 XOR 链，消除每页一次 `AES.new()` 的 key schedule 开销。`tests/test_regressions.py::TestCryptoIntact::test_handwritten_cbc_matches_stdlib` 对两种密文长度做了与标准库的逐字节比对 —— 这是**本仓库最值得称道的测试之一**。
3. **原子写**：`decrypt_database()` 先写 `.part` 再 `os.replace()`。测试 `test_failure_does_not_clobber_existing_file` 验证了「错误密钥时已有明文库保持原样且无残留」。
4. **唯一临时文件**：`_read_page1()` 的「被占用则复制到临时文件」分支改用 `tempfile.mkstemp()`。这是 D-2 的修复，测试 `test_read_page1_concurrent_no_crosstalk` 用 8 线程 + 屏障 + 模拟锁验证了无串扰。

**值得注意的边界**：`PAGE_SZ - RESERVE_SZ + IV_SZ - SALT_SZ == 4016`，这个等式被测试 `test_verify_enc_key_byte_layout` 固化，防止未来误改常量。

### 3.2 密钥提取策略链（`strategies/`，6 个模块）

**优先级与适用面**：

| # | 策略 | 原理 | 平台 | 优先级 |
|---|---|---|---|---|
| 1 | `keystore_source` | 读本工具自己的 DPAPI 密钥库缓存 | Win | 最高 |
| 2 | `mmkv` | MMKV 离线派生（AES-GCM + 账号级密钥） | Win | 高 |
| 3 | `config_cipher` | WCDB `Config.Cipher` 内存节点解析（微信 4.1.10+ 主力） | Win | 中 |
| 4 | `memscan` | 全内存扫 `x'<64~192hex>'` 字面量兜底 | Win | 低 |
| 5 | `macos_lldb` | LLDB 在 `sqlite3_key` / `sqlite3_key_v2` 下断点 + PBKDF2 | macOS | — |

**`config_cipher` 的实现细节**（本次审计重点核对）：

- needle：`com.Tencent.WCDB.Config.Cipher`
- 节点结构：`node+0x10` → 数据指针，`node+0x18` → 长度（30），`node+0x28` → config 指针
- config 解引用：`config_ptr+0x88` → obj，`obj+0x8` → blob 指针，`obj+0x10` → blob 长度
- 恢复：`CONFIG_XOR_MASK`（32 字节内置掩码）异或还原
- 求解：crib-drag 约束求解 + ASCII 打分兜底

**设计正确性评价**：策略只写 `ctx["key_map"]`，不做验证 —— 与 §2.1 的 propose-verify 约束严格一致。这是**正确的分层**：策略无需理解 SQLCipher，验证逻辑单点维护。

**`macos_lldb` 的 issue #3 修复质量**（由 `tests/test_macos_lldb.py` 378 行守护）：

- `AttachToProcessWithID(listener, pid, error)` —— 首参必须是 listener（测试用强类型 `_FakeSBListener` 校验）
- `frame.FindRegister(name)` —— 而非不存在的 `SBValueList.GetRegisterByName`
- `mod.FindSymbols(fn)` —— 而非 `FindSymbol`
- 显式事件泵：`listener.WaitForEvent` + `SBProcess.EventIsProcessEvent`
- 顶层异常输出 `FAIL:ScriptError`，避免再出现无解的 `module importing failed`
- 捕获成功后 **Detach 而非 Kill**（微信保持登录）

测试同时用「静态断言 API 用法」+「注入假 lldb 模块动态执行整段脚本」+「本机有真实 lldb 时校验 API 存在性」三层覆盖，**测试设计水平很高**。

### 3.3 密钥库（`keystore.py`，159 行）

- 索引方式：**按 `salt_hex` 索引**（而非按账号/路径）—— 这是正确选择，因为 salt 是数据库的物理身份。
- 加密：Windows 用 DPAPI `CryptProtectData`，熵为项目私有常量 `b"stories-in-wx::keystore::v1"`；非 Windows 降级为明文 JSON（`MACOS_SUPPORT.md` 已明示）。
- 原子写：`.tmp` + `os.replace`。
- 覆盖判定：`_keystore_preset` 检查既有条目，避免重复收割。

**审计观察**：`siwx/keystore.py` 中出现的 `v5.0.1` 字样是**历史兼容说明**，不是版本号遗漏 —— 这一点已在项目记忆中明确，未来版本升级时**不要误改**。

### 3.4 进程内存读取（`winproc.py`，113 行）

- 只申请 `PROCESS_VM_READ | PROCESS_QUERY_INFORMATION`，**不申请写权限** —— 最小权限原则。
- `VirtualQueryEx` 枚举可读区域（`MEM_COMMIT` + 可读保护位），`iter_chunks` 带 overlap 防止跨区域漏配。
- 上限约束：`REGION_LIMIT = 500MB`，`MAX_USER_ADDRESS` 防越界。

**评价**：纯 ctypes 实现，无第三方依赖，边界处理完整。这是「本地工具但依然克制」的范例。

### 3.5 发现层（`discover.py`，318 行）

- 扫描根：`Documents/xwechat_files`、`USERPROFILE`、A–Z 盘符；支持手动路径（`manual_paths_file()` 持久化，`resolve_db_paths()` 可接受 db_dir / 账号目录 / xwechat 根 / 单个 .db 文件四种粒度）。
- `find_wechat_pids()` 按 `rss` 降序 —— 内存占用最大的进程最可能是主进程，合理的启发式。
- `wxid_of()` 取 `db_dir.parent.name`。
- **`find_account_conflicts()`**：检测同名账号多副本，纯只读告警（D-1 的方案 C）。

### 3.6 编排与并行（`extract.py` 426 行 / `pool.py`）

- `extract_all()` → `global_harvest()`（一次扫描全内存，多账号共享）→ `extract_keys_for_dir()`（策略链）→ `decrypt_dir()`（缓存判定 + 并行解密）。
- `decrypt_parallel()`：`n = workers or min(8, cpu_count)`；Windows 必须 spawn，故 `_worker` 定义在**模块顶层**（PyInstaller 兼容）。
- `save_manifest()` 是**加载 → 叠加 → 写回**，不是全量重建（此点已在项目记忆中更正过初版误判）。
- `SOURCE_FIELD = "@source"`：来源不一致时跳过已有产物。**兼容语义关键**：字段缺失 = 放行，保证升级用户行为与旧版一致。`TestManifestSourceGuard` 用 7 个用例覆盖了这个矩阵。

### 3.7 媒体解密（`media.py`，499 行）

**三代加密**：

| 代 | 魔数 | 算法 |
|---|---|---|
| V0 | 无 | 整文件单字节 XOR（自动检测） |
| V1 | `V1` | AES-128-ECB + XOR，固定 key `cfcd208495d565ef` |
| V2 | `\x07\x08V2\x08\x07` | AES-128-ECB 头部 + 16 字节分隔尾 + 单字节 XOR 尾部 |

**V2 布局（`docs/media-decryption-principles.md` 权威声明，实测吻合）**：

```
偏移 0-6    : 魔数
偏移 6-10   : AES 段长度（LE u32，实测 0x400）
偏移 10-14  : XOR 段长度
偏移 14     : 标志字节（0x01）
偏移 15..   : AES 密文（长度 = AES 段长度）
之后 16 字节: 分隔尾  ← ⚠️ 不可硬编码，随微信版本变化
之后        : XOR 段
不变量      : file_size == 15 + aes_size + 16 + xor_size
```

**账号级密钥**：`MD5(str(code) + clean_wxid).hexdigest()[:16].encode()`；`xor_key = code & 0xFF`（0xC9 兜底）；`code` 来自 `key_<code>_*.statistic` 文件名。

**多级图片来源（按优先级）**：
1. ⓠ attach 原图目录直查（`_h` 高清优先）
2. ① hardlink（`image_hardlink_info_v4` + `dir2id` + `db_info` uuid）
3. ② Bubble 气泡缓存（`packed_info_data` 内嵌 32hex）
4. ③ Thumb 明文缩略图

wxgf 格式经 `VoipEngine.dll` 的 `wxam_dec_wxam2pic_5`（mode 0/3）转换，**全局单例 + 串行锁**（该 DLL 非线程安全）。

### 3.8 语音转码（`voice.py`，376 行）

- 语音数据：`VoiceInfo.voice_data`，多为明文 SILK（`#!SILK_V3`），可能带 1 字节控制前缀（如 `0x02`）。
- 转码链：`pilk`（纯 Python）→ PCM → 标准库 `wave` 封装 WAV。
- 回退顺序：`pilk` → 内置 decoder（`siwx/vendor/silk-decoder/`）→ `SIWX_SILK_DECODER` 环境变量 → PATH。
- API：`GET /api/chat/media/voice?...&format=wav|silk`，**默认 silk 保持向后兼容**（测试 `test_voice_api_serves_silk` 断言了这一点）。

### 3.9 导出管线（`export_stream.py` 266 行 / `exporter.py` 645 行）

**流式设计**：

- `message_stream()` 用 `heapq.merge` 做 K 路归并，内存复杂度 **O(分片数)** 而非 O(消息数)。
- `IncrementalJSONWriter` 逐条写出，不构造完整文档。
- `BATCH_SIZE = 500`。
- 媒体解密走多进程。

**分片索引（重要性能修复）**：`shards_for()` / `message_tables_by_shard()` 建立 `Msg_<md5(username)>` → 分片文件的反查索引。源码注释记录了原始瓶颈：**「原先每次调用把全部 *.db 逐个打开查 sqlite_master，实测占导出总耗时 99.5%」**。`test_shard_scan_is_avoided` 用 `sqlite3.connect` 打桩验证索引生效。

**8 种内建格式**：json / html / txt / csv / markdown / toml / sqlite / xlsx。全部支持媒体与语音引用 —— `test_all_formats_can_reference_exported_voice` 对 8 种格式逐一断言（含 SQLite 的 `mediaFile` 列与 XLSX 的末列）。

**`_safe_name()` 修复**：原实现 `name = ch.replace(ch, "_")` 恒返回 `"_"`（显然的笔误），导致导出目录名/文件名永远丢失联系人名。现实现：替换非法字符 → 去空白 → 去尾点 → 长度上限 48 → 空值回退 `"chat"` → Windows 保留名（CON/nul 等）加前缀 `_`。`TestSafeName` 用 4 个用例覆盖。

### 3.10 统计（`stats.py`，744 行）

- 跨分片聚合：总量 / 会话数 / 类型分布 / 月度趋势 / 24 小时分布 / 星期分布 / 私聊发送者排行。
- 时间范围过滤作用于**整页指标**（不只是裁剪图表）—— `test_date_filter_narrows_all_statistics` 验证。
- 排行**仅私聊**（群聊与公众号被排除，即使消息量更大）—— `test_top_senders_only_private_and_resolves_nickname` 用「给群聊/公众号塞 20/30 条」来反向验证。
- 缓存：`_CACHE_VERSION` + 签名（分片集合 + mtime/size），落盘 `.siwx_stats.json`。
- **既有 bug 修复**：`_sig_equal` 曾因 JSON 元组→列表序列化导致缓存**永不命中**。

### 3.11 Web 服务层（`server.py` 780 行 + 7 个蓝图）

- **任务槽**：单任务模型（`_run_job`），状态机覆盖 `keys` / `decrypt` / `auto` / `sync` / `export`。并发请求返回 409。
- **环形日志**：`_LOG_RING` 2000 条，`/api/logs` 返回**混合形状**（旧 `[ts, text]` 与结构化 `[ts, level, module, text]`）—— `TestLogsApi` 专门守护「每条都能取到文本」的健壮性。
- **自动同步调度器**：30s 轮询，条件 = 已开启 + 到间隔 + 微信在线 + 无运行中任务；间隔 1–1440 分钟（服务端夹紧，`test_auto_sync_interval_is_clamped` 断言 99999 → 1440）。
- `_ui_dir()` 支持 `_MEIPASS`（PyInstaller 单文件）。
- 静态 UI 资源按 `?v=2026092501` 版本串破缓存。

**`api_chat.py`（961 行，全库最大模块）**：承载会话列表 / 消息分页 / 头像 / 媒体 / 语音 / 时间轴 / 会话统计。三处缓存 `_SHARD_INDEX` / `_CONTACT_CACHE` / `_SESSION_CACHE`。

**`api_plugins.py::_find_page_dir()`** 实现了「字符白名单 + 目录存在」的路径校验 —— 这是本仓库中**唯一**规范的用户可控路径校验模式，值得作为其他端点的模板（见 §5.2 M-2/M-3）。

### 3.12 MCP 服务器（`mcp_server.py`，467 行）

- 协议：stdio + newline-delimited JSON-RPC 2.0，`PROTOCOL_VERSION = "2024-11-05"`，**纯标准库、零依赖**。
- 6 个内建工具：`get_status` / `list_accounts` / `list_sessions` / `get_messages` / `search_messages` / `export_chat`。
- `SCAN_CAP = 200_000` 限制搜索规模。
- **issue #11 修复**：`_mcp_log_path()` 曾兜底 `.`，导致双击 `.app` 时 cwd 是受 SIP 保护的只读 `/`，`mkdir` 报 Errno 30 秒退。现改用 `paths.data_dir()`。
- **性能修复**：`search_messages` 的 `smap`（`Msg_md5` → username 反查表）原先写在表循环**内部**，导致 `biz_message_0.db` 的 67 张表重复查 67 次 `Name2Id`；现提到分片循环外。
- 工具开关持久化到配置文件，`.tmp` + `os.replace`。

### 3.13 插件系统（`plugins/`，8 个模块 / 1,800+ 行）

**契约层**：`PLUGIN` 字典声明 **17 类扩展点**：pages / settings / renderers / message_decorators / session_decorators / content_transformers / session_filters / export_formats / after_export / mcp_tools / cli / task_listeners / themes / key_strategies / avatar_resolvers / media_providers / routes / api_blueprints。

**关键设计决策（审计确认全部落实）**：

| 决策 | 理由 |
|---|---|
| **函数一律用字符串名引用** | 插件不必 `import siwx`，避免循环依赖与加载顺序问题 |
| 两种插件形态 | 单文件 `<name>.py` 或包 `<name>/__init__.py`（+ `pages_hint` 可指向任意子目录） |
| 逐 hook 独立 try/except | 单个 hook 声明非法不影响同插件其他 hook（`test_bad_hook_does_not_break_others`） |
| 三级确定性排序 | `-priority` → 插件名 → 声明索引，保证渲染结果可复现 |
| 显示条件由**服务端**求值 | 前端不做判断（避免逻辑重复与泄漏）；`conditions.py` 支持 `requires_decrypted` / `requires_account` / `platform`（含 win/mac 别名）/ `min_version` / `env` / `config` / `any_of` |
| 装饰器超时**不能用 `with ThreadPoolExecutor`** | `__exit__` 的 `shutdown(wait=True)` 会把超时调用重新阻塞，使 `timeout` 形同虚设 —— 这是**极其容易踩的坑**，代码已正确处理 |
| 60s 熔断 | 超时装饰器被熔断后直接跳过，且**会到期恢复**（`test_breaker_expires`） |
| 热路径守卫 | `hot=false` 的装饰器不进热路径；`hot=true` 需显式开启（`test_decorator_not_applied_when_hot`） |
| 节点树而非 HTML | 插件返回结构化数据，白名单渲染，杜绝插件注入脚本 |
| 配置懒创建 | 无实际变更时不写配置文件（`test_apply_update_no_change_does_not_write`） |
| 插件名白名单 | `config_path()` 剥除路径分隔符，防目录穿越（`test_plugin_name_path_traversal_blocked`） |

**节点树安全边界（前端 `common.js::renderNodes`，服务端 `chat_bridge` 双层）**：

- 标签白名单 21 个，**不含** script / iframe / object / embed / form / link / style / base / meta
- 属性白名单仅 `class title style colspan rowspan`
- `on*` 事件属性、`href`、`src` 一律拒绝
- `safeUrl()` 仅放行 http(s) / 相对 / `#`；`safeStyle()` 拒绝 `url()` / `expression()` / `javascript:`
- `{t:'raw'}` 被显式忽略
- 服务端只采纳 `kind` / `render` / `text` / `extra` 四键（`test_renderer_only_whitelisted_keys`）
- 渲染器返回非 dict（如 HTML 字符串）→ 整体忽略（`test_renderer_non_dict_ignored`）

**向后兼容红线**：`SIWX_NO_PLUGINS=1` 下宿主行为必须与旧版字节级一致。`TestZeroPluginBackwardCompat` 守护：所有命名空间为空时均为 falsy，`chat_bridge` 短路返回。

### 3.14 前端（`siwx/ui/`，4,094 行）

**架构**：零框架、零构建、原生 ES Module。

- `index.html` 为壳页面，`app.js` 做 hash 路由 + 页面模块动态 `import()`
- 菜单顺序 `PAGES_BUILTIN`：guide → chat → stats → export → mcp → logs → settings；**插件页固定追加在内建项之后**，样式与内建完全一致
- 页面 id 全局化 `<plugin>:<name>`
- 每个页面模块导出 `init()` / `destroy()`，`navigate()` 在切换时调用 `destroy()` 清理定时器 —— **无内存泄漏**
- 免责声明门：`disclaimerAcked()` 用 localStorage + 版本号；弹层用不透明底色（不泄漏底层页面内容）；拒绝时 `window.close()`
- 主题：`localStorage['siwx-theme']`

**前端安全实践**：`SX.esc()` 全量使用；`renderNodes` 白名单（见 §3.13）。

**`stats.js`（496 行）的动画实现值得单独指出**：

- 所有图表为**手绘内联 SVG**，零图表库依赖
- 动画纪律：用 CSS 变量（`--grow` / `--sy` / `--arc`）承载目标值，**属性本身先写起点**，JS 在下一帧改成目标值 —— 因为浏览器不会对首次渲染的 SVG 属性做 transition。这个细节在 `stats.css` 里用大段注释解释，说明作者踩过坑
- 统一动画队列 `flushAnim()`（两帧后统一触发），避免每元素各自 `requestAnimationFrame`
- 尊重 `prefers-reduced-motion`
- `countUp()` 用 easeOutCubic 缓动 + 千分位格式化
- 环形图每段留 1.5px 视觉间隙，避免同色相邻糊成一片

**`stats.html` 的一处防御性注释**（值得称道）：

```html
<!-- 占位提示与正文容器是兄弟节点。
     注意：render() 会整体覆写 #st-body.innerHTML，若把占位符放在里面，
     它会被销毁，后续 el('st-empty') 拿到 null 并抛错。 -->
```

**`chat.js`（604 行）**：会话列表（公众号折叠分组）、消息分页（limit 80）、`msgKey` 去重（`ts:id:platformMessageId`）、时间 chip（>300s 显示）、时间轴跳转（`timelineDayCache`）、选择模式（shift 范围选择）、气泡渲染（t=3/43/47/34/50、转账/红包/位置/链接/名片）、灯箱、无限滚动（BATCH=50）。

### 3.15 CLI / TUI

**`cli.py`（284 行）**：`auto` / `keys extract [--json]` / `keys list` / `decrypt [--db-dir]` / `serve [--port]` / `mcp` / `doctor`。退出码规范（中断返回 130）。**既有 bug 修复**：`--json` 参数此前从未生效。

**`tui.py`（182 行）**：基于 `rich` 的组件层，含 `TAG_STYLES` 主题注册表、`TAG_RE` 日志着色、`run_live_status()` 后台状态栏（ANSI 转义定位 + Windows `SetConsoleMode(-11, 7)` 启用虚拟终端）。

### 3.16 辅助模块

| 模块 | 职责 | 审计要点 |
|---|---|---|
| `paths.py` | `app_root()` / `out_root()` / `exports_root()` | 与 cwd 解耦 —— 因 macOS 双击 `.app` 时 cwd 为只读 `/` |
| `logger.py` | 结构化日志 + 轮转（5×10MB） | `desensitize_msg` 脱敏；`_flush_logs()` |
| `env_info.py` | 环境信息采集（bug 报告用） | 路径用户名自动打码（`<user>`）；`collect(quiet=True)` 不污染 stdout（**故意不提供 `doctor --json`**，因插件注册日志会先写 stdout） |
| `auto_update.py` | 版本检查 + 更新 | 多源取最新；`_siwx_update=` 时间戳 + `no-cache` 头破缓存 |

---

## 四、工程亮点

1. **密码学实现经交叉验证**：手写 CBC 与标准库逐字节比对（`test_handwritten_cbc_matches_stdlib`），而非仅靠「能解开」判断正确。
2. **测试设计有多层次思维**：`test_macos_lldb.py` 用「静态断言 API 用法 + 假模块动态执行 + 真绑定存在性校验」三层；`test_source_uses_mkstemp` 甚至**剥离注释后做源码级断言**（防止注释里提到旧实现名而误报）。
3. **修复必带回归测试**：`test_regressions.py` 的模块 docstring 逐条列出 11 项历史缺陷编号，每项都有对应用例。这是**教科书式的回归防护**。
4. **注释解释「为什么」而非「做什么」**：例如 `export_stream.py` 记录 99.5% 耗时的实测数据、`registry.py` 解释为何不能用 `with ThreadPoolExecutor`、`stats.css` 解释 SVG 首帧动画的浏览器行为。
5. **双源同步由测试守护**：README 免责声明与 `ui/pages/disclaimer.html` 必须一致（`TestDisclaimerSync` 用 11 个哨兵短语双向断言）。
6. **文档密度与代码同构**：`docs/module-*.md` 与源码模块一一对应，`docs/architecture.md` 的声明与实现实测吻合。
7. **「不做防御性加固」是自觉选择**：项目明确定位本地单用户工具，威胁模型清晰（`docs/security-audit-2026-09-19.md` 第一章），未引入无谓复杂度。

---

## 五、问题清单

### 5.1 【新发现·中危】本人消息判定使用 `split("_6")[0]`，与既有通用实现不一致

**位置**：
- `siwx/api_chat.py:481`、`siwx/api_chat.py:600`
- `siwx/export_stream.py:126`

**实现**：
```python
my_base = account.split("_6")[0] if "_6" in account else account
```

**问题**：这是**基于经验假设的脆弱解析**。微信账号目录名形如 `wxid_xxx_<uin>`，作者观察到 uin 通常以 `6` 开头，故用 `"_6"` 作为分隔标记。但：

| 场景 | 结果 |
|---|---|
| `wxid_redacted_a_6409`（本机实际） | → `wxid_redacted_a` ✅ 正常 |
| `wxid_abc_7102`（uin 不以 6 开头） | → `wxid_abc_7102`（未剥离）❌ `is_me` 判定失效 |
| `wxid_6xyz_6409`（wxid 本身含 `_6`） | → `wxid` ❌ 严重截断 |

**`is_me` 失效的后果**：自己发送的消息不会被标记为「我发的」，前端会把它渲染成对方消息（头像、气泡方向、`isSend` 字段全部错误），导出产物中的 `isSend` 也会错。

**为什么这是真实缺陷而非理论风险**：项目**已经存在**更通用的实现，却未被复用：

| 位置 | 规则 | `wxid_owner_1234` 结果 |
|---|---|---|
| `media.py:73` `clean_wxid()` | 前缀 `wxid_` 且 ≥3 段 → 取前两段 | `wxid_owner` ✅ |
| `strategies/mmkv.py:22` `clean_wxid()` | 同上 | `wxid_owner` ✅ |
| `ui/pages/chat.js:157` `ownerUsername()` | `/_\d+$/` 剥离尾部数字 | `wxid_owner` ✅ |
| **`api_chat.py` / `export_stream.py` 的 `is_me`** | **`split("_6")[0]`** | **`wxid_owner_1234`** ❌ |

`api_chat.py:858-861` 的头像分支**已经**调用 `media.clean_wxid(account)` —— 说明作者明确知道目录名需要还原，但**只在头像路径做了**，消息归属路径漏掉了。

**建议方向**（不在此次审计范围内实施）：统一到 `clean_wxid()` 单点实现；`clean_wxid()` 本身也应处理「wxid 含下划线」的边界（当前 `"_".join(parts[:2])` 对 `wxid_a_b_1234` 会返回 `wxid_a`）。补充针对非 `_6` 后缀账号的回归用例。

---

### 5.2 【新发现·中危】`push_and_pr.sh` 将 GitHub Token 写入 `.git/config`

**位置**：`push_and_pr.sh:28`

```bash
git remote set-url origin "https://oauth2:${GITHUB_TOKEN}@github.com/${REPO}.git"
```

**问题**：该命令把 OAuth Token **明文写入 `.git/config` 的 remote URL**，且脚本**没有恢复原始 URL**。任何能读取该文件的人（其他进程、备份、误提交）都能拿到凭据。此外，`curl` 的 `-H "Authorization: Bearer ${GITHUB_TOKEN}"` 在进程列表中可能短暂可见。

**背景**：这是 issue #3 一次性 PR 脚本，属历史遗留物，仍留在仓库根目录。它同时硬编码了分支名 `fix/macos-key-extraction-0n`（含 `/`，与 `CONTRIBUTING.md` 的「分支名不要带 `/`」约定相悖 —— 该约定正是为修复 `push_and_pr.sh` 造成的 HEAD 异常而写入的）。

**建议方向**：删除该脚本（其使命已完成），或至少改为使用 `git credential` 机制 / 临时 `-c` 配置，并在结束时还原 remote。

---

### 5.3 【既有·高危·未修复】`docs/security-audit-2026-09-19.md` 记录的安全问题

以下条目在既有审计文档中已记录，本次审计**确认代码现状未变**（即仍未修复）：

| 编号 | 问题 | 位置 | 前提 |
|---|---|---|---|
| **H-1** | `/api/settings/clear` 路径穿越导致 `rmtree` 任意目录 | `api_settings.py` | 需构造请求体 |
| **H-2** | `/api/update/do` 接受请求体中的 assets URL + 空 sha256 绕过校验 → 任意 exe 执行 | `api_update.py` | **仅 frozen 版** |
| **M-1** | 全部 API 无鉴权 + 无 Host 头校验 → DNS rebinding 可致本机浏览器被诱导访问 | 全局 | 需用户访问恶意网页 |
| **M-2** | `account` 参数路径穿越 | `api_chat.py` 等 | — |
| **M-3** | `/api/run` 的 `out_dir` 未校验 | `server.py` | — |
| **L-1** | 脱敏规则未覆盖 32 位 hex（媒体密钥可能泄漏到日志） | `logger.py` | — |
| **L-2** | `_safe_name` 未处理前导 `..` | `exporter.py` | — |
| **L-3** | `media_key.json` 明文存储 | `media.py` | — |

**威胁模型与风险定性**（沿用既有文档，本次复核同意）：

- 默认绑 `127.0.0.1`（`run_server` 默认值 + `cli.py` 裸跑分支），MCP 走 stdio 不开端口 —— **这是上述问题在当前配置下不构成远程直接可利用的前提**。
- 攻击者画像：本机其他进程 + 用户浏览器访问的任意网页。
- ⚠️ **若将来有人把 `--host` 改为 `0.0.0.0`，H-1 / H-2 / M-1~M-3 全部升级为远程可利用**。这是本项目最重要的单点风险约束。

**复核补充**：本次审计确认全库**无** `shell=True` / `os.system` / `eval` / `exec`（`exec_module` 加载插件属预期行为）/ `tarfile` / `zipfile.extractall`。H-2 的入口链路与文档描述一致：`settings.js` 把服务端返回的 `chk.remote` 对象**原样回传**给 `/api/update/do`，服务端信任该对象的 URL 字段。

**结构性观察**：`account` / `wxid` / `out_dir` / `db_dir` 这类**从请求取的用户可控路径参数目前没有统一校验 helper**。全库唯一规范的写法是 `api_plugins.py::_find_page_dir()` 的「字符白名单 + 目录存在」。新增端点时不应复制旧写法。

---

### 5.4 【既有·数据安全·未修复】`docs/data-safety-audit-2026-09-19.md` 记录的数据问题

| 编号 | 问题 | 状态 |
|---|---|---|
| **D-1** | 多账号同名目录互相覆盖（本机实测：`wxid_redacted_a_6409` 在 C/D 盘各一份，6.6MB 空壳 vs 566.7MB 真实数据，共用 `output/<wxid>/`） | ✅ 已缓解（方案 B `@source` + 方案 C 冲突检测） |
| **D-2** | `sqlcipher` 临时文件用 PID 命名 → 并发 page1 错配污染密钥库（实测 8 线程正确 1/8、错配 7/8） | ✅ 已修复（`mkstemp`，修复后 8/8） |
| **D-3** | manifest 无来源标签 | ✅ 已加 `@source` |
| **D-4** | `media._IMG_CACHE` 无锁 | ❌ 未修复 |
| **D-5** | 导出目录秒级时间戳可能撞名 | ❌ 未修复 |

**本次审计对 D-4 的扩展发现**：同类的**无锁模块级缓存**不止 `_IMG_CACHE`，还包括 `api_chat._SHARD_INDEX` / `_CONTACT_CACHE` / `_SESSION_CACHE`。四者都是 `dict` + 「检查存在则复用」模式，在 Flask `threaded=True` 下存在与 D-2 同类的竞争窗口。实际影响面小于 D-2（这些缓存的值不涉及密钥，最坏情况是重复计算或读到部分构建的索引），但**成因相同，值得一并纳入修复范围**。

---

### 5.5 【新发现·低危】健壮性与一致性观察

| # | 位置 | 观察 | 影响 |
|---|---|---|---|
| 1 | `ui/pages/logs.js:11-16` | `levelClass()` 用**中文关键词**（"失败" / "成功" / "跳过"）匹配日志级别 | 任何正文含这些词的日志都会被误着色；日志级别信息实际已在结构化字段中（`entry[1]`），未使用 |
| 2 | `ui/pages/export.js:183-190` | `renderLogs()` 只取 `entry[3]` 或 `entry[1]`，**丢弃 level/module**，且不做级别着色 | 与 `logs.js` 的着色行为不一致 |
| 3 | `ui/pages/export.js:164-168` | 进度条靠正则 `/\[export\] (\d+)%/` 解析日志文本 | 日志格式一变进度条即失效（静默停在 0%）；与后端形成隐式文本契约 |
| 4 | `ui/pages/mcp.js:87-94` | 自行实现 `copy()`，只用 `navigator.clipboard`，**未复用 `SX.copyText()`**（后者含 `execCommand` 回退） | 在非安全上下文（如通过局域网 IP 访问）复制会静默失败 |
| 5 | `siwx/html_template.py` | `rawContent[:8000]` 截断 | 超长消息（长文/大段 XML）内容丢失且**无提示** |
| 6 | `siwx/stats.py` | 排行统计仅覆盖私聊 | 这是**有意设计**（`docs` 与测试均声明），但群聊活跃度完全不可见，属产品取舍 |
| 7 | `ui/pages/stats.js:312-319` | `countUp()` 用 `Number(raw.replace(/[^\d.]/g, ''))` 从**已渲染文本**反推数值 | 当前输入均为纯数字，安全；但若卡片值改为 "1.2 万" / "3 天" 之类会解析错 |
| 8 | `siwx/auto_update.py` + `version.json` | 版本检查与更新脚本均依赖第三方反代 `raw.gh.1s.fan` | 可用性与可信度取决于该第三方；`VERSION_URLS` 多源取最新缓解了单点，但更新脚本 URL 是单点 |

### 5.6 【新发现·低危】仓库整洁性

| 位置 | 观察 |
|---|---|
| 根目录 `diag_blobs.py` / `diag_media.py` / `diag_verify.py`（3 个诊断脚本，共 388 行） | 媒体解密研究期的工具，未归入 `scripts/` 或 `tests/`，散落在仓库根目录 |
| 根目录 `push_and_pr.sh` | 见 §5.2，一次性 PR 脚本，已无用途 |
| 根目录 `PR_DESCRIPTION.md` | issue #3 的 PR 描述，已完成使命 |
| `auto_sync.json` | 运行期配置（`enabled: true`, `interval_minutes: 3`），位于仓库根目录而非 `paths.data_dir()`；存在被误提交的风险（建议确认 `.gitignore` 覆盖） |
| `siwx/keystore.py` 中的 `v5.0.1` 字样 | **不是**版本号遗漏，是历史兼容说明（见 §3.3），未来升级时勿误改 |

---

## 六、测试体系

| 套件 | 行数 | 用例数 | 覆盖重点 |
|---|---|---|---|
| `test_regressions.py` | 1,696 | ~110 | 11 项历史缺陷 + 统计 API + 环境信息 + 贡献模板 + 密码学原语 + 临时文件唯一性 + 账号冲突 + manifest 来源保护 + 免责声明同步 |
| `test_plugins.py` | 1,393 | ~103 | 契约解析 / 条件求值 / 配置存储 / 加载报告 / 目录发现 / 注册表查询 / chat_bridge / 导出与 MCP 注册 / 节点树契约 / 零插件兼容 |
| `test_macos_lldb.py` | 378 | ~10 | LLDB 脚本编译 + API 用法不变量 + 假模块动态执行 + 真绑定存在性 |

**质量评价**：

- 全部测试**轻量、自包含、秒级**，使用合成数据与临时目录，不触碰真实聊天库（符合 `CONTRIBUTING.md` 要求）。
- 两个宿主套件通过 `SIWX_NO_PLUGINS=1` 与「用户装了什么插件」解耦；`TestTempRootCase._clear_plugins()` 处理了 `unittest discover` 同进程残留单例的问题 —— **这是有洞察力的处理**。
- 版本一致性由 `TestVersionSource` 守护（`__version__ == "5.0.3"`），CI 另在 `release.yml` 校验 tag 与 `__version__` 一致。

**测试覆盖的空白**（观察，非缺陷）：

- 无 `is_me` 判定在**非 `_6` 后缀账号**下的用例（与 §5.1 直接相关）
- 无 `html_template` 超长消息截断的用例
- 无前端 JS 的单元测试（`CONTRIBUTING.md` 只要求 `node --check` 语法检查；节点树契约以「服务端必须遵守的契约」形式在 Python 侧固化）

---

## 七、文档体系

28 篇 `docs/` 文档 + 5 篇根目录文档，**6,578 + 1,146 行**。分层清晰：

| 层次 | 文档 |
|---|---|
| 总览 | `architecture.md`（系统全景 + 5 个设计决策 + 安全边界表）、`README.md`（文档地图 + 项目结构树 + 5 条设计红线） |
| 模块详解 | `module-*.md` × 15：sqlcipher / keystore / discover / winproc / extract / strategies / pool / media / server / exporter / mcp / tui / paths / html-template / plugins |
| 专题 | `media-decryption-principles.md`（**媒体解密权威技术依据**）、`media-research.md`（研究笔记）、`plugin-development.md`（写插件）、`wal-support-plan.md`（设计稿） |
| 接口 | `api-reference.md`（全部 Web API 端点 + 错误码）、`cli-commands.md`（命令 + 退出码） |
| 工程 | `packaging.md`、`CONTRIBUTING.md`、`MACOS_SUPPORT.md` |
| 审计 | `security-audit-2026-09-19.md`、`data-safety-audit-2026-09-19.md`、**本报告** |

**值得称道的文档实践**：

1. `docs/media-decryption-principles.md` 明确标注「**分隔尾 16 字节不可硬编码**，实测 `a2b382a8394dfe19eb926c16b3719f7e` 随版本变化」—— 把研究结论中的**反例**写进文档，防止后来者走弯路。
2. `docs/data-safety-audit-2026-09-19.md` 含**对初版误判的更正说明**（「save_manifest 是叠加非重建」），保留了纠错痕迹。
3. `docs/wal-support-plan.md` 是完整的 WAL 支持设计稿，且 **§0 明确结论「不建议实施」**（实测 checkpoint 为秒级、真正瓶颈是同步间隔）—— 记录了**放弃某个方案的理由**，避免重复调研。
4. `docs/plugin-development.md` 从「60 秒上手」到「逐个 hook 详解」到「完整示例」，是可直接照做的开发指南。

---

## 八、结论与建议

### 8.1 总体评价

这是一份**成熟的、有明确自我约束的**代码库。密码学实现正确且验证充分；数据损坏防线（原子写 + 唯一临时文件 + 来源标记）是项目最扎实的部分；插件系统的 17 个扩展点设计克制、向后兼容红线有测试守护；测试与文档的密度、深度均显著高于同类项目。

作者对「本地单用户工具」的定位保持清醒：威胁模型写在文档第一章，不引入无谓的防御性复杂度，但**该有的数据安全防线一条不少**。

### 8.2 建议的处理优先级

| 优先级 | 事项 | 依据 |
|---|---|---|
| **P0** | 复核 `--host` 是否可能被改为 `0.0.0.0`；若可能，须先修复 H-1 / H-2 / M-1~M-3 | §5.3 |
| **P1** | 统一 `is_me` 的账号名还原逻辑到 `clean_wxid()`，并补齐非 `_6` 后缀账号的回归用例 | §5.1（数据正确性，当前对特定 uin 已失效） |
| **P1** | 删除或加固 `push_and_pr.sh`，清理 `.git/config` 中可能残留的 Token | §5.2（凭据泄漏） |
| **P2** | 将 D-4 的修复范围从 `_IMG_CACHE` 扩展到 4 个无锁模块级缓存 | §5.4 |
| **P2** | 为 `account` / `out_dir` / `db_dir` 增加统一路径校验 helper（照抄 `_find_page_dir` 模式），新端点必须使用 | §5.3 |
| **P3** | 前端一致性：`mcp.js` 复用 `SX.copyText`；`export.js` / `logs.js` 统一日志级别处理；进度条改为结构化字段 | §5.5 |
| **P3** | 仓库整洁：`diag_*.py` 归入 `scripts/`，移除已完成的 `PR_DESCRIPTION.md` / `push_and_pr.sh`，确认 `auto_sync.json` 已被 `.gitignore` 覆盖 | §5.6 |
| **P4** | 评估 `html_template` 的 8000 字符截断是否需要提示或提高上限 | §5.5 |

### 8.3 关于本报告

本报告为**只读审计产物**，未修改任何代码。§5.1、§5.2、§5.5、§5.6 为本次审计新识别的条目；§5.3、§5.4 汇总既有审计文档的未修复项并标注现状与本次复核结论。所有条目均可按标注的文件与行号复核。

---

*报告生成：2026-09-28 · 审计基准：SIWX v5.0.3*
