"""端到端自检：打真实 Flask API，验证「朋友圈图片/视频」现在是什么结果 + 有没有日志。

用法::

    python diag_sns_api.py [账号目录名]

覆盖四种真实情况：
  1. 能下载的图     → 200 image/*
  2. CDN 上已没有的图 → 404 + reason=http-404（不会再静默）
  3. 视频           → 200 video/mp4（修复前 100% 失败）
  4. 外链卡片（b23.tv）→ 400 + reason=not-cdn

最后打印本轮产生的 [sns-media] 日志行（证明失败有留痕）。
"""
from __future__ import annotations
import json
import logging
import sqlite3
import sys
import tempfile
from pathlib import Path
from urllib.parse import urlencode

sys.path.insert(0, str(Path(__file__).resolve().parent))

ACC = sys.argv[1] if len(sys.argv) > 1 else "wxid_z30ttr1sg8h222_6409"

# 把日志挂到一个临时文件：既能验证格式，又不碰真实 logs/siwx.log（可能被在跑的 SIWX 占用）
_log_path = Path(tempfile.gettempdir()) / "siwx-sns-diag.log"
_log_path.write_text("", encoding="utf-8")
logger = logging.getLogger("siwx")
logger.setLevel(logging.DEBUG)
_fh = logging.FileHandler(_log_path, encoding="utf-8")
_fh.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(message)s"))
logger.addHandler(_fh)

from siwx.server import app  # noqa: E402
from siwx import paths, sns  # noqa: E402

c = app.test_client()
print("账号", ACC)
d = c.get("/api/sns/accounts").get_json()
print("GET /api/sns/accounts ->", [a["wxid"] for a in d["accounts"]], "\n")

db = paths.out_root() / ACC / "sns" / "sns.db"
con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
con.text_factory = bytes
img_bad = img_ok = vid = None
try:
    for tid, content in con.execute(
            "SELECT tid, content FROM SnsTimeLine ORDER BY tid DESC LIMIT 120"):
        feed = sns.parse_timeline(content)
        for m in (feed or {}).get("medias") or []:
            if m.get("type") == 2 and m.get("url"):
                img_ok = img_ok or m
            if m.get("type") == 6 and m.get("url") and not vid:
                vid = m
finally:
    con.close()
# 实测 404 的一条（自己发的 4 图动态）
con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
con.text_factory = bytes
row = con.execute("SELECT content FROM SnsTimeLine WHERE tid=?",
                  (-3437738783055990261,)).fetchone()
con.close()
if row:
    img_bad = sns.parse_timeline(row[0])["medias"][0]


def hit(label, m):
    q = {"account": ACC, "url": m["url"], "key": m.get("key") or "",
         "token": m.get("token") or ""}
    r = c.get("/api/sns/media?" + urlencode(q))
    ct = r.headers.get("Content-Type", "")
    body = r.get_data()
    tail = ""
    if "json" in ct:
        j = json.loads(body)
        tail = f" reason={j.get('reason')} hosts_tried={j.get('hosts_tried')} err={j.get('error')}"
    print(f"{label:14s} type={m.get('type')} -> HTTP {r.status_code} {ct} {len(body)}B{tail}")
    return r.status_code


# 前若干张图里挑两张真的还能下的（自己发的旧图实测大量 404）
cands = []
con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
con.text_factory = bytes
try:
    for tid, content in con.execute(
            "SELECT tid, content FROM SnsTimeLine ORDER BY tid DESC LIMIT 200"):
        feed = sns.parse_timeline(content)
        for m in (feed or {}).get("medias") or []:
            if m.get("type") == 2 and m.get("url"):
                cands.append(m)
finally:
    con.close()

print("── 图片：找 CDN 上还有的 ──")
ok = 0
for i, m in enumerate(cands[:12], 1):
    if hit(f"图尝试{i}", m) == 200:
        ok += 1
        if ok >= 2:
            break
print(f"（前 12 张里能下的：{ok} 张；自己发的旧图大量为 http-404，见下）")

print("\n── 其余场景 ──")
if img_bad:
    hit("图(CDN 已无)", img_bad)
hit("视频", vid)
hit("外链卡片", {"url": "https://b23.tv/LgNWM4c", "key": None, "token": None, "type": 4})

_fh.flush()
lines = [ln for ln in _log_path.read_text(encoding="utf-8").splitlines()
         if "[sns-media]" in ln or "[sns-emoji]" in ln]
print(f"\n── 本轮日志文件里的 [sns-media] 记录（{len(lines)} 条）──")
for ln in lines:
    print(" ", ln)
