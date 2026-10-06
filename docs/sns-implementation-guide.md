# 朋友圈（SNS）功能实施指南

> **用途**：把「朋友圈」功能落地到 SIWX 的完整推进文档。
> 读这一篇就够了 —— 背景、结论、已有代码、待办、关键细节、风险全在这里。
> **创建**：2026-09-30 ｜ **状态**：技术验证已完成，待产品化

---

## 0. 一句话目标

**让 SIWX 能浏览 / 搜索 / 导出微信朋友圈**（文案 + 点赞评论 + 图片 + 视频），
复用现有 Web 控制台与 8 种导出格式。

---

## 1. 核心思路

> **不碰微信本地缓存**（因为动态↔文件名的映射只在客户端内存里），
> **直接用 XML 里的 URL 从 CDN 下载**，再用 ISAAC64 流解密。

本仓库为**纯 Python 独立实现**：ISAAC64 用官方测试向量自检（见 §5.3），
不依赖任何外部二进制或 WASM。

---

## 2. 核心结论（已实测验证）

### 2.1 为什么不能靠微信本地缓存

微信**不落盘**「动态 → 本地缓存文件名」的映射。实测证据：

| # | 证据 | 数据 |
|---|---|---|
| 1 | 缓存文件名 ≠ 任何可推导哈希 | md5(密文/明文/去尾/尾部记录) 全零命中 |
| 2 | **与 `url@md5` 统计独立** | 前 4 位重合 **113** vs 随机期望 **114.61** |
| 3 | `md5(解密明文) ≠ url@md5` | 本地缓存是**微信重压缩版**，与 CDN 原图不同源 |
| 4 | 尺寸不匹配 | **59%** 缓存图片尺寸在 XML 中不存在（缩放版） |
| 5 | 无本地映射 | 32 个解密库 / **596 张表**全字段零命中 |

**结论**：本地缓存只覆盖 **19%**（2037 / 10293），且无法精确归属。
**所以走 CDN 下载路线。**

### 2.2 已打通的完整链路

```
sns.db (SnsTimeLine.content XML)
   ↓ 解析 url / token / key 属性
构造 CDN URL（必须带 token 参数，否则 400）
   ↓ HTTPS + UA: MicroMessenger Client
下载密文（响应头 x-enc: 1）
   ↓ key（十进制数）→ ISAAC64 密钥流 → XOR
明文 JPEG / MP4
   ↓ 去掉微信 24 字节尾部
最终图片
```

**实测结果**：5/5 下载成功、5/5 解密成功、**微信尾部自校验 5/5 通过**。

---

## 3. 已完成代码（可直接用）

| 文件 | 行数 | 职责 | 验证状态 |
|---|---|---|---|
| `siwx/sns_isaac64.py` | 139 | **纯 Python ISAAC64**（零依赖） | ✅ 官方向量 8/8 + 真实样本 5/5 |
| `siwx/sns_cdn.py` | 339 | CDN URL 构造 / 下载 / 解密 / 缓存键 | ✅ 离线解密 5/5 |
| `siwx/sns.py` | 610 | sns.db 解析 / snsId 时间还原 / 本地关联 | ✅ 解析 5667/5684 |

### 3.1 `sns_isaac64.py` 的对外接口

```python
from siwx import sns_isaac64 as isaac

isaac.keystream(seed, size)   # seed → size 字节密钥流
isaac.Isaac64(seed).next_u64()
isaac.self_test()             # 官方测试向量自检（True/False）
isaac.ISAAC64_TEST_VECTOR     # 官方零种子向量，写测试时可复用
```

### 3.2 `sns_cdn.py` 的对外接口

```python
from siwx import sns_cdn as cdn

cdn.build_media_url(url, token, is_video=None)   # 构造 CDN URL（关键）
cdn.normalize_cache_url(url)                     # 去掉 token/idx → 稳定缓存键
cdn.cache_key(url)                               # md5(normalize_cache_url(url))
cdn.fetch_media(url, key, token)                 # 下载+解密 → {ok,data,ext,mime,...}
cdn.decrypt_isaac(data, key)                     # 纯 XOR 解密
cdn.detect_mime(data)                            # 魔数识别 (ext, mime)
cdn.strip_wechat_tail(data)                      # 去掉微信 24 字节尾部
cdn.decrypt_emoji_aes(enc, aes_key)              # 评论表情 AES-GCM
```

