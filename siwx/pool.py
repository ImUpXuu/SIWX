"""多进程解密池 + 输出缓存清单（mtime/size 未变则秒回）。

Windows spawn 模式要求 worker 为模块顶层函数；子进程通过 run.py 顶层
的 sys.path 注入定位 siwx 包。
"""
import json
import os
from pathlib import Path

CACHE_NAME = ".siwx_cache.json"


def load_manifest(out_dir) -> dict:
    p = Path(out_dir) / CACHE_NAME
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return {}


def save_manifest(out_dir, manifest: dict) -> None:
    p = Path(out_dir)
    p.mkdir(parents=True, exist_ok=True)
    (p / CACHE_NAME).write_text(
        json.dumps(manifest, ensure_ascii=False, indent=1), encoding="utf-8"
    )


def _worker(task):
    rel, src, dst, key_hex = task
    from siwx.sqlcipher import decrypt_database

    try:
        pages = decrypt_database(Path(src), Path(dst), bytes.fromhex(key_hex))
        return (rel, pages, "ok", "")
    except Exception as e:
        return (rel, 0, "failed", str(e))


def decrypt_parallel(tasks, workers=None, on_done=None):
    """tasks: [(rel, src, dst, key_hex)]；返回 [(rel, pages, status, err)]。"""
    if not tasks:
        return []
    n = workers or min(8, (os.cpu_count() or 4))
    if n <= 1 or len(tasks) <= 1:
        results = []
        for t in tasks:
            r = _worker(t)
            results.append(r)
            if on_done:
                on_done(r)
        return results
    import multiprocessing as mp

    results = []
    with mp.Pool(n) as pool:
        for r in pool.imap_unordered(_worker, tasks):
            results.append(r)
            if on_done:
                on_done(r)
    return results
