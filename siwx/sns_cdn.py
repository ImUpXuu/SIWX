"""微信朋友圈 CDN 媒体获取与解密。

方案来源：WeFlow（lurve1314/WeFlow）的成熟做法 —— **不依赖微信本地缓存，
直接从 CDN 下载**。本模块是其纯 Python 复刻，零第三方依赖。

## 为什么不用微信本地缓存

微信**不落盘**「动态 → 本地缓存文件名」的映射（该映射只在客户端内存里）。
实测证据（见 docs/sns-research-2026-09-29.md）：
- 缓存文件名与 XML 的 ``url@md5`` 属性**统计独立**（前 4 位重合 113 vs 随机期望 114.61）
- 32 个解密库 / 596 张表全字段零命中
- 本地缓存只覆盖 **19%** 的图片（微信只缓存浏览过的）

因此正确解法是：**用 XML 里的 URL 直接下载**。

## 关键实现要点（照搬 WeFlow）

1. **URL 重构**（``build_media_url``）::

       http:// → https://
       图片：/150|/200|/480 → /0        （取原图）
       追加 ?token=<token属性>&idx=1    （⚠️ 缺这个参数会 400）

   注意 ``url`` 元素有两个 token：**路径里的**和**``token`` 属性**，两者不同，
   必须用属性值做查询参数。

2. **请求头**：``User-Agent: MicroMessenger Client``（关键）

3. **解密**：响应头 ``x-enc: 1`` 表示加密。用 ``url@key`` 属性（纯十进制数）
   作为 ISAAC64 种子生成密钥流，逐字节 XOR。见 ``sns_isaac64``。

4. **缓存键**：``md5(normalize_cache_url(url))`` —— **去掉 token/idx**，
   因为 token 每次都变但资源是同一个。这是 WeFlow 踩过坑后改成的做法
   （旧版用完整 URL 的 md5，token 一变缓存全失效）。

5. **表情评论**走另一条路：``encrypt_url`` + ``aes_key`` → **AES-GCM**
   （nonce 在尾部 12 字节的格式优先）。
"""
from __future__ import annotations

import gzip
import hashlib
import os
import re
import ssl
import urllib.error
import urllib.request
import zlib
from pathlib import Path
from urllib.parse import urlsplit, parse_qsl, urlencode

from . import sns_isaac64 as isaac

# 微信客户端的 UA —— 缺了会被 CDN 拒绝
WECHAT_UA = "MicroMessenger Client"

# 下载时的通用请求头
DEFAULT_HEADERS = {
    "User-Agent": WECHAT_UA,
    "Accept": "*/*",
    "Accept-Encoding": "gzip, deflate",
    "Accept-Language": "zh-CN,zh;q=0.9",
    "Connection": "keep-alive",
}

# 图片尺寸档位 → 原图
_QUALITY_RE = re.compile(r"/(150|200|480)(?=$|\?)")

_IMAGE_SIGS = (
    (b"\xff\xd8\xff", "jpg", "image/jpeg"),
    (b"\x89PNG\r\n\x1a\n", "png", "image/png"),
    (b"GIF87a", "gif", "image/gif"),
    (b"GIF89a", "gif", "image/gif"),
    (b"BM", "bmp", "image/bmp"),
)


def detect_mime(data: bytes):
    """按魔数判断 (ext, mime)；无法识别返回 (None, None)。"""
    for sig, ext, mime in _IMAGE_SIGS:
        if data.startswith(sig):
            return ext, mime
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "webp", "image/webp"
    if len(data) > 12 and data[4:8] == b"ftyp":
        win = data[8:64].lower()
        if b"avif" in win or b"avis" in win:
            return "avif", "image/avif"
        if any(x in win for x in (b"heic", b"heix", b"hevc", b"mif1", b"msf1")):
            return "heic", "image/heic"
        return "mp4", "video/mp4"
    return None, None


def is_video_url(url: str) -> bool:
    """是否为视频 URL（排除 vweixinthumb 缩略图域名）。"""
    if not url:
        return False
    if "vweixinthumb" in url:
        return False
    return ("snsvideodownload" in url) or ("video" in url) or url.endswith(".mp4")


# ── URL 构造 ────────────────────────────────────────────────────────

