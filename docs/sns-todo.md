# 朋友圈（SNS）待办与接入指南

> 目的：把朋友圈功能**已完成 / 未完成**的边界一次性写清，让你后续接入时不用重新踩坑。
> 配套文档：`module-sns.md`（模块详解）、`sns-implementation-guide.md`（推进记录）、`sns-research-2026-09-29.md`（实验原始数据）。
> 数据来源：本机 `output/wxid_redacted_a_6409/sns/sns.db`（5684 条动态）实测统计。

---

## 0. 当前进度快照（已完成 ✅）

| 层 | 已完成 |
|---|---|
| 数据解析 | 文本、点赞、评论、表情（明文 url 路径）、评论内嵌图片、主图、视频、**实况照片 LivePhoto**、**卡片（链接/视频号/直播/音乐/笔记）** |
| 位置 | `location`（poiName / poiAddress / lat / lng）已解析 ✅ |
| API | 账号列表、时间线游标分页、详情、好友聚合 `/friends`、关键词过滤（**覆盖卡片文本 + 渐进扫描**）、发布者过滤（SQL 下推）、异步导出、媒体下载 |
| 导出 | `json` / `markdown` / `txt` / `html` 四格式（**含卡片块 + 位置 + 评论表情/图**），异步任务走任务槽 |
| 前端 | 列表 / 详情灯箱 / 实况角标 + 点击播视频 / 好友下拉 / **并发数可配** / **卡片渲染** |
| 测试 | 全量 250 通过 / 1 跳过；SNS 单测 48 例 |

**核心已闭环**：数据层、交互层、导出、测试、文档齐全。下面的项都是"增强"，不影响基本可用性。

### 0.1 2026-09-30 更新：P0 卡片渲染已完成 ✅

`siwx/sns.py::_parse_card` + 前端 `renderCard` + 四格式导出 + 13 个测试，已实测于真实库。

**⚠️ 本节推翻了初版文档的关键假设，接入前务必先读（详见 `module-sns.md` §3.2.1）**：

| 初版假设 | 实测结论 |
|---|---|
| 卡片字段在 `<appmsg>` 里 | **根本不存在 `<appmsg>` 节点**；字段是 `<ContentObject>` 的直接子元素 |
| 用 type 编号判断卡片类型 | type 语义会漂移：42/47 是**音乐**、34 是**直播**、26 是**笔记**、5 是外链；7(117)/54(91) **没有任何卡片字段** |
| 用 `_parse_media_el` 解析 `<finderFeed>` 媒体 | 结构完全不同（`thumbUrl`/`coverUrl`/扁平尺寸/**浮点字符串 "1080.0"**/`videoPlayDuration`），必须单独解析，且**不能混进 `feed["medias"]`** |
| music 的 `contentDesc` 为空 → 搜索不到 | 音乐卡片（42/47）的 `title`/`mvAlbumName` 已进搜索 blob；type 7 的 117 条**确实无任何文本可搜**（物理上限） |
| 关键词过滤「取 limit*3 候选」够用 | 实测深层卡片**永远搜不到**（"让风告诉你"返回 0 条）→ 已改为渐进扫描 + 5000 条上限 |

---

## 1. 卡片类型渲染（视频号 / 公众号 / 音乐 / 链接）—— **✅ 已完成（2026-09-30）**

> 本节保留原始分析作为背景，但**初版结论已被实测推翻**（见 §0.1）。
> 实际实现：`sns.py::_parse_card` → `card` 字段 → 前端 `renderCard` → 四格式导出块。
> 下面 §1.3 / §1.4 的字段路径仅作历史记录，**不要照抄**。

### 1.1 真实库数量分布

```
text          3575 条   image          819 条   video          499 条
finder_feed    353 条   link           183 条   music          117 条
mp_article      91 条   unknown5        17 条   note             4 条
finder_live      3 条   unknown26        3 条   ting             3 条
PARSE_FAIL      17 条
```

