# module-sns — 朋友圈（SNS）解析、解密与导出

> **文件**: `siwx/sns.py`(685) + `siwx/sns_cdn.py`(477) + `siwx/sns_isaac64.py`(139) + `siwx/sns_export.py`(378)
> **角色**: 朋友圈数据解析、CDN 媒体获取与 ISAAC64 解密、多格式导出
> **API**: `siwx/api_sns.py`(274) ｜ **前端**: `siwx/ui/pages/sns.{html,js,css}`
> **测试**: `tests/test_sns.py`(446，31 例)

---

## 职责

1. **sns.db 解析** —— `SnsTimeLine.content` 的 XML → 结构化动态（含点赞/评论/表情/评论图）
2. **snsId 时间还原** —— 直接从 `tid` 位运算还原发布时间，分页排序走索引
3. **CDN 媒体获取** —— 用 XML 里的 URL 下载图片/视频，**不依赖微信本地缓存**
4. **ISAAC64 流解密** —— 纯 Python 实现，零依赖
5. **多格式导出** —— JSON / Markdown / TXT / HTML，可带媒体

---

## 与「聊天图片」的根本区别

| 维度 | 聊天图片（`media.py`） | 朋友圈图片（本模块） |
|---|---|---|
| 加密体系 | **V0/V1/V2**（AES-ECB + XOR，账号级密钥） | **ISAAC64 流密码**（每图独立 key） |
| 密钥位置 | MMKV 离线派生 | XML 的 `url@key` / `<enc key>` |
| 数据来源 | 微信**本地缓存**（hardlink 索引可定位） | **CDN 下载**（本地缓存无法关联） |
| 缓存命名 | 内容 md5 | 服务器标识（**无法从 XML 推导**） |

> ⚠️ **两套体系完全独立，不要混用。** `media.py` 的 `decrypt_v2_body` 解不开朋友圈 CDN 媒体。

---

## 为什么不用微信本地缓存（重要背景）

微信**不落盘**「动态 → 本地缓存文件名」的映射，该映射只在客户端内存里。实测证据：

| # | 证据 | 数据 |
|---|---|---|
| 1 | 缓存文件名 ≠ 任何可推导哈希 | md5(密文/明文/去尾/尾部记录) 全零命中 |
| 2 | **与 `url@md5` 统计独立** | 前 4 位重合 **113** vs 随机期望 **114.61** |
| 3 | `md5(解密明文) ≠ url@md5` | 本地缓存是**重压缩版**，与 CDN 原图不同源 |
| 4 | 尺寸不匹配 | **59%** 缓存图片尺寸在 XML 中不存在 |
| 5 | 无本地映射 | 32 个解密库 / **596 张表**全字段零命中 |

**结论**：本地缓存只覆盖 **19%** 且无法精确归属 → **改走 CDN 下载**。

> 若仍需本地关联，`assign_images_globally()` 提供「尺寸 + 时间窗口 + 全局唯一」的
> 尽力归属（83% 时间差 ≤1 天），但**默认不用**。

---

## 模块分工

```
sns_isaac64.py    ISAAC64 流密码（纯算法，无依赖）
      ↑
sns_cdn.py        CDN URL 构造 / 下载 / 解密 / 缓存 / 表情
      ↑
sns.py            sns.db 解析 / snsId / 本地关联（备用）
      ↓
sns_export.py     多格式导出（独立于 exporter.py）
      ↓
api_sns.py        Flask 蓝图
      ↓
ui/pages/sns.*    前端页面
```

---

## 一、`sns_isaac64.py` — ISAAC64 流密码

```python
keystream(seed, size) -> bytes       # seed → 密钥流
Isaac64(seed).next_u64() -> int
Isaac64(seed).keystream(size) -> bytes
self_test() -> bool                  # 官方测试向量自检
ISAAC64_TEST_VECTOR                  # 官方零种子向量
```

### 三个必须遵守的细节（踩坑记录）

