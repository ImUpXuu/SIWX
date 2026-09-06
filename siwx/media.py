"""媒体解密 —— 按需解密 + 内存缓存（程序关闭即消失，明文不落盘）。

原理见 docs/media-decryption-principles.md：
- V2 = AES-128-ECB 头部 + 16 字节分隔 + 单字节 XOR 尾部；账号级密钥由
  MMKV kvcomm 文件名的 code 离线派生：MD5(str(code)+清洗后wxid)[:16]。
- V1 = 固定 key；V0 = 单字节 XOR 自动检测。
- 图片路径解析：消息 XML md5 → hardlink.db → 存储根 + msg/attach/.../Img/<file_name>
"""
import hashlib
import json
import os
import re
import sqlite3
import struct
import tempfile
import threading
from collections import OrderedDict
from pathlib import Path

from Crypto.Cipher import AES

V2_MAGIC = b"\x07\x08V2\x08\x07"
V1_MAGIC = b"\x07\x08V1\x08\x07"
V1_FIXED_KEY = b"cfcd208495d565ef"          # 社区已知固定 key（原项目记录）
DEFAULT_XOR = 0xC9

# 事件钩子：serve 模式下由 server 注入 tui.log，CLI 下默认静默
event = lambda msg: None

_IMAGE_SIGS = (
    (b"\xff\xd8\xff", "jpeg", "image/jpeg"),
    (b"\x89PNG", "png", "image/png"),
    (b"GIF8", "gif", "image/gif"),
    (b"RIFF", "webp", "image/webp"),
    (b"wxgf", "wxgf", "image/wxgf"),
)

# ── 内存缓存（程序关闭即释放，不落盘） ──────────────────────────────
_IMG_CACHE: "OrderedDict[str, tuple[bytes, str]]" = OrderedDict()
_IMG_CACHE_MAX = 200            # 最多 200 张（约几十 MB）

# 派生密钥持久缓存（只有密钥，没有明文）
_KEY_FILE_NAME = "media_key.json"


def _key_file() -> Path:
    base = (os.environ.get("LOCALAPPDATA")
            or os.environ.get("USERPROFILE")
            or str(Path(tempfile.gettempdir())))
    return Path(base) / "stories-in-wx" / "media_key.json"


def _load_key_cache() -> dict:
    try:
        return json.loads(_key_file().read_text(encoding="utf-8"))
    except Exception:
        return {}


def _save_key_cache(cache: dict) -> None:
    p = _key_file()
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".tmp")
    tmp.write_text(json.dumps(cache, ensure_ascii=False), encoding="utf-8")
    os.replace(tmp, p)


def clean_wxid(wxid: str) -> str:
    parts = wxid.split("_")
    if wxid.startswith("wxid_") and len(parts) >= 3:
        return "_".join(parts[:2])
    return wxid


def find_kvcomm_codes() -> list:
    """扫全部 kvcomm 目录提取 code。"""
    import glob
    codes = set()
    pats = [
        r"C:/Users/*/AppData/Roaming/Tencent/xwechat/net/kvcomm/key_*_*.statistic",
        r"C:/Users/*/AppData/Roaming/Tencent/xwechat/ilink/kvcomm/key_*_*.statistic",
        r"C:/Users/*/AppData/Roaming/Tencent/WeChat/*/kvcomm/key_*_*.statistic",
    ]
    for pat in pats:
        for f in glob.glob(pat):
            m = re.match(r".*[\\/]key_(\d+)_", f.replace("\\", "/"))
            if m:
                codes.add(int(m.group(1)))
    return sorted(codes)


def _image_sig(data: bytes):
    for sig, ext, ctype in _IMAGE_SIGS:
        if data.startswith(sig):
            return ext, ctype
    return None, None


# ── wxgf → 图片：调用微信自带的 VoipEngine.dll（wxam_dec_wxam2pic_5） ──
# DLL 全局单例 + 串行锁：浏览器并发请求下重复 LoadLibrary 会互相踩崩

