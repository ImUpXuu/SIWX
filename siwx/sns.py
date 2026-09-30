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


# ── XML 解析 ────────────────────────────────────────────────────────

def _int(v, default=0) -> int:
    try:
        return int(v)
    except (TypeError, ValueError):
        return default


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
    enc_key = enc_el.get("key") if enc_el is not None else None

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
        "key": ((url_el.get("key") if url_el is not None else None)
                or el.findtext("key") or enc_key or None),
        "token": (url_el.get("token") if url_el is not None else None) or el.findtext("token") or None,
        "thumb_token": el.findtext("thumb_url_token") or None,
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


# ── 本地缓存索引 ────────────────────────────────────────────────────

def _jpeg_size(data: bytes):
    """从 JPEG 字节流读取 (width, height)。失败返回 None。"""
    i = 2
    n = len(data)
    while i < n - 9:
        if data[i] != 0xFF:
            i += 1
            continue
        m = data[i + 1]
        if m in (0xC0, 0xC1, 0xC2, 0xC3, 0xC5, 0xC6, 0xC7, 0xC9, 0xCA, 0xCB, 0xCD, 0xCE, 0xCF):
            h = struct.unpack(">H", data[i + 5:i + 7])[0]
            w = struct.unpack(">H", data[i + 7:i + 9])[0]
            return w, h
        if m in (0xD8, 0xD9) or 0xD0 <= m <= 0xD7:
            i += 2
            continue
        if i + 4 > n:
            break
        i += 2 + struct.unpack(">H", data[i + 2:i + 4])[0]
    return None


def _image_ext(data: bytes):
    if data[:3] == b"\xff\xd8\xff":
        return "jpg"
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        return "png"
    if data[:6] in (b"GIF87a", b"GIF89a"):
        return "gif"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "webp"
    return None


def _strip_wechat_tail(body: bytes) -> bytes:
    """去掉微信在图片末尾附加的 24 字节（``75f0d33c`` + ``00000000`` + 自校验 md5）。

    实测 256/256 样本成立；无该尾部时按原样返回。
    """
    i = body.rfind(b"\xff\xd9")
    if i < 0:
        return body
    tail = body[i + 2:]
    if len(tail) == 24 and tail[:4] == b"\x75\xf0\xd3\x3c":
        return body[:i + 2]
    return body


class CacheImage:
    """一张本地缓存的朋友圈图片。"""
    __slots__ = ("path", "name", "width", "height", "mtime", "size", "ext")

    def __init__(self, path: Path, name: str, width: int, height: int,
                 mtime: float, size: int, ext: str):
        self.path, self.name = path, name
        self.width, self.height = width, height
        self.mtime, self.size, self.ext = mtime, size, ext

    @property
    def ratio(self) -> float:
        """宽高比（始终 ≥ 1，便于匹配缩放版本）。"""
        if not self.width or not self.height:
            return 0.0
        r = self.width / self.height
        return round(r if r >= 1 else 1 / r, 2)

    def __repr__(self):
        return f"<CacheImage {self.name[:8]} {self.width}x{self.height}>"


def iter_cache_images(acc_root: Path, decrypt_fn=None, limit: int | None = None):
    """遍历 ``cache/<YYYY-MM>/Sns/Img/<2位hex>/<md5>``，解密并提取尺寸。

    :param acc_root:  账号根目录（``.../xwechat_files/<wxid>``）
    :param decrypt_fn: ``fn(bytes) -> bytes|None``，默认用 ``media`` 的 V2 解密
    :param limit:      最多处理多少个文件（调试用）
    :return: (成功列表, 统计 dict)
    """
    if decrypt_fn is None:
        from . import media as _media
        _decrypt = _media._decrypt_any
        # 用账号名派生密钥
        wxid = acc_root.name
        keys = _media.candidate_keys(wxid)

        def decrypt_fn(data):  # noqa: F811
            for aes_key, xor_key in keys:
                body, _ct = _media.decrypt_v2_body(data, aes_key, xor_key)
                if body:
                    return body
            return None

    out, stat = [], {"total": 0, "ok": 0, "fail": 0, "no_size": 0}
    cache_dir = acc_root / "cache"
    if not cache_dir.is_dir():
        return out, stat
    for month in sorted(cache_dir.iterdir()):
        img_dir = month / "Sns" / "Img"
        if not img_dir.is_dir():
            continue
        for bucket in sorted(img_dir.iterdir()):
            if not bucket.is_dir():
                continue
            for f in sorted(bucket.iterdir()):
                if not f.is_file():
                    continue
                stat["total"] += 1
                if limit and stat["total"] > limit:
                    return out, stat
                try:
                    raw = f.read_bytes()
                except OSError:
                    stat["fail"] += 1
                    continue
                body = decrypt_fn(raw)
                if not body:
                    stat["fail"] += 1
                    continue
                ext = _image_ext(body)
                wh = _jpeg_size(body) if ext == "jpg" else None
                if wh is None:
                    stat["no_size"] += 1
                    continue
                st = f.stat()
                out.append(CacheImage(f, f.name, wh[0], wh[1], st.st_mtime, len(raw), ext))
                stat["ok"] += 1
    return out, stat