**卡片类合计 744 条（占 13.1%）**，现已全部按类渲染（其中 type 7 的 117 条实测无卡片字段，
只能按图片/视频渲染 —— 数量上 §1.1 的 "music 117 条" 是个**误判**）。

### 1.2 现状（**历史记录，2026-09-30 前**）

> ⚠️ 以下描述的是**改之前**的状态，现已修复。保留是为了说明"为什么要动这几处"。

`parse_timeline` 当时**只存了** `content_type` / `content_kind` / `contentDesc`（正文），
没有提取卡片专属字段。当时以为标题、链接、封面、作者都在 `<appmsg>`（或 `<finderFeed>`）里。

- 前端 `renderPost` 不按类型分支，所有类型走同一套"文本 + mediaList 缩略图"。
- 导出只有 JSON 里带了 `content_kind`，markdown/txt/html 没有卡片块。
- `contentDesc` 对 type 7（117 条）是**空**的 → 关键词搜索搜不到（已用 `search_text` 修复口径，
  但 type 7 本身确实无任何文本，属物理上限）。

### 1.3 待提取字段（**初版猜测，已证伪 —— 勿照抄**）

> ❌ 下表的 `<appmsg>` 路径**在真实库里一个都不存在**。真实字段位置见
> `module-sns.md` §3.2.1 与 `scripts/sns_card_probe.py` 的输出。

初版以为四类卡片的元数据都包在 `<appmsg>` 内：

| 字段 | 路径 | 含义 |
|---|---|---|
| 标题 | `.//title` | 链接/文章标题（music 样本里为空，需确认路径） |
| 描述 | `.//description` | 摘要/歌手/文案（多数 = contentDesc） |
| 封面 | `.//url` | `shmmsns.qpic.cn/mmsns/...` 封面缩略图（**非 CDN 媒体**，直链） |
| 封面2 | `.//thumburl` | 同上另一封面字段 |
| 来源名 | `.//appname` / `.//sourcedisplayname` | 公众号名 / 音乐 App 名 |
| 跳转 | `.//contentUrl` | 文章/视频号分享页链接 |
| 播放 | `.//musicUrl` / `.//dataUrl` | 音乐播放链接（仅 music） |

**视频号（finder_feed, type 28）专属**（在 `<finderFeed>` 内）：

| 字段 | 路径 | 含义 |
|---|---|---|
| 视频号 ID | `.//finderUsername` | 如 `25984981814268412@openim` |
| 昵称 | `.//finderNickname` | 视频号显示名 |
| feed ID | `.//feedId` | 视频唯一 ID |
| 视频 | `<finderFeed><mediaList>` | **独立**的视频媒体，路径与主图 mediaList 不同 |

### 1.4 接入点（**实际实现**）

1. **`siwx/sns.py`** — 新增 `_parse_card(co, to)` / `_parse_finder_feed` / `_parse_finder_media`，
   `parse_timeline` 返回 `card` 键；kind 由**实际字段**推导（不看 type）。
   另新增 `search_text(feed)` 统一搜索口径。
2. **`siwx/api_sns.py`** — 关键词改用 `sns.search_text(feed)`，并把「取最新 limit*3 条候选」
   改为**渐进扫描**（`KEYWORD_MAX_SCAN = 5000`）。
3. **`siwx/ui/pages/sns.js`** — 新增 `renderCard(card)` 按 `card.kind` 分支
   （link/music/finder/live/note），`renderPost` 接入；有卡片时不再显示「（无文字）」。
4. **`siwx/ui/pages/sns.css`** — 新增 `.sns-card` / `--link` / `--music` / `--finder` / `--live` / `--note`。
5. **`siwx/sns_export.py`** — `_card_to_dict` 进 JSON，`_card_lines` / `_html_card` 进 md/txt/html；
   位置补进 txt/html，评论表情与评论图补进四种格式。

### 1.5 测试（已落地）

- `TestSnsCardParsing`（7 例）：五类卡片字段、kind 按字段判定、finder 媒体不污染 `medias`、
  `search_text` 覆盖卡片/媒体描述/位置。