_VOIP_FN = None
_VOIP_LOCK = threading.Lock()


def _find_voip_dll():
    import glob
    pats = [
        r"C:\Program Files\Tencent\Weixin\*\VoipEngine.dll",
        r"C:\Program Files\Tencent\WeChat\*\VoipEngine.dll",
        r"C:\Program Files (x86)\Tencent\WeChat\*\VoipEngine.dll",
    ]
    for pat in pats:
        hits = sorted(glob.glob(pat), reverse=True)   # 取最新版本
        if hits:
            return hits[0]
    return None


def _get_voip_fn():
    global _VOIP_FN
    if _VOIP_FN is not None:
        return _VOIP_FN
    import ctypes

    dll_path = _find_voip_dll()
    if not dll_path:
        return None
    try:
        if hasattr(os, "add_dll_directory"):
            os.add_dll_directory(os.path.dirname(dll_path))
        voip = ctypes.WinDLL(dll_path)
        fn = voip.wxam_dec_wxam2pic_5
        fn.argtypes = [ctypes.c_int64, ctypes.c_int, ctypes.c_int64,
                       ctypes.POINTER(ctypes.c_int), ctypes.c_int64]
        fn.restype = ctypes.c_int64
        _VOIP_FN = fn
    except Exception:
        return None
    return _VOIP_FN


def convert_wxgf(data: bytes):
    """wxgf → JPEG/PNG（微信官方解码器）。失败返回 None。"""
    import ctypes

    fn = _get_voip_fn()
    if fn is None:
        return None

    class _WxAMConfig(ctypes.Structure):
        _fields_ = [("mode", ctypes.c_int), ("reserved", ctypes.c_int)]

    max_out = 52 * 1024 * 1024
    with _VOIP_LOCK:
        for mode in (0, 3):
            cfg = _WxAMConfig(mode, 0)
            in_buf = ctypes.create_string_buffer(data, len(data))
            out_buf = ctypes.create_string_buffer(max_out)
            out_sz = ctypes.c_int(max_out)
            try:
                ret = fn(ctypes.addressof(in_buf), len(data),
                         ctypes.addressof(out_buf), ctypes.byref(out_sz),
                         ctypes.addressof(cfg))
            except Exception:
                return None
            if ret == 0 and out_sz.value > 0:
                out = out_buf.raw[: out_sz.value]
                if _image_sig(out)[0]:
                    return out
    return None


def _xor_table(xor_key: int) -> bytes:
    return bytes(i ^ xor_key for i in range(256))


def decrypt_v2_body(data: bytes, aes_key: bytes, xor_key: int):
    """V2 整文件解密 → (bytes, ext) 或 (None, None)。"""
    if len(data) < 31:
        return None, None
    aes_size = struct.unpack("<I", data[6:10])[0]
    if aes_size <= 0 or 15 + aes_size + 16 > len(data):
        return None, None
    head = AES.new(aes_key, AES.MODE_ECB).decrypt(data[15: 15 + aes_size])
    ext, ctype = _image_sig(head)
    if ext is None:
        return None, None
    tail = data[15 + aes_size + 16: 15 + aes_size + 16 + struct.unpack(
        "<I", data[10:14])[0]]
    table = _xor_table(xor_key)
    return head + tail.translate(table), ctype


def candidate_keys(wxid_full: str):
    """生成候选 (aes_key, xor_key)：持久缓存优先，其次 kvcomm 派生。"""
    wx_clean = clean_wxid(wxid_full)
    out = []
    cache = _load_key_cache().get(wx_clean)
    if cache:
        try:
            out.append((bytes.fromhex(cache["aes"]), int(cache["xor"], 16)))
        except (KeyError, ValueError):
            pass
    for code in find_kvcomm_codes():
        for wx in (wx_clean, wxid_full):
            aes = hashlib.md5(f"{code}{wx}".encode()).hexdigest()[:16].encode()
            out.append((aes, code & 0xFF))
    # 去重保序
    seen, uniq = set(), []
    for k in out:
        if k not in seen:
            seen.add(k)
            uniq.append(k)
    return uniq


