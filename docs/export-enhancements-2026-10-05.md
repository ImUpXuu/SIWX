# SIWX 5.0.7 导出增强实施记录（2026-10-05）

本文档收录本阶段用户指示、已实施项、方案细节、HTML 渲染不足的处理情况与后续待办。
配套改动：`siwx/exporter.py`、`siwx/html_template.py`、`siwx/wx_faces.py`（新增）、
`siwx/wx_maps.py`（新增）、`siwx/assets/emoji/`（新增，146 张官方表情 PNG，约 940 KB）、
`siwx/templates/default/`（新增，模板包）、`siwx/paths.py`、`siwx/server.py`、
`siwx/api_export.py`、`siwx/ui/*`（前端四页 + 公共层）、`tests/test_regressions.py`。
当前回归：**174 项 + 16 子测试全部通过**（本阶段起点 149 项）。

---

## 一、用户指示（原话要点收录）

1. **不碰微信进程**；已导出的数据视为只读；修 bug 全修，但不多加东西。
2. HTML 导出的**小黄脸用官方 sprite 切图**，不用 unicode emoji；
   导出时按需 base64 内嵌，渲染器把 `[表情名]` 替换为图片。
3. **名片 / 位置 / 通话补全**：渲染器从 rawContent 解析展示，
   JSON 等数据格式保持原文不动；**位置要带地图跳转链接**
   （"跳转链接最好都要有"）。
4. **HTML 模板可替换框架**：用户提出的后续想法——把导出模板做成
   可替换/可定制的框架（本轮未实施，见 §五 待办）。
5. 每阶段写文档：收录用户指示、已完成项、待办。

---

## 二、本轮之前已完成（此前阶段，均已过回归）

| 问题 | 修复 |
| --- | --- |
| 前端"打开目录无反应" | export.js 内联 onclick 拼 Windows 路径被 JS 字符串转义吃掉反斜杠（日志实锤 8 次"打开拒绝"）→ 改 `data-open-path` + addEventListener；common.js `openPath` 失败弹窗提示 |
| 四页 init 竞态 null innerHTML | export/chat/logs/settings 页加 `gone()` 存活哨兵 |
| 缓存版本号 | index.html + app.js UI_VERSION → `2026100401` |
| 时间轴 hover 全量扫描 | hover 只展开已缓存月份，未缓存需点击（原先一次 hover 打 30+ 查询） |
| 多格式同时导出 | widgets.js 下拉 multi 多选；exporter `run_export` 支持格式列表（一次扫描、逐格式写出、files 数组、manifest 加 files）；前端结果页多下载链接；+2 回归测试 |
| 旧导出 HTML 表情分析遗留 | 旧产物 6753 条已删除 |

## 三、本轮已完成

### 3.1 小黄脸（微信内置表情）内嵌渲染

**数据源（关键结论：放弃 sprite 切图方案）**

- 名称映射最初卡在"GIF 编号 → 中文名"无权威对照。最终从 npm 包
  **wechat-emoji-parser 2.3.1**（仓库 mingtianyihou33/wechat-emoji-parser）
  的产物 JS 中提取到内嵌的 148 张官方 PNG（base64）+ 每张图的
  `cn` 中文名（简/繁/英三语）与经典 `code`（`/::)` 系）。
- 该 148 张与官方 sprite v1.2.4 的 148 块一一对应，但包内 PNG 为
  独立文件（48/64/128px），**不需要再切 sprite**，直接转正为素材。
- 148 条目中 `[再见]`、`[抱拳]` 各有新旧两版图（重名）；聊天文本里
  的经典名字对应历史版本，故取带经典 `/:code` 的变体
  （[再见]→Wave、[抱拳]→Fight），**最终 146 个名称**。
- 与用户此前肉眼定位的 sprite 锚点（菜刀/饭/玫瑰等序号）交叉验证一致。

**转正产物**

- `siwx/assets/emoji/f000.png … f145.png`：146 张官方图，统一缩放至
  ≤96px（原图 48/64/128 混杂），总计约 940 KB。
- `siwx/wx_faces.py`：`NAMES` 名称表（与 fNNN.png 顺序一一对应）、
  `load_faces()`（进程内缓存）、`find_used()/used_from_message()`
  （正文 + 引用块扫描）、`datauris()`（按需生成 dataURI）。

**导出链路**

