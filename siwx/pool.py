"""多进程解密池 + 输出缓存清单（mtime/size 未变则秒回）。

Windows spawn 模式要求 worker 为模块顶层函数；子进程通过 run.py 顶层
的 sys.path 注入定位 siwx 包。
"""
import json
import os
import time
from pathlib import Path

from siwx import logger as _slog

CACHE_NAME = ".siwx_cache.json"


def sanitize_manifest(manifest: dict) -> dict:
    """剥离缓存清单中的明文密钥（审计 S6，P0）。

    旧版把每个库的 64 位十六进制 SQLCipher 明文密钥写进 .siwx_cache.json
    （无加密、无权限收紧，Windows 同样明文可读）——任何能读用户输出目录的
    进程/同步盘/备份工具都能拿到全部库的解密密钥。密钥每次会话从 keystore
    重取即可，manifest 不再持久化；加载旧文件时就地剥离。"""
    out = {}
    for k, v in manifest.items():
        if isinstance(v, dict) and "key" in v:
            v = {vk: vv for vk, vv in v.items() if vk != "key"}
        out[k] = v
    return out


def manifest_has_keys(manifest: dict) -> bool:
    """manifest 中是否还残留明文密钥（用于检测旧版落盘文件）。"""
    return any(isinstance(v, dict) and "key" in v for v in manifest.values())


def load_manifest(out_dir) -> dict:
    p = Path(out_dir) / CACHE_NAME
    try:
        return sanitize_manifest(json.loads(p.read_text(encoding="utf-8")))
    except FileNotFoundError:
        return {}   # 首次运行本来就没有缓存清单，不算损坏
    except Exception as e:
        # 审计 §3.2：缓存损坏被当首次运行 → 全量重解，原因必须可见
        _slog.warn("pool", f"缓存清单加载失败 {p}: {type(e).__name__}: {e}")
        return {}


def save_manifest(out_dir, manifest: dict) -> None:
    p = Path(out_dir)
    try:
        p.mkdir(parents=True, exist_ok=True)
        (p / CACHE_NAME).write_text(
            json.dumps(sanitize_manifest(manifest), ensure_ascii=False, indent=1),
            encoding="utf-8"
        )
    except OSError as e:
        # 审计 §3.2：产物在、清单丢 → 下次全量重解。只补日志，不改传播语义
        _slog.error("pool", f"缓存清单写入失败 {out_dir}: {e}")
        raise


def _worker(task):
    rel, src, dst, key_hex = task
    from siwx.sqlcipher import decrypt_database

    try:
        pages = decrypt_database(Path(src), Path(dst), bytes.fromhex(key_hex))
        return (rel, pages, "ok", "")
    except Exception as e:
        # 审计 §3.2：异常类型名不能丢（区分 OSError/ValueError 等）；
        # 不带 traceback（返回串会整串进任务面板，核查 C.7）
        return (rel, 0, "failed", f"{type(e).__name__}: {e}")


def decrypt_parallel(tasks, workers=None, on_done=None):
    """tasks: [(rel, src, dst, key_hex)]；返回 [(rel, pages, status, err)]。"""
    if not tasks:
        return []
    n = workers or min(8, (os.cpu_count() or 4))
    t0 = time.time()
    _slog.detailed("pool", f"解密启动 workers={n} tasks={len(tasks)}")

    def _run_serial() -> list:
        results = []
        for t in tasks:
            r = _worker(t)
            results.append(r)
            if on_done:
                on_done(r)
        return results

    if n <= 1 or len(tasks) <= 1:
        results = _run_serial()
    else:
        import multiprocessing as mp
        try:
            pool = mp.Pool(n)
        except Exception as e:
            # 审计 §3.2：Windows spawn 环境问题只见崩溃无上下文 → 回退串行
            _slog.warn("pool", f"进程池创建失败({type(e).__name__}: {e})，回退串行解密")
            results = _run_serial()
        else:
            with pool:
                results = []
                for r in pool.imap_unordered(_worker, tasks):
                    results.append(r)
                    if on_done:
                        on_done(r)

    failed = sum(1 for r in results if r[2] != "ok")
    _slog.detailed("pool",
                   f"解密完成 tasks={len(results)} 失败={failed} "
                   f"耗时={time.time() - t0:.1f}s")
    return results
