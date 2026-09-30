"""朋友圈 CDN 拉媒体体检 —— 真库真网络，逐条给出失败原因。

用法::

    python diag_sns_cdn.py [账号目录名] [扫描动态数] [抽样 URL 数]

输出：
1. 库内统计：动态数 / 含媒体数 / 唯一 URL 数 / 本地缓存命中率
2. 抽样真实请求结果（图片、视频、卡片封面分别统计）
3. 失败原因聚合、失败域名分布、**按「自己 / 好友」分组**（自己发的旧图 404 最多）
4. 每条失败给出来源、HTTP 码、试过的域名

背景（2026-09-30 实测，本机 5684 条库）：
* 修复前视频 0% 成功 —— ``<url key="0">`` 占了 ``<enc key>`` 的位置；
* 自己发的图约 58% 被 CDN 404（好友的约 4%），这部分**不是代码问题**，
  本地缓存里也没有（微信没落盘），只能如实报错；
* 所以这个脚本的价值是：把「拉不下来」拆成「代码 bug / CDN 已无 / 外链非媒体」。
"""
from __future__ import annotations

import collections
import concurrent.futures as cf
import sqlite3
import sys
import time
import urllib.error
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from siwx import paths, sns, sns_cdn  # noqa: E402


def collect(acc: str, limit_posts: int):
    db = paths.out_root() / acc / "sns" / "sns.db"
    if not db.is_file():
        raise SystemExit(f"没有找到 {db}")
    con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    con.text_factory = bytes
    medias, posts = [], 0
    try:
        for tid, who, content in con.execute(
                "SELECT tid, user_name, content FROM SnsTimeLine "
                "ORDER BY tid DESC LIMIT ?", (limit_posts,)):
            posts += 1
            feed = sns.parse_timeline(content)
            if not feed:
                continue
            author = ((who or b"").decode("utf-8", "replace")
                      if isinstance(who, bytes) else (who or ""))
            for m in feed.get("medias") or []:
                if m.get("url"):
                    medias.append({**m, "tid": tid, "author": author})
    finally:
        con.close()
    return posts, medias


def probe(m: dict) -> dict:
    """走生产路径 ``fetch_media``（含域名回退），记录结果与耗时。"""
    t0 = time.time()
    r = sns_cdn.fetch_media(m["url"], key=m.get("key"), token=m.get("token"),
                            cache_dir=None)
    ms = int((time.time() - t0) * 1000)
    return {
        "tid": m.get("tid"), "author": m.get("author"), "type": m.get("type"),
        "url": m["url"], "host": sns_cdn.safe_url(m["url"]).split("/")[0],
        "key": m.get("key"), "token": m.get("token"), "ms": ms,
        "ok": r["ok"], "reason": r.get("reason"), "status": r.get("status"),
        "error": r.get("error"), "ext": r.get("ext"), "encrypted": r.get("encrypted"),
    }


def main():
    acc = sys.argv[1] if len(sys.argv) > 1 else "wxid_z30ttr1sg8h222_6409"
    limit_posts = int(sys.argv[2]) if len(sys.argv) > 2 else 300
    n = int(sys.argv[3]) if len(sys.argv) > 3 else 60

    posts, medias = collect(acc, limit_posts)
    uniq = {}
    for m in medias:
        uniq.setdefault(m["url"], m)
    cache = paths.out_root() / acc / "sns_media"
    hit = sum(1 for u in uniq if sns_cdn.read_cached(cache, u))
    print(f"账号 = {acc}")
    print(f"扫描动态 {posts} 条 → 媒体 {len(medias)} 个（唯一 URL {len(uniq)} 个）")
    print(f"本地缓存已命中 {hit}/{len(uniq)}（{hit * 100 // max(1, len(uniq))}%）")
    print(f"域名分布：{dict(collections.Counter(sns_cdn.safe_url(u).split('/')[0] for u in uniq))}\n")

    values = list(uniq.values())
    step = max(1, len(values) // max(1, n))
    sample = values[::step][:n]
    print(f"── 抽样真实请求 {len(sample)} 个（并发 5）──")
    t0 = time.time()
    with cf.ThreadPoolExecutor(max_workers=5) as ex:
        res = list(ex.map(probe, sample))
    elapsed = (time.time() - t0) * 1000
    ok = [r for r in res if r["ok"]]
    bad = [r for r in res if not r["ok"]]
    print(f"成功 {len(ok)}/{len(res)}（{len(ok) * 100 // max(1, len(res))}%），"
          f"总耗时 {elapsed:.0f}ms，平均 {elapsed / max(1, len(res)):.0f}ms/个")
    print(f"ISAAC 解密后成功 {sum(1 for r in ok if r['encrypted'])}，"
          f"明文直出 {sum(1 for r in ok if not r['encrypted'])}")

    print("\n── 按媒体类型 ──")
    for t in sorted({r["type"] for r in res}):
        g = [r for r in res if r["type"] == t]
        print(f"  type={t}: {sum(r['ok'] for r in g)}/{len(g)} 成功")

    if bad:
        print("\n── 失败原因聚合 ──")
        for k, v in collections.Counter(r["reason"] or "unknown" for r in bad).most_common():
            print(f"  {v:4d}  {k}")
        print("── 失败域名分布 ──")
        for k, v in collections.Counter(r["host"] for r in bad).most_common():
            print(f"  {v:4d}  {k}")
        print("── 失败样本（前 8 条）──")
        for r in bad[:8]:
            print(f"  type={r['type']} reason={r['reason']} status={r['status']} "
                  f"key={r['key']} token={(r['token'] or '')[:12]}… ms={r['ms']}")
            print(f"    {r['url'][:120]}")
            print(f"    {r['error']}")

    self_wxid = acc.rsplit("_", 1)[0] if "_" in acc else acc
    print(f"\n── 按发布者（自己 = {self_wxid}）──")
    for label, grp in (("自己", [r for r in res if r["author"] == self_wxid]),
                       ("好友", [r for r in res if r["author"] != self_wxid])):
        if grp:
            print(f"  {label}: {sum(r['ok'] for r in grp)}/{len(grp)} 成功"
                  f"（失败原因 {dict(collections.Counter(r['reason'] for r in grp if not r['ok']))}）")
    print("\n提示：http-404 是 CDN 上确实没有该资源（且本地缓存也没有），"
          "代码侧只能如实报错；undecodable 才是密钥/解密问题。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
