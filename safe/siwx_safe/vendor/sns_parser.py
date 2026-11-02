"""微信朋友圈（SNS）解析与媒体关联。

本模块解决一个具体问题：**如何把朋友圈动态（sns.db 的 XML）与本地缓存的
图片文件对应起来**。

结论先行（实测依据见 docs/sns-research-2026-09-29.md）：

1. 微信**不落盘**「URL → 本地缓存文件名」的映射。该映射只存在于客户端内存中。
   - 已穷举：sns.db 全字段、全部 32 个解密库 596 张表、hardlink、HttpResource
   - 统计学证明：缓存文件名与 XML 的 ``url@md5`` 属性完全独立
     （前 4 位重合 113 次 vs 随机期望 114.61 次）
2. 因此**不存在 100% 精确关联**。本模块提供的是**分级置信度**的关联：
   - ``exact``  : 评论表情/评论图片 —— XML 自带 md5，可精确关联
   - ``high``   : 尺寸精确匹配 + 时间窗口 + 唯一候选
   - ``low``    : 尺寸匹配但多候选（不给图，只标注）
   - ``none``   : 无法关联（只给 CDN URL）
3. **绝不猜测**：低置信度一律不返回图片路径，避免静默错配。

设计原则与 ``media.py`` 保持一致：按需解密、只读、不落盘明文。
"""
from __future__ import annotations

import hashlib
import os
import struct
import xml.etree.ElementTree as ET
from collections import defaultdict
from pathlib import Path


# ── 常量 ────────────────────────────────────────────────────────────

SNS_ID_SHIFT = 23          # snsId = (createTime_ms << 23) | random(23bit)
_SNS_ID_MASK = 0xFFFFFFFFFFFFFFFF
SNS_ID_EPOCH_LIMIT_MS = 1 << 41   # 41 位毫秒上限 → 2039-09-07

# 内容类型（ContentObject/type 实测枚举）
#
# ⚠️ 这些名字是**早期按外部资料推断**的，实测（5684 条真实库）与部分编号不符：
#   type 42（3 条）/ 47（4 条）实际是**音乐分享**（含 musicShareItem + 网易云/QQ音乐链接）
#   type 34（3 条）实际是**视频号直播**（含 finderLive）
#   type 26（3 条）实际是**笔记**（含 noteinfo）
#   type 7（117 条）/ 54（91 条）**没有任何卡片字段**，只有 mediaList
# 因此渲染判断**不要依赖这里的名字**，请用 ``card["kind"]``（由实际字段推导，权威）。
CONTENT_TYPES = {
    1: "text", 2: "image", 3: "link", 5: "unknown5", 7: "music",
    15: "video", 26: "unknown26", 28: "finder_feed", 34: "ting",
    42: "finder_live", 47: "note", 54: "mp_article",
}


# ── snsId 时间还原（本模块最核心的位运算）──────────────────────────

def sns_id_to_ms(sns_id: int) -> int:
    """snsId → 毫秒时间戳。

    ``snsId = (createTime_ms << 23) | random(23 bits)``

    ⚠️ SQLite 把 snsId 存成**有符号 int64**，读出来常是负数，
    必须先 ``& 0xFFFFFFFFFFFFFFFF`` 还原成无符号，否则位运算结果全错。

    实测 5684/5684 与 XML 内 ``<createTime>`` 吻合（最大偏差 938ms）。
    """
    return (sns_id & _SNS_ID_MASK) >> SNS_ID_SHIFT


def sns_id_to_seconds(sns_id: int) -> int:
    """snsId → 秒级时间戳。"""
    return sns_id_to_ms(sns_id) // 1000


def is_sns_id_in_range(sns_id: int) -> bool:
    """snsId 是否在 41 位毫秒可表达范围内（< 2039-09-07）。"""
    return sns_id_to_ms(sns_id) < SNS_ID_EPOCH_LIMIT_MS


def _to_signed64(u: int) -> int:
    """无符号 64 位 → SQLite 存储用的有符号 int64。"""
    return u - (1 << 64) if u >= (1 << 63) else u


