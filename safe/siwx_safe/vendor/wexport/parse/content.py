"""消息内容解析：把微信原始 type + XML 转成可读文本与附件引用。"""

from __future__ import annotations

import html
import re
import zlib
from datetime import datetime
from pathlib import Path
from typing import Any
from xml.etree import ElementTree as ET

from ..model import Attachment, Message

_CTRL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f]")
_GROUP_PREFIX = re.compile(r"^(wxid_[0-9a-zA-Z]+|[0-9]+@chatroom):\n")

TYPE_TEXT = 1
TYPE_IMAGE = 3
TYPE_VOICE = 34
TYPE_VIDEO = 43
TYPE_CARD = 42
TYPE_EMOJI = 47
TYPE_LOCATION = 48
TYPE_APP = 49
TYPE_SYSTEM = 10000
TYPE_RECALL = 10002


def _xml(raw: str) -> ET.Element | None:
    if not raw:
        return None
    raw = raw.strip()
    if not raw.startswith("<"):
        return None
    candidates = [raw, _CTRL.sub("", raw)]
    for text in candidates:
        try:
            return ET.fromstring(text)
        except ET.ParseError:
            continue
    # 有些记录被截断，补全闭合标签再试一次
    if raw.count("<") > raw.count(">"):
        try:
            return ET.fromstring(raw + "</msg>")
        except ET.ParseError:
            return None
    return None


def _attr(el: ET.Element | None, *names: str) -> str:
    if el is None:
        return ""
    for key, value in el.attrib.items():
        low = key.lower()
        for name in names:
            if low == name.lower():
                return value or ""
    return ""


def _child(el: ET.Element | None, *names: str) -> ET.Element | None:
    if el is None:
        return None
    for child in el.iter():
        if child is el:
            continue
        for name in names:
            if child.tag.lower() == name.lower():
                return child
    return None


def _text_of(el: ET.Element | None, *names: str) -> str:
    node = _child(el, *names)
    if node is None:
        return ""
    return (node.text or "").strip()


def strip_group_prefix(raw: str) -> tuple[str, str]:
    """3.x 群聊消息正文以 "wxid_xxx:\\n" 开头，返回 (发送者, 正文)。"""
    m = _GROUP_PREFIX.match(raw)
    if m:
        return m.group(1), raw[m.end() :]
    return "", raw


def _system_text(raw: str) -> str:
    """系统消息：4.x 是 sysmsg/systemtemplate 的 XML，取 <plain>/<template> 里的可读文字。"""
    text = raw.strip()
    if not text:
        return ""
    if not text.startswith("<"):
        return html.unescape(text)
    root = _xml(text)
    if root is None:
        stripped = _CTRL.sub("", text)
        return html.unescape(re.sub(r"<[^>]+>", "", stripped)).strip()
    parts: list[str] = []
    for el in root.iter():
        tag = str(el.tag).lower()
        if tag not in ("plain", "template", "title", "text"):
            continue
        value = (el.text or "").strip()
        if value and value not in parts:
            parts.append(value)
    if parts:
        return html.unescape(" ".join(parts))
    # 全是占位符时退回去标签的原文
    return html.unescape(re.sub(r"<[^>]+>", "", text)).strip()


def parse_message(msg: Message, media_root: Path | None = None) -> Message:
    """就地填充 Message 的 text / subtype / attachments / quoted，返回同一对象。"""
    raw = msg.raw or ""
    mtype = msg.type
    # 4.x 把 appmsg 的子类型放在高 32 位（如 57<<32|49），低 32 位才是基础类型
    if mtype > 0xFFFFFFFF:
        msg.extra = dict(msg.extra or {})
        msg.extra.setdefault("app_subtype", mtype >> 32)
        msg.extra.setdefault("raw_type", mtype)
        mtype &= 0xFFFFFFFF

    if strip_group_prefix(raw)[0]:
        sender, body = strip_group_prefix(raw)
        if not msg.sender and not sender.endswith("@chatroom"):
            msg.sender = sender
        raw = body

    if mtype == TYPE_TEXT:
        msg.subtype = "text"
        msg.text = html.unescape(raw.strip())
        return msg

    if mtype == TYPE_SYSTEM:
        msg.subtype = "system"
        msg.text = _system_text(raw)
        return msg

    if mtype == TYPE_RECALL:
        msg.subtype = "system"
        msg.text = "（撤回了一条消息）"
        return msg

    # XML 被截断/损坏时保留原始内容，不丢消息
    if raw.lstrip().startswith("<") and _xml(raw) is None:
        msg.subtype = "raw"
        msg.text = html.unescape(raw.strip())
        return msg

    if mtype == TYPE_IMAGE:
        return _parse_image(msg, raw, media_root)

    if mtype == TYPE_VOICE:
        return _parse_voice(msg, raw)

    if mtype == TYPE_VIDEO:
        return _parse_video(msg, raw, media_root)

    if mtype == TYPE_CARD:
        return _parse_card(msg, raw)

    if mtype == TYPE_EMOJI:
        return _parse_emoji(msg, raw)

    if mtype == TYPE_LOCATION:
        return _parse_location(msg, raw)

    if mtype == TYPE_APP:
        return _parse_app(msg, raw, media_root)

    msg.subtype = msg.subtype or "other"
    msg.text = f"[{mtype}]"
    return msg


