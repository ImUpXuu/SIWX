"""WAL 探测：只读观测微信 -wal 的写入与 checkpoint 节奏。

目的：量化 SIWX 排除 -wal 造成的可见性延迟到底有多大，
     并顺带用真实数据校验 WAL 格式假设（头/帧校验和、提交语义）。

安全：纯只读。不写入任何微信文件，不读取密钥，不触碰 SIWX 解密链路。

用法：
    python scripts/wal_probe.py                 # 快照 + 默认 90 秒观测
    python scripts/wal_probe.py --seconds 0     # 只看当前快照
    python scripts/wal_probe.py --seconds 180 --interval 5
"""
import argparse
import os
import struct
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

PAGE_SZ = 4096
WAL_HDR_SZ = 32
FRAME_HDR_SZ = 24
MAGIC_LE = 0x377F0682
MAGIC_BE = 0x377F0683


# ── WAL 解析（与设计方案 §3 的格式假设一一对应，用于真机校验）──────────

def _checksum(data, s0, s1, little_endian):
    order = "little" if little_endian else "big"
    for off in range(0, len(data) - 7, 8):
        x0 = int.from_bytes(data[off:off + 4], order)
        x1 = int.from_bytes(data[off + 4:off + 8], order)
        s0 = (s0 + x0 + s1) & 0xFFFFFFFF
        s1 = (s1 + x1 + s0) & 0xFFFFFFFF
    return s0, s1


def parse_wal(data):
    """解析 WAL 镜像，返回诊断字典；无法识别时返回 None。"""
    if len(data) < WAL_HDR_SZ:
        return None
    magic = int.from_bytes(data[0:4], "big")
    if magic not in (MAGIC_LE, MAGIC_BE):
        return {"magic_ok": False, "magic": f"0x{magic:08x}"}
    little = magic == MAGIC_LE
    page_size = int.from_bytes(data[8:12], "big")
    cp_seq = int.from_bytes(data[12:16], "big")
    salt1, salt2 = data[16:20], data[20:24]
    hdr_s0, hdr_s1 = _checksum(data[0:24], 0, 0, little)
    header_ok = (hdr_s0, hdr_s1) == (
        int.from_bytes(data[24:28], "big"), int.from_bytes(data[28:32], "big"))

    out = {
        "magic_ok": True, "little_endian": little, "page_size": page_size,
        "checkpoint_seq": cp_seq, "header_ok": header_ok,
        "frames_total": 0, "commits": 0, "commit_db_size": None,
        "frames_committed": 0, "stopped_early": False,
    }
    if not header_ok or page_size <= 0:
        return out

    s0, s1 = hdr_s0, hdr_s1
    offset = WAL_HDR_SZ
    frames = 0
    committed = 0
    commit_size = None
    while offset + FRAME_HDR_SZ + page_size <= len(data):
        fh = data[offset:offset + FRAME_HDR_SZ]
        pgno = int.from_bytes(fh[0:4], "big")
        dbsize = int.from_bytes(fh[4:8], "big")
        f_salt1, f_salt2 = fh[8:12], fh[12:16]
        page = data[offset + FRAME_HDR_SZ:offset + FRAME_HDR_SZ + page_size]
        n0, n1 = _checksum(fh[0:8] + page, s0, s1, little)
        if (f_salt1 != salt1 or f_salt2 != salt2
                or (n0, n1) != (int.from_bytes(fh[16:20], "big"),
                                int.from_bytes(fh[20:24], "big"))):
            out["stopped_early"] = True
            break
        s0, s1 = n0, n1
        frames += 1
        if dbsize != 0:
            committed = frames
            commit_size = dbsize
        offset += FRAME_HDR_SZ + page_size

    out.update(frames_total=frames, commits=(1 if commit_size else 0),
               commit_db_size=commit_size, frames_committed=committed)
    return out


# ── 采集 ────────────────────────────────────────────────────────────

def _stat(p):
    try:
        st = os.stat(p)
        return st.st_size, st.st_mtime
    except OSError:
        return None, None


