"""日志系统 — 双模式（粗略/详细）+ 脱敏导出。

模式说明:
- ROUGH: 仅记录关键步骤（当前行为）
- DETAILED: 每个模块记录所有可能的失败点，便于排查

留存策略（P0 改造）:
- detailed 埋点**始终写入 siwx.log 文件与 _FILE_LOG**（带容量上限），
  Debug 开关只控制环形缓冲/UI 展示——偶发问题无需"先开 Debug 再复现"，
  与主流桌面产品（微信/VS Code/Chrome）"全量写文件、门控只做展示"对齐。
- WARN/ERROR 无条件桥接进 siwx.log 文件通道。
- 高频逐条埋点可传 ring=False：只进文件轨，不进环形缓冲，
  防止大导出冲掉日志页早期关键记录（埋点越密丢得越快）。

脱敏规则:
- 密钥: 64 位 hex → ****
- wxid: wxid_xxx → wxid_*** (保留前缀)
- 路径: 绝对路径 → 相对路径
- 用户名: 保留首字符，其余脱敏
"""
import hashlib
import logging as _stdlib_logging
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
_log_level = LogLevel.ROUGH  # 默认粗略模式
_LOG_RING: list = []          # 环形缓冲（供日志页展示）
_LOG_RING_MAX = 5000          # 详细模式保留更多
_FILE_LOG: list = []          # 完整日志（供导出）
_FILE_LOG_MAX = 50000         # 最多保留 5 万条

# 任务级关联 ID（审计差距 3）：任务轨与结构化轨靠它对账，跨线程用
# threading.local——run_export 在 job 线程里执行，天然继承所属任务 ID
_job_local = threading.local()


def set_job_id(jid: str):
    """设置当前线程的任务 ID（server 任务入口调用；导出子线程自动继承）。"""
    _job_local.jid = jid


def get_job_id() -> str:
    return getattr(_job_local, "jid", "")


def job_prefix() -> str:
    """日志行前缀形式的任务 ID（无任务时为空前缀，调用方直接拼接）。"""
    jid = get_job_id()
    return f"[{jid}] " if jid else ""


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


def _append(ts: int, level: str, module: str, msg: str, *, ring: bool = True):
    entry = [ts, level, module, msg]
    with _lock:
        if ring:
            _LOG_RING.append(entry)
            if len(_LOG_RING) > _LOG_RING_MAX:
                del _LOG_RING[:len(_LOG_RING) - _LOG_RING_MAX]
        _FILE_LOG.append(entry)
        if len(_FILE_LOG) > _FILE_LOG_MAX:
            del _FILE_LOG[:len(_FILE_LOG) - _FILE_LOG_MAX]


def _bridge(level_name: str, module: str, msg: str):
    """结构化轨 → siwx.log 文件桥（P0）：detailed/warn/error 无条件落盘。

    只在 "siwx" logger 已配置 handler 时桥接（多进程子进程未跑
    logging_setup，无 handler，静默跳过）。控制台通道已由 _cprint 承担，
    用 extra 标记让 logging_setup 的控制台 handler 丢弃，避免双份输出；
    RotatingFileHandler 保留标记记录 → 文件轨全量、控制台不重复。
    rough/info 不桥接：任务轨 server._log 已覆盖文件通道，桥了只会刷屏。"""
    try:
        std = _stdlib_logging.getLogger("siwx")
        if not std.handlers:
            return
        text = f"[{module}] {msg}"
        if level_name == "DEBUG":
            std.debug(text, extra={"_siwx_struct": True})
        elif level_name == "WARN":
            std.warning(text, extra={"_siwx_struct": True})
        elif level_name == "ERROR":
            std.error(text, extra={"_siwx_struct": True})
    except Exception:
        pass


_CONSOLE = None


def _cprint(line: str) -> None:
    """控制台输出。打包成无控制台（--noconsole）运行时 stdout 可能为 None，
    print 会抛 AttributeError——控制台输出只是附加通道，失败必须静默。"""
    try:
        if _CONSOLE is not None:
            print(line, file=_CONSOLE)
        else:
            print(line)
    except Exception:
        pass


def set_console(stream) -> None:
    """把 rough/info/warn/error 的控制台输出切到指定流。

    MCP 走 stdio 的 JSON-RPC：stdout 里混进任何非 JSON 字节都会破坏协议帧，
    而共享的 siwx.logger 一直往 stdout 打日志（export_chat 每次必脏）。
    """
    global _CONSOLE
    _CONSOLE = stream


def rough(module: str, msg: str):
    """粗略模式日志（始终记录）。"""
    ts = _now_ms()
    _append(ts, "INFO", module, msg)
    # 同时输出到控制台
    _cprint(f"[{time.strftime('%H:%M:%S')}] [{module}] {msg}")


def detailed(module: str, msg: str, *, ring: bool = True):
    """详细埋点。文件轨（_FILE_LOG + siwx.log）**无条件**记录——用户遇到
    偶发问题后开 Debug 也能在文件里找到当时记录，无需先开再复现；环形
    缓冲/UI 仍受开关控制。ring=False 用于逐条高频埋点（大导出防冲掉
    日志页早期关键记录），此类记录只在 detailed 模式可见于文件/导出。"""
    ts = _now_ms()
    _append(ts, "DEBUG", module, msg, ring=ring)
    _bridge("DEBUG", module, msg)