# ── 路径解析：消息 md5 → 磁盘 .dat ──────────────────────────────────

def resolve_image_path(acc_out_dir: Path, md5: str, wxid: str = None):
    """hardlink 链路：md5 → (file_name, dir1, dir2) → 绝对路径。"""
    hl = acc_out_dir / "hardlink" / "hardlink.db"
    if not hl.is_file() or not md5 or len(md5) != 32:
        return []
    conn = sqlite3.connect(hl)
    try:
        rows = conn.execute(
            "SELECT file_name, dir1, dir2 FROM image_hardlink_info_v4 "
            "WHERE md5=? OR file_name LIKE ? "
            "ORDER BY CASE WHEN file_name LIKE '%\\_t\\_%' ESCAPE '\\' THEN 2 "
            "WHEN file_name LIKE '%\\_h%' ESCAPE '\\' THEN 3 ELSE 1 END",
            (md5, md5 + "%")).fetchall()
        dir_ids = set()
        for _fn, d1, d2 in rows:
            if d1:
                dir_ids.add(d1)
            if d2:
                dir_ids.add(d2)
        names = {}
        for did in dir_ids:
            r = conn.execute("SELECT username FROM dir2id WHERE rowid=?", (did,)).fetchone()
            names[did] = r[0] if r else ""
        uuid_row = conn.execute("SELECT ValueStdStr FROM db_info WHERE Key='uuid'").fetchone()
        storage_root = uuid_row[0].split("_", 2)[-1] if uuid_row else ""
    finally:
        conn.close()

    wxid = wxid or ""
    out = []
    for file_name, d1, d2 in rows:
        d1n, d2n = names.get(d1, ""), names.get(d2, "")
        base = Path(storage_root) / wxid / "msg" / "attach"
        for cand in (
            base / d1n / d2n / "Img" / file_name,
            base / d1n / d2n / "Img" / (file_name + ".dat"),
            base / d1n / d2n / file_name,
            base / d2n / "Img" / file_name,
        ):
            out.append(cand)
    return out


def _wechat_cache_roots(wxid_full: str):
    """该账号的 cache/<YYYY-MM> 根目录列表。"""
    from siwx.discover import find_wechat_data_dirs
    out = []
    for wxid, db in find_wechat_data_dirs():
        if wxid != wxid_full:
            continue
        cache = Path(db).parent / "cache"
        if cache.is_dir():
            out.append(cache)
    return out


def bubble_paths(wxid_full: str, chat: str, local_id: int, ts: int,
                 bubble_md5: str = None, xml_md5: str = None):
    """消息气泡缓存：cache/<月>/Message/<md5(chat)>/Bubble/<名>_b.dat。

    命名有两种实测形态：
      a) packed_info_data 内嵌的 md5（消息→气泡精确映射，优先）
      b) {local_id}_{ts} 消息定位
    目录名 = md5(会话username)，实测确认。
    """
    target = hashlib.md5(chat.encode()).hexdigest()
    stems = []
    if bubble_md5:
        stems.append(bubble_md5)
    if xml_md5:
        stems.append(xml_md5)
    stems.append(str(local_id))
    out = []
    for cache_root in _wechat_cache_roots(wxid_full):
        for stem in stems:
            for f in cache_root.glob(f"*/Message/{target}/Bubble/{stem}*.dat"):
                if f.is_file() and f not in out:
                    out.append(f)
    return out


def thumb_paths(wxid_full: str, chat: str, local_id: int, ts: int):
    """明文缩略图：cache/<月>/Message/<md5(chat)>/Thumb/{local_id}_*"""
    target = hashlib.md5(chat.encode()).hexdigest()
    out = []
    for cache_root in _wechat_cache_roots(wxid_full):
        for f in cache_root.glob(f"*/Message/{target}/Thumb/{local_id}_*"):
            if f.is_file():
                out.append(f)
    return out


