"""离线导出目录格式转换 —— 已有 JSON 导出 → Markdown / HTML。

对一次"仅 JSON"的导出目录就地补生成其他格式，不需要重跑解密：
每个会话目录 = manifest.json + <名>_<时间戳>.json + media/ + avatars/。
会话 JSON 的消息 dict 与导出管线 `_enrich_row` 产出同构
（含 rawContent/quote/link/record/mediaFile/md5/bubbleMd5），因此：
- Markdown：直接接 `export_stream.stream_export_md`（TXT/CSV 同理可接）；
- HTML：直接接 `html_template.stream_html_head/_msg/_tail` 流式写出，
  faces/maps 从消息重提取，头像按 avatars/md5(username).jpg 确定性规则
  重建映射，渲染器（templates/default/renderer.js）与原生导出完全同一条。

口径约束（docs/deferred-improvements-2026-10-11.md §三/§四）：
- 逐会话 json.load、写完即释放（防大会话 OOM；不做增量数组解析）；
- manifest 沿用原 parse/media 段，只追加 converted 段（parse 统计与
  媒体失败分布无法从 JSON 重算）；
- mediaFile 存在性校验，缺失计入结果提示（目录被搬丢/删改时防全裂图）；
- 位置瓦片与未下载表情的在线回退与原生导出同口径（离线静默退化占位）。

用法：
    python -m siwx.json_export_convert <export_dir> [--formats md,html]
                                        [--only 名称子串] [--template default]
                                        [--overwrite]
"""
import argparse
import hashlib
import sys
from datetime import datetime
from pathlib import Path

_MD_EXTS = ("md",)
_HTML_EXTS = ("html",)


def _session_dirs(root: Path):
    """导出根目录下的会话目录（含 manifest.json + 恰好一个会话 JSON）。"""
    for d in sorted(root.iterdir()):
        if not d.is_dir():
            continue
        jsons = [p for p in d.glob("*.json") if p.name != "manifest.json"]
        if not (d / "manifest.json").is_file() or len(jsons) != 1:
            continue
        yield d, jsons[0]


def _avatar_map(session_dir: Path, usernames) -> dict:
    """username → avatars/<md5(username).jpg>（存在才映射，与导出侧同规则）。"""
    av = session_dir / "avatars"
    if not av.is_dir():
        return {}
    out = {}
    for un in usernames:
        fn = hashlib.md5(un.encode()).hexdigest() + ".jpg"
        if (av / fn).is_file():
            out[un] = f"avatars/{fn}"
    return out


def _check_media(session_dir: Path, msgs) -> tuple[int, int]:
    """校验 mediaFile 存在性；返回 (引用数, 缺失数)。"""
    refs = missing = 0
    for m in msgs:
        mf = m.get("mediaFile")
        if not mf:
            continue
        refs += 1
        if not (session_dir / mf).is_file():
            missing += 1
    return refs, missing


def _convert_md(session: dict, msgs: list, out: Path) -> None:
    from siwx.export_stream import stream_export_md
    stream_export_md(out, session, iter(msgs))


def _convert_html(session: dict, msgs: list, avatar_map: dict,
                  out: Path, template: str | None) -> None:
    from siwx import wx_faces, wx_maps
    from siwx.html_template import (
        stream_html_head, stream_html_msg, stream_html_tail,
    )

    session = dict(session)
    session["messageCount"] = len(msgs)
    senders = sorted({m["senderUsername"] for m in msgs if m.get("senderUsername")})
    members = [{"id": un, "name": next(
                    (m["senderDisplayName"] or un for m in msgs
                     if m.get("senderUsername") == un), un),
                "avatar": avatar_map.get(un, "")}
               for un in senders]

    faces_used: set = set()
    map_keys: set = set()
    count = 0
    with open(out, "w", encoding="utf-8") as f:
        stream_html_head(f, session, members, avatar_map, template=template)
        for m in msgs:
            faces_used |= wx_faces.used_from_message(m)
            map_keys |= wx_maps.used_from_message(m)
            stream_html_msg(f, m, first=(count == 0))
            count += 1
        stream_html_tail(f, count, session["displayName"],
                         faces=wx_faces.datauris(faces_used),
                         maps=wx_maps.datauris(map_keys),
                         template=template)


def _update_manifest(session_dir: Path, added: dict) -> None:
    """manifest 追加 converted 段；parse/media 段原样保留（无法从 JSON 重算）。"""
    import json
    mf_path = session_dir / "manifest.json"
    try:
        mf = {}
        if mf_path.is_file():
            mf = json.loads(mf_path.read_text(encoding="utf-8"))
        conv = mf.setdefault("converted", {})
        conv.update(added)
        conv["updated_at"] = datetime.now().isoformat(timespec="seconds")
        mf_path.write_text(json.dumps(mf, ensure_ascii=False, indent=1),
                           encoding="utf-8")
    except OSError as e:
        print(f"  [warn] manifest 更新失败: {e}")