def build_media_url(url: str, token: str | None = None,
                    is_video: bool | None = None) -> str:
    """按 WeFlow 的规则重构 CDN URL。

    :param url:      XML 里的 ``<url>`` 文本
    :param token:    XML 里 ``<url token="...">`` 属性值（**必须**，否则 400）
    :param is_video: None 时自动判断
    """
    if not url:
        return url
    if is_video is None:
        is_video = is_video_url(url)

    fixed = url.replace("http://", "https://", 1)

    if not is_video:
        # 图片：把缩略图档位换回原图
        path, sep, query = fixed.partition("?")
        path = _QUALITY_RE.sub("/0", path)
        fixed = path + (sep + query if sep else "")

    if not token:
        return fixed

    # 去掉已有的 token / idx，再统一追加
    path, sep, query = fixed.partition("?")
    if sep:
        kept = [kv for kv in query.split("&")
                if not kv.startswith("token=") and not kv.startswith("idx=")]
        fixed = path + ("?" + "&".join(kept) if kept else "")

    connector = "&" if "?" in fixed else "?"
    return f"{fixed}{connector}token={token}&idx=1"


def normalize_cache_url(url: str) -> str:
    """缓存键用的 URL 规范化：**去掉 token / idx 与 scheme**。

    token 每次请求都变，但指向同一份资源 —— 不规范化会导致缓存永久失效。
    """
    if not url:
        return url
    try:
        u = urlsplit(url)
        q = [(k, v) for k, v in parse_qsl(u.query, keep_blank_values=True)
             if k not in ("token", "idx")]
        query = urlencode(q)
        # 与 WeFlow 一致：host + path + 其余 query（不带 scheme）
        return f"{u.netloc}{u.path}{'?' + query if query else ''}"
    except ValueError:
        return re.sub(r"([?&])(?:token|idx)=[^&]*", r"\1", url).rstrip("?&")


def cache_key(url: str) -> str:
    """媒体缓存键：``md5(normalize_cache_url(url))``。"""
    return hashlib.md5(normalize_cache_url(url).encode()).hexdigest()


# ── 下载 ────────────────────────────────────────────────────────────

def _decompress(raw: bytes, encoding: str | None) -> bytes:
    if not encoding:
        return raw
    enc = encoding.lower()
    try:
        if "gzip" in enc:
            return gzip.decompress(raw)
        if "deflate" in enc:
            try:
                return zlib.decompress(raw)
            except zlib.error:
                return zlib.decompress(raw, -zlib.MAX_WBITS)
    except (OSError, zlib.error):
        return raw
    return raw


def fetch(url: str, timeout: float = 15.0, ctx=None) -> tuple[bytes, dict]:
    """下载 URL，返回 (body, headers)。body 已解压。"""
    if ctx is None:
        ctx = ssl.create_default_context()
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
    req = urllib.request.Request(url, headers=DEFAULT_HEADERS)
    with urllib.request.urlopen(req, timeout=timeout, context=ctx) as resp:
        raw = resp.read()
        headers = {k.lower(): v for k, v in resp.headers.items()}
    return _decompress(raw, headers.get("content-encoding")), headers


# ── 解密 ────────────────────────────────────────────────────────────

def decrypt_isaac(data: bytes, key: str | int) -> bytes:
    """ISAAC64 流解密（XOR）。

    ``key`` 来自 XML 的 ``url@key`` 属性，是十进制数字字符串。
    """
    ks = isaac.keystream(int(str(key).strip()), len(data))
    return bytes(a ^ b for a, b in zip(data, ks))


def _aes_gcm_key_tries(aes_key: str):
    """表情 AES 密钥的候选解释（照搬 WeFlow 的 5 种变体）。"""
    from Crypto.Cipher import AES  # noqa: F401  (延迟导入，仅在需要时用)
    tries = []
    hexs = re.sub(r"\s", "", aes_key or "")
    if len(hexs) >= 32 and re.fullmatch(r"[0-9a-fA-F]+", hexs):
        try:
            tries.append(bytes.fromhex(hexs[:32]))
        except ValueError:
            pass
        tries.append(hexs[:32].encode())
    if len(aes_key) >= 16:
        tries.append(aes_key.encode()[:16])
    tries.append(hashlib.md5(aes_key.encode()).digest())
    try:
        import base64
        b = base64.b64decode(aes_key)
        if len(b) >= 16:
            tries.append(b[:16])
    except Exception:
        pass
    # 去重保序
    seen, out = set(), []
    for k in tries:
        if k not in seen:
            seen.add(k)
            out.append(k)
    return out