- `exporter._write_html_streaming`：流式消费消息时累积
  `faces_used |= wx_faces.used_from_message(msg)`，
  收尾时 `stream_html_tail(..., faces=wx_faces.datauris(used))`。
- `html_template.stream_html_tail` / `render_html`：有表情时注入
  `<script>window.WX_FACES = {"[微笑]": "data:image/png;base64,…"}</script>`，
  **只嵌本会话用到的**（空会话不注入，避免白付 ~1 MB）。
- 渲染器新增 `fmtText()`：先 `esc()` 再把 `[表情名]` token 查
  `FACES` 表替换为 `<img class="wx-face" …>`（24px、与文字基线对齐）；
  未收录/未注入的 token 原样保留，历史名称与非表情 `[xxx]` 不受影响。
  应用点：**正文、引用块、系统消息、未知类型回退文案**。
- 与 `[转账]/[红包]/[位置]` 前缀卡片分支的先后关系保持不变。

**验证**：除回归测试外，用浏览器实测渲染：
内联表情尺寸与对齐正常、引用块内表情正常、系统消息内表情正常。

### 3.2 名片（42）/ 位置（48）/ 通话（50）

全部为渲染器 JS 分支（rawContent 仅在渲染层解析，JSON/SQLite/XLSX
等数据格式的 rawContent 保持原文未动）：

- **名片 42**：解析 `<nickname>/<province>/<city>/<desc>/<sign>`，
  卡片显示昵称 + "省市 · 签名"（desc 缺失回退 sign；均缺失显示
  "个人名片"）。rawContent 为空时回退 content 文本。不加外链。
- **位置 48**：解析 `<location …>` 的属性（新增 `xmlAttr()` helper，
  属性值非标签体）：
  - x = **纬度**、y = **经度**（用户确认的微信 XML 约定）；
  - 有坐标 → 卡片整体包 URI API **marker** 跳转链接
    （`uri/v1/marker?marker=coord:纬,经;title:…;addr:…&referer=SIWX`，
    整段 marker 值整体 URL 编码）；
  - 无坐标但有名称 → 回退 `uri/v1/search?keyword=…`；
  - 标题取 poiname → label → content 文本；副标题取 label。
  - （第三轮修订：原 `uri/v1/poi` 接口已废弃，实测 HTTP 501 页面提示
    "暂不支持此API"，改用官方现行的 marker 标注接口，浏览器实测
    打开腾讯地图并正确落点标记。）
- **通话 50**：解析 `<calltype>`（1 语音 / 2 视频）与 `<duration>`（秒，
  兼容 CDATA）；副标题优先用 content 自带文案（剥 `[通话]` 前缀），
  否则由 duration 计算"通话时长 X分Y秒"。原"📞 通话"坏占位删除。

**验证**：构造 6 条混合消息经浏览器实测：名片卡（张三 · 广东 深圳 ·
签名文案）、位置卡（腾讯滨海大厦 + 正确 coord/URL 编码的跳转链接）、
通话卡（视频通话 · 通话时长 2分5秒）均正确渲染。

### 3.3 测试与清理

- 新增回归：`TestWxFaces`（素材完整性/扫描/过滤）、
  `TestHtmlFaceInjection`（按需注入、空会话不注入、渲染器静态契约）、
  `TestHtmlFacesEndToEnd`（真实分片 → run_export → HTML 只含用到的表情）。
- 回归全套 157 项 + 16 子测试通过。
- 过程目录 `build_emoji/`（sprite、tile、GIF、npm 包提取物等）已删除，
  素材只保留 `siwx/assets/emoji/` 转正版。

---

## 四、HTML 渲染不足清单 → 处理结果（第二轮实施）

清单按当轮排查顺序编号；除第 6、8 条外本轮全部修复，浏览器逐项实测：

1. **type 19 合并转发不展开（D5 导出侧，已修）**：`export_stream` 本就
   通过公共函数 `parse_quote_or_link` 预解析出 `record` 字段（全量原文，
   不受 8000 截断影响），只是 HTML 导出没透传。现 `_msg_entry` 透传
   `record`，渲染器新增 `renderRecord()`：标题 + 逐条发送者/正文
   （正文同样做表情替换）+ 条数脚注，微信合并转发卡样式。
2. **语音转文字（已修）**：`voiceTrans()` 同时兼容 `<voicetrans>` 子元素
   （CDATA）与 `voicemsg` 属性两种历史形态；已下载/未下载两种语音展示
   都补了转文字层。
