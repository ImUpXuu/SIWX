"""朋友圈卡片类动态 XML 探针（只读，诊断用）。

用途：为「卡片类型渲染」采集真实样本与字段清单。
**教训**：初版文档凭外部资料假设卡片字段在 ``<appmsg>`` 里 —— 真实库中
该节点根本不存在。改任何卡片相关代码前，先用本脚本 dump 真实结构。

用法::

    python scripts/sns_card_probe.py [sns.db] [样本数]        # dump 整棵树（默认）
    python scripts/sns_card_probe.py --tags  [sns.db]         # 各 type 的字段出现率
    python scripts/sns_card_probe.py --sub   [sns.db] [N]     # 子节点（music/live/note/finder）字段

默认库：本机 output 下第一个含 sns.db 的账号。
"""
from __future__ import annotations

import glob
import sqlite3
import sys
import xml.etree.ElementTree as ET
from collections import Counter, defaultdict

CARD_TYPES = (3, 5, 7, 26, 28, 34, 42, 47, 54)
SUBNODES = ("musicShareItem", "tingListenItem", "noteinfo", "finderLive", "finderFeed")
# --tags 时需要跳过的大块子树（它们的字段在 --sub 里单独看）
SKIP_SUBTREE = {"mediaList", "finderFeed", "mmreadershare", "LivePhoto"}


def default_db() -> str:
    hits = glob.glob("output/*/sns/sns.db")
    return hits[0] if hits else "output/<账号>/sns/sns.db"


def load_rows(db: str):
    con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    con.text_factory = bytes
    try:
        return con.execute("SELECT tid, content FROM SnsTimeLine").fetchall()
    finally:
        con.close()


def _paths(el, prefix="", depth=0, max_depth=8, out=None):
    if out is None:
        out = []
    if depth > max_depth:
        return out
    tag = f"{prefix}/{el.tag}"
    out.append((tag, dict(el.attrib), (el.text or "").strip()[:80]))
    for c in el:
        _paths(c, tag, depth + 1, max_depth, out)
    return out


def main_dump(db, per=1):
    seen = Counter()
    for tid, content in load_rows(db):
        try:
            root = ET.fromstring(content)
        except ET.ParseError:
            continue
        to = root.find("TimelineObject")
        co = to.find("ContentObject") if to is not None else None
        if co is None:
            continue
        ctype = int(co.findtext("type") or 0)
        if ctype not in CARD_TYPES or seen[ctype] >= per:
            continue
        seen[ctype] += 1
        print("=" * 78)
        print(f"### type={ctype} tid={tid} contentDesc={(to.findtext('contentDesc') or '')[:60]!r}")
        for tag, attrs, text in _paths(to):
            print(f"  {tag}  attrs={attrs}  text={text!r}")


def main_tags(db):
    tags = defaultdict(Counter)
    ex = defaultdict(dict)
    tot = Counter()
    for tid, content in load_rows(db):
        try:
            root = ET.fromstring(content)
        except ET.ParseError:
            continue
        to = root.find("TimelineObject")
        co = to.find("ContentObject") if to is not None else None
        if co is None:
            continue
        ctype = int(co.findtext("type") or 0)
        tot[ctype] += 1
        if ctype not in CARD_TYPES:
            continue
        for child in co:
            if child.tag in SKIP_SUBTREE:
                continue
            tags[ctype][child.tag] += 1
            t = (child.text or "").strip()
            if t and child.tag not in ex[ctype]:
                ex[ctype][child.tag] = t[:70]
    for ctype in CARD_TYPES:
        if not tot[ctype]:
            continue
        print("=" * 78)
        print(f"type={ctype} 总数={tot[ctype]} —— ContentObject 直接子元素")
        for tag, n in tags[ctype].most_common():
            print(f"  {tag:<24} {n:>5} ({100.0 * n / tot[ctype]:5.1f}%)  {ex[ctype].get(tag, '')!r}")


def main_sub(db, per=2):
    seen = Counter()
    for tid, content in load_rows(db):
        try:
            root = ET.fromstring(content)
        except ET.ParseError:
            continue
        to = root.find("TimelineObject")
        co = to.find("ContentObject") if to is not None else None
        if co is None:
            continue
        ctype = int(co.findtext("type") or 0)
        if ctype not in CARD_TYPES or seen[ctype] >= per:
            continue
        printed = False
        for sub in SUBNODES:
            el = co.find(sub)
            if el is None:
                continue
            if not printed:
                print("=" * 78)
                print(f"### type={ctype} tid={tid}")
                printed = True
            print(f"  <{sub}>")
            for tag, attrs, text in _paths(el, f"    {sub}", depth=0, max_depth=3):
                short = {k: (v[:34] + "…" if len(v) > 34 else v) for k, v in attrs.items()}
                print(f"    {tag}  attrs={short}  text={text!r}")
        if printed:
            seen[ctype] += 1


def main():
    argv = [a for a in sys.argv[1:]]
    mode = "dump"
    if argv and argv[0] in ("--tags", "--sub", "--dump"):
        mode = argv.pop(0)[2:]
    db = argv[0] if argv else default_db()
    n = int(argv[1]) if len(argv) > 1 else (2 if mode == "sub" else 1)
    print(f"# db={db} mode={mode}")
    if mode == "tags":
        main_tags(db)
    elif mode == "sub":
        main_sub(db, n)
    else:
        main_dump(db, n)


if __name__ == "__main__":
    main()