- `TestSnsCardExport`（4 例）：关键词命中卡片标题与视频号昵称、JSON `card` 结构、
  md/txt/html 卡片块 + 位置 + 评论图。
- `TestSnsApi.test_timeline_keyword_matches_card_fields`：API 层关键词命中卡片字段（含负面用例）。

---

## 2. 评论区表情 / 评论内嵌图片 —— **✅ 已完成**

### 2.1 现状

- **数据已解析**：评论里的表情在 `emojis[]`（含 `url` 明文直链 / `encrypt_url` + `aes_key`），评论图在 `images[]`（结构与主图一致）。
- **已渲染**：前端评论区把 `emojis[].url` 渲染成 `<img class="cm-emoji">`（22px），
  `images[]` 渲染成缩略图（`cm-img`，可点灯箱）。
- **已进导出**：JSON 带 `emojis` / `images` 字段；markdown 用 `![表情](url)` / `![评论图](url)`；
  txt 用 `<表情> url` / `<评论图> url`；html 用 `<img class="e">` / `<img class="cm-img">`。

### 2.2 仍可做（非阻塞）

- 评论图与**卡片封面/视频号视频**目前只引用 CDN 原始 URL，**没有随导出下载到本地**
  （`download_media` 只处理主 mediaList）。若要离线可用，需要在 `download_media` 里
  把 `comments[].images[]` 与 `card.finder.media`/`card.cover` 也加入任务队列。

---

## 3. 表情 AES 真实样本验证 —— **P2（阻塞：无样本）**

`siwx/sns_cdn.py` 的 `decrypt_emoji_aes` 路径（`encrypt_url` + `aes_key` → AES 解密）**至今没有真实样本验证过**。
只验证过"明文 url"路径（`url` 直链）。

- **待做**：在真实库里找一条 `encrypt_url` 非空的表情，跑通 AES 解密并加测试固化。
- **提示**：表情 `url` 是明文直链且域名不适用 CDN 回退（见 `module-sns.md` 踩坑表）；`encrypt_url` 才需要 `aes_key`。

---

## 4. 按好友聚合「视图」增强 —— **P2**

### 4.1 现状（已完成的部分）

- `/friends` 端点（`siwx/api_sns.py`）已用**纯 SQL 聚合** `user_name` 列，111ms 返回 113 位发布者 + 数量 + 最近时间，并解析联系人昵称（`api_chat._contact_names`）。
- 前端已有好友下拉，选某人后按 `username` **SQL 下推过滤**时间线（之前修复过：原先用 `limit*3` 候选后 Python 过滤，占比低的好友每页只返回 10 条；现 `limit=50` 返回满 50 条）。

### 4.2 待做

- 独立"某人的朋友圈"视图：目前只是"过滤时间线"，可加一个**汇总卡片**（该好友共发 N 条 / 首条 ~ 末条时间 / 含图 M 张）。
- 导出时 `username` 过滤已支持（见 `api_sns.py` export 的 `opts`），确认前端导出请求带了 `username`（目前导出请求体发了 `account/format/media/keyword/username/concurrency`，✅ 已带）。

---

## 5. 位置（POI）进导出 —— **✅ 已完成**

- `location` 现在进 **json（`location` 字段）/ markdown（`📍 名称 地址`）/ txt（`  📍 ...`）/ html（`.loc`）**。
- 地图类坐标（lat/lng）仍只保留在 JSON 里，未做地图渲染。

---

## 6. 小众内容类型 —— **✅ 已登记并渲染（2026-09-30）**

`CONTENT_TYPES` 里的名字**不可靠**（见 `module-sns.md` §3.2），实测结论：

