"""测试共享夹具：SIWX_ROOT 隔离 + 模块级缓存清空 + 零插件。

此前各套件各自实现隔离且**口径不一致**：test_regressions 清空 api_chat/paths
缓存与插件注册表，test_sns 只清 paths._PATH_CACHE —— `unittest discover` 把
多个套件放进同一进程时，残留的模块级单例会产生顺序依赖型 flaky（单文件绿、
全量偶发红）。统一到本基类：每个用例拿到干净的临时 SIWX_ROOT 与零插件状态。
"""
import os
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


class IsolatedRootCase(unittest.TestCase):
    """把 SIWX_ROOT / SIWX_DATA_DIR 指向临时目录，并在前后清空模块级缓存。

    SIWX_DATA_DIR 单独隔离（tmp/data）：keystore、log_level、media_key、
    wechat_paths.json 等持久化配置都落在 data_dir()，只隔离 SIWX_ROOT 时
    它们仍会写真实 %LOCALAPPDATA%/Application Support，测试之间互相污染、
    也会碰到用户真实配置。

    顺序注意：clear_caches 里 `registry._loaded = True`（已加载但为空），
    保证消费方短路跳过，而不是重新触发目录扫描。
    """

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(prefix="siwx_test_",
                                                ignore_cleanup_errors=True)
        self.tmp = Path(self._tmp.name)
        self._old_root = os.environ.get("SIWX_ROOT")
        self._old_data_dir = os.environ.get("SIWX_DATA_DIR")
        os.environ["SIWX_ROOT"] = str(self.tmp)
        os.environ["SIWX_DATA_DIR"] = str(self.tmp / "data")
        os.environ["SIWX_NO_PLUGINS"] = "1"
        self.clear_caches()

    def tearDown(self):
        if self._old_root is None:
            os.environ.pop("SIWX_ROOT", None)
        else:
            os.environ["SIWX_ROOT"] = self._old_root
        if self._old_data_dir is None:
            os.environ.pop("SIWX_DATA_DIR", None)
        else:
            os.environ["SIWX_DATA_DIR"] = self._old_data_dir
        self.clear_caches()
        try:
            self._tmp.cleanup()
        except OSError:
            pass    # Windows 上临时目录被索引/杀软瞬时占用，交给系统回收

    @staticmethod
    def clear_caches():
        from siwx import api_chat, paths
        paths._PATH_CACHE.clear()
        api_chat._SHARD_INDEX.clear()
        api_chat._CONTACT_CACHE.clear()
        api_chat._SESSION_CACHE.clear()
        try:
            from siwx.plugins.registry import registry
        except Exception:
            return
        for ns in vars(registry).values():
            if hasattr(ns, "items") and isinstance(ns.items, list):
                ns.items = []
        registry.metas = {}
        registry.report = None
        registry._loaded = True       # 已加载但为空 == 零插件
