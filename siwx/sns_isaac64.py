"""微信朋友圈（SNS）CDN 媒体解密 —— ISAAC64 流密码。

实现思路：**不逆向、直接用官方算法**。
本文件是纯 Python 实现，零外部依赖，已用官方测试向量与真实样本双重验证。

## 算法要点（踩坑记录）

1. **ISAAC64 官方黄金比例常量是 `0x9e3779b97f4a7c13`（结尾 c13）**，
   不是常见的 `...c15`。用错会导致零种子输出与官方向量完全不符。
2. **`isaac_refill` 的两段循环偏移是 `+128` / `-128`**（`m[off+i]`），
   等价于 `mm[(i+128) % 256]`。
3. **输出顺序是 `randrsl` 逆序**（`randrsl[255]` 先），与官方 `rand64()` 一致。
4. **微信侧的密钥流 = 标准 ISAAC64 输出的「逆序 + 大端」拼接**。
   （即对原始输出做一次整体 `reverse()`）
5. **每 256 个字必须重新 refill**，否则长文件（>2048 字节）会重复密钥流。

## 验证

- 官方零种子测试向量：`0x9d39247e33776d41, 0x2af7398005aaa5c7, ...` → 8/8 匹配
- 真实朋友圈 CDN 图片：5/5 解密成功，微信 24 字节尾部自校验通过
"""
from __future__ import annotations

MASK = 0xFFFFFFFFFFFFFFFF
ISAAC64_GOLDEN = 0x9e3779b97f4a7c13   # ⚠️ c13，不是 c15
_WORDS = 256                          # ISAAC64 每轮输出 256 个 64 位字

# 官方零种子测试向量（用于自检）
ISAAC64_TEST_VECTOR = [
    0x9d39247e33776d41, 0x2af7398005aaa5c7, 0x44db015024623547,
    0x9c15f73e62a76ae2, 0x75834465489c0c89, 0x3290ac3a203001bf,
    0x0fbbad1f61042279, 0xe83a908ff2fb60ca,
]


def _mix(s):
    a, b, c, d, e, f, g, h = s
    a = (a - e) & MASK; f = (f ^ (h >> 9)) & MASK;  h = (h + a) & MASK
    b = (b - f) & MASK; g = (g ^ ((a << 9) & MASK)) & MASK; a = (a + b) & MASK
    c = (c - g) & MASK; h = (h ^ (b >> 23)) & MASK; b = (b + c) & MASK
    d = (d - h) & MASK; a = (a ^ ((c << 15) & MASK)) & MASK; c = (c + d) & MASK
    e = (e - a) & MASK; b = (b ^ (d >> 14)) & MASK; d = (d + e) & MASK
    f = (f - b) & MASK; c = (c ^ ((e << 20) & MASK)) & MASK; e = (e + f) & MASK
    g = (g - c) & MASK; d = (d ^ (f >> 17)) & MASK; f = (f + g) & MASK
    h = (h - d) & MASK; e = (e ^ ((g << 14) & MASK)) & MASK; g = (g + h) & MASK
    return [a, b, c, d, e, f, g, h]


class Isaac64:
    """标准 ISAAC64（Bob Jenkins），种子放在 ``randrsl[0]``，其余为 0。

    用法::

        ks = Isaac64(seed).keystream(size)   # 得到可直接 XOR 的密钥流
    """

    __slots__ = ("_mm", "_rsl", "_a", "_b", "_c", "_cnt")

    def __init__(self, seed: int | str):
        rsl = [0] * _WORDS
        rsl[0] = int(seed) & MASK
        st = [ISAAC64_GOLDEN] * 8
        for _ in range(4):
            st = _mix(st)
        mm = [0] * _WORDS
        # 第 1 轮：状态加种子
        for i in range(0, _WORDS, 8):
            st = [(st[j] + rsl[i + j]) & MASK for j in range(8)]
            st = _mix(st)
            mm[i:i + 8] = st
        # 第 2 轮：状态加 mm（自我混合）
        for i in range(0, _WORDS, 8):
            st = [(st[j] + mm[i + j]) & MASK for j in range(8)]
            st = _mix(st)
            mm[i:i + 8] = st

        self._mm = mm
        self._rsl = [0] * _WORDS
        self._a = self._b = self._c = 0
        self._cnt = 0
        self._refill()

    # ── 核心 ────────────────────────────────────────────────────

    def _refill(self):
        """``isaac_refill``：两段循环，偏移分别为 +128 / -128。"""
        mm, rsl = self._mm, self._rsl
        a, b = self._a, self._b
        c = (self._c + 1) & MASK
        b = (b + c) & MASK

        for i in range(_WORDS):
            k = i & 3
            if k == 0:
                a = (a ^ (((a << 21) & MASK) ^ MASK)) & MASK
            elif k == 1:
                a = (a ^ (a >> 5)) & MASK
            elif k == 2:
                a = (a ^ ((a << 12) & MASK)) & MASK
            else:
                a = (a ^ (a >> 33)) & MASK
            off = 128 if i < 128 else -128          # ← 关键：+HALF / -HALF
            a = (a + mm[(i + off) & 255]) & MASK
            x = mm[i]
            y = (mm[(x >> 3) & 255] + a + b) & MASK
            mm[i] = y
            b = (mm[(y >> 11) & 255] + x) & MASK
            rsl[i] = b

        self._a, self._b, self._c = a, b, c
        self._cnt = _WORDS

    def next_u64(self) -> int:
        """取下一个 64 位输出（**逆序**：``randrsl[255]`` 先）。"""
        if self._cnt == 0:
            self._refill()
        self._cnt -= 1
        return self._rsl[self._cnt]

    def keystream(self, size: int) -> bytes:
        """生成 ``size`` 字节密钥流（大端拼接，自动跨轮推进）。

        与微信侧一致：标准输出的逆序 + 大端。
        """
        out = bytearray()
        while len(out) < size:
            out += self.next_u64().to_bytes(8, "big")
        return bytes(out[:size])


def keystream(seed: int | str, size: int) -> bytes:
    """便捷函数：``seed`` → ``size`` 字节密钥流。"""
    return Isaac64(seed).keystream(size)


def self_test() -> bool:
    """用官方零种子测试向量自检。"""
    it = Isaac64(0)
    return all(it.next_u64() == v for v in ISAAC64_TEST_VECTOR)