### 3.3 `sns.py` 的对外接口

```python
from siwx import sns

sns.sns_id_to_ms(tid)          # snsId → 毫秒（含无符号还原）
sns.sns_id_to_seconds(tid)
sns.parse_timeline(content)    # XML → dict（含 medias / likes / comments）
sns.iter_timeline(db_path)     # 按 tid 迭代（索引扫描，不解析 XML 排序）
sns.timeline_stats(db_path)    # 概览（纯 tid，361ms）
sns.iter_cache_images(acc_root)          # 本地缓存索引（备用）
sns.assign_images_globally(feeds, cache) # 本地关联（备用方案）
sns.match_feed_comments(feed, acc_root)  # 评论表情精确关联
sns.export_image_pool(cache, dest_dir)   # 图片池降级导出
```

---

## 4. 待办清单

> **进度快照（2026-09-30）**：P0 / P1 / P2 主体已完成并测试通过，详见 §4.0。
> 按依赖顺序排列，未完成项继续往下推进。

### 4.0 已完成（本次实现）

| 项 | 产出 | 验证 |
|---|---|---|
| 后端 API | `siwx/api_sns.py`（9 个端点） | 路由注册确认 |
| 前端页面 | `siwx/ui/pages/sns.{html,js,css}` | `node --check` 通过 |
| 菜单接入 | `app.js` + `index.html` | — |
| 媒体磁盘缓存 | `sns_cdn.cached_path/read_cached/_atomic_write` | 往返测试通过 |
| CDN 域名回退 | `sns_cdn._host_candidates` | 候选测试通过 |
| 导出（4 格式） | `siwx/sns_export.py` | 真实数据 4/4 导出成功 |
| 导出过滤 | 关键词 / 发布者 / 时间范围 | 3 项测试通过 |
| **评论表情** | `sns_cdn.fetch_emoji` + `/api/sns/emoji` | **实测 via=plain，无需 AES** |
| **评论内嵌图片** | `_parse_media_el` 统一解析 + 复用 `fetch_media` | **实测 964×1208 JPEG 解密成功** |
| **评论/点赞拆分** | `sns.parse_timeline` | 表情/图片评论正确归类 |
| **详情弹层 + 灯箱** | `sns.html/js/css` | 点击进详情、Esc 关闭、图片放大 |
| **位置过滤** | `sns.parse_timeline` | 过滤 0,0 占位坐标 |
| **异步导出任务** | `server._run_job` 新增 `sns_export` 模式 | 走任务槽，409 互斥 |
| **实况照片 LivePhoto** | `_parse_media_el` 支持 `<enc key>` + `videoSize` | **实测 2.2MB MP4 解密成功** |
| **API 层测试** | `tests/test_sns.py::TestSnsApi` | 8 例（含穿越/边界/异步） |
| **模块文档** | `docs/module-sns.md` | 已纳入 `docs/README.md` 文档地图 |
| 单元测试 | `tests/test_sns.py`（31 例） | 31/31 通过 |
| WAL 评估 | **实测确认无需实施**（见 §5.1） | 零有效提交帧 |

**全量回归：233 通过 / 1 跳过。**

**文档产出**（3 篇，共 65 KB）：

| 文档 | 用途 |
|---|---|
| `docs/module-sns.md` | 模块详解（对齐其他 `module-*.md`） |
| `docs/sns-implementation-guide.md` | 实施指南（推进用） |
| `docs/sns-research-2026-09-29.md` | 研究记录（实验原始数据） |

**导出改为异步**：`POST /api/sns/export` 现在**立即返回 `{started: true}`**，
前端轮询 `/api/job` 拿进度（与聊天导出一致）。原因：导出全量 + 媒体可能持续数分钟，
同步返回会一直占住 HTTP 请求，也无法与其它任务互斥。

**顺带优化**：`sns_export` 分支放在 `find_wechat_data_dirs()` **之前**并提前 `return` ——
朋友圈导出只依赖已解密产物，不需要全盘扫描微信数据目录。

