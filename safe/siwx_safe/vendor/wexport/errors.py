"""统一异常类型与退出码。"""

from __future__ import annotations


class WeexportError(Exception):
    """可预期的工具错误：消息直接展示给用户，不带堆栈。"""

    exit_code = 1


class DiscoveryError(WeexportError):
    """找不到微信数据目录/账号。"""

    exit_code = 2


class KeyError_(WeexportError):
    """密钥缺失、错误或无法提取。"""

    exit_code = 3


class DecryptError(WeexportError):
    """数据库解密失败（页头校验不通过、页大小不符等）。"""

    exit_code = 4


class DatabaseError(WeexportError):
    """解密后的数据库无法打开或 schema 无法识别。"""

    exit_code = 5


class ExportError(WeexportError):
    """导出过程失败。"""

    exit_code = 6


class UsageError(WeexportError):
    """命令行参数错误。"""

    exit_code = 7
