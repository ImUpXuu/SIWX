"""P1-7 回归：图片内存缓存必须有字节预算，不能只限条数。

修复前 _IMG_CACHE 只有 200 条的条数上限、无字节预算：聊天页每张图带
hq=1（高清原图中位几百 KB、可达数 MB），200 张 × 数 MB 可把进程内存
推到数百 MB 到 GB 级常驻。修复后仿照 api_chat._decoded_cache_put 的
「字节预算 + LRU」先例（128MB），条数上限降级为第二道闸。

全部使用合成字节串（带合法 JPEG 头尾的假图）与占位符账号（wxid_test），
不读取任何真实图片或真实账号数据。
"""
import unittest
from unittest import mock

from tests._base import IsolatedRootCase

# 合成"合法" JPEG：SOI 头 + 填充 + EOI 尾，能通过 _plausible_image 校验
JPEG_OK = b"\xff\xd8\xff" + b"\x00" * 100 + b"\xff\xd9"

ACCOUNT = "wxid_test"


class TestImageCacheByteBudget(IsolatedRootCase):
    """_img_cache_put 的字节预算 + LRU 语义。"""

    def setUp(self):
        super().setUp()
        from siwx import media
        self.media = media
        self._reset()

    def tearDown(self):
        self._reset()
        super().tearDown()

    def _reset(self):
        with self.media._IMG_CACHE_LOCK:
            self.media._IMG_CACHE.clear()
            self.media._IMG_CACHE_BYTES = 0

    def _put(self, key, body):
        self.media._img_cache_put(key, body, "image/jpeg")

    def test_budget_evicts_lru_and_total_stays_within(self):
        one = 1000
        with mock.patch.object(self.media, "_IMG_CACHE_MAX_BYTES", one * 3), \
                mock.patch.object(self.media, "_IMG_CACHE_MAX", 100):
            self._put("k1", b"a" * one)
            self._put("k2", b"b" * one)
            self._put("k3", b"c" * one)
            self.assertEqual(list(self.media._IMG_CACHE), ["k1", "k2", "k3"])
            self.assertEqual(self.media._IMG_CACHE_BYTES, one * 3)
            # 触碰 k1（LRU 里变最新，顺序变 k2,k3,k1），再写入 k4 顶破预算：
            # 应淘汰最久未用的 k2 而不是刚碰过的 k1
            self.media._IMG_CACHE.move_to_end("k1")
            self._put("k4", b"d" * one)
            self.assertEqual(list(self.media._IMG_CACHE), ["k3", "k1", "k4"])
            self.assertLessEqual(self.media._IMG_CACHE_BYTES, one * 3,
                                 "总字节不得超过预算")
        # 计数与实际缓存内容一致（不会凭空记账）
        self.assertEqual(
            self.media._IMG_CACHE_BYTES,
            sum(len(v[0]) for v in self.media._IMG_CACHE.values()))

    def test_same_key_replacement_recounts_bytes(self):
        with mock.patch.object(self.media, "_IMG_CACHE_MAX_BYTES", 10_000):
            self._put("k", b"a" * 8000)
            self.assertEqual(self.media._IMG_CACHE_BYTES, 8000)
            # 同键替换：先扣旧占用再计新占用，不能双份记账
            self._put("k", b"b" * 3000)
            self.assertEqual(self.media._IMG_CACHE_BYTES, 3000)
            self.assertEqual(len(self.media._IMG_CACHE), 1)

    def test_single_oversized_entry_self_evicts(self):
        # 单条就超预算：被自身淘汰（缓存为空）——预算是硬闸，与
        # api_chat._decoded_cache_put 同策略，GB 级单图本就不该常驻
        with mock.patch.object(self.media, "_IMG_CACHE_MAX_BYTES", 100):
            self._put("big", b"x" * 500)
            self.assertEqual(len(self.media._IMG_CACHE), 0)
            self.assertEqual(self.media._IMG_CACHE_BYTES, 0)

    def test_count_limit_still_applies_as_second_gate(self):
        with mock.patch.object(self.media, "_IMG_CACHE_MAX_BYTES", 10 ** 9), \
                mock.patch.object(self.media, "_IMG_CACHE_MAX", 3):
            for i in range(5):
                self._put(f"k{i}", b"z" * 10)
            self.assertEqual(len(self.media._IMG_CACHE), 3,
                             "条数上限仍是第二道闸")
            self.assertEqual(list(self.media._IMG_CACHE),
                             ["k2", "k3", "k4"])

    def test_get_image_counts_bytes_into_budget(self):
        # 端到端：get_image 备份复用路径命中后经 _img_cache_put 入缓存，
        # 字节计数必须同步（旧实现只维护条数）
        acc = self.tmp / "out" / ACCOUNT
        img_dir = (acc / "media_backup" / "images" / "chatmd5"
                   / "2026-01" / "Img")
        img_dir.mkdir(parents=True)
        (img_dir / ("a" * 32 + ".jpeg")).write_bytes(JPEG_OK)
        body, ctype = self.media.get_image(ACCOUNT, "a" * 32, acc)
        self.assertEqual(body, JPEG_OK)
        self.assertEqual(ctype, "image/jpeg")
        self.assertEqual(self.media._IMG_CACHE_BYTES, len(JPEG_OK))
        self.assertEqual(len(self.media._IMG_CACHE), 1)


if __name__ == "__main__":
    unittest.main()