| # | 坑 | 正确做法 |
|---|---|---|
| 1 | **黄金比例常量** | **`0x9e3779b97f4a7c13`**（结尾 **c13**），不是常见的 `...c15`。用错则零种子输出与官方向量完全不符 |
| 2 | **`isaac_refill` 偏移** | 两段循环分别是 `+128` / `-128`（等价 `mm[(i+128) % 256]`） |
| 3 | **输出顺序 + 字节序** | 标准输出**逆序**（`randrsl[255]` 先）+ **大端**拼接 |

**另**：每 256 个字必须重新 refill，否则 >2048 字节的文件密钥流会重复
（表现为「头对但尾部自校验失败」）。

> 注：常见的错误实现会误用 `...c15`，结果必须依赖外部 WASM 兜底；
> **本实现已修正，纯 Python 即可，不需要 WASM**。

**验证**：官方零种子向量 8/8；真实 CDN 样本 5/5 解密成功且微信尾部自校验通过。

---

## 二、`sns_cdn.py` — CDN 获取与解密

### 2.1 URL 构造（最容易踩的坑）

```python
build_media_url(url, token, is_video=None)
```

```
1. http:// → https://
2. 图片：把 /150|/200|/480 替换成 /0        ← 取原图
3. 追加 ?token=<token属性>&idx=1            ← ⚠️ 缺这个必 400
```

**⚠️ `<url>` 元素有两个 token** —— 路径里的（77 字符）和 **`token` 属性**（88 字符）。
**必须用属性值做查询参数**，两者不同。

**⚠️ `key="0"` 是占位值，不是密钥**（2026-09-30 修复）。视频 media 实测写成
`<url key="0">` + `<enc key="929615230">` —— 真密钥在 `<enc key>`。
旧实现用 `or` 串联取值，`"0"` 是真值字符串，于是密钥被当成 0，
下载回来的密文 XOR 后仍是乱码 → **视频 100% 拉不下来**。
现在 `sns._meta_attr()` 把 `""` / `"0"` / `none` 一律视为缺失，
再回退到 `<enc key>`（图片的 `url@key` 照旧优先）。

**请求头**（缺 UA 会被拒）：`User-Agent: MicroMessenger Client`

### 2.2 缓存键

```python
cache_key(url) = md5(normalize_cache_url(url))
# normalize_cache_url = 去掉 token 和 idx 参数，保留 host + path + 其余 query
```

**为什么必须去掉 token**：token 每次请求都变，但指向同一份资源。
若用完整 URL 的 md5，**token 一变缓存就全失效**，因此改为规范化命名。

### 2.3 主入口

```python
fetch_media(url, key=None, token=None, timeout=15.0,
            cache_dir=None, hosts=3) -> dict
# → {ok, data, ext, mime, error, status, encrypted, cached,
#    reason, hosts_tried, got_bytes}
```

流程：白名单 → 查缓存 → 域名回退（**仅 `*.qpic.cn`**）→ 读 `x-enc`
→ ISAAC64 XOR → 原子写缓存。

**⚠️ 域名回退只对 qpic 生效**：视频走 `*.video.qq.com`，把它塞给
`mmsns.qpic.cn` 只会拿到 400。旧实现对所有域名回退，于是「视频下载成功但
认不出来」的真实原因被后面几个 400 覆盖，报错方向完全错。现在：

- `undecodable`（拿到字节但认不出格式）**优先级最高**，不被 HTTP 错误覆盖；
- HTTP 错误取**第一次**遇到的（不再取最后一个域名的）；
- 非微信 CDN 地址（`b23.tv` / 网易云 / B 站直播等外链卡片）直接判
  `not-cdn`，顺带堵掉 `/api/sns/media` 的任意 URL 代理（SSRF）。

### 2.4 失败诊断与日志（2026-09-30 补）

失败不再静默，每次失败都写 `logs/siwx.log`（“运行日志”页会 tail 该文件）：

```
[sns-media] 下载失败 reason=http-404 status=404 hosts_tried=3 got=0B err=HTTP 404 url=<host+path>
```

`reason` 是机器可读分类，前端 / 导出报告都用它：

