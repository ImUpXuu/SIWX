"""Linux 专用密钥提取支撑：ELF 钩子定位 + 进程内 INT3 shim。"""

from siwx.linux_hook.elfhook import HookNotFoundError, find_hook_offset

__all__ = ["HookNotFoundError", "find_hook_offset"]
