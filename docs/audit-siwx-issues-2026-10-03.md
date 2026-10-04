# SIWX 问题审计报告（真实数据校准版 v2.1）

> 日期：2026-10-03（v2.1 同日修订）
> 审计方式：静态源码分析 + 构造样本复现 + **只读真实数据校准**（SQLite URI
> `mode=ro&immutable=1` 打开真实解密库，物理不可写、不触碰微信进程与内存）。
>
> **源码基线**：本仓库 SIWX **5.0.7** 快照（`main` @ `3fe8216`，由 v5.0.6 基线升级而来，
> 与 `ImUpXuu/SIWX` v5.0.7 源码一致；`siwx/__init__.py __version__ = "5.0.7"`）。
> 本报告由《SIWX 与 WeFlow 源码调研报告》v1.1 与《SIWX 问题补充清单》v1.0 合并而成。
> **源码行号与机制断言已逐条对照本仓库 5.0.7 源码核实；影响面断言已用真实解密库
> 只读重跑当前 5.0.7 解析函数校准**——v2.0 中"全部断言已实证"的说法不成立
> （构造样本复现 ≠ 实测），本轮已按实测结果降级/升级相应条目，见文末修订记录。

---

## 一、结论速览

1. 用户报告的"转发了入群申请被当作转发了群图片"（跨分片 local_id 撞号 + 媒体映射裸键），**在 5.0.7 已修复**（`exporter.py:102-119` 类型门禁），旧版导出产物升级重导即消除；但**图片对图片的跨分片互挂仍是官方自认的残留**，且已量化：真实库中跨分片同 localId 映射到不同 md5 的撞号键 **19210 个、涉及 350 个会话**，根治需组合键改造。
2. 引用/转发解析路径共 **8 个缺陷（D1–D8）**，按真实数据校准后的优先级：**D7 崩溃（P0，真实命中 182 条，整个会话被丢弃/聊天页 500）**、**D8 引用气泡实体转义乱码（P1，真实命中 7358 条，占全部引用的 23%——这才是用户看到的"引用乱码"）**、D5（实测 2252 条 type 19 合并转发被显示成 `[链接]`，其中 1206 条逐条 datadesc 被丢弃）、D3/D6 维持；**D1/D2/D4 代码缺陷真实（构造样本可复现）但真实库 0 例，降级为防御性修复**。
3. 安全与更新链共 **6 个问题（S1–S3、S5、S6 及 S4 流程项）**，最高危的是两条组合链：**S5 —— S1 的哈希同源/fail-open × S2 的无鉴权/无 Host 校验 = 未鉴权 RCE 链（P0，仅影响 frozen 打包版）**；**S6 —— SQLCipher 密钥明文写入产物目录 `.siwx_cache.json`（P0，全平台含 Windows，v2.0 的 S3 只说"非 Windows 明文"低估了一档）**。
4. 前端/导出存在 **5 个问题（F1–F5）**；工程化与 Agent 改进 **7 项（G1–G7）**。
5. WeFlow 新旧两版的解析架构（精确类型开关、合并转发热解析、复合 localType 解码、媒体组合键等）有 **7 个值得借鉴的点**，按优先级列于 §八（WeFlow 侧断言本地无源码、不可复核，已标注）。

---

## 二、问题定位："转发的入群申请 → 转发了群图片"

### 2.1 历史根因（≤ v5.0.6）与 5.0.7 修复状态

**机制链**（≤ v5.0.6）：

1. 微信 4.x/5.0 的 `message_*.db` 按 `local_id` 独立编号分片存储，**不同分片的 local_id 会撞号**；
2. 导出时媒体映射 `media_map` 以**裸 localId 为键**（现 `exporter.py:174/189/234` 仍如此）；
3. ≤ v5.0.6 回填媒体不分消息类型，文本/链接/系统消息一旦撞上别的消息的键，就被挂上**不属于它的图片**；
4. HTML 模板兜底分支把**任何带 mediaPath 的消息渲染成 `<img>`**——"转发入群申请卡片（type 49）"就变成了"一张群图片"；
5. v5.0.6 及之前的实测规模：某会话 885 条错挂，"邀请你加入群聊"卡片被整张顶掉。

**5.0.7 修复**（本仓库 `exporter.py:102-119`，已核实存在）：

```python
def _attach_media(msg, media_map):
    t = msg.get("localType")
    if t not in (3, 47, 34):   # 类型门禁：只有图片/表情/语音可获 mediaFile
        return
    mf = media_map.get(msg.get("localId"))
    if not mf: return
    if (t == 34) != ("/voice_" in mf):  # 图片槽与语音槽互斥
        return
    msg["mediaFile"] = mf
```

type 49 的入群申请卡片从此不可能再直接获得 mediaFile。**门禁在全部 5 个回填调用点生效**
（`exporter.py:347` HTML 流式、`:506`、`:559`、`:595`、`:621`，含插件路径 `export_stream_with_media()`）。

**官方自认的残留**（`exporter.py:109-110` docstring 原文）：
> 已知残留：同为图片的两条消息跨分片撞号时仍会共享同一张图，彻底解决需要把映射键升级为 (md5, localId, ts) 组合键。

即：**"错误图片"问题在 5.0.7 只是被限制在真图片消息之间，并未根治**。

**【v2.1 残留规模量化（真实库实测）】** 跨分片同 localId、但映射到不同图片 md5 的撞号键
**19210 个，涉及 350 个会话**（真实解密库只读统计，快照 2026-10-02）。这不是理论残留：
平均每个受影响会话约 55 个键，每次撞号即一次图片互挂。

### 2.2 引用/转发解析路径的现存缺陷（D1–D8，影响面以 v2.1 真实数据校准为准）

**D1 — `<refermsg>` 带属性即全盲，且 5.0.7 新分支同样盲（`api_chat.py:474`）**
**【v2.1 降级：代码缺陷真实（构造样本复现），真实库影响面为 0 → 防御性修复】**

```python
m = re.search(r"<refermsg>(.*?)</refermsg>", text, re.S)   # 只匹配无属性形态
```

构造样本可复现：refermsg 带 `type="3"` 属性时 → `quote=None`，引用关系静默丢失。
**但真实库实测：全部 31878 条引用消息中带属性形态 0 例**（31876 条为无属性形态 + 2 条其他），
v2.0 的"常见带属性形态"说法不成立。作为防御性修复仍值得做（微信服务端形态不受本控）。