def ts_to_tid_bounds(start_sec: int | None = None, end_sec: int | None = None):
    """秒级时间范围 → **tid 区间（含端点，有符号 int64）**，可直接下推 SQL。

    ``tid = (createTime_ms << 23) | rand23``，因此：
      * 下界 = ``start_ms << 23``
      * 上界 = ``(end_sec + 1) * 1000 << 23  - 1``（覆盖 end 那一秒的 1000ms）

    为什么可以比较大小：modern tid（2012 年后）的 ``ms << 23`` 都超过 2^63，
    存进 SQLite 变成负数；而 u → s 的映射在 [2^63, 2^64) 上**单调递增**，
    所以同一侧（都为负）时 ``BETWEEN`` 语义正确。
    跨 2004-11 这种「正负混用」的区间本项目不可能出现，调用方仍会做兜底（回退 Python 过滤）。
    """
    lo = _to_signed64((int(start_sec) * 1000) << SNS_ID_SHIFT) if start_sec is not None else None
    hi = (_to_signed64((((int(end_sec) + 1) * 1000) << SNS_ID_SHIFT) - 1)
          if end_sec is not None else None)
    return lo, hi


def parse_ts_arg(v):
    """解析时间参数：支持 unix 秒、``YYYY-MM-DD``（本地时区）、``YYYY-MM-DD HH:MM:SS``。

    ``end`` 传日期时由调用方决定是否补到当日 23:59:59（见 ``api_sns._ts_arg``）。
    """
    import datetime as _dt
    if v is None:
        return None
    if isinstance(v, (int, float)):
        return int(v)
    s = str(v).strip()
    if not s:
        return None
    if s.isdigit():
        return int(s)
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%d", "%Y/%m/%d"):
        try:
            return int(_dt.datetime.strptime(s, fmt).timestamp())
        except ValueError:
            continue
    return None


# ── XML 解析 ────────────────────────────────────────────────────────

def _int(v, default=0) -> int:
    try:
        return int(v)
    except (TypeError, ValueError):
        return default


# 微信会把「没有这个值」写成 "0"（而不是省略属性）。
# ⚠️ 只能按**字符串语义**判断：`"0"` 在 Python 里是真值，用 `or` 串起来会把它当成有效值。
_NULL_ATTRS = frozenset({"", "0", "none", "null"})


def _meta_attr(*vals):
    """取第一个「有效」的属性值；``"0"`` / 空 / ``none`` 一律视为缺失。

    实测（本机 5684 条真实库）踩过的坑：视频 media 写成
    ``<url key="0">`` + ``<enc key="929615230">`` —— 真正的 ISAAC64 密钥在
    ``<enc key>``，而 ``url@key="0"`` 只是占位。旧实现按 ``or`` 取值，于是
    密钥被当成 0，下载回来的密文 XOR 后仍是乱码，表现为「CDN 视频拉不下来」。
    """
    for v in vals:
        if v is None:
            continue
        s = v.strip() if isinstance(v, str) else str(v).strip()
        if s and s.lower() not in _NULL_ATTRS:
            return s
    return None