### 4.1 剩余待办

- [ ] **按好友聚合视图**（"某人的朋友圈"）
- [ ] **视频号 / 公众号 / 音乐卡片渲染**
- [ ] **媒体并发数可配**（`download_media` 已支持 `concurrency`，未接到 API）
- [ ] **`decrypt_emoji_aes` 真实样本验证**（备用路径，当前无样本）

> `docs/module-sns.md` 与 `docs/README.md` 文档地图**已完成**。

### 4.2 原始待办（保留备查）

> 按依赖顺序排列。**建议从 P0 开始，每完成一项跑一次测试。**

### P0 — 打通 API（让数据能用）

- [ ] **`siwx/api_sns.py`** —— Flask 蓝图（照抄 `api_chat.py` 的结构）
  - [ ] `GET /api/sns/accounts` —— 有 sns.db 的账号列表
  - [ ] `GET /api/sns/timeline?account=&before_tid=&limit=` —— 动态分页
        （**走 tid 游标，不要 offset**，因为 tid 是主键 DESC 索引）
  - [ ] `GET /api/sns/detail?account=&tid=` —— 单条详情（含全部评论）
  - [ ] `GET /api/sns/media?account=&tid=&index=&hq=1` —— 按需下载+解密单张图
  - [ ] `GET /api/sns/emoji?account=&tid=&md5=` —— 评论表情（AES-GCM）
  - [ ] `GET /api/sns/stats?account=` —— 统计（条数/好友数/时间跨度）
  - [ ] `GET /api/sns/search?account=&kw=` —— 全文搜索文案
- [ ] **注册蓝图**到 `siwx/server.py`（参照其他 `api_*.py` 的注册方式）
- [ ] **媒体落盘缓存**：`output/<wxid>/sns_media/<md5>.<ext>`
      - 缓存键用 `sns_cdn.cache_key(url)`（**不要用完整 URL**，token 会变）
      - 参照 `media.py` 的 LRU + `.part` 原子写模式

### P1 — 前端页面

- [ ] **`siwx/ui/pages/sns.html`** —— 页面骨架（照抄 `chat.html` 的三段式）
- [ ] **`siwx/ui/pages/sns.js`** —— 逻辑
  - [ ] 时间线列表（无限滚动，`before_tid` 游标）
  - [ ] 图片九宫格（点击开灯箱，复用 `chat.js` 的灯箱实现）
  - [ ] 点赞 / 评论展示（复用 `chat.js` 的气泡样式）
  - [ ] 视频内联播放
  - [ ] 按好友筛选 / 按关键词搜索
- [ ] **`siwx/ui/pages/sns.css`** —— 样式（沿用黑白描边设计语言）
- [ ] **注册到 `app.js`** 的 `PAGES_BUILTIN`（位置建议：chat 之后、stats 之前）
- [ ] 侧边栏图标 + 菜单项（`index.html`）

### P2 — 导出

- [ ] **接入现有导出引擎**（`exporter.py` 的 8 种格式）
  - 新增数据源：朋友圈动态（而非聊天消息）
  - JSON / HTML / Markdown 优先
- [ ] **媒体导出**：`media/<postId>_<index>.<ext>`
- [ ] **导出选项**：图片 / 视频 / 实况照片 / 表情 分开勾选
- [ ] **进度上报**：复用 `_run_job` + 环形日志（日志格式 `[sns] N%`）

### P3 — 增强

- [ ] **评论表情**：`decrypt_emoji_aes` 需要真实样本验证
- [ ] **实况照片（LivePhoto）**：XML 里 `<LivePhoto><liveMedia>`，含独立 url/key
- [ ] **笔记（noteinfo）**：带 `cdndatakey` / `fullmd5`
- [ ] **视频号（finderFeed）**：`ContentObject/type=28`
- [ ] **公众号文章 / 音乐分享**的卡片渲染
- [ ] **年度报告**（可选增强，非阻塞）

### P4 — 测试与文档

