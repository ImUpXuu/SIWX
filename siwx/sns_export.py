"""朋友圈导出。

**独立于 `exporter.py`** —— 聊天导出链路有「零插件模式下行为字节级一致」的红线，
朋友圈的数据形状（动态 + 互动 + CDN 媒体）与聊天消息差异很大，因此单独成模块，
只复用 `_safe_name` 与原子写模式，不触碰聊天导出路径。

支持格式：``json`` / ``markdown`` / ``txt`` / ``html``

媒体（可选）：按需从 CDN 下载并 ISAAC64 解密，落盘为
``media/<postId>_<index>.<ext>``。
"""
from __future__ import annotations

import html as _html
import json
import logging
import time
from pathlib import Path

from . import sns
from . import sns_cdn
from .exporter import _safe_name

# 媒体下载失败要有地方查（logs/siwx.log，“运行日志”页会 tail 它）
_file_log = logging.getLogger("siwx")

FORMATS = ("json", "markdown", "txt", "html")

_EXT = {"json": "json", "markdown": "md", "txt": "txt", "html": "html"}

# 媒体并发上限。微信 CDN 对高并发不友好，实测 5 路稳定；
# 调太高可能被限流（表现为大量超时/失败），调太低则导出很慢。
DEFAULT_CONCURRENCY = 5
MAX_CONCURRENCY = 16


def clamp_concurrency(v) -> int:
    """把并发数夹到 [1, MAX_CONCURRENCY]；非法值回退默认。"""
    try:
        n = int(v)
    except (TypeError, ValueError):
        return DEFAULT_CONCURRENCY
    return max(1, min(n, MAX_CONCURRENCY))


def _ts(sec: int) -> str:
    try:
        return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(sec))
    except (OSError, ValueError):
        return str(sec)


def _iter_feeds(db_path, usernames=None, start=None, end=None, desc=True,
                keyword=None):
    """按条件迭代动态（全部走 tid 索引，不解析 XML 排序）。"""
    users = set(usernames) if usernames else None
    kw = (keyword or "").strip().lower()
    for feed in sns.iter_timeline(db_path, desc=desc):
        ts = feed.get("ts") or 0
        if start and ts < start:
            continue
        if end and ts > end:
            continue
        if users and feed.get("user_name") not in users:
            continue
        # 与 Web 搜索口径一致：标题/歌手/视频号昵称等卡片文本也算命中
        if kw and kw not in sns.search_text(feed):
            continue
        yield feed


def _card_to_dict(card: dict | None) -> dict | None:
    """卡片对外形状 —— 统一复用 ``sns.public_card``（API 与导出同一份）。

    保留一个薄封装是为了让导出侧的调用点集中，将来若要给导出加字段只改这里。
    """
    return sns.public_card(card)


def _feed_to_dict(feed: dict, media_map: dict | None = None) -> dict:
    d = {
        "tid": feed.get("tid"),
        "id": feed.get("id"),
        "time": feed.get("ts"),
        "timeText": _ts(feed.get("ts") or 0),
        "author": feed.get("user_name") or feed.get("username"),
        "text": feed.get("content_desc") or "",
        "kind": feed.get("content_kind"),
        "card": _card_to_dict(feed.get("card")),
        "private": bool(feed.get("private")),
        "location": feed.get("location"),
        "likes": [{"name": x.get("nickname") or x.get("username"),
                   "username": x.get("username"),
                   "time": x.get("create_time")} for x in feed.get("likes", [])],
        "comments": [{"name": x.get("nickname") or x.get("username"),
                      "username": x.get("username"),
                      "text": x.get("content") or "",
                      "time": x.get("create_time"),
                      "replyTo": x.get("ref_username") or "",
                      "deleted": bool(x.get("deleted")),
                      "emojis": [{"md5": e.get("md5"), "url": e.get("url"),
                                  "width": e.get("width"), "height": e.get("height")}
                                 for e in (x.get("emojis") or [])],
                      "images": [{"md5": i.get("md5"), "url": i.get("url")}
                                 for i in (x.get("images") or [])]}
                     for x in feed.get("comments", [])],
        "media": [],
    }
    for i, m in enumerate(feed.get("medias", [])):
        item = {
            "index": i,
            "type": "video" if m.get("type") == 6 else "image",
            "width": m.get("width"), "height": m.get("height"),
            "url": m.get("url"), "thumb": m.get("thumb_url"),
            "md5": m.get("md5"),
        }
        if media_map and (feed.get("tid"), i, "") in media_map:
            item["localFile"] = media_map[(feed.get("tid"), i, "")]
        if m.get("live_photo"):
            lp = m["live_photo"]
            live = {"md5": lp.get("md5"), "url": lp.get("url"),
                    "duration": lp.get("video_duration"),
                    "stillMs": lp.get("live_still_ms"),
                    "width": lp.get("width"), "height": lp.get("height")}
            if media_map and (feed.get("tid"), i, "_live") in media_map:
                live["localFile"] = media_map[(feed.get("tid"), i, "_live")]
            item["livePhoto"] = live
        d["media"].append(item)
    return d


