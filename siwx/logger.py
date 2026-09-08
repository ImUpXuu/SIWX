"""日志系统 — 双模式（粗略/详细）+ 脱敏导出。

模式说明:
- ROUGH: 仅记录关键步骤（当前行为）
- DETAILED: 每个模块记录所有可能的失败点，便于排查

脱敏规则:
- 密钥: 64 位 hex → ****
- wxid: wxid_xxx → wxid_*** (保留前缀)
- 路径: 绝对路径 → 相对路径
- 用户名: 保留首字符，其余脱敏
"""
import hashlib
import re
import threading
import time
from enum import Enum
from pathlib import Path


class LogLevel(Enum):
    ROUGH = "rough"         # 粗略模式：仅关键步骤
    DETAILED = "detailed"   # 详细模式：全量埋点


# 全局状态
_lock = threading.Lock()
_log_level = LogLevel.ROUGH
_LOG_RING: list = []          # 环形缓冲（供日志页展示）
_LOG_RING_MAX = 5000          # 详细模式保留更多
_FILE_LOG: list = []          # 完整日志（供导出）
_FILE_LOG_MAX = 50000         # 最多保留 5 万条


def set_level(level: LogLevel):
    """设置日志模式。"""
    global _log_level, _LOG_RING_MAX
    with _lock:
        _log_level = level
        _LOG_RING_MAX = 5000 if level == LogLevel.DETAILED else 2000
    info("log", f"日志模式切换为: {level.value}")


def get_level() -> LogLevel:
    with _lock:
        return _log_level


def _now_ms() -> int:
    return int(time.time() * 1000)


def _append(ts: int, level: str, module: str, msg: str):
    entry = [ts, level, module, msg]
    with _lock:
        _LOG_RING.append(entry)
        if len(_LOG_RING) > _LOG_RING_MAX:
            del _LOG_RING[:len(_LOG_RING) - _LOG_RING_MAX]
        _FILE_LOG.append(entry)
        if len(_FILE_LOG) > _FILE_LOG_MAX:
            del _FILE_LOG[:len(_FILE_LOG) - _FILE_LOG_MAX]


def rough(module: str, msg: str):
    """粗略模式日志（始终记录）。"""
    ts = _now_ms()
    _append(ts, "INFO", module, msg)
    # 同时输出到控制台
    print(f"[{time.strftime('%H:%M:%S')}] [{module}] {msg}")


def detailed(module: str, msg: str):
    """详细模式日志（仅 DETAILED 模式记录）。"""
    if _log_level == LogLevel.DETAILED:
        ts = _now_ms()
        _append(ts, "DEBUG", module, msg)


def info(module: str, msg: str):
    """信息日志（始终记录）。"""
    ts = _now_ms()
    _append(ts, "INFO", module, msg)
    print(f"[{time.strftime('%H:%M:%S')}] [{module}] {msg}")


def warn(module: str, msg: str):
    """警告日志（始终记录）。"""
    ts = _now_ms()
    _append(ts, "WARN", module, msg)
    print(f"[{time.strftime('%H:%M:%S')}] [WARN] [{module}] {msg}")


def error(module: str, msg: str):
    """错误日志（始终记录）。"""
    ts = _now_ms()
    _append(ts, "ERROR", module, msg)
    print(f"[{time.strftime('%H:%M:%S')}] [ERROR] [{module}] {msg}")


def get_logs(limit: int = 2000, detailed_only: bool = False) -> list:
    """获取日志（供日志页展示）。"""
    with _lock:
        logs = list(_LOG_RING)
    if detailed_only:
        logs = [l for l in logs if l[1] == "DEBUG"]
    return logs[-limit:]


def export_logs(start_ts=None, end_ts=None, desensitize=True) -> str:
    """导出日志为文本，支持时间范围和脱敏。"""
    with _lock:
        logs = list(_FILE_LOG)

    # 时间范围过滤
    if start_ts:
        logs = [l for l in logs if l[0] >= start_ts]
    if end_ts:
        logs = [l for l in logs if l[0] <= end_ts]

    lines = []
    for ts, level, module, msg in logs:
        time_str = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(ts / 1000))
        ms = ts % 1000
        if desensitize:
            msg = desensitize_msg(msg)
        lines.append(f"{time_str}.{ms:03d} [{level:7}] [{module}] {msg}")

    return "\n".join(lines)


# ── 脱敏规则 ──────────────────────────────────────────────────

# 匹配 64 位 hex 密钥
_KEY_RE = re.compile(r"[0-9a-fA-F]{64}")
# 匹配 wxid_xxx
_WXID_RE = re.compile(r"wxid_[a-zA-Z0-9_\-]+")
# 匹配 gh_xxx (公众号)
_GH_RE = re.compile(r"gh_[a-zA-Z0-9_\-]+")
# 匹配 @chatroom
_CHATROOM_RE = re.compile(r"\d+@chatroom")
# 匹配绝对路径 (Windows/Mac)
_PATH_RE = re.compile(r"([A-Za-z]:[\\/][^\s]+|[/][^\s]+)")


def desensitize_msg(msg: str) -> str:
    """脱敏单条日志消息。"""
    # 密钥 → ****
    msg = _KEY_RE.sub("****", msg)
    # wxid → wxid_*** (保留前缀)
    msg = _WXID_RE.sub(lambda m: m.group()[:5] + "***", msg)
    # gh_ → gh_***
    msg = _GH_RE.sub(lambda m: m.group()[:3] + "***", msg)
    # @chatroom → ***@chatroom
    msg = _CHATROOM_RE.sub("***@chatroom", msg)
    # 路径 → 仅保留文件名
    msg = _PATH_RE.sub(lambda m: Path(m.group()).name, msg)
    return msg