def info(module: str, msg: str):
    """信息日志（始终记录）。"""
    ts = _now_ms()
    _append(ts, "INFO", module, msg)
    _cprint(f"[{time.strftime('%H:%M:%S')}] [{module}] {msg}")


def dual_log(task_log, module: str):
    """任务轨 + 结构化轨双写适配器（审计 §2.1 模式 A）。

    提取/策略链只持有注入的任务回调（ctx["log"] / log=print），看不到本模块，
    插件策略更没有 logger 可用——双写必须发生在注入点，而不是策略内部。
    返回同签名闭包：任务轨原样转发（保留人类可读全文），结构化轨走 detailed
    （受 Debug 开关控制，落盘前统一脱敏）。"""
    def _write(msg) -> None:
        text = msg if isinstance(msg, str) else str(msg)
        try:
            task_log(text)
        except Exception:
            pass
        detailed(module, desensitize_msg(text))
    _write.task = task_log          # 原始任务轨回调（策略链按策略重新打标签时用）
    _write.module = module
    _write._siwx_dual = True
    return _write


def ensure_dual(task_log, module: str):
    """已是双写回调则原样返回（防嵌套入口重复包装 → 结构化轨重复条目）。"""
    if getattr(task_log, "_siwx_dual", False):
        return task_log
    return dual_log(task_log, module)


def dbg_log(module: str):
    """仅结构化轨（详细模式）的写入器——给只持有注入回调的策略层当 ctx["dbg"]。

    与 dual_log 的区别：不写任务轨。用于"开了 Debug 才该出现"的失败细分/
    计数埋点，避免刷爆任务面板（审计 §2.1 双通道方案）。"""
    def _dbg(msg) -> None:
        detailed(module, desensitize_msg(msg if isinstance(msg, str) else str(msg)))
    return _dbg


def warn(module: str, msg: str):
    """警告日志（始终记录，并无条件桥接进 siwx.log 文件）。"""
    ts = _now_ms()
    _append(ts, "WARN", module, msg)
    _cprint(f"[{time.strftime('%H:%M:%S')}] [WARN] [{module}] {msg}")
    _bridge("WARN", module, msg)


def error(module: str, msg: str):
    """错误日志（始终记录，并无条件桥接进 siwx.log 文件）。"""
    ts = _now_ms()
    _append(ts, "ERROR", module, msg)
    _cprint(f"[{time.strftime('%H:%M:%S')}] [ERROR] [{module}] {msg}")
    _bridge("ERROR", module, msg)


def get_logs(limit: int = 2000, detailed_only: bool = False,
             desensitize: bool = True) -> list:
    """获取日志（供日志页展示）。默认脱敏：密钥/账号标识/路径不外泄。"""
    with _lock:
        # 拷贝条目：ring 与文件缓冲共享同一批 list 对象，不能原地改
        logs = [list(l) for l in _LOG_RING]
    if detailed_only:
        logs = [l for l in logs if l[1] == "DEBUG"]
    if desensitize:
        for l in logs:
            l[3] = desensitize_msg(l[3])
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
# 匹配绝对路径 (Windows/Mac)。审计 §7.7：原规则 `[/][^\s]+` 会把 `17/5684`、
# `img=3/5` 这类比率/分数当路径匹配，脱敏后输出损坏文本（175684）。
# 收紧为带明确锚点的形式：盘符 / UNC / ~/ / 常见 POSIX 绝对根。
_PATH_RE = re.compile(
    r"(?:[A-Za-z]:[\\/][^\s]+"                      # Windows 盘符路径
    r"|\\\\[^\s]+"                                  # UNC 路径
    r"|~/[^\s]+"                                    # 用户目录简写
    r"|/(?:Users|home|tmp|var|etc|private|opt|usr|Applications|System|Library)/[^\s]+)"
)
# 匹配键值对形式的账号标识：account=/chat=/ownerId=/username=/talker= 后的值，
# 覆盖 wxid_/gh_ 之外的自定义微信号（如 wxalias_xxx）
_ACCOUNT_KV_RE = re.compile(r"\b(account|chat|ownerId|username|talker)=([^\s'\"&,;]+)")


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
    # 路径 → 仅保留末段（同时兼容 \ 与 / 分隔，POSIX 上 Path 不切反斜杠）
    msg = _PATH_RE.sub(lambda m: re.split(r"[\\/]", m.group())[-1], msg)
    # 键值对形式的账号标识 → 保留前 4 字符 + ***（wxid/gh/chatroom 已被上面
    # 的规则遮盖，值里含 *** 的跳过，保证重复脱敏幂等）
    def _mask_kv(m):
        v = m.group(2)
        if "***" in v or len(v) <= 4:
            return m.group()
        return f"{m.group(1)}={v[:4]}***"
    return _ACCOUNT_KV_RE.sub(_mask_kv, msg)