# ── 卡片渲染（txt / markdown / html 共用）────────────────────────────

def _dur(sec) -> str:
    """秒 → ``m:ss``。"""
    try:
        sec = int(sec)
    except (TypeError, ValueError):
        return ""
    if sec <= 0:
        return ""
    return f"{sec // 60}:{sec % 60:02d}"


def _card_lines(card: dict | None) -> list[str]:
    """卡片 → 纯文本行（空行会被过滤）。"""
    if not card:
        return []
    kind = card.get("kind")
    if kind == "music":
        m = card.get("music") or {}
        head = "🎵 " + (m.get("album") or card.get("title") or "音乐")
        who = m.get("singer") or card.get("description") or ""
        if who:
            head += f" — {who}"
        d = _dur((m.get("duration_ms") or 0) // 1000)
        if d:
            head += f"（{d}）"
        return [head, card.get("url") or ""]
    if kind == "finder":
        f = card.get("finder") or {}
        head = f"📹 视频号 @{f.get('nickname') or ''}"
        d = _dur(card.get("duration"))
        if d:
            head += f"（{d}）"
        return [head, card.get("description") or ""]
    if kind == "live":
        lv = card.get("live") or {}
        return [f"📺 直播 @{lv.get('nickname') or ''}", lv.get("desc") or ""]
    if kind == "note":
        nt = card.get("note") or {}
        return ["📝 笔记", nt.get("text") or card.get("title") or ""]
    # link
    src = card.get("source") or ""
    head = "🔗 " + (card.get("title") or "")
    if src:
        head += f"（{src}）"
    return [head, card.get("description") or "", card.get("url") or ""]


def _html_card(card: dict | None) -> str:
    """卡片 → HTML 片段。找不到 url 时降级为纯文本，绝不生成死链。"""
    if not card:
        return ""

    def _a(url, text):
        url = str(url or "")
        if not url:
            return _html.escape(str(text or ""))
        return f'<a href="{_html.escape(url, quote=True)}" target="_blank" rel="noreferrer">{_html.escape(str(text or ""))}</a>'

    kind = card.get("kind")
    cover = str(card.get("cover") or "")
    cov = (f'<img class="card-cover" loading="lazy" src="{_html.escape(cover, quote=True)}" alt="">'
           if cover else "")

    if kind == "music":
        m = card.get("music") or {}
        d = _dur((m.get("duration_ms") or 0) // 1000)
        title = m.get("album") or card.get("title") or "音乐"
        return (f'<div class="card card--music">{cov}<div class="card-body">'
                f'<div class="card-tag">🎵 音乐</div>'
                f'<div class="card-title">{_a(card.get("url"), title)}</div>'
                f'<div class="card-sub">{_html.escape(m.get("singer") or card.get("description") or "")}'
                f'{" · " + d if d else ""}</div></div></div>')

    if kind == "finder":
        f = card.get("finder") or {}
        d = _dur(card.get("duration"))
        sub = " · ".join(x for x in [_html.escape(f.get("nickname") or ""), d] if x)
        return (f'<div class="card card--finder">{cov}<div class="card-body">'
                f'<div class="card-tag">📹 视频号</div>'
                f'<div class="card-title">{_html.escape(f.get("nickname") or "")}</div>'
                f'<div class="card-sub">{sub}</div>'
                f'<div class="card-desc">{_html.escape(card.get("description") or "")}</div>'
                f'</div></div>')

    if kind == "live":
        lv = card.get("live") or {}
        return (f'<div class="card card--live">{cov}<div class="card-body">'
                f'<div class="card-tag">📺 视频号直播</div>'
                f'<div class="card-title">{_html.escape(lv.get("nickname") or "")}</div>'
                f'<div class="card-desc">{_html.escape(lv.get("desc") or "")}</div>'
                f'</div></div>')

    if kind == "note":
        nt = card.get("note") or {}
        return ('<div class="card card--note"><div class="card-body">'
                '<div class="card-tag">📝 笔记</div>'
                f'<div class="card-desc">{_html.escape(nt.get("text") or card.get("title") or "")}</div>'
                '</div></div>')
    # link（封面已在媒体网格里出现过，这里不重复放图）
    sub = card.get("source") or ""
    return ('<div class="card card--link"><div class="card-body">'
            '<div class="card-tag">🔗 链接</div>'
            f'<div class="card-title">{_a(card.get("url"), card.get("title") or card.get("url"))}</div>'
            f'<div class="card-sub">{_html.escape(sub)}</div>'
            f'<div class="card-desc">{_html.escape(card.get("description") or "")}</div>'
            '</div></div>')


# ── 媒体下载 ────────────────────────────────────────────────────────

def download_media(feeds, media_dir: Path, cache_dir=None,
                   want_images=True, want_videos=True, want_livephotos=True,
                   progress=None, concurrency: int = DEFAULT_CONCURRENCY) -> dict:
    """并发下载动态里的媒体，返回 ``{(tid, index): 相对路径}`` 与统计。

    媒体文件命名：``<postId>_<index>.<ext>``，实况照片加 ``_live`` 后缀。
    """
    from concurrent.futures import ThreadPoolExecutor, as_completed

    media_dir.mkdir(parents=True, exist_ok=True)
    tasks = []
    for feed in feeds:
        for i, m in enumerate(feed.get("medias", [])):
            if not m.get("url"):
                continue
            is_video = m.get("type") == 6
            if is_video and not want_videos:
                continue
            if not is_video and not want_images:
                continue
            tasks.append((feed.get("tid"), i, m, ""))
            # 实况照片：主图之外再下一段短视频（key 在 <enc key>）
            lp = m.get("live_photo")
            if lp and lp.get("url") and want_livephotos:
                tasks.append((feed.get("tid"), i, lp, "_live"))

    media_map: dict = {}
    stat = {"ok": 0, "fail": 0, "bytes": 0, "total": len(tasks),
            "live_ok": 0, "live_fail": 0, "reasons": {}, "fail_samples": []}
    if not tasks:
        return {"map": media_map, **stat}

    def _one(task):
        tid, idx, m, suffix = task
        r = sns_cdn.fetch_media(m.get("url"), key=m.get("key"),
                                token=m.get("token"), cache_dir=cache_dir)
        if not r["ok"]:
            return tid, idx, suffix, None, 0, r.get("error"), r.get("reason")
        data = sns_cdn.strip_wechat_tail(r["data"])
        name = f"{tid}_{idx}{suffix}.{r['ext']}"
        try:
            (media_dir / name).write_bytes(data)
        except OSError as e:
            return tid, idx, suffix, None, 0, f"write: {e}", "write-error"
        return tid, idx, suffix, f"media/{name}", len(data), None, None

    done = 0
    workers = clamp_concurrency(concurrency)
    with ThreadPoolExecutor(max_workers=workers) as ex:
        futs = [ex.submit(_one, t) for t in tasks]
        for fu in as_completed(futs):
            tid, idx, suffix, rel, nbytes, err, reason = fu.result()
            done += 1
            if rel:
                media_map[(tid, idx, suffix)] = rel
                stat["ok"] += 1
                stat["bytes"] += nbytes
                if suffix == "_live":
                    stat["live_ok"] += 1
            else:
                stat["fail"] += 1
                if suffix == "_live":
                    stat["live_fail"] += 1
                # 失败原因要有地方看：明细进 stat，前 30 条进日志（避免刷爆）
                key = reason or "unknown"
                stat["reasons"][key] = stat["reasons"].get(key, 0) + 1
                if len(stat["fail_samples"]) < 30:
                    stat["fail_samples"].append(
                        {"tid": tid, "index": idx, "live": suffix == "_live",
                         "reason": key, "error": err})
                    _file_log.warning("[sns-media] 导出下载失败 tid=%s idx=%s live=%s "
                                      "reason=%s err=%s", tid, idx, suffix == "_live", key, err)
            if progress:
                try:
                    progress(done, stat["total"], f"媒体 {done}/{stat['total']}")
                except Exception:
                    pass
    if stat["fail"]:
        _file_log.warning("[sns-media] 导出媒体失败 %d/%d，原因分布 %s",
                          stat["fail"], stat["total"], stat["reasons"])
    return {"map": media_map, **stat}


# ── 各格式写出 ──────────────────────────────────────────────────────

def _write_json(path: Path, feeds, media_map, meta: dict) -> int:
    """增量写 JSON，避免把所有动态一次性堆在内存里。"""
    with path.open("w", encoding="utf-8") as f:
        f.write("{\n  \"meta\": ")
        json.dump(meta, f, ensure_ascii=False, indent=2)
        f.write(",\n  \"posts\": [\n")
        n = 0
        for feed in feeds:
            if n:
                f.write(",\n")
            json.dump(_feed_to_dict(feed, media_map), f, ensure_ascii=False)
            n += 1
        f.write("\n  ]\n}\n")
    return n


def _write_txt(path: Path, feeds, media_map, meta=None) -> int:
    n = 0
    with path.open("w", encoding="utf-8") as f:
        for feed in feeds:
            d = _feed_to_dict(feed, media_map)
            f.write(f"[{d['timeText']}] {d['author']}\n")
            if d["text"]:
                f.write(d["text"] + "\n")
            for line in _card_lines(d.get("card")):
                if line:
                    f.write("  " + line + "\n")
            for m in d["media"]:
                src = m.get("localFile") or m.get("url")
                f.write(f"  <{m['type']}> {src}\n")
                if m.get("livePhoto"):
                    f.write(f"  <livephoto> {m['livePhoto'].get('localFile') or m['livePhoto'].get('url')}\n")
            if d["location"]:
                loc = d["location"]
                f.write("  📍 " + " ".join(
                    x for x in [loc.get("name") or "", loc.get("address") or ""] if x) + "\n")
            if d["likes"]:
                f.write("  赞：" + "、".join(x["name"] or "" for x in d["likes"]) + "\n")
            for c in d["comments"]:
                f.write(f"  {c['name']}：{c['text']}\n")
                for e in (c.get("emojis") or []):
                    f.write(f"    <表情> {e.get('url') or ''}\n")
                for im in (c.get("images") or []):
                    f.write(f"    <评论图> {im.get('url') or ''}\n")
            f.write("\n")
            n += 1
    return n


def _write_markdown(path: Path, feeds, media_map, meta=None) -> int:
    n = 0
    with path.open("w", encoding="utf-8") as f:
        f.write("# 朋友圈导出\n\n")
        for feed in feeds:
            d = _feed_to_dict(feed, media_map)
            f.write(f"## {d['timeText']} · {d['author']}\n\n")
            if d["text"]:
                f.write(d["text"] + "\n\n")
            card = d.get("card") or {}
            lines = [ln for ln in _card_lines(card) if ln]
            if lines:
                # 卡片用引用块呈现；有链接时写成 Markdown 链接（可点）
                kind, url = card.get("kind"), card.get("url") or ""
                head, rest = lines[0], lines[1:]
                if kind == "link" and url:
                    head = f"🔗 [{card.get('title') or url}]({url})"
                    rest = [x for x in rest if x != url]
                elif url:
                    rest = [x for x in rest if x != url]
                    head += f"（[打开]({url})）" if kind == "music" else ""
                f.write(f"> {head}\n")
                for ln in rest:
                    f.write(f"> {ln}\n")
                if kind in ("finder", "live") and card.get("cover"):
                    f.write(f"> ![封面]({card['cover']})\n")
                f.write("\n")
            for m in d["media"]:
                src = m.get("localFile") or m.get("url")
                f.write(f"![{m['type']}]({src})\n\n" if m["type"] == "image"
                        else f"[视频]({src})\n\n")
                lp = m.get("livePhoto")
                if lp:
                    lsrc = lp.get("localFile") or lp.get("url")
                    f.write(f"[实况照片]({lsrc})\n\n")
            if d["location"]:
                loc = d["location"]
                f.write(f"📍 {loc.get('name') or ''} {loc.get('address') or ''}\n\n")
            if d["likes"]:
                f.write("**赞**：" + "、".join(x["name"] or "" for x in d["likes"]) + "\n\n")
            for c in d["comments"]:
                who = c["name"] or ""
                emo = "".join(f"![{e['md5'][:8]}]({e.get('url') or ''})" for e in (c.get("emojis") or []))
                imgs = "".join(f"![评论图]({im.get('url') or ''})" for im in (c.get("images") or []))
                f.write(f"- **{who}**：{c['text']}{emo}{imgs}\n")
            if d["comments"]:
                f.write("\n")
            f.write("---\n\n")
            n += 1
    return n


_HTML_HEAD = """<!DOCTYPE html>
<html lang="zh-CN"><head><meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>朋友圈导出</title>
<style>
body{max-width:760px;margin:0 auto;padding:24px 16px;background:#f5f5f5;
 font:14px/1.7 -apple-system,"Segoe UI","Microsoft YaHei",sans-serif;color:#111}
.post{background:#fff;border:1px solid #e5e5e5;border-radius:10px;
 padding:16px;margin:0 0 14px}
.head{display:flex;justify-content:space-between;align-items:baseline;margin-bottom:8px}
.who{font-weight:700}.time{color:#888;font-size:12px}
.text{white-space:pre-wrap;word-break:break-word}
.grid{display:grid;grid-template-columns:repeat(3,1fr);gap:6px;margin-top:10px}
.grid img,.grid video{width:100%;aspect-ratio:1;object-fit:cover;border-radius:6px;background:#eee}
.act{margin-top:10px;color:#888;font-size:12px}
.loc{margin-top:6px;color:#576b95;font-size:12px}
.card{display:flex;gap:10px;margin-top:10px;padding:10px;border:1px solid #e5e5e5;
 border-radius:8px;background:#fafafa}
.card-cover{width:96px;height:96px;object-fit:cover;border-radius:6px;background:#eee;flex:none}
.card-body{min-width:0}
.card-tag{color:#888;font-size:12px}
.card-title{font-weight:600;margin-top:2px;word-break:break-word}
.card-title a{color:#576b95;text-decoration:none}
.card-sub{color:#888;font-size:12px;margin-top:2px}
.card-desc{color:#444;font-size:13px;margin-top:4px;white-space:pre-wrap;word-break:break-word}
.cm-img{max-width:120px;max-height:120px;border-radius:4px;vertical-align:middle;margin-left:4px}
.cm{margin-top:8px;border-top:1px dashed #e5e5e5;padding-top:8px;font-size:13px}
.cm b{color:#576b95}
.cm .e{width:20px;height:20px;vertical-align:-4px;margin-left:2px}
</style></head><body>
"""


def _write_html(path: Path, feeds, media_map, meta=None) -> int:
    n = 0
    with path.open("w", encoding="utf-8") as f:
        f.write(_HTML_HEAD)
        for feed in feeds:
            d = _feed_to_dict(feed, media_map)
            f.write('<article class="post">')
            f.write(f'<div class="head"><span class="who">{_html.escape(str(d["author"] or ""))}</span>'
                    f'<span class="time">{_html.escape(d["timeText"])}</span></div>')
            if d["text"]:
                f.write(f'<div class="text">{_html.escape(d["text"])}</div>')
            f.write(_html_card(d.get("card")))
            if d["media"]:
                f.write('<div class="grid">')
                for m in d["media"]:
                    src = _html.escape(str(m.get("localFile") or m.get("url") or ""))
                    if not src:
                        continue
                    if m["type"] == "video":
                        f.write(f'<video controls preload="metadata" src="{src}"></video>')
                    else:
                        f.write(f'<img loading="lazy" src="{src}" alt="">')
                    lp = m.get("livePhoto")
                    if lp:
                        lsrc = _html.escape(str(lp.get("localFile") or lp.get("url") or ""))
                        if lsrc:
                            f.write(f'<video controls preload="metadata" '
                                    f'title="实况照片" src="{lsrc}"></video>')
                f.write("</div>")
            f.write(f'<div class="act">赞 {len(d["likes"])} · 评论 {len(d["comments"])}</div>')
            if d["location"]:
                loc = d["location"]
                f.write('<div class="loc">📍 {}{}</div>'.format(
                    _html.escape(str(loc.get("name") or "")),
                    (" " + _html.escape(str(loc.get("address") or ""))) if loc.get("address") else ""))
            if d["comments"]:
                f.write('<div class="cm">')
                for c in d["comments"]:
                    emo = "".join(
                        f'<img class="e" loading="lazy" src="{_html.escape(str(e.get("url") or ""))}" alt="">'
                        for e in (c.get("emojis") or []) if e.get("url"))
                    cimg = "".join(
                        f'<img class="cm-img" loading="lazy" src="{_html.escape(str(im.get("url") or ""))}" alt="评论图">'
                        for im in (c.get("images") or []) if im.get("url"))
                    f.write(f'<div><b>{_html.escape(str(c["name"] or ""))}</b>：'
                            f'{_html.escape(str(c["text"] or ""))}{emo}{cimg}</div>')
                f.write("</div>")
            f.write("</article>\n")
            n += 1
        f.write("</body></html>\n")
    return n


_WRITERS = {"json": _write_json, "markdown": _write_markdown,
            "txt": _write_txt, "html": _write_html}


# ── 主入口 ──────────────────────────────────────────────────────────

def run_sns_export(db_path: Path, account: str, fmt: str = "json",
                   export_root: Path | None = None,
                   usernames=None, start=None, end=None,
                   want_media: bool = False, want_images: bool = True,
                   want_videos: bool = True,                    want_livephotos: bool = True,
                   cache_dir=None,
                   progress=None, limit: int | None = None,
                   keyword: str | None = None,
                   concurrency: int = DEFAULT_CONCURRENCY) -> dict:
    """导出朋友圈动态。

    :param fmt: ``json`` / ``markdown`` / ``txt`` / ``html``
    :return: 结果 dict（含 file / count / media 统计）
    """
    if fmt not in _WRITERS:
        return {"ok": False, "error": f"不支持的格式: {fmt}"}

    t0 = time.time()
    feeds = list(_iter_feeds(db_path, usernames, start, end, keyword=keyword))
    if limit:
        feeds = feeds[:limit]
    if not feeds:
        return {"ok": False, "error": "没有符合条件的动态"}

    stamp = time.strftime("%Y%m%d_%H%M%S")
    folder = _safe_name(f"{account}_朋友圈_{stamp}")
    if export_root is None:
        from . import paths as _paths
        export_root = _paths.exports_root()
    root = Path(export_root)
    out_dir = root / folder
    out_dir.mkdir(parents=True, exist_ok=True)

    media_stat = {"ok": 0, "fail": 0, "bytes": 0, "total": 0,
                  "live_ok": 0, "live_fail": 0}
    media_map = {}
    if want_media:
        got = download_media(feeds, out_dir / "media", cache_dir=cache_dir,
                             want_images=want_images, want_videos=want_videos,
                             want_livephotos=want_livephotos,
                             progress=progress,
                             concurrency=clamp_concurrency(concurrency))
        media_map = got.pop("map")
        media_stat = got

    meta = {
        "account": account, "exportedAt": _ts(int(time.time())),
        "count": len(feeds), "format": fmt,
        "range": {"start": feeds[-1].get("ts"), "end": feeds[0].get("ts")},
        "media": media_stat,
    }
    ext = _EXT[fmt]
    path = out_dir / f"{_safe_name(account)}_朋友圈.{ext}"
    n = _WRITERS[fmt](path, feeds, media_map, meta)

    return {
        "ok": True, "file": str(path), "export_dir": str(out_dir),
        "count": n, "media": media_stat,
        "duration_ms": int((time.time() - t0) * 1000),
    }