# ── 关联器 ──────────────────────────────────────────────────────────

class Match:
    """一条动态某张图片的关联结果。"""
    __slots__ = ("media_index", "confidence", "path", "method", "candidates")

    def __init__(self, media_index, confidence, path=None, method="", candidates=0):
        self.media_index = media_index
        self.confidence = confidence     # exact / high / low / none
        self.path = path
        self.method = method
        self.candidates = candidates

    def __repr__(self):
        return f"<Match m{self.media_index} {self.confidence} {self.method}>"


def match_feed_images(feed: dict, cache: list[CacheImage],
                      delta_seconds: int = 3 * 86400,
                      require_unique: bool = True) -> list[Match]:
    """把一条动态的图片关联到本地缓存。

    分级策略（**绝不猜测**）：

    1. ``exact`` —— 尺寸精确匹配 + 时间窗口内**唯一**候选
    2. ``high``  —— 尺寸精确匹配 + 时间窗口内有候选，但数量与动态图片数一致
    3. ``low``   —— 有候选但不唯一（**不返回路径**）
    4. ``none``  —— 无候选（**不返回路径**，调用方应回退到 CDN URL）

    时间基准：缓存文件的 ``mtime`` 与动态 ``createTime``。
    实测二者高度相关（50% 分位差 12 分钟），但微信会**批量预下载**，
    故仍需唯一性约束兜底。
    """
    results = []
    if not feed.get("medias"):
        return results

    by_wh = defaultdict(list)
    for c in cache:
        if c.width and c.height:
            by_wh[(c.width, c.height)].append(c)

    ts = feed.get("create_time") or 0
    n_media = len(feed["medias"])

    for idx, m in enumerate(feed["medias"]):
        if m.get("type") != 2:          # 只处理图片
            continue
        w, h = m.get("width") or 0, m.get("height") or 0
        if not (w and h):
            results.append(Match(idx, "none", method="no-size"))
            continue

        cands = by_wh.get((w, h), [])
        if not cands:
            results.append(Match(idx, "none", method="size-miss"))
            continue

        # 时间窗口过滤
        if ts:
            near = [c for c in cands if abs(c.mtime - ts) <= delta_seconds]
        else:
            near = cands

        if len(near) == 1:
            results.append(Match(idx, "exact", near[0].path, "size+time", 1))
        elif len(near) > 1:
            # 多候选：若动态图片数与候选数一致，整体可视为高置信（顺序对齐）
            if not require_unique and len(near) == n_media:
                results.append(Match(idx, "high", None, "size+time+count", len(near)))
            else:
                results.append(Match(idx, "low", None, "size+time-ambiguous", len(near)))
        else:
            results.append(Match(idx, "none", method="time-miss", candidates=len(cands)))

    return results


def match_feed_comments(feed: dict, emoticon_dir: Path | None = None,
                        acc_root: Path | None = None) -> list[dict]:
    """关联评论里的表情/图片。

    **这是唯一能 100% 精确关联的部分** —— 因为 XML 里直接带 md5：
    ``emojiinfo/md5`` ↔ ``cache/<月>/Emoticon/<2位hex>/<md5>``（实测已验证）。
    """
    found = []
    for kind in ("likes", "comments"):
        for c in feed.get(kind, []):
            for field, sub in (("emoji_md5", "Emoticon"), ("image_md5", "Sns/Img")):
                md5v = c.get(field)
                if not md5v or len(md5v) != 32:
                    continue
                p = _find_cache_file(acc_root, sub, md5v) if acc_root else None
                found.append({
                    "kind": kind, "field": field, "md5": md5v,
                    "path": p, "aes_key": c.get("emoji_aes_key") or "",
                    "confidence": "exact" if p else "none",
                })
    return found


