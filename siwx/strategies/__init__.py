"""策略注册表 —— 插件缝。

每个策略是一个模块级函数 extract(ctx) -> int，ctx 携带：
  db_dir / entries / page1_by_salt / key_map / attrib / log / use_memory
策略只产出候选并经统一 HMAC 验证入库（propose-verify 分离）；
外部插件只需向 STRATEGY_REGISTRY 追加同签名函数即可接入。

日志双写（审计 §2.1）：策略层只持有注入的 ctx["log"]，看不到 logger 模块；
双写发生在本注入点——每个策略运行前把 ctx["log"] 重新包装为带策略标签
（strategy:<name>）的双写回调，任务轨原样转发、结构化轨受 Debug 开关控制。
"""
import platform
import traceback

from siwx import logger as _slog

if platform.system() == "Windows":
    # Windows: 密钥库 → MMKV 离线 → Config.Cipher 主力 → 内存字面量兜底
    from siwx.strategies import config_cipher, keystore_source, mmkv, memscan
    STRATEGY_REGISTRY = [keystore_source, mmkv, config_cipher, memscan]
    # 依赖微信进程内存扫描的策略（use_memory=False 时跳过）
    _PROCESS_DEPENDENT = {config_cipher, memscan}
else:
    # 非 Windows（macOS/Linux）：不加载依赖 winproc(ctypes.windll) 的 Windows 策略，
    # 否则 import 包即崩溃。macOS 额外启用 LLDB 策略。
    from siwx.strategies import keystore_source, mmkv
    STRATEGY_REGISTRY = [keystore_source, mmkv]
    _PROCESS_DEPENDENT = set()
    if platform.system() == "Darwin":
        from siwx.strategies import macos_lldb
        STRATEGY_REGISTRY.append(macos_lldb)
        _PROCESS_DEPENDENT.add(macos_lldb)


def _tagged_ctx(ctx, module: str) -> dict:
    """以原始任务轨回调为基础，产出带策略标签的双写 ctx（防重复包装）。

    同时注入 ctx["dbg"]（仅结构化轨、详细模式可见）：策略层的失败细分/
    计数埋点用它，避免刷爆任务面板（审计 §2.1 双通道方案）。
    插件策略可能不认识 dbg——一律用 ctx.get("dbg") 并回退 ctx["log"]。"""
    raw = getattr(ctx.get("log"), "task", ctx.get("log"))
    sctx = dict(ctx)
    sctx["log"] = _slog.dual_log(raw, module)
    sctx["dbg"] = _slog.dbg_log(module)
    return sctx


def run_strategies(ctx):
    ran_any = False
    for mod in STRATEGY_REGISTRY:
        name = mod.__name__.rsplit(".", 1)[-1]
        if len(ctx["key_map"]) >= len(ctx["page1_by_salt"]):
            if ran_any:
                _slog.detailed("strategy", f"策略链早停: 已覆盖全部 salt（止于 {name} 前）")
            break
        # use_memory=False 时跳过依赖微信进程的策略（全局收割已覆盖）
        if not ctx.get("use_memory", True) and mod in _PROCESS_DEPENDENT:
            _slog.detailed("strategy", f"跳过 strategy:{name} (use_memory=False)")
            continue
        _slog.detailed("strategy", f"执行策略 strategy:{name}")
        ran_any = True
        try:
            mod.extract(_tagged_ctx(ctx, f"strategy:{name}"))
        except Exception as e:  # 单策略失败不影响整链
            ctx["log"](f"[策略 {name}] 异常: {type(e).__name__}: {e}")
            _slog.warn("strategy", f"策略 {name} 异常: {type(e).__name__}: {e}")
            _slog.detailed("strategy",
                           f"策略 {name} traceback:\n{traceback.format_exc(limit=5)}")

    _run_plugin_strategies(ctx)


def _run_plugin_strategies(ctx):
    """运行插件贡献的密钥策略（追加在内置策略之后，逐条隔离）。

    插件策略与内置策略同签名：extract(ctx) -> int（返回值忽略，统一走 propose-verify）。
    某个插件抛异常只跳过它自己，不影响其它策略与已验证结果。
    """
    try:
        from siwx.plugins import ensure_loaded, registry
        ensure_loaded()
        ns = registry.key_strategies
    except Exception as e:
        _slog.warn("strategy", f"插件策略加载失败: {type(e).__name__}: {e}")
        return
    if not ns:
        return

    process_dependent = ns.process_dependent()
    for _i, s in ns.sorted_items():
        label = s.meta.name if s.meta else (s.name or "?")
        if len(ctx["key_map"]) >= len(ctx["page1_by_salt"]):
            _slog.detailed("strategy", f"插件策略链早停: 已覆盖全部 salt（止于 {label} 前）")
            break
        if not ctx.get("use_memory", True) and s.fn in process_dependent:
            _slog.detailed("strategy", f"跳过插件策略 {label} (use_memory=False)")
            continue
        _slog.detailed("strategy", f"执行插件策略 {label}")
        try:
            s.fn(_tagged_ctx(ctx, f"strategy:{label}"))
        except Exception as e:
            ctx["log"](f"[插件策略 {label}] 异常: {type(e).__name__}: {e}")
            _slog.warn("strategy", f"插件策略 {label} 异常: {type(e).__name__}: {e}")
            _slog.detailed("strategy",
                           f"插件策略 {label} traceback:\n{traceback.format_exc(limit=5)}")