def attach_paths(wxid_full: str, chat: str, xml_md5: str):
    """按消息 XML md5 直查原图目录：msg/attach/{md5(chat)}/**/Img/<md5>*.dat。

    返回按质量排序的候选：聊天显示版(.dat) → 高清(_h.dat) → 缩略(_t.dat)。
    （目录名 = md5(会话username)，与 hardlink 表解耦，覆盖其缺记录的情况。）
    """
    if not xml_md5 or len(xml_md5) != 32:
        return []
    target = hashlib.md5(chat.encode()).hexdigest()
    out = []
    for cache_root in _wechat_cache_roots(wxid_full):
        attach_root = cache_root.parent / "msg" / "attach" / target
        if attach_root.is_dir():
            for f in attach_root.rglob(f"{xml_md5}*.dat"):
                if f.is_file():
                    out.append(f)
    def _rank(p: Path):
        n = p.name
        if "_t.dat" in n: return 2
        if "_h" in n: return 1     # 高清原图
        return 0                    # 聊天显示版
    return sorted(out, key=_rank)


# ── 主入口 ──────────────────────────────────────────────────────────

def _finalize(body: bytes, ext: str, ctype: str):
    """wxgf 统一转码为浏览器可显示格式。"""
    if ext == "wxgf":
        converted = convert_wxgf(body)
        if converted:
            ext, ctype = _image_sig(converted) or ("gif", "image/gif")
            body = converted
    return body, ext, ctype


def _decrypt_any(data: bytes, wxid: str):
    """按文件头分派解密 → (bytes, ctype) 或 (None, None)。"""
    head = data[:6]
    if head == V2_MAGIC:
        for aes_key, xor_key in candidate_keys(wxid):
            body, ctype = decrypt_v2_body(data, aes_key, xor_key)
            if body:
                _remember_key(wxid, aes_key, xor_key)
                return body, ctype
        return None, None
    if head == V1_MAGIC:
        return decrypt_v2_body(data, V1_FIXED_KEY, DEFAULT_XOR)
    for known, ext, ctype in _IMAGE_SIGS:
        xk = data[0] ^ known[0]
        body = data.translate(_xor_table(xk))
        ext2, _ct = _image_sig(body)
        if ext2:
            return body, ctype
    return None, None