# ---------------------------------------------------------------- 具体类型
def _parse_image(msg: Message, raw: str, media_root: Path | None) -> Message:
    msg.subtype = "image"
    root = _xml(raw)
    img = _child(root, "img")
    md5 = _attr(img, "md5", "cdnthumbmd5")
    aeskey = _attr(img, "aeskey")
    length = _attr(img, "length")
    thumb = _attr(img, "cdnthumburl", "cdnbigimgurl")
    att = Attachment(kind="image", name=f"{md5 or 'image'}.dat", ext="jpg")
    if length.isdigit():
        att.size = int(length)
    att.encrypted = "dat-v2" if md5 else ""
    file_id = str((msg.extra or {}).get("file_id") or "")
    chat_md5 = str((msg.extra or {}).get("chat_md5") or "")
    candidates = _image_candidates(media_root, md5, file_id, chat_md5)
    img_path = (msg.extra or {}).get("img_path") or ""
    if not candidates and media_root and img_path:
        found = _resolve_img_path(media_root, str(img_path))
        if found:
            candidates = [found]
    att.source = candidates[0] if candidates else None
    att.extra = {
        "md5": md5,
        "aeskey": aeskey,
        "cdn": thumb,
        "candidates": [str(p) for p in candidates],
    }
    msg.attachments = [att]
    msg.text = "[图片]"
    return msg


def _resolve_img_path(media_root: Path, img_path: str) -> Path | None:
    """安卓库里的 imgPath 形如 THUMBNAIL_DIRPATH://th_xxx / bigimg://.../xxx.img。"""
    raw = img_path.strip()
    for prefix in ("THUMBNAIL_DIRPATH://", "bigimg://", "img://", "//"):
        if raw.startswith(prefix):
            raw = raw[len(prefix) :]
    raw = raw.lstrip("/")
    candidates = [media_root / raw, media_root / "image" / raw, media_root / "Image" / raw]
    name = Path(raw).name
    for base in (media_root, media_root / "image", media_root / "Image"):
        if base.is_dir():
            for p in base.rglob(name):
                candidates.append(p)
    for p in candidates:
        if p.is_file():
            return p
    return None


def _parse_voice(msg: Message, raw: str) -> Message:
    msg.subtype = "voice"
    root = _xml(raw)
    voice = _child(root, "voicemsg")
    length = _attr(voice, "length", "voicelength")
    duration = int(length) if length.isdigit() else 0
    if duration > 60000:  # 部分版本是毫秒
        duration //= 1000
    msg.attachments = [Attachment(kind="voice", ext="amr", duration=duration)]
    msg.text = f"[语音 {duration}s]" if duration else "[语音]"
    return msg


def _parse_video(msg: Message, raw: str, media_root: Path | None) -> Message:
    msg.subtype = "video"
    root = _xml(raw)
    video = _child(root, "videomsg")
    length = _attr(video, "playlength", "length")
    duration = int(length) if length.isdigit() else 0
    att = Attachment(kind="video", ext="mp4", duration=duration)
    att.source = _video_path(media_root) if media_root else None
    msg.attachments = [att]
    msg.text = f"[视频 {duration}s]" if duration else "[视频]"
    return msg


def _parse_card(msg: Message, raw: str) -> Message:
    msg.subtype = "card"
    root = _xml(raw)
    nick = _attr(root, "nickname")
    username = _attr(root, "username")
    msg.extra["card"] = {"nickname": nick, "username": username}
    msg.text = f"[名片] {nick}" if nick else "[名片]"
    return msg


