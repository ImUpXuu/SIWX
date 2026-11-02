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
import importlib.util
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

_WXGF_WORKER_FLAG = "--wxgf-worker"

# requirements.txt 的「导入名 → pip 包名」映射。第三方模块在 siwx 里基本是
# 模块级裸导入（`discover.py` 的 psutil 是有意例外：它能降级），缺依赖时直接
# `python run.py ...` 会抛一条埋在多行 import 链尾部的 ModuleNotFoundError
# （用户看到 `No module named 'psutil'` 无从下手）。更糟的是本机 PATH 上往往
# 有多个解释器（Python 官方 / WindowsApps 存根 / msys64 等），报错的那个未必是
# 装齐依赖的那个——提示里必须带上解释器路径。这里在导入 siwx.cli 之前做一次
# 预检，换成可执行的指引。`tests/test_console_glyphs.py::TestRunPreflight`
# 会把它与 requirements.txt 对账，防两边漂移。
_REQUIRED_DEPS = {
    "Crypto": "pycryptodome",
    "flask": "flask",
    "psutil": "psutil",
    "openpyxl": "openpyxl",
    "rich": "rich",
    "zstandard": "zstandard",
    "pilk": "pilk",
}


def _missing_deps() -> list:
    """返回未安装的第三方模块导入名（不导入模块，只查 finder）。"""
    return [name for name in _REQUIRED_DEPS if importlib.util.find_spec(name) is None]


def _deps_failure(missing: list) -> int:
    """打印缺依赖指引，返回退出码 2。"""
    pip_names = " ".join(_REQUIRED_DEPS[m] for m in missing)
    print("缺少依赖模块: " + ", ".join(missing), file=sys.stderr)
    print(f"当前解释器: {sys.executable}", file=sys.stderr)
    print("请用同一个解释器安装依赖后重试：", file=sys.stderr)
    print(f'  "{sys.executable}" -m pip install -r requirements.txt',
          file=sys.stderr)
    print(f'  (或 "{sys.executable}" -m pip install {pip_names})',
          file=sys.stderr)
    print("若 PATH 上有多个 Python，注意确认调用的是哪一个（py -0p）。",
          file=sys.stderr)
    return 2


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
    # 控制台编码兜底：cp1252 等西文控制台编不了中文——argparse 打印 --help
    # 会直接 UnicodeEncodeError 崩掉（CI 冒烟与英语区 Windows 用户都会踩）。
    # 只把不可编码字符降级为替换符，不改变 UTF-8 控制台的行为。
    for _stream in (sys.stdout, sys.stderr):
        try:
            if _stream is not None and (_stream.encoding or "").lower() not in (
                    "utf-8", "utf8"):
                _stream.reconfigure(errors="replace")
        except (AttributeError, OSError, ValueError):
            pass
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
        # worker 只需 stdlib（sys/os/ctypes），不碰 siwx 包，免预检
        sys.exit(_run_wxgf_worker(_argv[1]))
    _missing = _missing_deps()
    if _missing:
        sys.exit(_deps_failure(_missing))
    from siwx.cli import main
    sys.exit(main())