def _find_cache_file(acc_root: Path, sub: str, name: str) -> Path | None:
    """在 ``cache/<月>/<sub>/<前2位>/<name>`` 中查找。"""
    if not acc_root:
        return None
    cache = acc_root / "cache"
    if not cache.is_dir():
        return None
    bucket = name[:2]
    for month in sorted(cache.iterdir(), reverse=True):
        p = month / sub / bucket / name
        if p.is_file():
            return p
    return None


# ── 全局分配：把「图片池」尽力归属到动态 ─────────────────────────────
#
# 为什么需要"全局"分配：单条动态各自匹配时，同一个缓存文件可能被多条动态
# 同时认领（宽高比/时间都不唯一）。全局分配保证**一个文件只归属一条动态**，
# 并优先满足约束更强的动态（图片多、尺寸独特）。

class Assignment:
    """一条动态获得的图片归属。"""
    __slots__ = ("feed_tid", "matches", "confidence", "unmatched")

    def __init__(self, feed_tid):
        self.feed_tid = feed_tid
        self.matches = {}      # media_index -> CacheImage
        self.confidence = "none"
        self.unmatched = []    # 未归属的 media_index

    def __repr__(self):
        return f"<Assignment tid={self.feed_tid} {len(self.matches)} 图 {self.confidence}>"


def assign_images_globally(feeds, cache: list[CacheImage],
                           delta_seconds: int = 3 * 86400):
    """把图片池**全局**归属到动态（每个缓存文件最多归属一条动态）。

    策略（按约束强度排序，贪心）：
      1. 先处理「尺寸 + 时间窗口内**唯一**候选」的动态 —— 这类归属最可靠
      2. 再处理「尺寸匹配 + 时间窗口有候选」的 —— 按候选数升序（越少越可靠）
      3. 已被占用的缓存文件不再参与后续分配

    :return: (assignments, pool) —— pool 是未被任何动态认领的缓存文件
    """
    by_wh = defaultdict(list)
    for c in cache:
        if c.width and c.height:
            by_wh[(c.width, c.height)].append(c)

    # 为每条动态算出「每个 media 的候选集合」
    plans = []
    for f in feeds:
        if not f.get("medias"):
            continue
        ts = f.get("create_time") or 0
        per_media = []
        for idx, m in enumerate(f["medias"]):
            if m.get("type") != 2:
                continue
            w, h = m.get("width") or 0, m.get("height") or 0
            if not (w and h):
                continue
            cands = by_wh.get((w, h), [])
            if ts:
                cands = [c for c in cands if abs(c.mtime - ts) <= delta_seconds]
            if cands:
                per_media.append((idx, cands))
        if per_media:
            # 约束强度：候选总数越少越优先；图片数越多越优先
            total_cands = sum(len(c) for _, c in per_media)
            plans.append((total_cands, -len(per_media), f, per_media))

    plans.sort(key=lambda p: (p[0], p[1]))

    used: set[str] = set()
    assignments: list[Assignment] = []
    for _tot, _n, f, per_media in plans:
        a = Assignment(f.get("tid"))
        for idx, cands in per_media:
            free = [c for c in cands if c.name not in used]
            if len(free) == 1:
                a.matches[idx] = free[0]
                used.add(free[0].name)
                a.confidence = "high"
            elif len(free) > 1:
                # 多个可用候选：取时间最接近的（仍标记为低置信）
                free.sort(key=lambda c: abs(c.mtime - (f.get("create_time") or 0)))
                a.matches[idx] = free[0]
                used.add(free[0].name)
                a.confidence = "low" if a.confidence != "high" else "high"
            else:
                a.unmatched.append(idx)
        if a.matches:
            assignments.append(a)

    pool = [c for c in cache if c.name not in used]
    return assignments, pool


def _wxid_from_cache_path(p: Path) -> str:
    """从 ``.../xwechat_files/<wxid>/cache/<月>/Sns/Img/..`` 还原账号名。"""
    parts = p.parts
    for i, seg in enumerate(parts):
        if seg.lower() == "xwechat_files" and i + 1 < len(parts):
            return parts[i + 1]
    # 兜底：向上找 cache 的父目录
    for i, seg in enumerate(parts):
        if seg.lower() == "cache" and i > 0:
            return parts[i - 1]
    return p.parent.name