def scan(dirs):
    """扫描全部账号的 .db 及其 -wal / -shm，返回条目列表。"""
    rows = []
    for wxid, db_dir in dirs:
        root = Path(db_dir)
        if not root.is_dir():
            continue
        for dirpath, _d, files in os.walk(root):
            for name in files:
                if not name.endswith(".db") or name.endswith("-wal") or name.endswith("-shm"):
                    continue
                main = Path(dirpath) / name
                wal = Path(str(main) + "-wal")
                shm = Path(str(main) + "-shm")
                m_size, m_mtime = _stat(main)
                if m_size is None:
                    continue
                w_size, w_mtime = _stat(wal)
                s_size, _ = _stat(shm)
                rel = str(main.relative_to(root)).replace(os.sep, "/")
                rows.append({
                    "wxid": wxid, "rel": rel, "main": main, "wal": wal,
                    "main_size": m_size, "main_mtime": m_mtime,
                    "wal_size": w_size or 0, "wal_mtime": w_mtime,
                    "shm_present": s_size is not None,
                })
    rows.sort(key=lambda r: (r["wxid"], r["rel"]))
    return rows


def read_wal_head(path, limit=4 * 1024 * 1024):
    try:
        with open(path, "rb") as f:
            return f.read(limit)
    except OSError:
        return None


def wal_signature(path, limit=4 * 1024 * 1024):
    """WAL 的关键身份信号。

    注意：checkpoint **不会**把 WAL 文件清零 —— 它只是把 salt1 加一并重写文件头，
    旧帧作为陈旧残留留在原处（这也是 4 MiB 的 WAL 可能零有效帧的原因）。
    所以判断 checkpoint 必须看 salt1 / checkpoint_seq，不能看文件大小。

    `limit` 控制读取量：有效帧总是从文件头之后开始，观测场景可用 1 MiB 换取速度；
    精确统计请用默认值。
    """
    data = read_wal_head(path, limit=limit)
    if not data:
        return None
    info = parse_wal(data)
    if not info or not info.get("magic_ok"):
        return None
    return {
        "salt1": data[16:20].hex(),
        "salt2": data[20:24].hex(),
        "cp_seq": info["checkpoint_seq"],
        "frames_committed": info["frames_committed"],
        "commit_db_size": info["commit_db_size"],
    }


def fmt_ts(t):
    return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(t)) if t else "—"


def age(now, t):
    if not t:
        return "—"
    d = int(now - t)
    if d < 60:
        return f"{d} 秒前"
    if d < 3600:
        return f"{d // 60} 分钟前"
    if d < 86400:
        return f"{d // 3600} 小时前"
    return f"{d // 86400} 天前"


def snapshot(rows, label):
    now = time.time()
    with_wal = [r for r in rows if r["wal_size"] > 0]
    print(f"\n{'=' * 78}")
    print(f"[{label}] 扫描到 {len(rows)} 个 .db，其中 {len(with_wal)} 个带非空 -wal")
    print("=" * 78)

    total_committed_frames = 0
    for r in with_wal:
        data = read_wal_head(r["wal"])
        info = parse_wal(data) if data else None
        extra = ""
        if info and info.get("magic_ok"):
            pages = info["frames_committed"]
            total_committed_frames += pages
            extra = (f"  page={info['page_size']} 帧={info['frames_total']}"
                     f"(已提交 {info['frames_committed']})"
                     f" commitDbSize={info['commit_db_size']}")
            if not info["header_ok"]:
                extra += "  ⚠头校验失败"
            if info["stopped_early"]:
                extra += "  (尾部未提交/不完整)"
        elif info is not None:
            extra = f"  ⚠非 WAL magic={info.get('magic')}"
        print(f"\n  {r['wxid']} / {r['rel']}")
        print(f"    主库 {r['main_size'] / 1048576:>10.2f} MB  改动于 {age(now, r['main_mtime'])}")
        print(f"    WAL  {r['wal_size'] / 1048576:>10.2f} MB  改动于 {age(now, r['wal_mtime'])}")
        print(f"    -shm {'存在' if r['shm_present'] else '无'}{extra}")
        if r["main_mtime"] and r["wal_mtime"]:
            lag = r["wal_mtime"] - r["main_mtime"]
            print(f"    WAL 比主库新 {lag:.0f} 秒 → 这段时间的消息 SIWX 当前看不到")

    if not with_wal:
        print("\n  当前没有任何 -wal 有内容（微信可能未运行，或刚完成 checkpoint）")
    else:
        print(f"\n  合计：{len(with_wal)} 个库的 WAL 中共 {total_committed_frames} 个已提交页 "
              f"≈ {total_committed_frames * PAGE_SZ / 1048576:.2f} MB 未落盘数据")
    return with_wal