def _parse_media_el(el, url_tag: str = "url") -> dict:
    """解析一个「媒体元素」—— ``mediaList/media`` / ``imageinfo`` / ``liveMedia`` 同构。

    三者都带 ``url`` / ``key`` / ``enc_idx`` / ``md5`` / 尺寸 等字段，
    区别在标签名、尺寸来源（``<size>`` 属性 / 扁平字段 / ``<videoSize>``）
    以及解密密钥的位置。

    ⚠️ ``token`` 属性是 CDN 下载的必需参数（缺了会 400），务必保留。
    ⚠️ **``liveMedia`` 的密钥在 ``<enc key="...">``**，不是 ``url@key``。
    """
    url_el = el.find(url_tag)
    sz = el.find("size")
    w = h = total = 0
    if sz is not None:
        w = _int(sz.get("width"))
        h = _int(sz.get("height"))
        total = _int(sz.get("totalSize"))
    if not (w or h):
        w = _int(el.findtext("width"))
        h = _int(el.findtext("height"))
        total = total or _int(el.findtext("file_size"))
    if not (w or h):
        vs = el.find("videoSize")
        if vs is not None:
            w = _int(vs.get("width"))
            h = _int(vs.get("height"))

    enc_el = el.find("enc")
    enc_key = _meta_attr(enc_el.get("key")) if enc_el is not None else None
    thumb_el = el.find("thumb")

    return {
        "id": el.findtext("id") or el.findtext("media_id") or "",
        "type": _int(el.findtext("type")),
        "sub_type": _int(el.findtext("sub_type")) or _int(el.findtext("subType")),
        "width": w,
        "height": h,
        "total_size": total,
        "url": (url_el.text or "").strip() if url_el is not None else "",
        # media 用 <thumb>，imageinfo 用 <thumb_url>，两者都要认
        "thumb_url": (el.findtext("thumb_url") or el.findtext("thumb") or "").strip(),
        "md5": (url_el.get("md5") if url_el is not None else None) or el.findtext("md5") or None,
        # ⚠️ 必须是 _meta_attr：url@key="0" 是占位值，真密钥在 <enc key>
        # （实测视频 100% 因此解不出来；见 _meta_attr 注释）
        "key": _meta_attr(url_el.get("key") if url_el is not None else None,
                          el.findtext("key"), enc_key),
        "token": _meta_attr(url_el.get("token") if url_el is not None else None,
                            el.findtext("token")),
        # <thumb> 自带 token 属性；<thumb_url_token> 是扁平字段的写法
        "thumb_token": _meta_attr(el.findtext("thumb_url_token"),
                                  thumb_el.get("token") if thumb_el is not None else None),
        "videomd5": url_el.get("videomd5") if url_el is not None else None,
        "enc_idx": (_int(url_el.get("enc_idx")) if url_el is not None
                    else _int(el.findtext("enc_idx"))),
        "enc_key": enc_key,
        "video_duration": el.findtext("videoDuration") or "",
        "live_still_ms": _int(el.findtext("liveStillImageTimeMs")),
        "description": el.findtext("description") or "",
        "title": el.findtext("title") or "",
    }


def _num(v, default=0) -> int:
    """解析可能是 ``"1080"`` 或 ``"1080.0"`` 的数值字段（finderFeed 实测两种都有）。"""
    try:
        return int(float(v))
    except (TypeError, ValueError):
        return default


def _txt(el, tag: str) -> str:
    if el is None:
        return ""
    return (el.findtext(tag) or "").strip()


def _parse_finder_media(m) -> dict:
    """解析 ``<finderFeed><mediaList><media>``。

    ⚠️ 结构与主图 ``mediaList/media`` **完全不同**：没有 ``url``/``key``/``enc_idx``，
    封面字段是 ``coverUrl`` / ``thumbUrl``，尺寸是扁平的 ``width``/``height``。
    """
    return {
        "media_type": _num(m.findtext("mediaType")),
        "url": _txt(m, "url"),
        "thumb_url": _txt(m, "thumbUrl"),
        "cover_url": _txt(m, "coverUrl"),
        "width": _num(m.findtext("width")),
        "height": _num(m.findtext("height")),
        "duration_s": _num(m.findtext("videoPlayDuration")),
    }