def decrypt_emoji_aes(enc: bytes, aes_key: str) -> bytes | None:
    """表情评论的 AES-GCM 解密。

    优先按「nonce 在尾部」的格式 ``[ciphertext][nonce 12B][tag 16B]`` 尝试，
    其次按「nonce = key 前 12 字节」。
    """
    from Crypto.Cipher import AES
    if len(enc) <= 16:
        return None

    def try_gcm(key: bytes, nonce: bytes, ct: bytes, tag: bytes):
        if len(key) not in (16, 32):
            return None
        try:
            cipher = AES.new(key, AES.MODE_GCM, nonce=nonce)
            cipher.update(b"")
            pt = cipher.decrypt_and_verify(ct, tag)
        except (ValueError, KeyError):
            return None
        if detect_mime(pt)[0]:
            return pt
        for fn in (zlib.decompress, gzip.decompress):
            try:
                d = fn(pt)
                if detect_mime(d)[0]:
                    return d
            except Exception:
                pass
        return pt

    keys = _aes_gcm_key_tries(aes_key)

    # 格式 A：nonce 在尾部
    if len(enc) > 28:
        ct, nonce, tag = enc[:-28], enc[-28:-16], enc[-16:]
        for k in keys:
            r = try_gcm(k, nonce, ct, tag)
            if r and detect_mime(r)[0]:
                return r

    # 格式 B：nonce = key 前 12 字节
    ct, tag = enc[:-16], enc[-16:]
    for k in keys:
        r = try_gcm(k, k[:12], ct, tag)
        if r and detect_mime(r)[0]:
            return r
    return None


# ── 磁盘缓存 ────────────────────────────────────────────────────────

def _atomic_write(path: Path, data: bytes) -> bool:
    """原子写：先写 .part 再 replace。"""
    tmp = path.with_suffix(path.suffix + ".part")
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp.write_bytes(data)
        os.replace(tmp, path)
        return True
    except OSError:
        try:
            tmp.unlink(missing_ok=True)
        except Exception:
            pass
        return False


def cached_path(cache_dir, url: str, ext: str | None = None) -> Path | None:
    """缓存文件路径；``cache_dir`` 为空表示不启用缓存。"""
    if not cache_dir:
        return None
    d = Path(cache_dir)
    suffix = f".{ext}" if ext else ""
    return d / f"{cache_key(url)}{suffix}"


def read_cached(cache_dir, url: str):
    """读取已缓存媒体，返回 (bytes, ext, mime)；未命中返回 None。"""
    d = Path(cache_dir) if cache_dir else None
    if not d or not d.is_dir():
        return None
    stem = cache_key(url)
    for p in d.glob(f"{stem}.*"):
        if p.suffix == ".part":
            continue
        try:
            data = p.read_bytes()
        except OSError:
            return None
        ext = p.suffix.lstrip(".")
        mime = detect_mime(data)[1] or "application/octet-stream"
        return data, ext, mime
    return None


def _host_candidates(url: str):
    """域名候选：原样优先，其余补齐备用域名。"""
    try:
        host = urlsplit(url).netloc
    except ValueError:
        return [url]
    alts = ["mmsns.qpic.cn", "shmmsns.qpic.cn", "szmmsns.qpic.cn"]
    out = [url]
    for alt in alts:
        if alt != host:
            out.append(re.sub(r"//[^/]+/", f"//{alt}/", url, count=1))
    return out


# ── 对外主入口 ──────────────────────────────────────────────────────