**【5.0.7 同样盲 + 三处判定需同步修】** 5.0.7 的"外层 49 + 内层 refermsg"引用分支共有
**3 处**包含检查 `"<refermsg>" in text`（`api_chat.py:568`、`api_chat.py:715`、
`export_stream.py:71`——v2.0 只列了 2 处；`export_stream.py:71` 处上游注释载明
"实测全量导出 31437 条内层 57 的引用消息外层全部为 49"），对带属性 refermsg 与 ：474
的正则**等价失败**。且 md5/49-57 门控与引用分流逻辑在这 3 处**逐字重复**
（`api_chat.py:556-581`、`:703-728`、`export_stream.py:59-79`），改类型判定必须
三处同步，建议抽公共函数。

**D2 — `type="?3"?` 宽松正则误判为图片（`api_chat.py:487-488`）**

```python
if re.search(r"type=\"?3\"?", blk) or "<img" in content:
    content = content if content and not content.startswith("<?xml") else "[图片]"
```

该正则**未锚定**，会把以下内容全部当成"被引用的是图片"：
- `datatype="3"`（合并转发 recordinfo 的图片项——字符串 `datatype="3"` 包含子串 `type="3"`）
- `type="33"` / `type="36"` / `type="39"` 等任何以 3 开头的属性值（小程序/文件变体）
- `<emoji … type="3" …>`（表情消息变体）
- `<appmsg type="3">`（音乐类卡片）

凡命中且嵌套 content 以 `<?xml` 开头或为空 → quote 显示 **`[图片]`**。

> **【触发条件收窄（v1.1 修订，本轮静态复核维持）】** 上列"命中集合"是**必要条件而非充分条件**——
> `_parse_refer` 先提取内层 `<title>`（`api_chat.py:484-486`），提取成功时 title 优先输出，不会落到 `[图片]`。
> 实测引用小程序卡片 → `'某小程序'`、音乐卡片 → `'歌曲名'`、带 title 的合并转发 → `'张三和李四的聊天记录'`，均不误标。
> `[图片]` 误标只在三条件同时成立时触发：blk 内有 type=3 形态子串 **且** content 中提取不到 `<title>`（或提取后以 `<?xml` 开头）**且** 内容非空非 CDATA 残留。
> 现实锚点是"引用内嵌图片项（datatype="3"）且 title 缺失/被 `</content>` 截断吞掉的合并转发"，与用户报的"被当成群图片"同源；带 title 的常规卡片不在此列。
>
> **【v2.1 降级】** 真实库实测：**31878 条引用中触发 `[图片]` 误标的为 0 例**——
> 上述触发条件（type=3 子串 + title 提取失败 + 非 CDATA 残留三条件同时成立）在真实数据中
> 未出现。构造样本可复现，属代码鲁棒性缺陷，降为防御性修复（引用类型判定改 refermsg
> `type` 字段全等开关，见 §七第 4 行）。

**D3 — 引用视频/表情时嵌套 XML 原文直出**

实测：引用一条视频 → `quote.content` = `'<msg><videomsg aeskey="v1" cdnvideourl="…" …/></msg>'`
整段 XML 直接展示给用户（无 `type=3` 命中、无 `<title>` 可提取、也没有 `[视频]` 兜底）。5.0.7 代码同（`_parse_refer` 仅有 type=3/`<img` 一个分支）。

**D4 — 引用 appmsg 卡片时 CDATA 残留泄漏**
**【v2.1 降级：CDATA 残留真实库 0 例；真实乱码根因是 D8 实体转义，见下】**

构造样本可复现：引用一条被转发的入群申请卡片（外层 49 + refermsg，内层
`<title><![CDATA["张三"的入群申请]]></title>`）→ `quote.content` =
`'<![CDATA["张三"的入群申请'`——CDATA 包装符残留、文本截断。原因是 `api_chat.py:484-486`
提取内层 `<title>` 后**没有再做一次 `_xml_text` 清洗**，而外层 `_xml_text` 的非贪婪
`re.sub` 在双层 CDATA 嵌套下切分错位。**但真实库实测：quote 路径出现 CDATA 残留的为
0 例**，v2.0 把它列为"引用显示乱码"的根因是误判——真实乱码是 D8 的 HTML 实体转义
（7358 条，23%），v2.0 给出的修法（"内层 title 二次清洗"）对 D8 无效。

> **【5.0.7 部分缓解】** `_fmt` 的 t==49 分支（`api_chat.py:314-322`）现在会经 `_xml_text` 剥 CDATA，
> 导出 TXT/MD 的正文文本不再带 CDATA 包装；但 **`quote.content` 路径（`_parse_refer`）未改**，
> 前端/JSON 导出的引用气泡仍受 D4 影响。

**D5 — type 19 合并转发完全未解析 【真实命中 2252 条】**

合并转发的聊天记录（appmsg type 19 / recordinfo）被当作普通链接卡：实测输出
`[链接] 张三和李四的聊天记录`，内部逐条消息（`<dataitem datatype="…">`）全部丢弃，
datadesc 里的 `[图片][文本]` 字面量原样混入 des。5.0.7 代码同（`KIND_MAP`/`FALLBACK_LABEL`
`api_chat.py:120-123` 无 19，`_fmt` t==49 统一 `[链接] title`）。
**真实库实测：2252 条 type 19 合并转发全部被显示成 `[链接]`，其中 1206 条的逐条
datadesc 被整体丢弃**——是 D 系列里除 D7/D8 外真实影响最大的一条。

**D6 — HTML 模板兜底渲染分支是"错挂成图"的放大器（5.0.7 起不可达，保留收敛建议）**

`html_template.py:336-340` 渲染器对带 mediaPath 的消息：t===3 → img、t===43 → video、
t===47 → img、t===34 → voice，**其余类型一律 `return '<img …>'`**。

> **【v1.1 降级结论，本轮复核维持】** 5.0.7 中 `_attach_media` 门禁在全部 5 个调用点生效
> （`exporter.py:347/:506/:559/:595/:621`），主管线与插件路径均不可达该兜底分支；
> 裸 `media_map` 仅作为参考 dict 传入 `_run_plugin_writer`，只有插件作者自行按 localId
> 查 map 回填才可能绕过——属**理论风险**。该兜底分支本身仍是隐患，值得收敛。

**D7 — `_parse_refer` 对空 content 直接崩溃（v2.1 新增，P0，真实命中 182 条）**