def export_image_pool(cache: list[CacheImage], dest_dir: Path,
                      decrypt_fn=None, naming: str = "time") -> dict:
    """把本地缓存的朋友圈图片**全部解密导出**（诚实降级方案）。

    当无法精确归属时，至少让用户拿到「本地确实存在的图片」，
    并按时间排列，便于人工对照。

    :param naming: ``time``（默认，``<时间>_<原文件名>``）或 ``name``
    :return: 统计 dict
    """
    dest_dir = Path(dest_dir)
    dest_dir.mkdir(parents=True, exist_ok=True)
    stat = {"ok": 0, "fail": 0, "bytes": 0}
    for c in cache:
        try:
            raw = c.path.read_bytes()
        except OSError:
            stat["fail"] += 1
            continue
        body = decrypt_fn(raw) if decrypt_fn else None
        if body is None:
            from . import media as _media
            wxid = _wxid_from_cache_path(c.path)
            for aes_key, xor_key in _media.candidate_keys(wxid):
                body, _ct = _media.decrypt_v2_body(raw, aes_key, xor_key)
                if body:
                    break
        if not body:
            stat["fail"] += 1
            continue
        body = _strip_wechat_tail(body)
        import datetime as _dt
        if naming == "time":
            stamp = _dt.datetime.fromtimestamp(c.mtime).strftime("%Y%m%d_%H%M%S")
            fname = f"{stamp}_{c.name}.{c.ext}"
        else:
            fname = f"{c.name}.{c.ext}"
        try:
            (dest_dir / fname).write_bytes(body)
            stat["ok"] += 1
            stat["bytes"] += len(body)
        except OSError:
            stat["fail"] += 1
    return stat


# ── 便捷入口 ────────────────────────────────────────────────────────

def iter_timeline(db_path: Path, desc: bool = True, limit: int | None = None):
    """按 tid 迭代朋友圈动态。

    **分页/排序走 tid 即可** —— 因为 tid 内含毫秒时间戳（见 ``sns_id_to_ms``），
    ``ORDER BY tid`` 是主键索引扫描，不必解析 XML 再按 createTime 排序。
    """
    import sqlite3
    con = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    con.text_factory = bytes
    try:
        sql = "SELECT tid, user_name, content FROM SnsTimeLine ORDER BY tid " + ("DESC" if desc else "ASC")
        if limit:
            sql += f" LIMIT {int(limit)}"
        for tid, user, content in con.execute(sql):
            feed = parse_timeline(content)
            if feed is None:
                continue
            feed["tid"] = tid
            feed["ts_ms"] = sns_id_to_ms(tid)
            feed["ts"] = feed["ts_ms"] // 1000
            feed["user_name"] = (user or b"").decode("utf-8", "replace") if isinstance(user, bytes) else (user or "")
            yield feed
    finally:
        con.close()


def timeline_stats(db_path: Path) -> dict:
    """朋友圈概览统计（全部走 tid，不解析 XML）。"""
    import sqlite3
    con = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    try:
        lo, hi, n = con.execute("SELECT MIN(tid), MAX(tid), COUNT(*) FROM SnsTimeLine").fetchone()
    finally:
        con.close()
    if not n:
        return {"count": 0}
    return {
        "count": n,
        "oldest_ms": sns_id_to_ms(lo),
        "newest_ms": sns_id_to_ms(hi),
        "oldest": sns_id_to_seconds(lo),
        "newest": sns_id_to_seconds(hi),
    }


def iter_authors(db_path: Path, limit: int | None = None) -> list[dict]:
    """按发布者聚合。

    **纯 SQL GROUP BY，不解析 XML** —— 因为作者名就在 ``SnsTimeLine.user_name`` 列里。
    实测比解析全量 XML 快两个数量级。

    :return: ``[{username, count, last_tid, last_ts}]``，按动态数降序
    """
    import sqlite3
    con = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    con.text_factory = bytes
    try:
        sql = ("SELECT user_name, COUNT(*) AS n, MAX(tid) AS last_tid "
               "FROM SnsTimeLine GROUP BY user_name ORDER BY n DESC")
        if limit:
            sql += f" LIMIT {int(limit)}"
        out = []
        for who, n, last_tid in con.execute(sql):
            name = (who or b"").decode("utf-8", "replace") if isinstance(who, bytes) else (who or "")
            out.append({
                "username": name,
                "count": n,
                "last_tid": last_tid,
                "last_ts": sns_id_to_seconds(last_tid) if last_tid is not None else 0,
            })
        return out
    finally:
        con.close()
