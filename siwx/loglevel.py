"""Debug 日志级别的持久化与多入口（审计 §2.3 + P0 留存改造）。

此前 Debug 开关只有一个入口（POST /api/logs/settings）、只改内存、重启回
ROUGH，且 siwx.log 文件通道与结构化环形轨口径脱节。本模块提供：
- apply(): 切换级别（仅控制环形缓冲/UI 展示）并可选持久化到
  data_dir()/log_level.json
- startup(): 启动恢复（环境变量 SIWX_LOG_LEVEL 优先，其次持久化文件）

留存语义（P0）：文件通道恒 DEBUG——detailed 埋点始终落盘，开关只决定
日志页/环形缓冲是否展示 DEBUG。与主流桌面产品"全量写文件、门控展示"对齐。

时序约定（审计 §2.3 C.1 陷阱）：startup() 必须在 logging_setup.setup_file_logger()
之后调用——后者无条件把 "siwx" logger 置 DEBUG，先调用会被覆盖回去。
"""
import json
import logging
import os

from siwx import logger as _log

_ENV_VAR = "SIWX_LOG_LEVEL"
_LEVELS = ("rough", "detailed")


def _settings_file():
    from siwx.paths import data_dir
    return data_dir() / "log_level.json"


def apply(level_name: str, persist: bool = False) -> str:
    """切换 Debug 级别。

    只控制环形缓冲/UI 展示（logger.set_level）。文件通道（siwx.log）自 P0
    改造后**恒为 DEBUG**：detailed 埋点始终落盘，开关不再影响留存——否则
    "详细模式才写文件"会让偶发问题在开关打开前丢失记录（先复现再开 Debug
    的范式不成立）。"""
    level_name = str(level_name or "").strip().lower()
    if level_name not in _LEVELS:
        level_name = "rough"
    if _log.get_level().value != level_name:
        _log.set_level(_log.LogLevel.DETAILED if level_name == "detailed"
                       else _log.LogLevel.ROUGH)
    # 文件通道恒 DEBUG（含控制台 handler 自身 INFO 过滤，不会刷控制台）
    logging.getLogger("siwx").setLevel(logging.DEBUG)
    if persist:
        try:
            p = _settings_file()
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(json.dumps({"level": level_name}), encoding="utf-8")
        except OSError:
            pass  # 持久化失败只影响下次启动，不阻塞本次切换
    return level_name


def startup() -> str:
    """启动时恢复级别（在 setup_file_logger() 之后调用）。"""
    env = os.environ.get(_ENV_VAR, "").strip().lower()
    if env in _LEVELS:
        return apply(env, persist=False)
    try:
        data = json.loads(_settings_file().read_text(encoding="utf-8"))
        lvl = str(data.get("level", "")).strip().lower()
        if lvl in _LEVELS:
            return apply(lvl, persist=False)
    except (OSError, ValueError):
        pass
    return apply("rough", persist=False)