def _parse_card(co, to) -> dict | None:
    """解析「卡片类」动态的专属字段（链接 / 视频号 / 直播 / 音乐 / 笔记）。

    ⭐ 实测（5684 条真实库）最重要的一条：**不存在 ``<appmsg>`` 节点**。
    卡片元数据是 ``<ContentObject>`` 的**直接子元素**：

    ============  ====================================================
    字段            实测出现率
    ============  ====================================================
    title         type 3/5/26/42/47 100%；type 28 仅 2%（"版本不支持"占位）
    description   type 26/42/47 100%；type 3 65%；type 28 28%
    contentUrl    同上；type 28 的 30% 是"请升级微信"的兜底链接
    ============  ====================================================

    ``kind`` 由**实际存在的字段**推导（而不是只信 type 编号），
    因为编号语义在不同微信版本间会漂移（见 ``CONTENT_TYPES`` 注释）。
    """
    if co is None:
        return None

    title, desc, curl = _txt(co, "title"), _txt(co, "description"), _txt(co, "contentUrl")
    source = _txt(to, "sourceNickName") or _txt(to, "publicUserName")

    card = {"title": title, "description": desc, "content_url": curl, "source": source}

    # ── 音乐分享（实测 type 42 酷狗 / type 47 QQ音乐）──────────────
    ms = co.find("musicShareItem")
    if ms is not None:
        card["kind"] = "music"
        card["music"] = {
            "singer": _txt(ms, "mvSingerName"),
            "album": _txt(ms, "mvAlbumName"),
            "genre": _txt(ms, "mvMusicGenre"),
            "duration_ms": _num(ms.findtext("musicDuration")),
            "mid": _txt(ms, "mid"),
        }
        return card

    # ── 视频号动态（type 28，353 条）──────────────────────────────
    ff = co.find("finderFeed")
    if ff is not None:
        card["kind"] = "finder"
        medias = [_parse_finder_media(m) for m in ff.iter("media")]
        cover = next((m for m in medias if m.get("cover_url") or m.get("thumb_url")), None)
        # 视频地址可能不在第一个 media 上（实测 76% 的 type 28 才有 <url>），单独找
        video = next((m for m in medias if m.get("url")), None)
        card["finder"] = {
            "nickname": _txt(ff, "nickname"),
            "avatar": _txt(ff, "avatar"),
            "username": _txt(ff, "username"),
            "object_id": _txt(ff, "objectId"),
            "nonce_id": _txt(ff, "objectNonceId"),
            "feed_type": _num(ff.findtext("feedType")),
            "media_count": _num(ff.findtext("mediaCount")),
            "live_id": _txt(ff, "liveId"),
            "medias": medias,
        }
        if cover:
            card["cover_url"] = cover.get("cover_url") or cover.get("thumb_url") or ""
            card["cover_width"] = cover.get("width") or 0
            card["cover_height"] = cover.get("height") or 0
        if video:
            card["video_url"] = video.get("url") or ""
        # 时长：优先封面那条，缺失时退回有视频地址的那条
        card["duration_s"] = ((cover or {}).get("duration_s")
                              or (video or {}).get("duration_s") or 0)
        return card

    # ── 视频号直播（type 34，3 条）────────────────────────────────
    fl = co.find("finderLive")
    if fl is not None:
        card["kind"] = "live"
        fm = fl.find("media")
        card["live"] = {
            "nickname": _txt(fl, "nickname"),
            "head_url": _txt(fl, "headUrl"),
            "desc": _txt(fl, "desc"),
            "live_id": _txt(fl, "finderLiveID"),
            "username": _txt(fl, "finderUsername"),
            "object_id": _txt(fl, "finderObjectID"),
            "nonce_id": _txt(fl, "finderNonceID"),
            "status": _num(fl.findtext("liveStatus")),
            "cover_url": _txt(fl, "coverUrl") or _txt(fm, "coverUrl"),
            "width": _num(fm.findtext("width")) if fm is not None else 0,
            "height": _num(fm.findtext("height")) if fm is not None else 0,
        }
        card["cover_url"] = card["live"]["cover_url"]
        card["cover_width"] = card["live"]["width"]
        card["cover_height"] = card["live"]["height"]
        return card

    # ── 笔记（type 26，3 条）─────────────────────────────────────
    ni = co.find("noteinfo")
    if ni is not None:
        card["kind"] = "note"
        texts, images = [], 0
        for item in ni.iter("dataitem"):
            dtype = item.get("datatype")
            if dtype == "1":
                t = _txt(item, "datadesc")
                if t:
                    texts.append(t)
            elif dtype == "2":
                images += 1
        card["note"] = {
            "edit_time": _num(ni.findtext("edittime")),
            "text": "\n".join(texts),
            "image_count": images,
        }
        if not desc and texts:
            card["description"] = texts[0]
        return card

    # ── 小视频（Sight，type 15）：内容就是 mediaList 里那条视频，
    # 卡片只是「微信小视频 + 升级页」占位（实测 61/61，title 有两种写法）。
    # 渲染成外链卡只会把用户引到"请升级微信"死胡同，还会把视频流 URL
    # 误当 cover_url；视频本身已在媒体网格里播放（密钥在 <enc key>）。
    if title in ("微信小视频", "Weixin Sight") and "common_page__upgrade" in curl:
        return None

    # ── 普通外链（type 3 链接 / type 5 外部直播等）─────────────────
    if title or desc or curl:
        card["kind"] = "link"
        # 封面：主 mediaList 第一张（前端已在媒体网格里渲染，这里给导出用）
        ml = co.find("mediaList")
        if ml is not None:
            first = ml.find("media")
            if first is not None:
                card["cover_url"] = (first.findtext("url") or "").strip()
        return card

    return None