| reason | 含义 | 能修吗 |
|---|---|---|
| `http-404` | CDN 上确实没有该资源（token 正确也 404） | ❌ 只能如实报错 |
| `http-400` / `http-403` | 链接失效（视频 `encfilekey` 短时有效） | ❌ |
| `undecodable` | 下载成功但解密/格式识别失败 | ✅ 类 bug（如上面的 key="0"） |
| `not-cdn` | 外链卡片，不是媒体 | 预期拒绝 |
| `network` | 连接层失败 | 重试即可 |

日志里的 URL **去掉 query**（`safe_url()`）—— token 是凭据，不进日志。

### 2.5 表情（`fetch_emoji`）

```python
fetch_emoji(emoji_dict, cache_dir=None) -> dict   # → {ok, data, ext, mime, error, via}
```

**⭐ 实测关键：`sns_emoji_data/url` 就是明文直链，不需要 AES 解密。**

```
[url]         HTTP 200 → 明文 GIF / PNG / JPEG
[encrypt_url] HTTP 200 → 密文（才需要 aes_key）
```

所以优先用明文 `url`；`decrypt_emoji_aes`（AES-GCM）降级为备用且**尚无真实样本验证**。

**⚠️ 表情域名是 `vweixinf.tc.qq.com` / `mmbiz.qpic.cn` / `wxapp.tc.qq.com`**，
不是 `mmsns.qpic.cn` —— **`_host_candidates` 的域名回退不适用于表情**。

### 2.5 微信图片尾部

解密后 JPEG **不以 `FFD9` 结尾**，后面还有 24 字节：

```
75f0d33c | 00000000 | <16 字节 = 明文(不含本尾部)的 MD5>
└ 固定魔数 ┘  └保留┘
```

- 256/256 样本中 `尾部[8:24] == md5(body[:i+2])` 全部成立（`i = rfind(FFD9)`）
- **用法**：① 解析时 `strip_wechat_tail()` 截断；② **当解密正确性校验用**

---

## 三、`sns.py` — 解析与本地关联

### 3.1 snsId 位布局（本项目独有发现）

```
snsId（64 位无符号） = (createTime_ms << 23) | random(23 bits)
createTime_ms = snsId >> 23
```

- **5,684/5,684 验证通过**，最大偏差 938 ms
- 41 位毫秒 → 设计寿命到 **2039-09-07**（超期溢出）
- **⚠️ `tid` 是有符号 int64**，读出来常是负数，必须 `& 0xFFFFFFFFFFFFFFFF` 还原

**价值**：分页/排序/过滤全走 SQL 索引扫描，不必解析 XML。

```python
sns_id_to_ms(tid) / sns_id_to_seconds(tid) / is_sns_id_in_range(tid)
```

### 3.2 XML 解析

```python
parse_timeline(content) -> dict | None
```

顶层骨架：

```
SnsDataItem
├── TimelineObject                   ← 服务端正文
│   ├── id / username / createTime / contentDesc
│   ├── ContentObject
│   │   ├── type                     ← 见下表
│   │   ├── mediaList/media          ← 图片/视频（含 LivePhoto）
│   │   ├── finderFeed / mmreadershare / musicShareItem / noteinfo
│   ├── private / isTop / location
└── LocalExtraInfo                   ← 本地互动
    ├── like_user_list/user_comment      ← 点赞
    ├── comment_user_list/user_comment   ← 评论
    └── with_user_list
```

**`ContentObject/type` 枚举（本机实测，2026-09-30 复核）**：

| type | 条数 | 含义（实测） | 卡片字段 |
|---|---|---|---|
| 1 | 3,575 | 纯文本（**但多数带图**） | 无 |
| 2 | 819 | 图片 | 无 |
| 15 | 499 | 视频 | 无 |
| 28 | 353 | **视频号动态** | `finderFeed` |
| 3 | 183 | 链接 | `title` / `description` / `contentUrl` |
| 7 | 117 | ~~音乐~~ **实测无任何卡片字段**（只有 mediaList） | 无 |
| 54 | 91 | ~~公众号文章~~ **实测无卡片字段**（正文在 `mediaList/media/description`） | 无 |
| 5 | 17 | 外部链接/直播分享 | `title` / `contentUrl` |
| 26 | 3 | ~~unknown26~~ **笔记** | `noteinfo` |
| 42 | 3 | ~~finder_live~~ **音乐分享**（酷狗） | `musicShareItem` |
| 47 | 4 | ~~note~~ **音乐分享**（QQ 音乐） | `musicShareItem` + `tingListenItem` |
| 34 | 3 | ~~ting~~ **视频号直播** | `finderLive` |