def fetch_media(url: str, key: str | int | None = None,
                token: str | None = None,
                timeout: float = 15.0,
                cache_dir=None,
                hosts: int = 3) -> dict:
    """下载并按需解密一个朋友圈媒体资源（可选磁盘缓存 + 域名回退）。

    :param cache_dir: 媒体缓存目录；命中缓存时不再访问网络
    :param hosts:     最多尝试几个域名（微信 CDN 域名可能部分失效）
    :return: ``{ok, data, ext, mime, error, status, encrypted, cached}``
    """
    out = {"ok": False, "data": None, "ext": None, "mime": None,
           "error": None, "status": None, "encrypted": False, "cached": False}

    hit = read_cached(cache_dir, url) if cache_dir else None
    if hit:
        data, ext, mime = hit
        out.update(ok=True, data=data, ext=ext, mime=mime, cached=True)
        return out

    candidates = _host_candidates(build_media_url(url, token))[: max(1, hosts)]
    last_err = None
    for target in candidates:
        try:
            body, headers = fetch(target, timeout=timeout)
        except urllib.error.HTTPError as e:
            last_err = (e.code, f"HTTP {e.code}")
            continue
        except Exception as e:  # noqa: BLE001
            last_err = (None, f"{type(e).__name__}: {e}")
            continue

        x_enc = str(headers.get("x-enc", "")).strip()
        ext, mime = detect_mime(body)
        need = (x_enc == "1" or ext is None) and key not in (None, "", 0)
        if need and re.fullmatch(r"\d+", str(key).strip()):
            try:
                body = decrypt_isaac(body, key)
                out["encrypted"] = True
                ext, mime = detect_mime(body)
            except Exception as e:  # noqa: BLE001
                last_err = (None, f"decrypt: {e}")
                continue

        if ext is None:
            last_err = (None, "无法识别的媒体格式（密钥不匹配或 token 过期）")
            continue

        if cache_dir:
            _atomic_write(cached_path(cache_dir, url, ext), body)
        out.update(ok=True, data=body, ext=ext, mime=mime)
        return out

    if last_err:
        out["status"], out["error"] = last_err
    else:
        out["error"] = "下载失败"
    return out


def strip_wechat_tail(body: bytes) -> bytes:
    """去掉微信在图片末尾附加的 24 字节（``75f0d33c`` + 4 字节 0 + 自校验 md5）。

    实测 256/256 样本成立；无该尾部时原样返回。顺带可用于校验解密正确性。
    """
    i = body.rfind(b"\xff\xd9")
    if i < 0:
        return body
    tail = body[i + 2:]
    if len(tail) == 24 and tail[:4] == b"\x75\xf0\xd3\x3c":
        return body[:i + 2]
    return body


# ── 评论表情 ────────────────────────────────────────────────────────

def fetch_emoji(emoji: dict, cache_dir=None, timeout: float = 12.0) -> dict:
    """获取评论表情。

    **实测关键**：``sns_emoji_data/url`` 返回的就是**明文图片**
    （GIF / PNG / JPEG 直接可读），**不需要** AES 解密。

    ``encrypt_url`` + ``aes_key`` 只是备用路径 —— 仅当明文 url 不可用时才尝试，
    且需要 AES-GCM 解密（``decrypt_emoji_aes``，该路径**尚无真实样本验证**）。

    :param emoji: ``sns.parse_timeline`` 产出的 emoji dict
    :return: ``{ok, data, ext, mime, error, via}``
    """
    out = {"ok": False, "data": None, "ext": None, "mime": None,
           "error": None, "via": None}

    # 1) 明文 url（实测可用，优先）
    plain = (emoji or {}).get("url") or ""
    if plain:
        key = cache_key(plain)
        hit = read_cached(cache_dir, plain) if cache_dir else None
        if hit:
            data, ext, mime = hit
            return {**out, "ok": True, "data": data, "ext": ext, "mime": mime, "via": "cache"}
        try:
            body, _h = fetch(plain, timeout=timeout)
            ext, mime = detect_mime(body)
            if ext:
                if cache_dir:
                    _atomic_write(cached_path(cache_dir, plain, ext), body)
                return {**out, "ok": True, "data": body, "ext": ext, "mime": mime, "via": "plain"}
        except Exception as e:  # noqa: BLE001
            out["error"] = f"plain: {type(e).__name__}: {e}"

    # 2) encrypt_url + aes_key（备用；未经验证）
    enc_url = (emoji or {}).get("encrypt_url") or ""
    aes_key = (emoji or {}).get("aes_key") or ""
    if enc_url and aes_key:
        try:
            body, _h = fetch(enc_url, timeout=timeout)
            ext, mime = detect_mime(body)
            if not ext:
                pt = decrypt_emoji_aes(body, aes_key)
                if pt:
                    body = pt
                    ext, mime = detect_mime(body)
            if ext:
                if cache_dir:
                    _atomic_write(cached_path(cache_dir, enc_url, ext), body)
                return {**out, "ok": True, "data": body, "ext": ext, "mime": mime, "via": "encrypt"}
        except Exception as e:  # noqa: BLE001
            out["error"] = (out["error"] or "") + f" | encrypt: {type(e).__name__}: {e}"

    out["error"] = out["error"] or "表情获取失败"
    return out