```python
content = _xml_text(g("content"))   # api_chat.py:482；g() 未命中返回 ""，_xml_text("") 返回 None
inner = re.search(r"<title>(.*?)</title>", content, re.S)   # :484 对 None 做 re.search
```

refermsg 内**无 `<content>` 标签或 content 为空**时，`_xml_text` 返回 `None`
（`api_chat.py:461` 的 `return s or None`），`:484` 的 `re.search` 抛出未捕获的
`TypeError: expected string or bytes-like object, got 'NoneType'`。
**真实库只读实测命中 182 条**。后果按入口分级：

- **全量导出**：`export_all` 的会话级 try（`exporter.py:658` 起，`except` 在 `:678`）
  捕获后把**整个会话标记为 error 并跳过**——一条坏消息丢弃一个会话的全部导出；
- **聊天页**：`build_messages` 无兜底，`/api/chat/messages` 直接 500，该会话无法打开。

v2.0 完全没提这条——它是真实数据下影响最大的缺陷。修法：`content` 为 None 时置空串
兜底 + `_parse_refer` 调用点套 try 并在导出 manifest 计数（配合 G2）。

**D8 — 引用气泡实体转义乱码（v2.1 新增，P1，真实命中 7358 条 / 占全部引用 23%）**

`_xml_text`（`api_chat.py:454-460`）**只剥 CDATA、从不做 `html.unescape`**。微信把
嵌套 XML 以 HTML 实体转义形态存储（内层 `<title>` 存成 `&lt;title&gt;`），提取出的
`quote.content` 因此是 `&lt;title&gt;…` 字面量，用户看到的就是"引用显示乱码"。
**真实库实测 7358 条（占 31878 条引用的 23%）**。同一机制还使 `:484` 的 `<title>`
提取与 `:487` 的 `"<img" in content` 判断对实体转义形态**全盲**（提取不到 → 不走
title 优先输出，直接把转义串透传）。v2.0 的 D4 把乱码根因误判为 CDATA，修法无效；
正确修法：提取后 `html.unescape`（或整体改用 `xml.etree` + CDATA 感知解析）。

### 2.3 用户问题的归因结论

| 场景 | 版本 | 是否存在（v2.1 真实数据校准后） | 处置 |
|---|---|---|---|
| 转发的入群申请卡片被整张顶掉、显示成图片 | ≤ v5.0.6 | 存在（官方实测 885 条） | **升级 5.0.7 重新导出即消除**（本仓库已是 5.0.7） |
| 同为图片的跨分片互挂 | 5.0.7 | **残留且已量化：19210 个撞号键 / 350 个会话** | 组合键改造 |
| **引用含空 refermsg content 的消息导致整会话导出失败/聊天页 500** | 5.0.7 | **存在（D7，真实命中 182 条）** | None 兜底 + 调用点 try |
| **引用气泡实体转义乱码 `&lt;title&gt;`** | 5.0.7 | **存在（D8，真实命中 7358 条 / 23%）** | `html.unescape` / XML 感知解析 |
| type 19 合并转发显示成 `[链接]`、逐条内容丢弃 | 5.0.7 | 存在（D5，真实命中 2252 条 / 1206 条 datadesc 丢弃） | 借鉴 WeFlow forwardRecordParser |
| 引用视频/表情时嵌套 XML 原文直出 | 5.0.7 | 存在（D3） | refermsg `type` 全等开关 |
| 引用内嵌图片项且 title 缺失的合并转发被误标为 `[图片]`（条件性） | 5.0.7 | 代码缺陷真实（构造样本可复现），**真实库 0 例**（D2） | 防御性修复：借鉴 WeFlow quoteParser |
| 引用入群申请卡片显示 CDATA 乱码（quote 路径） | 5.0.7 | 构造样本可复现，**真实库 0 例**（D4；真实乱码根因是 D8） | 防御性修复，随 D8 一并处理 |
| 带属性 refermsg 引用关系全丢 | 5.0.7 | 构造样本可复现，**真实库 31878 条引用中 0 例**（D1；5.0.7 三处分支同样盲） | 防御性修复：自写 `<refermsg[\s>]` 容忍匹配（三处同步） |

> v2.0 表中"**WeFlow 新版同样存在**（带属性 refermsg 全盲）"一条：本地无 WeFlow 源码
> （工作区 `分析输出/weflow_*.txt` 不含解析器代码），无法复核，已从表中断言移除；
> SIWX 侧对照本身已验证。

---

## 三、安全与更新链（S 系列）

### S1 — 自动更新"自证清白"：SHA-256 与安装包同源，且可静默跳过 【P0】

- **证据**：`auto_update.py:34-35` 首选下载通道是**第三方镜像 `raw.gh.1s.fan`**（GitHub 官方 raw 仅作 fallback，`:37`）；`:194-208` 期望哈希取自**同一份远程 `version.json` 内的 `sha256` URL**——镜像被劫持时，攻击者可同时替换安装包与配套哈希，校验形同虚设。
- **更糟（两处静默放行）**：
  ① `:239` `if expected_sha and not _verify_sha256(...)` ——**`sha256` 字段缺失/抓取失败时直接跳过校验继续安装**；
  ② `:176-179` `_verify_sha256` 在 `expected_sha` 为空时**直接 `return True`**——静默失败而非拒绝。
- **建议**：① 哈希与二进制走分离通道（GitHub Releases API 直连 + 与本地内嵌的最低安全版本号比对）；② `expected_sha` 为空时**拒绝安装**；③ 长期方案：release 产物签名（minisign/ed25519），公钥内嵌代码。
- **【v2.1】** 本条单独看是"更新被投毒"风险；与 S2 组合后升格为**未鉴权 RCE 链**，见 S5。

### S2 — API 全部 16 条核心路由无鉴权、无 Host 校验 【P1；与 S1 组合为 P0，见 S5】

- **证据**：`server.py` 核心路由 16 条（`@app.get/post`）+ `api_*.py` 蓝图若干，全文无 token/Origin/Host 检查（`rg "CORS|Access-Control|token|auth"` 零命中）；`cli.py:180` 提供 `--host`，可绑 `0.0.0.0`。
- **影响分层**：默认 `127.0.0.1` 下，CSRF 因 `/api/run` 要求 JSON content-type（`get_json(silent=True)` 无 `force`，`server.py:587/723/762`）基本免疫；但**无 Host 校验 → DNS rebinding 可跨源读取 `/api/status`、`/api/logs`、`/api/job`**；显式 `--host 0.0.0.0` 时整个局域网可无凭证触发导出、改日志级别。
- **建议**：启动时生成随机 token 注入首屏 + 全路由校验；Host 白名单；`--host` 非 loopback 时要求 `--trust-lan` 显式确认。
- **【v2.1】** 本条单独看是信息泄露/局域网误触；与 S1 组合后是未鉴权 RCE，见 S5。