> ⚠️ **type 编号的语义在不同微信版本间会漂移**，早期按外部资料推断的名字（斜体删除线部分）
> 与本机实测不符。渲染判断**一律以 `card["kind"]`（由实际字段推导）为准**，不要看 `content_kind`。

**评论 vs 点赞的判定**：有 `content` / 表情 / 图片 / `type=2` → **评论**，否则点赞。

### 3.2.1 卡片类动态 —— `card` 字段（⭐ 2026-09-30 新增）

**最重要的实测结论：XML 里根本不存在 `<appmsg>` 节点。**
卡片元数据是 `<ContentObject>` 的**直接子元素**。早期文档（含 `sns-todo.md` 初版）
假设的 `appmsg/title`、`appmsg/url`、`appmsg/appname`、`appmsg/musicUrl` 路径**全部不存在**，
照抄会得到全空的卡片。

`parse_timeline` 现在返回 `feed["card"]`（无卡片字段时为 `None`）：

```python
card = {
    "kind": "link" | "finder" | "live" | "music" | "note",
    "title": str, "description": str, "content_url": str, "source": str,
    "cover_url": str, "cover_width": int, "cover_height": int, "duration_s": int,
    # 按 kind 附带的子结构
    "music":  {"singer", "album", "genre", "duration_ms", "mid"},
    "finder": {"nickname", "avatar", "username", "object_id", "nonce_id",
               "feed_type", "media_count", "live_id", "medias": [...]},
    "live":   {"nickname", "head_url", "desc", "live_id", "username",
               "object_id", "nonce_id", "status", "cover_url", "width", "height"},
    "note":   {"edit_time", "text", "image_count"},
}
```

**`kind` 的推导顺序**（由存在的字段决定，与 type 无关）：

```
musicShareItem → music ｜ finderFeed → finder ｜ finderLive → live ｜
noteinfo → note ｜ 有 title/description/contentUrl → link
```

**⚠️ 对外形状由 `sns.public_card(card)` 统一**（Web API 与导出共用同一份）：

| 内部（仅 sns.py 内部使用） | 对外（`public_card`） |
|---|---|
| `content_url` | `url` |
| `cover_url` / `cover_width` / `cover_height` | `cover` / `cover_width` / `cover_height` |
| `duration_s` | `duration` |
| `finder.medias` / `finder.media_count` / `finder.object_id` | `finder.media` / `finder.media_count` / `finder.object_id` |
| （`finder` 里的视频在 `card["video_url"]`） | `finder.video_url` |

`api_sns._feed_json()` 与 `sns_export._feed_to_dict()` 都调用它，前端只认对外名字。
**踩坑记录**：首版前端读的是 `card.cover` 而 API 直接透传内部 `card.cover_url`，
封面静默不显示；当时的验证脚本喂的是"导出形状"，所以没暴露 —— 现在统一走 `public_card`，
并加了 `test_public_card_shape_is_stable` + API 形状断言防回归。

**视频号视频地址要单独找**：实测很多 `finderFeed/mediaList/media` **只有 `coverUrl` 没有 `<url>`**，
封面取第一条有 `coverUrl/thumbUrl` 的、视频地址取第一条有 `<url>` 的，两者可能不是同一条
（本机 356 条视频号卡片中 256 条有可用视频地址）。

**字段实测出现率**（分母为该 type 的总条数）：

| 字段 | type 3（链接） | type 28（视频号） |
|---|---|---|
| `title` | 100% | 2%（其余是"版本不支持"占位） |
| `description` | 65% | 28%（正文多在 `TimelineObject/contentDesc`） |
| `contentUrl` | 100% | 30%（多为"请升级微信"兜底链接，**不要当跳转用**） |
| 来源 | `TimelineObject/sourceNickName`（公众号名） | `finderFeed/nickname` |

**`<finderFeed>` 的媒体结构与主 `mediaList` 完全不同**（这是一个大坑）：