def public_card(card: dict | None) -> dict | None:
    """内部 ``card`` → **对外统一形状**（Web API 与导出共用同一份）。

    为什么要有这一层：内部字段名（``content_url`` / ``duration_s`` / ``finder.medias``）
    与前端/导出想用的名字（``url`` / ``duration`` / ``finder.media``）不一致，
    如果让前端各自猜名字，就会出现「后端改了字段、前端静默不显示」的漂移
    （本次开发中就踩到一次：前端读 ``card.cover`` 而 API 给的是 ``card.cover_url``，
    卡片封面直接不显示，且单测因为用的是导出形状而没发现）。

    返回 **snake_case**，与 feed 的其他字段（``content_desc`` / ``user_name``）一致。
    """
    if not card:
        return None
    out = {
        "kind": card.get("kind"),
        "title": card.get("title") or "",
        "description": card.get("description") or "",
        "source": card.get("source") or "",
        "url": card.get("content_url") or "",
        "cover": card.get("cover_url") or "",
        "cover_width": card.get("cover_width") or 0,
        "cover_height": card.get("cover_height") or 0,
        "duration": card.get("duration_s") or 0,
    }
    if card.get("music"):
        out["music"] = dict(card["music"])
    if card.get("live"):
        out["live"] = dict(card["live"])
    if card.get("note"):
        out["note"] = dict(card["note"])
    f = card.get("finder")
    if f:
        out["finder"] = {
            "nickname": f.get("nickname") or "",
            "avatar": f.get("avatar") or "",
            "username": f.get("username") or "",
            "object_id": f.get("object_id") or "",
            "nonce_id": f.get("nonce_id") or "",
            "feed_type": f.get("feed_type") or 0,
            "media_count": f.get("media_count") or 0,
            "live_id": f.get("live_id") or "",
            "video_url": card.get("video_url") or "",
            "media": f.get("medias") or [],
        }
    return out


def search_text(feed: dict) -> str:
    """把一条动态里**所有可搜索的文本**拼成一个 blob（小写）。

    为什么需要它：实测 ``contentDesc`` 对卡片类动态经常为空
    （type 7 全部为空、type 28/47 大量为空），只搜 ``contentDesc``
    会漏掉标题/歌手/视频号昵称这些用户真正会去搜的词。
    """
    card = feed.get("card") or {}
    parts = [feed.get("content_desc") or "", card.get("title") or "",
             card.get("description") or "", card.get("source") or ""]
    m = card.get("music")
    if m:
        parts += [m.get("singer") or "", m.get("album") or "", m.get("genre") or ""]
    f = card.get("finder")
    if f:
        parts += [f.get("nickname") or "", f.get("username") or ""]
    lv = card.get("live")
    if lv:
        parts += [lv.get("nickname") or "", lv.get("desc") or ""]
    nt = card.get("note")
    if nt:
        parts.append(nt.get("text") or "")
    loc = feed.get("location")
    if loc:
        parts += [loc.get("name") or "", loc.get("address") or ""]
    # 实测 type 54 的正文只存在于 mediaList/media/description
    for md in (feed.get("medias") or []):
        parts.append(md.get("description") or "")
        parts.append(md.get("title") or "")
    return " ".join(p for p in parts if p).lower()