def observe(dirs, seconds, interval, only=None):
    """高粒度观测：捕捉「写入 WAL → checkpoint」的完整过程。

    只跟踪 WAL 的 salt1 / cp_seq / 有效帧数与主库 mtime，
    因为 checkpoint 不会清空文件，只看大小会漏掉事件。

    `only` 为路径子串过滤器（例如 "message/message_"），用于只盯活跃库，
    避免把全部 76 个 WAL 每秒读一遍。
    """
    if seconds <= 0:
        return
    print(f"\n{'=' * 78}")
    print(f"[观测] 每 {interval} 秒采样，共 {seconds} 秒 —— 请在这期间发几条微信消息")
    if only:
        print(f"       过滤: {only}")
    print("=" * 78)

    def snap():
        out = {}
        for r in scan(dirs):
            if only and only not in r["rel"]:
                continue
            # 观测场景只读 1 MiB：有效帧总是紧跟在文件头之后，
            # 全量读 4 MiB × 76 个库会让每秒采样变成近 300 MB 的 I/O。
            sig = wal_signature(r["wal"], limit=1024 * 1024) if r["wal_size"] > 0 else None
            out[r["wxid"] + "/" + r["rel"]] = {
                "main_mtime": r["main_mtime"], "main_size": r["main_size"],
                "wal_size": r["wal_size"],
                "salt1": sig["salt1"] if sig else None,
                "cp_seq": sig["cp_seq"] if sig else None,
                "frames": sig["frames_committed"] if sig else 0,
            }
        return out

    prev = snap()
    t0 = time.time()
    n = max(1, int(seconds // interval))
    counts = {"写WAL": 0, "checkpoint": 0, "主库改写": 0}
    for _ in range(n):
        time.sleep(interval)
        cur = snap()
        el = time.time() - t0
        for key, c in cur.items():
            p = prev.get(key)
            if p is None:
                continue
            if p["salt1"] != c["salt1"] or (
                    p["cp_seq"] is not None and c["cp_seq"] is not None
                    and c["cp_seq"] != p["cp_seq"]):
                counts["checkpoint"] += 1
                print(f"  [+{el:6.1f}s] ★ checkpoint  {key}\n"
                      f"                salt1 {p['salt1']} → {c['salt1']}  "
                      f"cp_seq {p['cp_seq']} → {c['cp_seq']}")
            if c["frames"] > p["frames"]:
                counts["写WAL"] += 1
                print(f"  [+{el:6.1f}s] 写入 WAL     {key}  有效帧 {p['frames']} → {c['frames']}"
                      f"  (~{(c['frames'] - p['frames']) * 4096 / 1024:.0f} KB)")
            if p["main_mtime"] != c["main_mtime"]:
                counts["主库改写"] += 1
                print(f"  [+{el:6.1f}s] 主库改写     {key}  "
                      f"{p['main_size']} → {c['main_size']} 字节")
        prev = cur

    print(f"\n  观测结束：写WAL {counts['写WAL']} 次 / checkpoint {counts['checkpoint']} 次 / "
          f"主库改写 {counts['主库改写']} 次")
    if not any(counts.values()):
        print("  未观测到任何变化 —— 这段时间微信没有写入")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--seconds", type=int, default=90, help="观测时长（0=只看快照）")
    ap.add_argument("--interval", type=int, default=5, help="采样间隔秒")
    ap.add_argument("--only", default=None,
                    help="只观测路径含该子串的库，例如 message/message_")
    args = ap.parse_args()

    from siwx.discover import find_wechat_data_dirs
    dirs = find_wechat_data_dirs()
    print(f"发现 {len(dirs)} 个微信账号数据目录：")
    for wxid, db in dirs:
        print(f"  - {wxid}  →  {db}")
    if not dirs:
        print("未发现账号目录。请确认微信已登录，或检查路径设置。")
        return 1

    rows = scan(dirs)
    snapshot(rows, "当前快照")
    observe(dirs, args.seconds, args.interval, only=args.only)
    return 0


if __name__ == "__main__":
    sys.exit(main())