| | 主 `mediaList/media` | `finderFeed/mediaList/media` |
|---|---|---|
| 地址 | `<url>` + `token`/`key` 属性 | `<url>` / `<thumbUrl>` / `<coverUrl>`（无 key/token） |
| 尺寸 | `<size width height>` 属性 | 扁平 `<width>`/`<height>`（**实测可能是 `"1080.0"`**） |
| 时长 | `<videoDuration>` 秒（浮点） | `<videoPlayDuration>` 秒（整数） |
| 类型 | `<type>`（2 图 / 6 视频） | `<mediaType>`（实测 4 = 视频） |

因此 `_parse_finder_media()` 单独解析，且**不混进 `feed["medias"]`**（否则会被当成主图去 CDN 下载并错配）。

### 3.2.2 `search_text(feed)` —— 搜索口径

```python
sns.search_text(feed) -> str   # 小写 blob
```

拼接：`contentDesc` + 卡片 `title/description/source` + 音乐歌手/专辑/流派 +
视频号昵称 + 直播昵称/简介 + 笔记正文 + **每个 media 的 `description`/`title`** + 位置名称/地址。

为什么需要它：卡片类动态的 `contentDesc` 大量为空（type 7 全空、type 54 正文只在
`mediaList/media/description`），只搜 `contentDesc` 会出现"明明有这条却搜不到"。
**Web 搜索与导出过滤共用这一个口径**，避免两处行为不一致。

### 3.3 `_parse_media_el(el)` — 三种媒体元素统一解析

`mediaList/media`、`imageinfo`、`liveMedia` **同构**，差异只在：

| | `media` | `imageinfo` | `liveMedia` |
|---|---|---|---|
| 尺寸 | `<size>` 属性 | 扁平 `width`/`height`/`file_size` | `<size>` 属性 |
| 缩略图 | `<thumb>` | `<thumb_url>` | `<thumb>` |
| 解密 key | `url@key` | `url@key` | **`<enc key>`** ⚠️ |

**⚠️ 实况照片的 key 在 `<enc key="...">`，不是 `url@key`！**
（即用 `<enc\s+key="(\d+)"` 正则从 XML 里提取的那个值）

**⚠️ `liveMedia` 的 `<videoSize>` 恒为 0×0**，尺寸必须取 `<size>`。

### 3.4 本地关联（备用，默认不用）

```python
iter_cache_images(acc_root)              # 扫描 cache/*/Sns/Img/，解密并取尺寸
match_feed_images(feed, cache)           # 单条动态分级匹配
match_feed_comments(feed, acc_root)      # 评论表情/图片精确关联（XML 自带 md5）
assign_images_globally(feeds, cache)     # ⭐ 全局唯一分配
export_image_pool(cache, dest_dir)       # 图片池降级导出
```

**`assign_images_globally` 的实测效果**（用时间相关性作正确性信号）：

| 时间差 | 全局分配 | 随机基线 |
|---|---|---|
| ≤1 天 | **83%** | 0% |
| ≤7 天 | **100%** | 2% |

原理：单条匹配时同一文件会被多条动态认领；加上**全局唯一约束**后，
多候选被其它动态占走，剩下的经时间验证是真匹配。

### 3.5 便捷入口

```python
iter_timeline(db_path, desc=True, limit=None)   # 按 tid 迭代
timeline_stats(db_path)                         # 概览（纯 tid，361ms）
search_text(feed)                               # 统一搜索 blob（见 §3.2.2）
```

---

## 四、`sns_export.py` — 多格式导出

**独立于 `exporter.py`** —— 聊天导出有「零插件模式下行为字节级一致」的红线，
朋友圈数据形状不同，因此单独成模块，只复用 `_safe_name` 与原子写模式。

```python
FORMATS = ("json", "markdown", "txt", "html")

run_sns_export(db_path, account, fmt="json", export_root=None,
               usernames=None, start=None, end=None,
               want_media=False, want_images=True, want_videos=True,
               want_livephotos=True, cache_dir=None,
               progress=None, limit=None, keyword=None) -> dict
```