def _parse_emoji(msg: Message, raw: str) -> Message:
    msg.subtype = "emoji"
    root = _xml(raw)
    emoji = _child(root, "emoji")
    md5 = _attr(emoji, "md5")
    msg.attachments = [Attachment(kind="emoji", ext="gif", name=f"{md5 or 'emoji'}.gif")]
    msg.text = "[表情]"
    return msg


def _parse_location(msg: Message, raw: str) -> Message:
    msg.subtype = "location"
    root = _xml(raw)
    loc = _child(root, "location")
    label = _attr(loc, "poiname") or _attr(loc, "label")
    msg.extra["location"] = {
        "x": _attr(loc, "x"),
        "y": _attr(loc, "y"),
        "label": label,
        "scale": _attr(loc, "scale"),
    }
    msg.text = f"[位置] {label}" if label else "[位置]"
    return msg


APP_FILE = 6
APP_LINK = 5
APP_QUOTE = 57
APP_MERGE = 19
APP_TRANSFER = 2000
APP_REDPACKET = 2001
APP_CHANNEL = 51
APP_MUSIC = 2003
APP_MINI = 33


def _parse_app(msg: Message, raw: str, media_root: Path | None) -> Message:
    root = _xml(raw)
    app = _child(root, "appmsg")
    app_type = _text_of(app, "type")
    title = _text_of(app, "title")
    des = _text_of(app, "des")
    url = _text_of(app, "url")
    try:
        kind = int(app_type)
    except ValueError:
        kind = 0
    if not kind:
        # 4.x 的 local_type 高 32 位就是 appmsg 子类型，XML 被截断时可兜底
        kind = int(msg.extra.get("app_subtype") or 0)
    msg.extra["app_type"] = kind

    if kind == APP_QUOTE:
        msg.subtype = "quote"
        msg.text, msg.quoted, msg.quoted_sender = _parse_quote(app, title)
        return msg

    if kind == APP_FILE:
        msg.subtype = "file"
        attach = _child(app, "appattach")
        fname = _text_of(attach, "filename") or title or "文件"
        total = _text_of(attach, "totallen")
        ext = _text_of(attach, "fileext") or Path(fname).suffix.lstrip(".")
        att = Attachment(kind="file", name=fname, ext=ext)
        if total.isdigit():
            att.size = int(total)
        att.source = _file_path(media_root, fname) if media_root else None
        msg.attachments = [att]
        msg.text = f"[文件] {fname}"
        return msg

    if kind == APP_MERGE:
        msg.subtype = "merge"
        items = _parse_merge(app)
        msg.extra["merge_items"] = items
        msg.text = f"[聊天记录] {title}" if title else "[聊天记录]"
        return msg

    if kind == APP_TRANSFER:
        msg.subtype = "transfer"
        msg.text = f"[转账] {des or title}".strip()
        return msg

    if kind == APP_REDPACKET:
        msg.subtype = "redpacket"
        msg.text = f"[红包] {title}".strip()
        return msg

    if kind == APP_CHANNEL:
        msg.subtype = "channel"
        msg.text = f"[视频号] {title}".strip()
        return msg

    if kind == APP_MUSIC:
        msg.subtype = "music"
        msg.text = f"[音乐] {title}".strip()
        return msg

    if kind == APP_MINI:
        msg.subtype = "mini"
        msg.text = f"[小程序] {title}".strip()
        return msg

    if kind == APP_LINK:
        msg.subtype = "link"
        label = title or des or url
        msg.text = f"[链接] {label} {url}".strip()
        return msg

    msg.subtype = "app"
    msg.text = f"[应用消息] {title or des or url}".strip()
    return msg


def _parse_quote(app: ET.Element | None, title: str) -> tuple[str, str, str]:
    refer = _child(app, "refermsg")
    display = _text_of(refer, "displayname")
    content = _text_of(refer, "content")
    chatusr = _text_of(refer, "chatusr")
    quoted = _readable_inner(content)
    return title or "[引用]", quoted, display or chatusr