### S5 — S1+S2 组合：未鉴权更新触发链 = 本机/局域网 RCE 【v2.1 新增，P0，仅影响 frozen 打包版】

- **证据链（v2.1 新增，逐行核实）**：
  ① `api_update.py:34-39`：`POST /api/update/do` 把**客户端提交的 JSON 原样当作远程
  manifest** 传给 `run_update(remote)`（仅当 JSON 为空才回退到真实拉取）——下载 URL、
  哈希 URL、版本号全部由该 JSON 提供；
  ② 下载地址取 `remote["assets"][key]`（`auto_update.py:131-143`，version.json 提供的
  URL 优先于 GitHub Release 兜底），SHA-256 取 `remote["sha256"]` 指向的**任意 URL**
  （`:194-209`）——即攻击者同时控制下载源与校验源（S1 的同源问题在这里从"镜像被劫持
  才触发"变成"一个 POST 即触发"）；
  ③ 哈希校验双重 fail-open（S1：`:239` 与 `:176-179`）——JSON 里不放 `sha256` 字段即可
  整体跳过校验；
  ④ `run_update` 唯一的前置检查是 `is_frozen()`（`auto_update.py:214-215`），通过后
  Windows 侧 `copyfile` 落位 + `subprocess.Popen` 拉起（`:285-321`）。
- **组合 S2**：该路由无鉴权、无 Origin/Host 校验——任何本机进程（CSRF 免疫对 `fetch`
  + JSON content-type 成立，但**本机任意进程可直接 POST**）、`--host 0.0.0.0` 时的局域网
  对端、以及经 DNS rebinding 的网页均可让程序**下载并落位任意二进制并拉起**。
- **范围限定**：`is_frozen()` 使源码运行模式免疫——RCE 仅影响 PyInstaller 打包的
  发布产物；但发布产物恰是绝大多数用户的形态。
- **建议**（在 S1/S2 各自修法之上）：`/api/update/do` **不接受客户端提交的 manifest**，
  服务端自行拉取并仅使用 `VERSION_URLS` 白名单域名；manifest 与下载/哈希 URL 域名做
  一致性校验。

### S3 — 非 Windows 平台密钥明文落盘 【P1，官方已自认】

- **证据**：`keystore.py:1-12` docstring：Windows DPAPI 加密；"非 Windows（macOS/Linux）作为开关兜底**降级为明文 JSON**"；`:77` 有对应明文兜底实现。
- **关联**：与 G1（明文产物威胁模型）合并处置——macOS 支持刚落地（PR #3），这条会在新用户群上放大。
- **【v2.1】** 本条只覆盖 keystore 兜底路径；密钥的**全平台明文落盘**另见 S6
  （`.siwx_cache.json`，连 Windows DPAPI 也被绕过），S6 才是密钥暴露的主通道。

### S6 — SQLCipher 密钥明文写入产物目录 `.siwx_cache.json` 【v2.1 新增，P0，全平台含 Windows】

- **证据（逐行核实）**：`extract.py:315-317` 把每个库的 64 位十六进制 SQLCipher 明文
  密钥写进 manifest（`manifest[rel] = {..., "key": key_hex}`），经 `pool.save_manifest`
  （`pool.py:21-26`）以普通 `write_text` 落盘到 `output/<wxid>/.siwx_cache.json`——
  无加密、无 DPAPI、无权限收紧。**本机实测：现存 29 个条目全部含明文 key**，文件权限
  0666，Windows 上同样明文可读。
- **影响**：任何能读用户输出目录的进程/同步盘/备份工具都拿到全部库的解密密钥；
  与 G1 的"明文产物常驻磁盘"叠加后，密钥比库本身更危险（库换了目录密钥仍有效）。
  v2.0 的 S3 只说"非 Windows 明文"，低估一档。
- **建议**：manifest 不存密钥（缓存命中所需的 mtime/size 之外字段全部剥离，密钥每次
  会话从 keystore 重取）；过渡方案至少用 DPAPI/keyring 加密后落盘 + 权限收紧到 0600，
  并在数据安全文档（G1）中明示。

### S4 — 快照期版本元数据不同步（本仓库实测，上游发布流程问题）【P3】

- **证据**：本仓库 5.0.7 快照中 `siwx/__init__.py __version__ = "5.0.7"`，但 `version.json` 仍广告 `"version": "5.0.6"`（快照取自 5.0.7 正式发布前）；回归测试 `TestVersionSource::test_current_version_comes_from_package_init` 因此失败（实测 302 通过 / 1 跳过 / 1 失败）。
- **性质**：上游发布流程应在发布时同步两处；非代码缺陷。上游发布 5.0.7 版的 `version.json` 后即消。

---

## 四、前端/导出问题（F 系列）

### F1 — 双渲染器漂移 【P1】

聊天页 `chat.js:384 renderMessageList` 与导出 `html_template.py` 内联渲染器（`:336-340`）是
**两套独立实现**，type 49/引用/媒体口径各自演进。5.0.7 已出现一次真实分叉：HTML 侧修了
"type 49 分支复用预解析 link 字段"，前端侧需另行核对。建议抽公共渲染模块（Python 生成 JS
或共享 schema），并把"两侧输出一致性"加入回归测试。

### F2 — 导出 HTML 体量失控 【P1】

全量消息 JSON 内嵌单 HTML（含 rawContent 截 8000，`html_template.py:40`），大会话单文件数十
MB，浏览器解析卡顿。建议：按月分片导出或导出页虚拟滚动。

### F3 — 导出媒体外链，单文件分享即断图 【P2】

`html_template.py:336-339` img/video/audio 均为相对路径引用 `media/` 目录；把单个 HTML 发给
别人即全部裂图。建议：提供"小图内嵌 base64 + 全量打包 zip"选项。

### F4 — 静态资源 `?v=` 手工版本串 【P2】

5 个页面 import 写死 `/widgets.js?v=2026100203`（chat/export/stats/settings/mcp 的 `*.js:2`，
已逐个核实），发版漏改即用户端缓存旧 JS。建议：由 `version.json` 统一注入。

### F5 — 导出 HTML 灯箱无触屏适配 【P2】