- [ ] **`tests/test_sns.py`**
  - [ ] `sns_id_to_ms` 边界（负数 tid、2039 溢出）
  - [ ] `parse_timeline` 容错（17/5684 解析失败不能崩）
  - [ ] `build_media_url` 各分支（图片/视频/无 token）
  - [ ] `normalize_cache_url` 去 token 后稳定
  - [ ] `isaac.self_test()` 官方向量
  - [ ] **离线解密测试**：把 CDN 密文样本放进 `tests/data/`，断言解出 JPEG + 尾部自校验
- [ ] **`docs/module-sns.md`** —— 对齐其他 `module-*.md` 的格式
- [ ] **更新 `docs/README.md`** 的文档地图

---

## 5. 关键实现细节（照抄即可）

### 5.1 数据源：`sns.db` 的位置与结构

**位置**：`<账号根>/db_storage/sns/sns.db`

```
D:\xwechat_files\<wxid>\db_storage\sns\sns.db      ← 真实数据（本机 39.5 MB）
C:\Users\<user>\xwechat_files\<wxid>\db_storage\sns\sns.db  ← 可能是空壳（同名账号冲突）
```

**⚠️ 已解密产物在** `output/<wxid>/sns/sns.db`（`discover.py` 已覆盖，无需改动）。

**⚠️ 关于 `-wal`：已实测确认「无需处理」。**

原始 `sns.db` 旁边有 4 MB 的 `sns.db-wal`，看起来像有未落盘数据，但实测（用
`scripts/wal_probe.py` 的解析器）：

```
D:/xwechat_files/<wxid>/db_storage/sns/sns.db-wal
  文件 4.00 MB   magic_ok=True 头校验=True
  总帧=0  已提交帧=0  commitDbSize=None  提前停止=True
  → 零有效提交帧（陈旧残留，无数据损失）
```

**机理**：SQLite 的 checkpoint **不清空 WAL 文件**，只把 `salt1` 加一并重写文件头，
旧帧原地留存。所以「WAL 文件大」≠「有未落盘数据」。

这与 `docs/wal-support-plan.md` §0 的实测结论一致：**微信 checkpoint 是秒级的**
（消息发出到主库可见仅 6~10 秒），加 WAL 的收益上限只有约 10 秒，而真正主导延迟的是
**同步间隔**。

**结论：不需要为朋友圈实现 WAL 支持。** 若用户反馈"最新动态看不到"，
正确做法是**缩短自动同步间隔**（设置页已有，零改动），而不是改 WAL 解析。

**13 张表，只用 2 张**：

| 表 | 行数（本机） | 用途 |
|---|---|---|
| **`SnsTimeLine`** | 5,684 | **主表**：`tid` / `user_name` / `content`(XML) / `pack_info_buf` |
| `SnsMessage_tmp3` | 1,684 | 互动消息流（type 1=赞 1278 / 2=评论 405 / 4=其它） |

其余（`SnsTopItem_1` 3761 / `SnsMainTimeLineBreakFlag` 5002 / `SnsUserTimeLineBreakFlagV2` 1374）是增量拉取的元信息，**做浏览不需要**。

### 5.2 ⭐ `snsId` 位布局（本项目的独有发现）

```
snsId（64 位无符号） = (createTime_ms << 23) | random(23 bits)
createTime_ms = snsId >> 23
```

- **5,684/5,684 验证通过**，最大偏差 938 ms（毫秒截断）
- 41 位毫秒 → **设计寿命到 2039-09-07**（超期会溢出，加断言或降级解析 XML）
- **⚠️ 坑：`tid` 是有符号 int64**，读出来常是负数，必须 `tid & 0xFFFFFFFFFFFFFFFF` 还原

**价值**：分页 / 排序 / 过滤**全走 SQL 索引扫描**，不必解析 XML。
本机跨度：2013-11-01 → 2026-09-12。

### 5.3 ⭐ ISAAC64 的三个坑（已踩完，务必照做）

| # | 坑 | 正确做法 |
|---|---|---|
| 1 | **黄金比例常量** | **`0x9e3779b97f4a7c13`**（结尾 **c13**），不是常见的 `...c15`。用错则零种子输出与官方向量完全不符 |
| 2 | **`isaac_refill` 的偏移** | 两段循环分别是 `+128` / `-128`（等价 `mm[(i+128) % 256]`） |
| 3 | **输出顺序 + 字节序** | 标准输出**逆序**（`randrsl[255]` 先）+ **大端**拼接。等价于「WASM 原始输出整体 `reverse()`」 |