def _readable_inner(content: str) -> str:
    """引用里的 content 往往又是一段 XML，尽量取标题/正文。"""
    if not content:
        return ""
    if not content.strip().startswith("<"):
        return content.strip()
    root = _xml(content)
    if root is None:
        return _CTRL.sub("", content).strip()
    app = _child(root, "appmsg")
    if app is not None:
        title = _text_of(app, "title")
        if title:
            return title
        des = _text_of(app, "des")
        if des:
            return des
    img = _child(root, "img")
    if img is not None:
        return "[图片]"
    return _CTRL.sub("", content).strip()[:200]


def _parse_merge(app: ET.Element | None) -> list[dict[str, str]]:
    item = _child(app, "recorditem")
    if item is None or not item.text:
        return []
    inner = _xml(item.text)
    if inner is None:
        return []
    out: list[dict[str, str]] = []
    for data in inner.iter("dataitem"):
        title = (data.findtext("title") or "").strip()
        desc = (data.findtext("desc") or "").strip()
        name = (data.findtext("sourcename") or "").strip()
        out.append({"title": title, "desc": desc, "sender": name})
    return out


# ---------------------------------------------------------------- 附件定位
def _md5_dirs(md5: str) -> list[Path]:
    if len(md5) != 32:
        return []
    return [
        Path(md5[:2]) / md5[2:4],
        Path(md5),
        Path(md5[:2]),
    ]


def _image_candidates(
    media_root: Path | None, md5: str, file_id: str = "", chat_md5: str = ""
) -> list[Path]:
    """按画质从高到低列出同一张图片的所有本地容器：_h（原图）/ .dat / _t（缩略）。"""
    if not media_root:
        return []
    out: list[Path] = []

    def add(path: Path) -> None:
        if path.is_file() and path not in out:
            out.append(path)

    attach = media_root / "attach"
    ids = [x for x in (file_id, md5) if x and len(x) == 32]
    if chat_md5 and (attach / chat_md5).is_dir():
        base = attach / chat_md5
        for folder in sorted(base.iterdir()):
            img = folder / "Img"
            if not img.is_dir():
                continue
            for fid in ids:
                for suffix in ("_h.dat", ".dat", "_t.dat"):
                    add(img / (fid + suffix))
                for p in sorted(img.glob(f"{fid}*.dat")):
                    add(p)
    if not out and attach.is_dir():
        for fid in ids:
            for pattern in (
                f"*/*/Img/{fid}_h.dat",
                f"*/*/Img/{fid}.dat",
                f"*/*/Img/{fid}_t.dat",
                f"*/*/Img/{fid}*.dat",
                f"*/{fid}*.dat",
            ):
                for p in attach.glob(pattern):
                    add(p)
            if md5:
                for prefix in (fid, fid[:2]):
                    base = attach / prefix
                    if base.is_dir():
                        for p in base.rglob(f"{fid}*.dat"):
                            add(p)
    if not out:
        # PC 3.x：FileStorage/Image/<yyyy-mm>/<md5>.jpg，安卓：media_root 下同样结构
        for name in ("Image", "image"):
            image = media_root / name
            if not image.is_dir():
                continue
            for fid in ids:
                for ext in ("jpg", "jpeg", "png", "gif", "webp", "bmp"):
                    for p in image.rglob(f"{fid}*.{ext}"):
                        add(p)
    return out


def _image_path(
    media_root: Path | None, md5: str, file_id: str = "", chat_md5: str = ""
) -> Path | None:
    candidates = _image_candidates(media_root, md5, file_id, chat_md5)
    return candidates[0] if candidates else None


def _video_path(media_root: Path | None) -> Path | None:
    if not media_root:
        return None
    for name in ("video", "Video"):
        folder = media_root / name
        if folder.is_dir():
            for p in folder.glob("*.mp4"):
                return p
    return None


def _file_path(media_root: Path | None, name: str) -> Path | None:
    if not media_root or not name:
        return None
    for folder_name in ("file", "File"):
        base = media_root / folder_name
        if not base.is_dir():
            continue
        for p in base.rglob(name):
            return p
    return None


def message_time(ts: int) -> datetime:
    return datetime.fromtimestamp(ts) if ts else datetime.fromtimestamp(0)


def decode_content_blob(blob: bytes) -> str:
    """3.x 的 CompressContent 是 zlib 压缩的 XML。"""
    if not blob:
        return ""
    try:
        return zlib.decompress(blob).decode("utf-8", "replace")
    except zlib.error:
        try:
            return blob.decode("utf-8", "replace")
        except UnicodeDecodeError:
            return ""