- JSON **增量写**（不把全部动态堆内存）
- 默认导出目录 `paths.exports_root()`
- 过滤：关键词（走 `sns.search_text`，卡片标题/歌手也能命中）/ 发布者 / 时间范围

**卡片块**（四格式都带，`_card_lines()` / `_html_card()` 共用）：

| kind | markdown / txt | html |
|---|---|---|
| `link` | `> 🔗 [标题](url)` + 来源 + 摘要 | `.card--link`，标题为可点 `<a>` |
| `music` | `> 🎵 专辑 — 歌手（4:48）（[打开](url)）` | `.card--music` |
| `finder` | `> 📹 视频号 @昵称（时长）` + 封面图 | `.card--finder` + `.card-cover` |
| `live` | `> 📺 直播 @昵称` + 简介 + 封面 | `.card--live` |
| `note` | `> 📝 笔记` + 正文 | `.card--note` |

另外：位置（📍）进 md/txt/html，评论表情与**评论图**进四种格式（此前只进 JSON）。

**媒体命名**：

```
media/<tid>_<index>.jpg
media/<tid>_<index>_live.mp4      ← 实况照片
```

`download_media(feeds, media_dir, cache_dir, ..., concurrency=5)` 并发下载。

---

## 五、`api_sns.py` — Web API

| 端点 | 说明 |
|---|---|
| `GET /api/sns/accounts` | 有 sns.db 的账号 + 条数 |
| `GET /api/sns/timeline` | 动态分页（`before_tid` 游标 / `keyword` / `username`） || `GET /api/sns/detail` | 单条详情（评论不截断） |
| `GET /api/sns/search` | 复用 timeline 过滤 |
| `GET /api/sns/stats` | 概览 |
| `GET /api/sns/media` | 按需下载+解密媒体（带磁盘缓存） |
| `GET /api/sns/emoji` | 评论表情（JSON 传参） |
| `GET /api/sns/formats` | 支持的导出格式 |
| `POST /api/sns/export` | **启动异步导出任务**（返回 `{started}`，轮询 `/api/job`） |
| `GET /api/sns/export/download` | 下载产物（`relative_to(exports_root)` 越界校验） |

**安全**：`_account_dir()` 做字符白名单（`[A-Za-z0-9_.@-]`）+ `relative_to(out_root)` 收敛。

**关键词搜索的扫描策略**（2026-09-30 调整）：关键词可能只出现在卡片字段里，
SQL 无法下推，只能逐条解析 XML。因此有关键词时**不设 SQL LIMIT**，改为
命中 `limit` 条即停 + `KEYWORD_MAX_SCAN = 5000` 条上限兜底
（实测 0.19 ms/条，5000 条约 1s，本机全库 5684 条）。

> 旧实现是「取最新 `limit*3` 条候选再过滤」，实测会导致**深层卡片动态永远搜不到**
> （搜"让风告诉你"返回 0 条）。改成渐进扫描后同样查询返回 1 条。

**边界实测**：账号穿越 404 ｜ tid 非法 400 ｜ 不存在 404 ｜
download 越界 403 ｜ 非法格式 400 ｜ 缺参 400 ｜ 已有任务 409

---

## 六、前端 `ui/pages/sns.*`

- 时间线（游标分页 + 无限加载）
- 关键词 / 发布者筛选（350ms 防抖）
- **卡片渲染**（`renderCard`，按 `card.kind` 分支）：链接 / 音乐 / 视频号 / 直播 / 笔记
  - 读的是 `public_card` 的字段名（`url` / `cover` / `duration` / `finder.video_url` / `finder.media_count`）
  - 链接、音乐卡片整体可点（`<a target="_blank">`）
  - 视频号封面点一下直接在灯箱里播视频（复用实况照片的 `data-live` 通道）
  - 有卡片时不再显示「（无文字）」占位
  - ⚠️ `esc()` 走 `innerHTML`，**不转义引号**；放进属性值必须再用 `attr()` 转一次
- 详情弹层（评论不截断）+ 图片灯箱（支持视频）+ `Esc` 逐层关闭
- 实况照片：「实况」角标，点击灯箱播放视频
- 评论内联渲染表情（22px）与图片（≤120px）
- 导出面板（格式 + 是否含媒体 + 下载/打开目录）