`html_template.py:266` lightbox 结构已在，但导出页**无任何 touch 事件处理**（grep 0 命中）；
Web 前端 `chat.js:142-154` 反而有完整 touch 处理。触屏长按保存/关闭手势仅对导出 HTML 缺失。
细节以此为准，留档防止遗失。

---

## 五、工程化与 Agent 改进（G 系列，源自 GreenBubbles 对比 + debug 调研）

| 编号 | 优先级 | 问题 | 建议方案 | 依据 |
|---|---|---|---|---|
| **G1** | **P0** | 解密产物明文常驻磁盘（`message_*.db`、`media/`、明文语音），无清理/加密选项（exporter/api_export/server 全库 grep 无 cleanup/encrypt 相关代码——v2.1 复核仍零命中），无威胁模型文档；**v2.1 补强：明文的还包括密钥本身——`.siwx_cache.json` 内是全部库的 SQLCipher 明文密钥（见 S6），密钥比库更危险** | 补 THREAT_MODEL.md；导出完成页加"清理解密中间产物"（含 `.siwx_cache.json` 密钥文件）与"产物加密压缩包"选项 | GreenBubbles 默认不落明文 + RECOVERABLE_SNAPSHOTS 范式 |
| **G2** | **P0** | **导出无完整性裁决**：解析 fallback（`[链接]`/`quote=None`）静默、媒体失败仅内部日志，用户拿到 3 万条导出后不知道缺了什么——D1–D8 这类缺陷正是靠"静默"藏住的（`manifest` 仅存在于 `pool.py` 分片索引缓存） | 导出目录生成 `manifest.json`（各类型解析成功/兜底计数、媒体失败分布、分片覆盖、发送者未命中数），HTML 头部加统计条 | GreenBubbles `HistoryContextHealth` 的缺口计数器模型 |
| **G3** | **P0** | **debug 通道缺失**：`log.detailed` 全库精确 4 个调用点（`extract.py:42,46`、`plugins/loader.py:201`、`sns_cdn.py:416`），api_chat/decrypt 主链路**零日志**；无 `--debug`、无 `SIWX_DEBUG` 环境变量 | 在 fallback 分支加 `log.detailed` 打点（localId + 原始 XML 前 500 字符，日志页脱敏管道现成）；或 `SIWX_DEBUG_PARSE=1` 落盘 jsonl | 当日 debug 专项调研；**现成 workaround**：导出 JSON/SQLite 的 rawContent 字段、导出 HTML 的 `window.CHAT_DATA` |
| **G4** | P1 | MCP 面（`api_mcp.py`/`mcp_server.py`）无会话级授权与审计：聊天记录喂 AI 是注入面，消息正文应视为不可信数据。（分页条数上限已有：`api_mcp.py:59`、`mcp_server.py:159` 等；缺的是白名单与审计） | 工具参数绑定允许的 chat 白名单/字段限制 + 调用审计日志 | GreenBubbles AI_TOOL_BOUNDARY（确定性授权层，非提示词） |
| **G5** | P1 | 每次导出全量重跑（分片索引缓存只解决打开开销；代码内"增量"均指流式写入器 `export_stream.py`，非增量导出） | 按分片 mtime/大小指纹跳过未变化分片；按 (create_time, local_id) 水位续写追加型会话 | GreenBubbles change-proportional acquisition |
| **G6** | P1 | 缺三篇关键文档：KNOWN_LIMITATIONS（把 D1–D8、媒体残留互挂及量化规模写明）、WECHAT_DATABASE_FORMAT（社区价值大）、THREAT_MODEL（并入 G1/S3/S6）——`docs/` 现有 30+ 篇均无此三类 | 三篇各约半天工作量 | GreenBubbles 30 篇文档体系 |
| **G7** | P2 | 无 SECURITY.md（密钥提取涉及调试器注入，应收漏洞报告，已核实根目录无此文件）；`packaging/` 仅 PyInstaller spec，发布链无一键安装脚本 | 补 SECURITY.md + 安装脚本；CHANGELOG 走 version.json 可接受 | GreenBubbles 发布链 |

---

## 六、WeFlow 新旧版本对比（解析架构视角）

> **【v2.1 可复核性说明】** 本节 WeFlow 侧断言（仓库关系、文件名、行号、代码摘录）出自
> 早期调研记录；**本地现无 WeFlow 源码**（工作区 `分析输出/weflow_*.txt` 不含解析器代码），
> 本轮无法复核，包括"WeFlow 对带属性 refermsg 同样全盲"（v1.1 勘误）这一条。SIWX 侧的
> 对照结论（缺陷机制与行号）已全部核实。借鉴点的设计思路不受影响，但具体参照实现请以
> 重新拉取 WeFlow 源码核对为准。

### 6.1 仓库关系澄清

- `hicccc77/WeFlow` 是**官方现役仓库**（2026-09-28 重新 init；README 全部徽章/Release/贡献者链接指向它）；代码量明显更新：新增整套 agent 模块（`agent*` 命名文件实测 40 个，相对旧仓净新增独有文件 118 个）。
- `lurve1314/WeFlow` 是旧快照（2026-07-07），SIWX 文档引用的是它。两者的消息解析核心（`chatService.ts`、`httpService.ts`、`export/parsers/`）基本同源。

### 6.2 WeFlow 对"引用/转发/卡片"消息的处理方式（对照 SIWX 缺陷）

**① quoteParser.ts —— 引用解析用"精确类型开关"，杜绝正则误判**

```typescript
const referType = extractXmlValue(referMsgXml, 'type')
switch (referType) {
  case '1':  displayContent = extractPreferredQuotedText(referMsgXml); break
  case '3':  displayContent = '[图片]';   break
  case '34': displayContent = '[语音]';   break
  case '43': displayContent = '[视频]';   break
  case '47': displayContent = '[表情包]'; break
  case '49': displayContent = '[链接]';   break
  case '42': displayContent = '[名片]';   break
  case '48': displayContent = '[位置]';   break
  default:   /* 兜底走 sanitize，绝不猜类型 */
}
```