def get_image(account: str, md5: str, acc_out_dir: Path,
              chat: str = None, local_id: int = None, ts: int = None,
              bubble_md5: str = None, hq: bool = False):
    """按需解密一张图 → (bytes, content_type) 或 (None, error_reason)。

    多级来源：attach 原图目录（hq=True 时优先高清 _h 版）→ Bubble 气泡缓存
    （packed_info md5 映射）→ hardlink → Thumb 明文缩略图。
    """
    cache_key = f"{account}:{chat}:{local_id}:{md5}:{bubble_md5}:{hq}"
    if cache_key in _IMG_CACHE:
        _IMG_CACHE.move_to_end(cache_key)
        b, ct = _IMG_CACHE[cache_key]
        return b, ct

    wxid = account
    last_err = "未找到文件"
    label = f"{(chat or '')[:12]}… local_id={local_id} md5={(md5 or '')[:8]}… bm={(bubble_md5 or '')[:8]}…"

    def _emit(body: bytes, ext: str):
        body, ext, ctype = _finalize(body, ext, f"image/{ext}")
        _IMG_CACHE[cache_key] = (body, ctype)
        if len(_IMG_CACHE) > _IMG_CACHE_MAX:
            _IMG_CACHE.popitem(last=False)
        return body, ctype

    # ⓪ attach 原图目录直查（按消息 XML md5 命名，不依赖 hardlink；
    #    hq=True 时优先高清 _h 版，供点击查看大图使用）
    if chat and md5 and len(md5) == 32:
        cands = attach_paths(wxid, chat, md5)
        if hq:
            cands = sorted(cands, key=lambda p: (0 if "_h" in p.name else 1))
        if not cands:
            event(f"attach 目录无该图: {label}")
        for path in cands:
            if not path.is_file():
                continue
            data = path.read_bytes()
            body, ctype = _decrypt_any(data, wxid)
            if body:
                event(f"图片attach命中({path.name[:24]}): {label} ({len(body)}B)")
                return _emit(body, ctype.split("/")[1])
            last_err = "attach 解密失败"

    # ① hardlink 原图
    if md5 and len(md5) == 32:
        for path in resolve_image_path(acc_out_dir, md5, wxid):
            if not path.is_file():
                last_err = f"文件不存在: {path.name}"
                continue
            data = path.read_bytes()
            head = data[:6]
            if head == V2_MAGIC:
                for aes_key, xor_key in candidate_keys(wxid):
                    body, ctype = decrypt_v2_body(data, aes_key, xor_key)
                    if body:
                        _remember_key(wxid, aes_key, xor_key)
                        event(f"图片原图命中 hardlink: {label} ({len(body)}B, {ctype})")
                        return _emit(body, ctype.split("/")[1])
                last_err = "V2 密钥未命中（请确认微信已登录过该账号）"
            elif head == V1_MAGIC:
                body, ctype = decrypt_v2_body(data, V1_FIXED_KEY, DEFAULT_XOR)
                if body:
                    return _emit(body, ctype.split("/")[1])
                last_err = "V1 解密失败"
            else:
                # V0：单字节 XOR 自动检测（按已知图像首字节推导）
                for known, ext, ctype in _IMAGE_SIGS:
                    xk = data[0] ^ known[0]
                    body = data.translate(_xor_table(xk))
                    ext2, _ct = _image_sig(body)
                    if ext2:
                        return _emit(body, ext2)
                last_err = "未知格式"

    # ② Bubble 气泡缓存（packed_info 的 md5 精确映射 + local_id 定位）
    if chat and local_id and ts:
        cands = bubble_paths(wxid, chat, local_id, ts,
                             bubble_md5=bubble_md5, xml_md5=md5)
        if not cands:
            event(f"图片气泡未命中: {label}")
        for f in cands:
            data = f.read_bytes()
            head = data[:6]
            if head == V2_MAGIC:
                for aes_key, xor_key in candidate_keys(wxid):
                    body, ctype = decrypt_v2_body(data, aes_key, xor_key)
                    if body:
                        _remember_key(wxid, aes_key, xor_key)
                        event(f"图片气泡命中: {label} ← {f.name[:20]}… ({len(body)}B)")
                        return _emit(body, ctype.split("/")[1])
                last_err = "Bubble V2 密钥未命中"
            else:
                ext, ctype = _image_sig(data)
                if ext:
                    event(f"图片气泡命中(明文): {label} ← {f.name[:20]}…")
                    return _emit(data, ext)
                last_err = "Bubble 未知格式"

    # ③ Thumb 明文缩略图
    if chat and local_id and ts:
        thumbs = thumb_paths(wxid, chat, local_id, ts)
        if not thumbs:
            event(f"图片三级来源全部未命中: {label}")
        for f in thumbs:
            data = f.read_bytes()
            ext, ctype = _image_sig(data)
            if ext:
                event(f"缩略图命中: {label} ← {f.name[:20]}…")
                return _emit(data, ext)
        last_err = "本地无原图/气泡/缩略图"
    return None, last_err


def _remember_key(wxid_full: str, aes_key: bytes, xor_key: int) -> None:
    """把验证成功的派生 key 记下来（只存密钥，不存明文）。"""
    wx_clean = clean_wxid(wxid_full)
    cache = _load_key_cache()
    rec = {"aes": aes_key.hex(), "xor": f"{xor_key:02x}"}
    if cache.get(wx_clean) != rec:
        cache[wx_clean] = rec
        _save_key_cache(cache)


def extract_md5_from_xml(text: str):
    m = re.search(r'md5\s*=\s*["\']([0-9a-fA-F]{32})["\']', text)
    return m.group(1).lower() if m else None
