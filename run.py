#!/usr/bin/env python3
"""stories-in-wx — 微信 4.x 密钥提取与解密 (Python 自研重写)。

全自动流水线：目录扫描 → 密钥提取 → DPAPI 加密保存 → 数据库解密。
用法：
    python run.py auto                     # 全自动（推荐）
    python run.py keys extract [--db-dir X] [--json]
    python run.py keys list
    python run.py decrypt [--db-dir X] [--out DIR]
    python run.py serve [--port 8787]
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

_WXGF_WORKER_FLAG = "--wxgf-worker"


def _run_wxgf_worker(dll: str) -> int:
    """打包版 wxgf 转码 worker 入口（P1-1）。

    PyInstaller 打包后 sys.executable 是本程序的 exe，`python -c <脚本>`
    的调用方式必然被 CLI 的 argparse 当成未知参数。media.convert_wxgf()
    在 frozen 下改为 `[exe, --wxgf-worker, <dll>]`，由这里在导入 cli /
    argparse 之前拦截：把 argv 调整为 worker 期望的 `[prog, dll]`，直接
    执行 media.py 里同一个 `_WXGF_WORKER` 字符串（单一事实来源，与源码态
    `-c` 完全一致）。worker 自身 `import sys/os/ctypes` 并用 `sys.exit`
    返回码退出，这里把 SystemExit 归一化成函数返回值。
    """
    from siwx.media import _WXGF_WORKER

    sys.argv = [sys.argv[0], dll]
    try:
        exec(compile(_WXGF_WORKER, "<wxgf-worker>", "exec"), {"__name__": "__main__"})
    except SystemExit as e:
        code = e.code
        return code if isinstance(code, int) else (0 if code is None else 1)
    return 0


if __name__ == "__main__":
    # PyInstaller 打包后必须最先调用：multiprocessing 子进程以本 exe
    # 重新拉起时，由 freeze_support 分流到 spawn_main，避免整包重跑 CLI
    from multiprocessing import freeze_support
    freeze_support()
    _argv = sys.argv[1:]
    if _argv and _argv[0] == _WXGF_WORKER_FLAG:
        # 必须在 `from siwx.cli import main`（argparse 构造）之前处理
        if len(_argv) < 2:
            print("wxgf worker: 缺少 DLL 路径参数", file=sys.stderr)
            sys.exit(2)
        sys.exit(_run_wxgf_worker(_argv[1]))
    from siwx.cli import main
    sys.exit(main())