3. **47 动画表情 CDN 回退（已修）**：未下载时从 rawContent 的
   `<emoji cdnurl>` 在线加载；加载失败 `__stickerErr` 降级为原占位。
   自包含性不受影响（离线只是回退占位）。
4. **10002 撤回文案（已修）**：content 为 `<sysmsg type="revokemsg">`
   XML 时取 `<replacemsg>` 可读文案（含撤回后重新编辑的内容），
   取不到再剥标签兜底。
5. **文件消息大小/格式（已修）**：49 链接卡解析 `<totallen>`/`<fileext>`，
   副标题追加 "PDF · 2.4 MB" 形态的信息（`fmtSize()` helper）。
6. **位置静态缩略图（已修，第三轮）**：见 §7。原方案"调腾讯静态图 API"
   不可行（必须申请开发者 key），改为高德无 key 瓦片服务，导出时联网
   下载、失败静默回退——联网是尽力而为，离线导出不受影响。
7. **`[红包]` 前缀歧义（已修）**：转账/红包卡片分支现在要求 rawContent
   含 `<wcpayinfo>` 才认；以 `[红包]` 表情开头的普通文本恢复按文本渲染
   （表情图正常替换）。
8. **rawContent 8000 字截断（维持现状）**：这是 P3 阶段的内存权衡决策
   （导出日志有留痕）；受影响的合并转发已由第 1 条的 record 预解析
   规避（解析发生在截断前的全量原文上），其余类型的展示损失可接受。

## 七、位置静态缩略图（第三轮已实施，清单第 6 条收尾）

与 §3.1 表情同一模式（流式扫描收集 → 尾部按需注入），全链路：

- `siwx/wx_maps.py`（新增）：解析 `<location x=纬 y=经>`（与渲染器同一
  XML 约定）→ slippy map 瓦片键 `z/x/y`（z=15）；瓦片键集合 → dataURI。
- **数据源改为高德无 key 瓦片服务**（`wprd01.is.autonavi.com/appmaptile`）：
  腾讯静态图 API 必须申请开发者 key，拿不到即完全不可用；且微信位置
  坐标是 GCJ-02，高德瓦片同为 GCJ-02 不偏移（OSM 是 WGS-84，会差
  几百米）。渲染器 JS 的 `tileKey()` 与 Python 端同算法，键值一致才能
  命中。
- 注入契约：`window.WX_MAPS = {"15/x/y": "data:image/png;base64,…"}`
  （`stream_html_tail` / `render_html` 新增 `maps=` 参数，为空不注入）。
- 渲染器：位置卡命中瓦片时 64×48 缩略图替换 📍 图标（`.wx-map` /
  `.wx-card-mapleft` 样式），卡片仍整体包跳转链接；未命中（离线导出、
  下载失败、坐标非法）回退原文字卡，`[位置]` 文本分支不受影响。
- 尽力而为的三重限流：进程内瓦片缓存（同瓦片多消息去重）；首次下载
  异常置离线标记，本进程内不再尝试（多会话导出不逐会话付超时，日志
  warn 一次）；单次注入新下载上限 24 块瓦片。
- 数据格式（JSON/SQLite/XLSX）与导出产物自包含性不受影响——离线只是
  回退文字卡，导出本身永不因联网失败而报错。

**验证**：回归新增 9 项（`TestWxMaps` / `TestHtmlMapInjection` /
`TestHtmlMapsEndToEnd`，网络全部 mock，不打真瓦片）；浏览器实测：
联网导出两张位置卡显示真实地图缩略图（深圳/北京坐标各一）、表情替换
共存正常；模拟离线导出回退 📍 文字卡，无破图。

## 八、HTML 渲染管线体检与修复（第四轮）

浏览器实测 + 真实数据全量检查，修复项（除标注外均已实测验证）：

1. **类型数字裸露（已修）**：统计面板/类型筛选此前把无映射类型直接显示
   "类型50/类型42/类型48"。两个 names 映射补齐 名片/位置/通话/撤回。
2. **合并转发子消息时间全丢（已修）**：`sourcetime` 兼容三种历史形态——
   epoch 整数、"YYYY-MM-DD HH:MM" 字符串、**HTML 实体转义**
   （"2026-02-03&#x20;11:14:46"）与 12 小时制（"05:33 PM"，归一 24h）。
   此前字符串形态 int() 转换失败被静默吞掉，时间全为 0。渲染器记录卡
   逐条右对齐显示子消息时间（`.rt`）。