| type | 数量 | 实测含义 | 处理 |
|---|---|---|---|
| 5 | 17 | 外部链接/直播分享（title + contentUrl） | ✅ link 卡片 |
| 26 | 3 | **笔记**（noteinfo，正文在 `datainfo/datadesc`） | ✅ note 卡片 |
| 34 | 3 | **视频号直播**（finderLive） | ✅ live 卡片 |
| 42 / 47 | 3 / 4 | **音乐分享**（musicShareItem，酷狗/QQ音乐） | ✅ music 卡片 |
| 7 | 117 | 无任何卡片字段（只有 mediaList） | 按图片/视频渲染（物理上限） |
| 54 | 91 | 无卡片字段（正文在 `mediaList/media/description`） | 正文已进搜索 blob |

`CONTENT_TYPES` 里的旧名字（**仅供兼容，勿用于判断**）：

| type | 旧名（不准） | 数量 | 实测 |
|---|---|---|---|
| 5 | unknown5 | 17 | 外部链接 |
| 26 | unknown26 | 3 | 笔记 |
| 34 | ting | 3 | 视频号直播 |
| 42 | finder_live | 3 | 音乐 |
| 47 | note | 4 | 音乐 |

- 复现用：`python scripts/sns_card_probe.py <sns.db>`（字段全集与样本 dump）。

---

## 7. 已知限制（搬运自实施指南，接入时注意）

- **CDN token 过期**：朋友圈媒体是 CDN 下载，链接带时效，长时间不导出可能 403（需重跑微信使其重新缓存/刷新）。
- **本地覆盖率上限 ~19%**：只有微信本地已下载/缓存的资源能取到；未下载的图尝试 CDN，但 CDN 也有失效风险。
- **17 条 PARSE_FAIL**：特殊字符/版本差异导致 XML 解析失败，`parse_timeline` 返回 `None`，调用方已容错跳过。
- **表情加密路径未验证**（见 §3）。

---

## 8. 测试覆盖缺口汇总（2026-09-30 更新）

| 缺口 | 状态 | 说明 |
|---|---|---|
| 卡片解析测试 | ✅ 已补 | `TestSnsCardParsing`（7 例） |
| 卡片/位置/评论图进导出 | ✅ 已补 | `TestSnsCardExport`（4 例） |
| API 关键词命中卡片字段 | ✅ 已补 | `TestSnsApi.test_timeline_keyword_matches_card_fields` |
| 表情 AES 真实样本 | ⬜ P2 | 无样本，路径未验证（`decrypt_emoji_aes`） |
| `/friends` 昵称解析测试 | ⬜ P1 | 已实测但未固化 |
| 并发参数夹紧测试 | ⬜ P1 | `concurrency=99→16` 已实测未固化（`test_concurrency_clamped` 已覆盖 clamp，端点层未覆盖） |
| 评论图随导出下载到本地 | ⬜ P1 | 目前只引用 CDN URL（见 §2.2） |
| 前端 `renderCard` 自动化测试 | ⬜ P2 | 前端无测试基建（只有 `node --check`）；本次用临时 Node 脚本验证过 17 项断言 |

---

## 9. 建议接入顺序（2026-09-30 更新）

```
✅ P0  卡片渲染（已完成：解析 + 前端 + 四格式导出 + 测试 + 真实库实测）
✅ P1  评论表情/图渲染、位置进导出、搜索口径修复（已完成）
⬜ P1  评论图随导出下载到本地、/friends 昵称测试、端点层并发夹紧测试
⬜ P2  按好友视图增强（汇总卡片）、表情 AES 真实样本验证、前端渲染测试基建
⬜ P3  其余小众类型（若将来出现新的 type，先跑 sns_card_probe.py 看字段）
```

> 一条经验（已被验证）：**先改 `parse_timeline` 把字段抓全，API 和前端几乎免费拿到**；
> 真正要写代码的是前端分支渲染 + 导出各格式的卡片块。这是投入产出比最高的第一刀。
>
> 第二条经验（本次踩到）：**动手前先用探针脚本 dump 真实 XML**。初版文档凭外部资料
> 假设的 `<appmsg>` 路径在真实库里完全不存在，若照抄会写出一整套永远取不到值的代码。