**另**：每 256 个字必须重新 refill，否则 >2048 字节的文件密钥流会重复（这会导致"头对但尾部校验失败"）。

> 注：常见的错误实现会误用 `...c15`，结果必须依赖外部 WASM 兜底；
> **本仓库的纯 Python 版已修正，不需要 WASM**。

**自检**：`isaac.self_test()` 必须返回 `True`。

### 5.4 URL 构造（`sns_cdn.build_media_url`）

```
1. http:// → https://
2. 图片：把 /150|/200|/480 替换成 /0        ← 取原图
3. 追加 ?token=<token属性>&idx=1            ← ⚠️ 缺这个会 400
   视频：<base>?token=<token>&idx=1&<原query>
```

**⚠️ 最容易踩的坑**：`<url>` 元素有**两个 token** ——
**路径里的**（如 `/mmsns/<77字符>/0`）和 **`token` 属性**（88 字符）。
**必须用属性值做查询参数**，两者不同。

**请求头**（缺 UA 会被拒）：
```
User-Agent: MicroMessenger Client
Accept: */*
Accept-Encoding: gzip, deflate
Accept-Language: zh-CN,zh;q=0.9
```

### 5.5 ⭐ 微信图片的 24 字节尾部

解密后 JPEG **不以 `FFD9` 结尾**，后面还有 24 字节：

```
75f0d33c | 00000000 | <16 字节 = 明文(不含本尾部)的 MD5>
└ 固定魔数 ┘  └保留┘
```

- 256/256 样本中 `尾部[8:24] == md5(body[:i+2])` **全部成立**（`i = rfind(FFD9)`）
- 分布：`FFD9` 后 24 字节 256 个 / 0 字节 143 个
- **用法**：① 解析时按 `rfind(b"\xff\xd9")` 截断；② **当解密正确性校验用**（比只看 FFD8 严格得多）

`cdn.strip_wechat_tail()` 已实现。

### 5.6 缓存设计

```python
cache_key = md5(normalize_cache_url(url))
# normalize_cache_url = 去掉 token 和 idx 参数，保留 host+path+其余 query
```

**为什么必须去掉 token**：token 每次请求都变，但指向同一份资源。
若用完整 URL 的 md5，**token 一变缓存就全失效**；因此改为规范化 URL 命名，
并对旧缓存做一次性迁移。

**缓存目录建议**：`output/<wxid>/sns_media/<cache_key>.<ext>`
（`ext`：视频 `mp4`，图片按 `detect_mime` 结果）

### 5.7 XML 结构速查

```
SnsDataItem
├── TimelineObject                     ← 服务端正文
│   ├── id / username / createTime / contentDesc
│   ├── ContentObject
│   │   ├── type                       ← 见下方枚举
│   │   ├── mediaList/media            ← 图片/视频
│   │   ├── finderFeed                 ← 视频号（type=28）
│   │   ├── mmreadershare              ← 公众号文章（type=54）
│   │   ├── musicShareItem             ← 音乐（type=7）
│   │   ├── noteinfo                   ← 笔记（type=47，带 cdndatakey/fullmd5）
│   │   └── finderLive                 ← 视频号直播（type=42）
│   ├── private                        ← 0=公开 1=私密
│   ├── location                       ← 位置（**在属性里**：latitude/longitude/poiName）
│   └── appInfo / weappInfo / streamvideo
└── LocalExtraInfo                     ← 本地附加（互动数据）
    ├── like_user_list/user_comment    ← 点赞
    ├── comment_user_list/user_comment ← 评论
    └── with_user_list                 ← 可见用户
```

**`ContentObject/type` 枚举（本机实测分布）**：

| type | 条数 | 含义 |
|---|---|---|
| 1 | 3,575 | 纯文本 |
| 2 | 819 | 图片 |
| 15 | 499 | 视频（与 `media/enc=1` 数量吻合） |
| 28 | 353 | 视频号 |
| 3 | 183 | 链接 |
| 7 | 117 | 音乐 |
| 54 | 91 | 公众号文章 |
| 47 / 42 / 34 | 4 / 3 / 3 | 笔记 / 直播 / 听一听 |

