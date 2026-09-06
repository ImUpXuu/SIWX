"""策略注册表 —— 插件缝。

每个策略是一个模块级函数 extract(ctx) -> int，ctx 携带：
  db_dir / entries / page1_by_salt / key_map / attrib / log / use_memory
策略只产出候选并经统一 HMAC 验证入库（propose-verify 分离）；
外部插件只需向 STRATEGY_REGISTRY 追加同签名函数即可接入。
"""
import platform

from siwx.strategies import config_cipher, keystore_source, mmkv, memscan

# 顺序即优先级：密钥库秒回 → MMKV 离线 → Config.Cipher 主力 → 内存字面量兜底
STRATEGY_REGISTRY_WIN = [keystore_source, mmkv, config_cipher, memscan]

# macOS 专用策略（LLDB + PBKDF2）
if platform.system() == "Darwin":
    try:
        from siwx.strategies import macos_lldb
        STRATEGY_REGISTRY = STRATEGY_REGISTRY_WIN + [macos_lldb]
    except ImportError:
        STRATEGY_REGISTRY = STRATEGY_REGISTRY_WIN
else:
    STRATEGY_REGISTRY = STRATEGY_REGISTRY_WIN

# 依赖微信进程的策略（use_memory=False 时跳过）
_PROCESS_DEPENDENT = {config_cipher, memscan}
if platform.system() == "Darwin":
    try:
        from siwx.strategies import macos_lldb as _macos_lldb_mod
        _PROCESS_DEPENDENT.add(_macos_lldb_mod)
    except ImportError:
        pass


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