- 引用类型取自 refermsg 的 `type` **字段值并全等比较**——不存在 SIWX D2 的子串误判；
- **每种类型都有兜底文案**——不存在 SIWX D3 的 XML 直出；
- `extractXmlValue` 做正规 XML 值抽取 + HTML 实体解码——不存在 SIWX D4 的 CDATA 残留；
- **【v1.1 勘误，维持】** `indexOf('<refermsg>')` **不**容忍属性——复核实测 WeFlow 官方
  `parseQuoteMessage` 与 `chatService.ts:7246/7327` 对**带属性 refermsg 同样全盲**——
  与 SIWX 的 D1 等价失败。讽刺的是 WeFlow 自己在 `chatService.ts:5480/6833` 写过容忍属性的
  `/<refermsg[\s\S]*?<\/refermsg>/gi`（仅用于剥离场景），且拥有 `extractXmlAttribute` 工具，
  却没用在引用定位上——**D1 是两个项目的共同缺陷，不能照抄，需自写 `<refermsg[\s>]` 定位**；
- `looksLikeAccountId` + `sanitizeQuotedContent`：对 `wxid_xxx`/`accountId_xxx` 形态的发送者与正文脱敏、清洗（SIWX 无此处理）；
- `extractPartialQuotedText`：用引用时的 start/end 偏移精确截取被引用文本，而不是全文塞进去。

**② xmlExtractor.ts —— normalizeAppMessageContent 统一预处理**

对 `&lt;` 实体转义、双层 XML 包装、CDATA 混排做统一归一化后再解析。SIWX 是"裸正则 + 一次性
`_xml_text`"，双层 CDATA（D4）正是缺这一层的直接后果。

**③ forwardRecordParser.ts —— 合并转发热解析（对照 SIWX D5）**

按 `<dataitem datatype="N">` 逐项解析（datatype 分发表），提取 sourcename/sourcetime/datadesc/datatitle/fileext/datasize，按 `datatype|sourcename|sourcetime|datadesc|datatitle` 组合键去重。SIWX 目前只显示标题。

**④ 复合 localType 变体解码（新旧 WeFlow 均有）**

`httpService.ts` 的 `mapType49` + `resolveType49Subtype`：除 XML 内 `<type>` 外，还处理 WeChat 4.x packed_info 复合类型——`244813135921`（引用）、`266287972401`（拍一拍）、`8594229559345`（红包）、`8589934592049`（转账）等，映射为类型化枚举 ChatLabType。SIWX 只按 `localType & 0xFFFF` 处理，这批复合类型会被归入"类型49/其他"。

**⑤ type 49 文案细化**

WeFlow 输出 `[聊天记录] 标题`、`[小程序]`、`[转账]`、`[红包]`、`[文件]` 等；SIWX 统一 `[链接] 标题`，导出的 TXT/MD 可读性差一截。

**⑥ 新版独有**：导出管线拆成 `export/parsers/{messageParser, contentDecoder, fileAppParser, transferParser, voipParser, quoteParser, forwardRecordParser, xmlExtractor}` 纯函数模块 + `apiMessageMapperPool` 映射器池，单测友好；agent 记忆/多模型适配是另一域的能力。

### 6.3 SIWX 相对优势（避免盲目照搬）

- SIWX 的分片索引缓存、K 路归并流式导出、媒体门禁+失败对账日志，是 WeFlow 没有的工程亮点（WeFlow 旧版曾因全量加载在大会话上 OOM）；
- SIWX 纯 Python 实现 ISAAC64 流解密，摆脱了 WeFlow 的 WASM 依赖；
- 回归测试规模（304 项）比 WeFlow 的解析测试覆盖面更成体系。

---

## 七、修复优先级清单（借鉴 WeFlow 的 7 个点 + SIWX 自身健壮性修复，按优先级）

| 优先级 | 修复点 | 解决的 SIWX 缺陷 | 参照实现 |
|---|---|---|---|
| **P0** | **`_parse_refer` 崩溃兜底**：content 为 None 时置空串；`_parse_refer` 调用点（3 处）套 try 并计入 G2 的 manifest 计数，**不允许单条坏消息丢弃整个会话** | **D7（真实命中 182 条：全量导出丢整会话、聊天页 500）** | 无 WeFlow 参照，SIWX 自身健壮性修复 |
| **P0** | **密钥不落盘**：`.siwx_cache.json` 剥离 `key` 字段（S6 修法），已落盘的存量文件提示清理 | **S6（全平台明文密钥）** | keystore 的 DPAPI 通道现成 |
| **P0** | **`/api/update/do` 拒绝客户端提交的 manifest**，服务端白名单域名自行拉取 + 域名一致性校验 | **S5（未鉴权 RCE 链）** | 无外部参照，S5 建议条目 |
| **P0** | 引用类型判定改为 **refermsg `type` 字段全等开关**，每种类型给固定文案（[图片]/[语音]/[视频]/[表情包]/[链接]/[名片]/[位置]），删除 `type="?3"?` 正则 | D2（误标[图片]，真实 0 例）、D3（XML直出） | `quoteParser.ts` switch(referType) |
| **P0** | `<refermsg>` 匹配改为容忍属性的正则 `<refermsg[\s>]`；**包含判断共 3 处**（`api_chat.py:568`、`:715`、`export_stream.py:71`）需同步修 | D1（引用关系丢失，真实 0 例，防御性） | 无现成实现；参考 `chatService.ts:5480` 的剥离用正则 |
| **P0** | 媒体映射键升级为 **(md5, localId, ts) 或 svrid 组合键**，根治跨分片互挂（真实 19210 键/350 会话） | 2.1 残留 | WeFlow 媒体按消息 svrid 关联；SIWX docstring 已自认方案 |
| **P1** | 引用提取后做 **`html.unescape` + CDATA 清洗**；引用解析整体改用 `xml.etree` + CDATA 感知 | **D8（真实 7358 条/23%，真实乱码根因）**、D4（CDATA，真实 0 例，随上一并修） | `xmlExtractor.ts` normalizeAppMessageContent |
| **P1** | **md5/49-57 门控与引用分流抽公共函数**：现于 `api_chat.py:556-581`、`:703-728`、`export_stream.py:59-79` 三处逐字重复，改类型判定必须三处同步 | 防回归（D1/D2/D7 的修复都会动这三处） | SIWX 自身工程化修复 |
| **P2** | type 19 合并转发热解析（datatype 分发 + 去重），导出为结构化条目而非链接卡（真实 2252 条受影响） | D5 | `forwardRecordParser.ts` |
| **P2** | packed_info 复合 localType（244813135921 引用 / 转账 / 红包 / 拍一拍）解码入 KIND_MAP | 引用消息在复合类型下全部失联 | `httpService.ts mapType49` |
| **P3** | 引用发送者/正文 **accountId 脱敏**；type 49 文案细分（[聊天记录]/[小程序]/[转账]…）；HTML 兜底分支去掉"未知类型渲染成 img" | 隐私 + D6 | `sanitizeQuotedContent`、`getType49Content`、WeFlow 渲染器 |