**事件用委托绑在 `document`**（列表与弹层共用），`destroy()` 里解绑并清理轮询定时器。

---

## 七、导出/任务集成（`server.py`）

`_run_job` 新增 `sns_export` 模式，**放在 `find_wechat_data_dirs()` 之前并提前 `return`**
—— 朋友圈导出只依赖已解密产物，不需要全盘扫描微信目录。

任务日志前缀 `[sns]`，进度形如 `[sns] N/M 媒体 ...`。

---

## 八、性能参考（本机 5684 动态 / 2037 缓存）

| 操作 | 耗时 |
|---|---|
| 概览统计（纯 tid） | 361 ms |
| XML 全量解析 | 2.4 s |
| 缓存索引（解密+尺寸） | 3.7 s |
| 全局关联 | < 4 s |
| 导出 30 条（无媒体） | 1.5 s |

---

## 九、已知边界

| 项 | 说明 |
|---|---|
| **CDN 已无此资源** | 实测（2026-09-30，抽样 664 张图）：**自己发的图 58% 404**、好友的 4% 404。token / 尺寸档 / 备用域名 / 本地缓存全部试过，都拿不到 —— 属 CDN 侧事实，只能如实报错 |
| **本地缓存覆盖率** | 19%（微信只缓存浏览过的图片）—— **物理上限**；404 的那批图实测连尺寸都对不上，本地也没有 |
| **视频链接短时有效** | `snsvideodownload?encfilekey=...` 过期后 400（实测 6 个样本中 1 个） |
| **域名失效** | `shmmsns.qpic.cn` 已见 404，故对 qpic 保留域名回退 |
| **`decrypt_emoji_aes`** | 备用路径，**尚无真实样本验证** |
| **私密动态** | `private=1`（本机 9 条），默认照常展示 |
| **位置** | 多数动态是 `0,0` 占位，已过滤 |

---

## 十、测试

`tests/test_sns.py`（64 例）：

- ISAAC64 官方向量自检、snsId 还原（含负数）
- URL 构造（token 属性 / 图片档位替换）、缓存键去 token
- XML 解析（media / imageinfo / liveMedia 三种形态、`<enc key>`、位置占位过滤）
- 表情字段提取、评论/点赞拆分
- **卡片解析**（`TestSnsCardParsing`）：链接 / 音乐 / 视频号 / 直播 / 笔记五类字段、
  **kind 按字段而非 type 判定**、finder 媒体不混进主 mediaList、`search_text` 口径
- **卡片导出**（`TestSnsCardExport`）：关键词命中卡片标题/昵称、JSON `card` 结构、
  md/txt/html 的卡片块 + 位置 + 评论图
- **CDN 诊断**（`TestSnsCdnDiagnosis`）：`key="0"` 回退 `<enc key>`（修复视频）、
  域名回退只对 qpic、CDN 白名单（含 `wxapp.tc.qq.com`）、
  `undecodable` 不被 HTTP 错误覆盖、失败必写日志、`/api/sns/media` 拒绝外链
- 导出四格式、关键词/发布者过滤、空结果、非法格式
- **API 层**（`SIWX_ROOT` 隔离）：账号列表、游标分页、详情、**路径穿越**、
  导出校验、**异步任务全流程**、download 越界、emoji 参数、**关键词命中卡片字段**

```bash
python -m unittest tests.test_sns -v
```

**改 CDN 相关代码前先跑真库体检**（不写任何文件，只读 sns.db + 真网络）：

```bash
python diag_sns_cdn.py <账号目录名> 300 60
```

---

## 相关文档

- `docs/sns-implementation-guide.md` — **实施指南**（推进用，含待办清单）
- `docs/sns-research-2026-09-29.md` — 完整研究记录（30 轮实验原始数据）
- `scripts/sns_card_probe.py` — **卡片 XML 探针**（改卡片相关代码前先跑它 dump 真实结构）
- `docs/media-decryption-principles.md` — 聊天媒体的 V2 解密（**另一套体系**）
- `docs/module-media.md` — 媒体模块（本地缓存解密）