**`media` 元素**（⚠️ **属性里有关键信息，别只用 `findtext`**）：

```xml
<media>
  <id>...</id><type>2</type><sub_type>0</sub_type>
  <thumb>URL</thumb><url type="1" md5="..." key="14014..." token="WSEN..." enc_idx="1">URL</url>
  <size width="1564" height="960" totalSize="425866"/>
  <videoDuration>...</videoDuration><enc>0|1</enc>
  <LivePhoto><liveMedia .../></LivePhoto>
</media>
```

- `url@key` —— **解密密钥**（十进制数，ISAAC64 种子）
- `url@token` —— **CDN 必需参数**
- `url@md5` —— 原图 md5（**≠ 本地缓存文件名**）
- `url@enc_idx` —— 1 = 需要解密
- `size@width/height/totalSize` —— 尺寸（`totalSize` 与密文/明文都不匹配，含义未明）

**评论 `user_comment`**：`username` / `nickname` / `content` / `create_time` /
`comment_id` / `comment_64id` / `ref_username` / `ref_comment_id`（回复关系）/
`b_deleted` / `imagelist/imageinfo/md5` / `emojilist/emojiinfo/{md5,aes_key}`

**⭐ 评论表情：直接用 `sns_emoji_data/url`，实测无需解密**

`sns_emoji_data` 是**结构化节点**（8 个字段，不是文本）：

```xml
<emojilist><emojiinfo>
  <md5>...</md5><width>86</width><height>86</height><size>1782</size>
  <sns_emoji_data>
    <url>http://vweixinf.tc.qq.com/110/20401/stodownload?m=...</url>       ← 明文直链
    <thumb_url>...</thumb_url>
    <encrypt_url>http://wxapp.tc.qq.com/262/20304/stodownload?m=...</encrypt_url>
    <aes_key>d024f5f4ba4ea0b3c16c3c1a0b9ee34d</aes_key>
    <extern_md5>...</extern_md5>
    <product_id>com.tencent.xin.emoticon...</product_id>
    <extern_url>...</extern_url>
  </sns_emoji_data>
</emojiinfo></emojilist>
```

**实测结论（重要，修正了先前假设）**：

| 字段 | 实测结果 |
|---|---|
| **`url`** | ✅ **HTTP 200 直接返回明文 GIF / PNG / JPEG** —— 无需任何解密 |
| `encrypt_url` | 密文（需 `aes_key`），仅在明文 url 不可用时作备用 |

**所以不要走 AES-GCM 路径**（`decrypt_emoji_aes` 保留为备用，但**尚无真实样本验证**）。
实测 `fetch_emoji` → `via=plain`、`ext=gif`、二次调用 `via=cache`，全部正常。

**⚠️ 注意**：表情域名是 `vweixinf.tc.qq.com` / `mmbiz.qpic.cn` / `wxapp.tc.qq.com`，
**不是** `mmsns.qpic.cn` —— 所以 `_host_candidates` 的域名回退**不适用于表情**。

**实况照片（LivePhoto）：`<media>` 里嵌一段短视频**

```xml
<media>
  <type>2</type><size width="1920" height="1920" totalSize="398618"/>
  <url token="..." key="..." enc_idx="1">http://shmmsns.qpic.cn/mmsns/<TOKEN>/0</url>
  <LivePhoto><liveMedia>
    <id>0</id><type>6</type><subType>0</subType>
    <videoSize width="0" height="0"/>
    <url type="1" md5="...">http://shzjwxsns.video.qq.com/102/20202/snsvideodownload?encfilekey=...</url>
    <thumb type="1">http://vweixinthumb.tc.qq.com/150/20250/snsvideodownload?encfilekey=...</thumb>
    <size width="288" height="288" totalSize="9884"/>
    <videoDuration>2.37800002</videoDuration>
    <liveStillImageTimeMs>734</liveStillImageTimeMs>
    <enc key="1884729990">1</enc>
  </liveMedia></LivePhoto>
</media>
```