---

## 八、验证与证据

- **源码静态复核**：全部行号与机制断言已对照本仓库 5.0.7 源码逐条复核（2026-10-03，v2.1 再次复核）；关键源码定位：
  - `siwx/api_chat.py:474`（D1 refermsg 无属性匹配）、`:482`（content → None）、`:484`（D7 崩溃点：对 None 做 re.search）、`:487-488`（D2 title 提取 + type=3 正则）、`:568/:715` 与 `siwx/export_stream.py:71`（5.0.7 外层 49 引用分支，包含判断共 3 处同样盲）、`:556-581/:703-728` 与 `export_stream.py:59-79`（md5/49-57 门控三处逐字重复）、`:454-461`（`_xml_text` 只剥 CDATA、无 html.unescape，D8 根因）、`:120-123`（KIND_MAP/FALLBACK_LABEL 无 19）、`:557`（md5 仅 3/47 提取）、`:565-581`（49 分流）；
  - `siwx/exporter.py:102-119`（媒体门禁 + 残留声明）、`:347/:506/:559/:595/:621`（5 处 `_attach_media` 调用，门禁均生效）、`:658-679`（export_all 会话级 try，`except` 在 `:678`——D7 的整会话丢弃后果）；
  - `siwx/html_template.py:40`（rawContent 截断）、`:266`（lightbox）、`:336-340`（渲染器兜底 img 分支）；
  - `siwx/auto_update.py:34-35/37`（镜像优先）、`:131-143`（下载 URL 取自 manifest）、`:194-209`（SHA URL 同源）、`:239` + `:176-179`（两处静默放行）、`:214-215`（仅 frozen 放行）、`:285-321`（落位 + Popen 拉起）；`siwx/api_update.py:34-39`（客户端 JSON 直传 `run_update`，S5 链入口）；
  - `siwx/server.py`（16 条核心路由无鉴权）、`siwx/cli.py:180`（`--host`）、`siwx/keystore.py:1-12/:77`（非 Windows 明文兜底）、`siwx/extract.py:315-317` + `siwx/pool.py:21-26`（明文密钥落盘 `.siwx_cache.json`，S6）。
- **真实数据校准（v2.1 新增）**：以只读模式（SQLite URI `mode=ro&immutable=1`，物理不可写、
  不触碰微信进程与内存）打开本机真实解密库 `output/wxalias_example_01/`（15 个库、3481 张
  `Msg_` 表、约 126 万条消息，快照 2026-10-02），用**当前 5.0.7 源码**重跑解析函数，实测：
  - **D7 崩溃：182 条**（refermsg 无 content/空 content）；
  - **D8 实体转义乱码：7358 条 quote.content 含 `&lt;title&gt;` 字面量**（占 31878 条引用的 23%）；
  - **D5：2252 条 type 19 全部显示 `[链接]`**，其中 1206 条逐条 datadesc 被丢弃；
  - **D1/D2/D4：真实命中 0 例**（31878 条引用中带属性 refermsg 0 例；`[图片]` 误标 0 例；CDATA 残留 0 例）；
  - **跨分片互挂残留：19210 个撞号键（同 localId 映射到不同 md5）、涉及 350 个会话**。
- **WeFlow 侧证据**：`electron/services/export/parsers/quoteParser.ts`、`forwardRecordParser.ts`、`xmlExtractor.ts`、`electron/services/httpService.ts`（mapType49/复合类型）——出自早期调研记录，**本地现无 WeFlow 源码，本轮无法复核**（见 §六说明）。
- **行为复现说明**：原报告的 12 组入群申请/转发/引用结构复现（`test_siwx_parsing.py`、`test_siwx_refer.py`）为会话内临时脚本，**未提交到本仓库**；D1–D6 的机制结论为构造 XML 样本下的只读复现，v2.1 起影响面一律以真实数据校准结果为准。
- **产物时效说明**：`exports/export_20261002_181213/` 为旧版本产物，**不用于任何 5.0.7 行为判断**；上文真实数据计数全部来自 `output/` 解密库的输入侧（`message_content` 为微信原始 XML，与哪个版本执行解密无关），解析函数用当前源码树。时效限制：库快照停在 2026-10-02，不含之后的新消息。
- **回归测试**：全量 304 项实测 302 通过 / 1 跳过 / 1 失败（失败项即 S4 所述
  `TestVersionSource::test_current_version_comes_from_package_init`，v2.1 重跑确认）。
- 审计全程未修改任何真实数据库、未触碰微信进程（只读 URI 打开 + 纯 Python 解析）。

---

## 九、待验证清单（v2.1 更新）

1. ~~真实 `message_*.db` 样本到手后：① D1–D6 触发面收敛；② 媒体跨分片互挂实测规模~~
   **已完成（v2.1 真实数据校准，见 §八）**。仍待验证：
   ① packed_info 复合 localType（244813135921 引用/转账/红包）在实际库中的出现频率——决定是否在 G 系列之外升 P0；② 时区漂移（原报告 C1）对真实导出的影响面；③ 库快照 2026-10-02 之后的新消息是否出现 v2.1 判为 0 例的形态（带属性 refermsg 等）。
2. **上游动态**：监控 SIWX issue/PR 中 refermsg、媒体映射键、auto-update 相关改动；上游发布 5.0.7 版 `version.json` 后重跑回归测试（预期 S4 失败项自愈）。
3. **S5 链受控复现**：静态链已逐行核实（`api_update.py:34-39` → `run_update` → fail-open 双路径）；剩余验证为在隔离环境（frozen 打包副本）实际走通"POST 假 manifest → 下载落位"路径，确认无其他隐藏校验。
4. **G3 效果验证**：打点 patch 建议可一并验证——详细模式下日志页能否直接看到 D7/D8 触发现场（D7 的 182 条、D8 的 7358 条是现成回归样本）。

---

## 修订记录

**v2.1（2026-10-03，真实数据校准版）**