def parse_timeline(content: str | bytes) -> dict | None:
    """解析 ``SnsTimeLine.content`` 的 XML → 结构化 dict。

    容错：实测 5684 条中有 17 条无法解析（疑为特殊字符/版本差异），
    调用方应处理 None。
    """
    try:
        root = ET.fromstring(content)
    except ET.ParseError:
        return None
    to = root.find("TimelineObject")
    if to is None:
        return None

    co = to.find("ContentObject")
    ctype = _int(co.findtext("type")) if co is not None else 0

    medias = []
    if co is not None:
        ml = co.find("mediaList")
        if ml is not None:
            for m in ml.findall("media"):
                item = _parse_media_el(m)
                # 实况照片：嵌套的 liveMedia 是一段短视频（自带 <enc key>）
                lp = m.find("LivePhoto")
                if lp is not None:
                    lm = lp.find("liveMedia")
                    if lm is not None:
                        live = _parse_media_el(lm)
                        if live.get("url"):
                            item["live_photo"] = live
                medias.append(item)

    # 互动（本地附加）
    extra = root.find("LocalExtraInfo")
    likes, comments = [], []
    if extra is not None:
        for uc in extra.iter("user_comment"):
            # 表情：sns_emoji_data 是**结构化节点**（8 个字段），不是文本
            emojis = []
            for ei in uc.iter("emojiinfo"):
                sd = ei.find("sns_emoji_data")
                emojis.append({
                    "md5": ei.findtext("md5") or "",
                    "width": _int(ei.findtext("width")),
                    "height": _int(ei.findtext("height")),
                    "size": _int(ei.findtext("size")),
                    # ⚠️ url 是**明文**直链；encrypt_url 才需要 aes_key 解密
                    "url": (sd.findtext("url") if sd is not None else "") or "",
                    "thumb_url": (sd.findtext("thumb_url") if sd is not None else "") or "",
                    "encrypt_url": (sd.findtext("encrypt_url") if sd is not None else "") or "",
                    "aes_key": (sd.findtext("aes_key") if sd is not None else "") or "",
                    "extern_md5": (sd.findtext("extern_md5") if sd is not None else "") or "",
                })
            # 评论内嵌图片（结构与主图一致，路径为 /mmcomment/）
            images = []
            for ii in uc.iter("imageinfo"):
                parsed = _parse_media_el(ii)
                if parsed.get("url"):
                    parsed["type"] = 2
                    images.append(parsed)
            item = {
                "username": uc.findtext("username") or "",
                "nickname": uc.findtext("nickname") or "",
                "content": uc.findtext("content") or "",
                "create_time": _int(uc.findtext("create_time")),
                "comment_id": uc.findtext("comment_id") or "",
                "comment_64id": uc.findtext("comment_64id") or "",
                "ref_username": uc.findtext("ref_username") or "",
                "ref_comment_id": uc.findtext("ref_comment_id") or "",
                "type": _int(uc.findtext("type")),
                "deleted": _int(uc.findtext("b_deleted")),
                "is_rich_text": _int(uc.findtext("is_rich_text")),
                "emojis": emojis,
                "images": images,
                # 兼容旧字段
                "emoji_md5": emojis[0]["md5"] if emojis else "",
                "emoji_aes_key": emojis[0]["aes_key"] if emojis else "",
                "image_md5": images[0]["md5"] if images else "",
            }
            # 评论 vs 点赞：有 content / 表情 / 图片 / type=2 视为评论，其余为点赞
            if item["content"] or item["type"] == 2 or emojis or images:
                comments.append(item)
            else:
                likes.append(item)

    loc = to.find("location")
    location = None
    if loc is not None and loc.attrib:
        name = loc.get("poiName")
        address = loc.get("poiAddress")
        lat = loc.get("latitude")
        lng = loc.get("longitude")
        # 只有名称/地址，或非零坐标才算「有位置」—— 实测多数动态是 0,0 占位
        has_coord = bool(lat and lng and (lat != "0" or lng != "0"))
        if name or address or has_coord:
            location = {"lat": lat, "lng": lng, "name": name, "address": address}

    return {
        "id": to.findtext("id") or "",
        "username": to.findtext("username") or "",
        "create_time": _int(to.findtext("createTime")),
        "content_desc": to.findtext("contentDesc") or "",
        "content_type": ctype,
        "content_kind": CONTENT_TYPES.get(ctype, f"type{ctype}"),
        # 卡片专属字段（链接/视频号/直播/音乐/笔记），无卡片字段时为 None
        "card": _parse_card(co, to),
        "private": _int(to.findtext("private")),
        "is_top": _int(to.findtext("isTop")),
        "location": location,
        "medias": medias,
        "likes": likes,
        "comments": comments,
        "source_username": to.findtext("sourceUserName") or "",
    }