**⚠️ 最大的坑：实况视频的解密 key 在 `<enc key="...">`，不是 `url@key`！**
（即用正则 `<enc\s+key="(\d+)"` 从 XML 里提取的那个值）

- 尺寸取 `<size>`（316×420 / 288×288），**不是** `<videoSize>`（实测恒为 0×0）
- `liveStillImageTimeMs` = 定格帧时间点（毫秒）
- 下载同样走 `fetch_media`（key 传 `enc_key` 即可）

**实测**：2.2 MB MP4，`ftyp` 头正确，`ok=True ext=mp4 encrypted=True`。本机共 **201 个**。

**导出命名**：主图 `<tid>_<index>.jpg`，实况视频 `<tid>_<index>_live.mp4`（实况加 `_live` 后缀）。

**评论内嵌图片：与主图结构完全一致，可复用同一条下载链路**

```xml
<imagelist><imageinfo>
  <url token="..." key="..." enc_idx="1" md5="...">http://shmmsns.qpic.cn/mmcomment/<TOKEN>/0</url>
  <thumb_url token="..." key="...">http://shmmsns.qpic.cn/mmcomment/<TOKEN>/60</thumb_url>
  <width>964</width><height>1208</height><file_size>96147</file_size>
  <media_id>...</media_id><md5>...</md5>
</imageinfo></imagelist>
```

**注意**：
- 路径是 **`/mmcomment/`**（主图是 `/mmsns/`），但 `build_media_url` 无需改动即可处理
- 缩略图档位是 **`/60`**，不在 `build_media_url` 的 `(150|200|480)` 替换列表里 —— 这是**有意的**（缩略图本就该小）
- 同样带 `token` / `key` / `enc_idx` → **走 `fetch_media` 即可**（ISAAC64 解密）

**实测**：964×1208 JPEG，96,147 B，`ok=True ext=jpg encrypted=True`，二次调用 `cached=True`。

**本机规模**：评论图片共 **221 张**。

**解析实现**：`sns._parse_media_el()` 同时服务 `mediaList/media` 与 `imageinfo`
（两者字段相同，区别只在 `<size>` 属性 vs 扁平字段、`<thumb>` vs `<thumb_url>`）。

**本机规模**：评论中共 **547 个表情**。

---

## 6. 风险与红线

### 6.1 红线（务必遵守）

1. **不要试图关联微信本地缓存** —— 已用统计学证明不可行（§2.1），别再花时间。
2. **不要在 `sns.py` 里解析 XML 做排序/分页** —— 走 `tid` 索引，快 100 倍。
3. **不要把 token 写进缓存键** —— 缓存会永久失效。
4. **不要硬编码 `shmmsns.qpic.cn`** —— 域名有 `mmsns` / `shmmsns` / `szmmsns` 多种，
   且实测**部分域名会 404**。用 XML 里的原样 URL，失败时再换域名重试。
5. **`SIWX_NO_PLUGINS=1` 下行为必须与不带插件时一致**（项目通用红线）。

### 6.2 风险

| 风险 | 说明 | 缓解 |
|---|---|---|
| **CDN token 过期** | 实测 6 个样本中 1 个 404。旧动态大概率失效 | 降级到本地缓存（19%）；UI 提示"该图片需在微信中打开过一次" |
| **WAL 未 checkpoint** | 最新朋友圈读不到 | 优先处理 WAL（见 `docs/wal-support-plan.md`） |
| **CDN 反爬** | 大量并发下载可能被限流 | 并发 ≤5，加退避重试 |
| **域名失效** | `shmmsns.qpic.cn` 已见 404 | 多域名回退：原样 → `mmsns` → `szmmsns` |
| **表情 AES 未验证** | `decrypt_emoji_aes` **没有真实样本验证过** | 实现时先找样本验证 |
| **`private=1` 的私密动态** | 本机 9 条 | 默认隐藏或标记 |
| **未下载过的原图** | 微信只自动下载缩略图，原图要点开才拉 | 已下载的才能拿到；UI 如实说明 |

### 6.3 数据规模参考（本机）