1. **方法论修正**：撤销 v2.0"全部断言已实证"的自我声明——其"实证"是构造样本复现，而工作区一直有真实解密库可供实测。本轮以只读模式（`mode=ro&immutable=1`，不写盘、不触碰微信进程）对 126 万条真实消息重跑当前 5.0.7 解析函数，所有影响面断言改为真实数据口径（§八"真实数据校准"）。
2. **新增 D7（P0）**：`_parse_refer` 对空 refermsg content 崩溃（`api_chat.py:484` 对 None 做 re.search），真实命中 182 条；全量导出按会话级 try 丢弃整个会话（`exporter.py:678`），聊天页 500。v2.0 完全遗漏。
3. **新增 D8（P1）并重定 D4 根因**：`_xml_text` 从不 `html.unescape`，实体转义的嵌套 XML 使 7358 条（23%）引用气泡显示 `&lt;title&gt;` 字面量——这才是用户看到的"引用乱码"；D4（CDATA 残留）实测 0 例，v2.0 的修法（"内层 title 二次清洗"）对 D8 无效，修法改为 `html.unescape`/XML 感知解析。
4. **D1/D2 降级为防御性修复**：真实库 31878 条引用中带属性 refermsg 0 例、`[图片]` 误标 0 例；v2.0"带属性形态常见"“D1 影响面扩大"的说法按实测收回（代码缺陷本身经构造样本确认真实）。"WeFlow 新版同样存在"一条因本地无 WeFlow 源码无法复核，已标注（§六说明）。
5. **新增 S5（P0）**：S1+S2 组合成未鉴权 RCE 链——`api_update.py:34-39` 把客户端 POST JSON 直传 `run_update`，下载/哈希 URL 均由该 JSON 提供，校验双 fail-open；本机进程、`--host 0.0.0.0` 的局域网对端、DNS rebinding 页面均可触发任意二进制下载落位并拉起（仅 frozen 打包版受影响）。v2.0 把 S1、S2 分列，未连成链。
6. **新增 S6（P0）**：SQLCipher 明文密钥经 `extract.py:315-317` → `pool.save_manifest` 写入 `output/<wxid>/.siwx_cache.json`（实测 29 条目全含明文 key，权限 0666，Windows 同样明文）。S3"非 Windows 明文"的说法低估一档，已在 S3 标注。
7. **2.1 残留量化**：跨分片同 localId 撞号映射到不同 md5 的键 19210 个、涉及 350 个会话（v2.0 只转述 docstring，未给出规模）。
8. **修法站点勘正**：`"<refermsg>"` 包含判断实为 **3 处**（`api_chat.py:568`、`:715`、`export_stream.py:71`），md5/49-57 门控逻辑 3 处逐字重复（`api_chat.py:556-581`、`:703-728`、`export_stream.py:59-79`）；v2.0 只列 2 处。§七 修复清单已按此更新并新增"抽公共函数"条目。
9. **D5 补真实规模**：2252 条 type 19 全部显示 `[链接]`、1206 条逐条 datadesc 丢弃。
10. **§八 局限声明更正**：v2.0"未持有真实微信数据库样本"为假——真实库一直存在（`output/wxalias_example_01/`，15 库/3481 张 Msg_ 表）；已改为如实描述数据来源与时效（快照 2026-10-02），并声明 `exports/export_20261002_181213/` 为旧版产物、不用于任何 5.0.7 行为判断。
11. **G1 证据补强**（cleanup/encrypt 零命中经复核成立；补入 `.siwx_cache.json` 明文密钥）；**G4 行号复核无误**（`api_mcp.py:59`、`mcp_server.py:159` 精确命中）；**S4 复核成立**（全量回归实测 302 通过/1 跳过/1 失败，失败项即 S4 所述版本元数据测试）；**G3 的 log.detailed 4 个调用点复核精确命中**。v2.0 其余行号断言（媒体门禁 5 调用点、16 条路由、html_template/auto_update/server/cli/keystore 各定位）复核全部命中，维持原文。

**v2.0（2026-10-03，合并修订版）**

1. **合并来源**：《SIWX 与 WeFlow 源码调研报告》v1.1 + 《SIWX 问题补充清单》v1.0；基线从"upstream main @ 973b4f9"换为本仓库 5.0.7 快照（`0597be1`）。
2. **全量行号复核**：两份原稿引用的行号逐条对照本仓库 5.0.7 源码，绝大多数精确命中（如 `api_chat.py:474`、`exporter.py:102-119`、`chat.js:384`、`html_template.py:266/336-339`、`auto_update.py:34-35`）；偏差处已按本仓库修正。
3. **D1 影响面扩大（新增）**：5.0.7 新增"外层 49 + 内层 refermsg"引用分支（`api_chat.py:568`、`export_stream.py:71`），判定用 `"<refermsg>" in text` 包含检查，对带属性 refermsg 与 D1 等价失败；借鉴点 P0 第 2 条已合并该修法。同分支确认 5.0.7 的 `_fmt` t==49 走 `"[引用]"` 口径并经 `_xml_text` 剥 CDATA（D4 的 fmt 路径已缓解，quote 路径未缓解）。
4. **`_attach_media` 调用点计数修正**：原稿"两处"→ 实为 **5 处**（`:347/:506/:559/:595/:621`），门禁全部生效；D6 降级结论维持。
5. **S1 证据补强**：新增第二处静默放行路径——`_verify_sha256` 空值直接 `return True`（`auto_update.py:176-179`）。
6. **S2 计数核实**："16 条路由"精确命中（server.py 核心 `@app.get/post` 恰 16 条 + api_*.py 蓝图）。
7. **F4 状态确认**：`?v=2026100203` 在 5.0.7 的 5 个页面逐个核实存在（此前 5.0.6 基线无此问题，属 5.0.7 引入）。
8. **G4 细化**：MCP 分页条数上限已存在（`api_mcp.py:59`、`mcp_server.py:159` 等），缺失项收窄为"chat 白名单 + 审计日志"。
9. **新增 S4**：快照期 `__version__`（5.0.7）与 `version.json`（广告 5.0.6）不同步，导致 `test_current_version_comes_from_package_init` 失败（实测 302 通过/1 跳过/1 失败）；属上游发布流程问题，非代码缺陷。
10. **复现脚本位置勘正**：`test_siwx_parsing.py`/`test_siwx_refer.py` 为会话内临时脚本，未提交本仓库，已在 §八注明。
11. **原稿复核确认无误、原文保留的部分**：三处仓库 commit 指针、D2 触发条件收窄（v1.1）、D3/D4/D5 逐字复现结论、v5.0.7 release note "885 条"引用、WeFlow 8 个 switch case 与 4 个复合类型常量、F1/F2/F3/F5、G1–G7 主体、6.2 节 v1.1 勘误（WeFlow 对带属性 refermsg 同样全盲）。
