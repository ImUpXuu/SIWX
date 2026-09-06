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
