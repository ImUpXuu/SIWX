"""siwx.log 文件日志与崩溃钩子（审计 §2.3 改法 3：独立模块）。

从 server.py 抽出：CLI 模式（keys/decrypt/auto/mcp/doctor）此前不装崩溃钩子、
不建 siwx.log——崩了没有 crash.log。run.py/cli.py main() 与 server 模块导入
统一走本模块。不塞进 logger.py：logger.py 保持零业务依赖的纯缓冲设计，
本模块依赖 siwx.paths 与标准 logging。
"""
import logging
import sys
import threading
from logging.handlers import RotatingFileHandler

from siwx import logger as _log
from siwx import paths as _paths


def setup_file_logger() -> logging.Logger:
    log_dir = _paths.app_root() / "logs"
    log_dir.mkdir(exist_ok=True)
    logger = logging.getLogger("siwx")
    logger.setLevel(logging.DEBUG)
    # 避免重复添加
    if logger.handlers:
        return logger
    fh = RotatingFileHandler(log_dir / "siwx.log", maxBytes=10 * 1024 * 1024,
                              backupCount=5, encoding="utf-8")
    fh.setLevel(logging.DEBUG)
    fh.setFormatter(logging.Formatter(
        "%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S"))
    logger.addHandler(fh)
    # 控制台也输出 INFO+；丢弃结构化轨桥接的记录（logger._bridge 带
    # _siwx_struct 标记）——结构化轨自己的 _cprint 已输出控制台，桥接只为
    # 落盘，不桥控制台会导致同一行打两遍
    ch = logging.StreamHandler()
    ch.setLevel(logging.INFO)
    ch.addFilter(lambda r: not getattr(r, "_siwx_struct", False))
    ch.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(message)s"))
    logger.addHandler(ch)
    return logger


def flush_logs() -> None:
    """尽量把日志落盘；logging 的 handler 通常会自动 flush，异常路径兜底用。"""
    for h in logging.getLogger("siwx").handlers:
        try:
            h.flush()
        except Exception:
            pass


_CRASH_FH = None


def install_crash_hooks() -> None:
    """记录未捕获异常（主线程/子线程）和 Python fatal traceback → siwx.log + crash.log。"""
    global _CRASH_FH
    log_dir = _paths.app_root() / "logs"
    log_dir.mkdir(exist_ok=True)
    logger = logging.getLogger("siwx")

    def _sys_excepthook(exc_type, exc, tb):
        logger.critical("未捕获主线程异常", exc_info=(exc_type, exc, tb))
        flush_logs()
        sys.__excepthook__(exc_type, exc, tb)

    sys.excepthook = _sys_excepthook

    if hasattr(threading, "excepthook"):
        def _thread_excepthook(args):
            logger.critical("未捕获线程异常: %s", args.thread.name,
                            exc_info=(args.exc_type, args.exc_value, args.exc_traceback))
            flush_logs()
        threading.excepthook = _thread_excepthook

    try:
        import faulthandler
        _CRASH_FH = open(log_dir / "crash.log", "a", encoding="utf-8")
        faulthandler.enable(_CRASH_FH, all_threads=True)
    except Exception as e:
        _CRASH_FH = None
        # 审计 §5.1：faulthandler 启用失败静默 → 用户以为有崩溃记录实际没有
        _log.warn("server", f"crash.log 初始化失败: {e}")