```
动态总数        5,684 条（2013-11-01 → 2026-09-12）
XML 可解析      5,667（99.7%，17 条解析失败 → 必须 try/except）
图片 media      10,293 个
视频 media      510 个（其中 enc=1 的 499 个）
本地缓存图片    2,037 个（覆盖率 19%）
sns.db 大小     39.5 MB
```

**性能参考**：概览统计（纯 tid）**361 ms**；XML 全量解析 **2.4 s**；
缓存索引（解密+尺寸）**3.7 s**；全局关联 **< 4 s**。

---

## 7. 验证方法（改完代码跑这个）

### 7.1 单元级

```bash
python -c "import sys;sys.path.insert(0,'.');from siwx import sns_isaac64 as I;print(I.self_test())"
# 期望输出: True
```

### 7.2 端到端（离线，用已保存的密文样本）

样本在 `G:/project/_ref/snsdl/*.bin`（5 个真实 CDN 密文，文件名含 key）。

```python
import sys, glob, re, hashlib
sys.path.insert(0, r"G:/project/stories-in-wx/stories-in-wx-py")
from siwx import sns_cdn as C

for f in sorted(glob.glob(r"G:/project/_ref/snsdl/*.bin")):
    key = re.search(r"_(\d+)\.bin$", f).group(1)
    body = C.decrypt_isaac(open(f, "rb").read(), key)
    i = body.rfind(b"\xff\xd9")
    t = body[i+2:]
    ok = (C.detect_mime(body)[0] == "jpg"
          and len(t) == 24 and t[:4] == b"\x75\xf0\xd3\x3c"
          and t[8:24].hex() == hashlib.md5(body[:i+2]).hexdigest())
    print(f.name, "OK" if ok else "FAIL")
```

**期望**：5/5 输出 `OK`。

### 7.3 在线（需要有效 token）

```python
from siwx import sns, sns_cdn as C
DB = r"output/<wxid>/sns/sns.db"
for feed in sns.iter_timeline(DB, limit=20):
    for m in feed["medias"]:
        if m["type"] != 2 or not m["url"]:
            continue
        r = C.fetch_media(m["url"], m["key"], m["token"])
        print(feed["tid"], m["md5"], r["ok"], r["ext"], r["error"])
        break
    break
```

---

## 8. 相关文档

| 文档 | 内容 |
|---|---|
| `docs/sns-research-2026-09-29.md` | 完整研究记录（30 轮实验的原始数据、排除的假设、未解之谜） |
| `docs/media-decryption-principles.md` | 聊天媒体的 V2 解密（与朋友圈的 ISAAC64 是**两套不同体系**） |
| `docs/wal-support-plan.md` | WAL 支持设计（朋友圈的 WAL 影响更明显） |
| `docs/module-media.md` | 媒体模块（本地缓存解密，可复用其 LRU + 原子写模式） |
| `docs/module-server.md` | Web 服务层（任务槽 / 环形日志，新增 API 参照它） |
| `docs/plugin-development.md` | 插件系统（如果想让朋友圈支持插件扩展） |

**外部参考**：
- ISAAC64 官方：http://www.burtleburtle.net/bob/rand/isaac.html
- ISAAC64 参考实现（本方案所用常量来源）：https://sources.debian.org/src/coreutils/8.26-3/lib/rand-isaac.c/
- 微信视频号 WASM 解密分析：https://github.com/29583855/WeChat-Channels-Video-File-Decryption

---

## 9. 未解之谜（不必阻塞，但记录在案）

| 项 | 状态 |
|---|---|
| 缓存文件名的生成算法 | 未解（已证明与 XML 无关；疑为服务器端 ID） |
| 17 条 XML 解析失败的原因 | 未查（疑为特殊字符或旧版本格式） |
| `pack_info_buf` 的 protobuf schema | 只确认是状态标记（长度 1~24 字节） |
| `totalSize` 的真实含义 | 与密文/明文/去尾明文均不匹配 |
| `SnsTopItem_1` 的用途 | `summary` 全空，疑为「谁有新动态」提醒 |
| `business/sns/publish` 结构 | 本机为空，未采样到 |
| CDN token 的有效期 | 未测（决定旧动态能否补全） |

---

*实施指南 · 2026-09-30 · 所有技术结论均在本机实测验证*