3. **服务器端合并记录漏识别（已修）**：转发历史记录微信只下发
   `<type>19</type>` + 标题 + `<des>` 预览（无 recordinfo 体），此前整卡
   错挂 🔗 链接卡且预览被 8000 截断。`_is_recordinfo` 补 `<type>19</type>`
   形态；`_parse_recordinfo` 无 dataitem 时按 "发送者: 文本" 逐行拆
   `<des>` 成子消息。实测 9→14 条记录、55→85 条子消息。
4. **去重折叠合法重复（已修）**：全局组合键去重会把"同一张图连发两次"
   折叠掉（真实库 7→6）。改为仅折叠紧邻完全重复（防解析瑕疵双计）。
5. **文件卡语义（已修）**：链接卡中 82% 实为文件消息（本会话 191/232，
   url 为空不可点）。有 fileext/totallen 时图标 🔗→📄；content 前缀
   "[链接]"→"[文件]"（JSON/网页端同步受益）。
6. **引用块时间（已修）**：`_parse_refer` 解析出的 ts 此前透传时丢弃，
   现随 entry.quote 传入并在引用块头部显示日期。
7. **esc() 引号加固（已修）**：原 div.innerHTML 转义不转义引号，联系人
   名首字符为 ' " 时可破坏 onerror 属性字面量；改手写全量转义。
8. **搜索范围（已修）**：导出页搜索覆盖链接卡标题/URL 与合并转发
   子消息文本（此前 content 里只有前 3 条预览可命中）。
9. **网页端渲染一致性（已修）**：网页聊天页此前合并转发只有单行文本
   预览——补 m-record 展开卡（与导出 HTML 同口径）；文件消息经
   _fmt 修复后显示 "[文件] xxx.docx"。

**未修（记录为已知边界）**：查看器全量数据内联（十万条级会话浏览器吃力，
生成侧已流式）；47 动画表情 CDN 在线回退（离线降级占位，自包含性取舍）；
文件本体（微信 Files 目录）不随导出——文件卡不可点击。

回归：**182 项 + 16 子测试全部通过**（新增记录解析/正向翻页/身份判定等 8 项）。

## 五、HTML 模板可替换框架（第二轮已实施）模板 = **模板包目录**：

```
siwx/templates/default/        内置默认模板（即原实现拆包）
  manifest.json                {"name", "label", "version"}
  head.html                    样式 + 页面骨架（header/面板/灯箱等 DOM）
  renderer.js                  渲染器 + 交互逻辑
<SIWX_ROOT>/templates/<name>/  用户自定义（同名覆盖内置）
```

- **数据契约不变**：`window.CHAT_DATA` / `window.WX_FACES` / `MSG_COUNT`
  / `CHAT_TITLE` 由 html_template 统一注入，模板不负责 JSON 拼装；
  模板只需提供 head.html + renderer.js 这一对（页面骨架里的元素 id
  要与自带的 renderer.js 匹配——契约在模板包内部自洽）。
- `html_template.get_template(name)` / `list_templates()`：解析顺序
  用户目录 → 内置目录，带 lru_cache（SIWX_ROOT 变化会随缓存键失效）；
  缺失/损坏抛 RuntimeError（导出报告可见），不静默回退。
- `stream_html_head/tail` / `render_html` 均接受 `template=` 参数；
  `run_export` / `run_export_multi` 新增 `template` 形参一路透传。
- API：`GET /api/export/templates` 返回可用模板列表（label/builtin 标注）。
- UI：导出页新增"HTML 模板"下拉（默认"默认 · 微信风格"，自定义模板
  标注"（自定义）"），选中值随 export_opts.template 提交。
- 用户自定义模板的规范位置 `<SIWX_ROOT>/templates/` 由
  `paths.templates_root()` 提供（可写回退逻辑与 exports_root 一致）。

## 六、待办

1. 模板包可考虑支持 theme 覆盖（仅换配色不改渲染器）——目前需整包
   复制后改 head.html，功能可用但略繁琐；等真实需求再定。
2. 旧消息里若出现清单外的新版小黄脸（微信后续版本新增名称），
   `wx_faces.NAMES` 需要随官方表情集更新（素材表是静态的）。
3. 地图缩略图数据源是高德无 key 瓦片接口，属非官方契约，若后续失效
   可在 `wx_maps._TILE_URL` 单点换源（key 计算逻辑不变）。
