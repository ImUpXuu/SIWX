"""策略注册表 —— 插件缝。

每个策略是一个模块级函数 extract(ctx) -> int，ctx 携带：
  db_dir / entries / page1_by_salt / key_map / attrib / log / use_memory
策略只产出候选并经统一 HMAC 验证入库（propose-verify 分离）；
外部插件只需向 STRATEGY_REGISTRY 追加同签名函数即可接入。
"""
import platform

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


def run_strategies(ctx):
    for mod in STRATEGY_REGISTRY:
        if len(ctx["key_map"]) >= len(ctx["page1_by_salt"]):
            break
        # use_memory=False 时跳过依赖微信进程的策略（全局收割已覆盖）
        if not ctx.get("use_memory", True) and mod in _PROCESS_DEPENDENT:
            continue
        try:
            mod.extract(ctx)
        except Exception as e:  # 单策略失败不影响整链
            ctx["log"](f"[策略 {mod.__name__.rsplit('.', 1)[-1]}] 异常: {e}")

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
    except Exception:
        return
    if not ns:
        return

    process_dependent = ns.process_dependent()
    for _i, s in ns.sorted_items():
        if len(ctx["key_map"]) >= len(ctx["page1_by_salt"]):
            break
        if not ctx.get("use_memory", True) and s.fn in process_dependent:
            continue
        label = s.meta.name if s.meta else (s.name or "?")
        try:
            s.fn(ctx)
        except Exception as e:
            ctx["log"](f"[插件策略 {label}] 异常: {e}")