def convert_dir_report(root: Path, formats, only: str | None = None,
                       template: str | None = None, overwrite: bool = False,
                       log=print, progress=None, should_cancel=None) -> dict:
    """convert_dir 的结构化版本：供 server 的 mode=convert（方案 A）复用。

    progress(done, total, msg)；should_cancel 为真时优雅停止（已完成会话
    保留），报告带 cancelled=True。返回
    {"total","ok","skipped","failed","missing_media","cancelled"}。"""
    import json

    if not root.is_dir():
        log(f"导出目录不存在: {root}")
        return {"total": 0, "ok": 0, "skipped": 0, "failed": 0,
                "missing_media": 0, "cancelled": False, "error": "目录不存在"}
    want_md = "md" in formats
    want_html = "html" in formats
    if not (want_md or want_html):
        log("--formats 里没有任何受支持的格式（md/html）")
        return {"total": 0, "ok": 0, "skipped": 0, "failed": 0,
                "missing_media": 0, "cancelled": False,
                "error": "没有受支持的格式"}

    total = ok = skipped = failed = 0
    missing_total = 0
    cancelled = False
    dirs = list(_session_dirs(root))
    for i, (d, jf) in enumerate(dirs, 1):
        if should_cancel is not None and should_cancel():
            cancelled = True
            log(f"已取消（处理到 {i - 1}/{len(dirs)} 个会话，已完成部分保留）")
            break
        if progress:
            progress(i - 1, len(dirs), d.name)
        if only and only not in d.name:
            continue
        total += 1
        try:
            data = json.loads(jf.read_text(encoding="utf-8"))
        except (OSError, ValueError) as e:
            log(f"[fail] {d.name}: 会话 JSON 读取失败: {e}")
            failed += 1
            continue
        session, msgs = data.get("session") or {}, data.get("messages") or []
        base = jf.stem  # 同名位置：与会话 JSON 同基名
        refs, missing = _check_media(d, msgs)
        missing_total += missing
        added = {"source_json": jf.name, "message_count": len(msgs),
                 "media_refs": refs, "media_missing": missing, "formats": []}
        try:
            if want_md:
                out = d / f"{base}.md"
                if overwrite or not out.exists():
                    _convert_md(session, msgs, out)
                    added["formats"].append("md")
            if want_html:
                out = d / f"{base}.html"
                if overwrite or not out.exists():
                    _convert_html(session, msgs, _avatar_map(d, set(
                        m.get("senderUsername") or "" for m in msgs) | {
                        session.get("ownerId") or ""}),
                        out, template)
                    added["formats"].append("html")
        except Exception as e:  # noqa: BLE001 单会话失败不拖垮整批
            log(f"[fail] {d.name}: {type(e).__name__}: {e}")
            failed += 1
            continue
        if added["formats"]:
            _update_manifest(d, added)
            ok += 1
        else:
            skipped += 1
        if i % 50 == 0:
            log(f"…已处理 {i} 个会话（成功 {ok} / 跳过 {skipped} / 失败 {failed}）")

    log(f"\n完成：会话 {total}，补生成 {ok}，已有跳过 {skipped}，失败 {failed}；"
        f"媒体引用缺失 {missing_total} 处"
        + ("（导出时未下载成功或目录被改动）" if missing_total else ""))
    if failed:
        log("失败会话见上方 [fail] 行，可修复后加 --overwrite 重跑。")
    return {"total": total, "ok": ok, "skipped": skipped, "failed": failed,
            "missing_media": missing_total, "cancelled": cancelled}


def convert_dir(root: Path, formats, only: str | None = None,
                template: str | None = None, overwrite: bool = False) -> int:
    """CLI 入口：返回 0（无失败）/ 2（有失败）。结构化结果走 convert_dir_report。"""
    r = convert_dir_report(root, formats, only=only, template=template,
                           overwrite=overwrite)
    return 0 if not r["failed"] else 2


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(
        description="已有 JSON 导出目录就地补生成 Markdown/HTML（不重跑解密）")
    ap.add_argument("export_dir", help="导出目录（含多个会话子目录）")
    ap.add_argument("--formats", default="md,html",
                    help="要补生成的格式，逗号分隔：md,html（默认两者）")
    ap.add_argument("--only", default=None,
                    help="只处理目录名含该子串的会话（试跑用）")
    ap.add_argument("--template", default=None,
                    help="HTML 模板名（默认 default）")
    ap.add_argument("--overwrite", action="store_true",
                    help="已存在的 .md/.html 一并重写")
    args = ap.parse_args(argv)
    formats = [f.strip().lower() for f in args.formats.split(",") if f.strip()]
    return convert_dir(Path(args.export_dir), formats, only=args.only,
                       template=args.template, overwrite=args.overwrite)


if __name__ == "__main__":
    sys.exit(main())
