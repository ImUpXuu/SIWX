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
import time
from collections import OrderedDict
from pathlib import Path

from Crypto.Cipher import AES

from siwx import logger as _media_logger

V2_MAGIC = b"\x07\x08V2\x08\x07"
V1_MAGIC = b"\x07\x08V1\x08\x07"
V1_FIXED_KEY = b"cfcd208495d565ef"          # 社区已知固定 key（原项目记录）
DEFAULT_XOR = 0xC9


def _default_event(msg: str) -> None:
    """媒体诊断事件默认进结构化轨（审计 §2.2）：CLI/serve 下开 Debug 均可见，
    不再是 no-op。serve 模式由 server 再叠加 TUI 实时输出。
    ring=False：图片三级来源诊断是逐张埋点，只进文件轨防冲环形缓冲。"""
    try:
        _media_logger.detailed("media", str(msg), ring=False)
    except Exception:
        pass


# 事件钩子：默认结构化轨；serve 模式下由 server 注入 TUI 双写
event = _default_event

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
# LRU OrderedDict 的 move_to_end / popitem 并发调用会损坏内部链表
# （最坏返回错误图片字节），Web API 多线程访问必须持锁
_IMG_CACHE_LOCK = threading.Lock()

# media_key.json 的"读-改-写"线程锁（跨进程由 _save_key_cache 的唯一临时名兜）
_KEY_CACHE_LOCK = threading.Lock()

# cache 根目录的进程内 memo：带 TTL，避免微信目录中途出现/消失时长期失真
_CACHE_ROOTS_MEMO: dict = {}
_CACHE_ROOTS_TTL = 60.0

# 派生密钥持久缓存（只有密钥，没有明文）
_KEY_FILE_NAME = "media_key.json"


def _key_file() -> Path:
    """派生密钥缓存位置：系统数据目录（与 cwd / 安装位置解耦，issue #11）。"""
    try:
        from siwx.paths import data_dir
        return data_dir() / "media_key.json"
    except Exception:
        base = (os.environ.get("LOCALAPPDATA")
                or os.environ.get("USERPROFILE")
                or str(Path(tempfile.gettempdir())))
        return Path(base) / "stories-in-wx" / "media_key.json"


def _load_key_cache() -> dict:
    try:
        return json.loads(_key_file().read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}
    except Exception as e:
        # 审计 §4.4：区分"缓存损坏"与"缓存没坏但密钥不对"——损坏只是少了一条
        # 密钥来源（candidate_keys 还有 kvcomm 派生兜底），但要可见
        try:
            _media_logger.detailed("media", f"密钥缓存读取失败: {type(e).__name__}")
        except Exception:
            pass
        return {}


def _save_key_cache(cache: dict) -> None:
    """原子写入密钥缓存：唯一临时名 + os.replace。

    旧实现用固定 media_key.tmp：导出是多进程（每会话重建 Pool）、Web API 是
    threading=True，两个写者会抢同一个临时文件（os.replace 抛
    FileNotFoundError / Windows PermissionError），或交错写出半个 JSON ——
    缓存损坏后所有账号的派生密钥一起丢，下次全部走"V2 密钥未命中"。
    唯一临时名把"多个写者"这一条彻底消掉；os.replace 本身仍是原子的。
    """
    p = _key_file()
    p.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(dir=str(p.parent),
                                    prefix=p.name + ".", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(json.dumps(cache, ensure_ascii=False))
        os.replace(tmp_name, p)
    except OSError:
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        raise


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
# 实测该 DLL 在长驻进程内会随机 access violation（crash.log 2026-10-05 21:51：
# 两线程同入 DLL，整个服务进程被带走；同一输入在干净进程里必成功）。
# 因此转码固定在一次性子进程进行：崩溃只死子进程，主服务无感，且可安全重试。

_VOIP_DLL_MISSING = False   # 审计 §4.4：DLL 缺失告警每进程只发一次

_WXGF_WORKER = r'''
import base64, ctypes, os, sys

dll_path = sys.argv[1]
data = sys.stdin.buffer.read()
os.add_dll_directory(os.path.dirname(dll_path))
voip = ctypes.WinDLL(dll_path)
fn = voip.wxam_dec_wxam2pic_5
fn.argtypes = [ctypes.c_int64, ctypes.c_int, ctypes.c_int64,
               ctypes.POINTER(ctypes.c_int), ctypes.c_int64]
fn.restype = ctypes.c_int64

class _Cfg(ctypes.Structure):
    _fields_ = [("mode", ctypes.c_int), ("reserved", ctypes.c_int)]

# 只接受转码产物；wxgf→wxgf 无意义（透传正是要消灭的行为）
_SIGS = (b"\xff\xd8\xff", b"\x89PNG", b"GIF8", b"RIFF")
max_out = 52 * 1024 * 1024
for mode in (0, 3):
    cfg = _Cfg(mode, 0)
    in_buf = ctypes.create_string_buffer(data, len(data))
    out_buf = ctypes.create_string_buffer(max_out)
    out_sz = ctypes.c_int(max_out)
    try:
        ret = fn(ctypes.addressof(in_buf), len(data),
                 ctypes.addressof(out_buf), ctypes.byref(out_sz),
                 ctypes.addressof(cfg))
    except Exception:
        continue
    if ret == 0 and out_sz.value > 0:
        cand = out_buf.raw[:out_sz.value]
        if cand.startswith(_SIGS):
            sys.stdout.buffer.write(base64.b64encode(cand))
            sys.exit(0)
sys.exit(1)
'''


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


def convert_wxgf(data: bytes):
    """wxgf → JPEG/PNG（微信官方解码器，子进程隔离）。失败返回 None。

    子进程内失败/崩溃自动重试一次（每次都是干净 DLL 状态，重试有意义）。
    """
    import base64
    import subprocess
    import sys as _sys

    dll = _find_voip_dll()
    if dll is None:
        global _VOIP_DLL_MISSING
        if not _VOIP_DLL_MISSING:
            _VOIP_DLL_MISSING = True
            try:
                _media_logger.warn("media", "VoipEngine.dll 未找到，wxgf 将无法转码")
            except Exception:
                pass
        return None
    for attempt in (1, 2):
        try:
            proc = subprocess.run(
                [_sys.executable, "-c", _WXGF_WORKER, str(dll)],
                input=data, capture_output=True, timeout=30,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        except (OSError, subprocess.TimeoutExpired) as e:
            try:
                _media_logger.detailed("media",
                                       f"wxgf 转码子进程异常: {type(e).__name__}")
            except Exception:
                pass
            continue
        if proc.returncode == 0 and proc.stdout:
            try:
                return base64.b64decode(proc.stdout) or None
            except Exception:
                pass
        try:
            _media_logger.detailed(
                "media",
                f"wxgf 转码失败(第{attempt}次) rc={proc.returncode} "
                f"{len(data)}B stderr={proc.stderr[:120]!r}", ring=False)
        except Exception:
            pass
    return None


def _xor_table(xor_key: int) -> bytes:
    return bytes(i ^ xor_key for i in range(256))


def decrypt_v2_body(data: bytes, aes_key: bytes, xor_key: int):
    """V2 整文件解密 → (bytes, ext) 或 (None, None)。

    审计 §4.4（方案②）：三处失败在函数内部直接 detailed 区分原因
    （too-short / bad-aes-size / head-not-image），不改返回签名、
    不动 5 个解包调用点。head-not-image 是"密钥错或密文损坏"的混合信号
    （V2 无签名字段，用 reason 码 head-not-image 而非 sig-miss）。"""
    if len(data) < 31:
        try:
            _media_logger.detailed("media", f"V2解密失败 too-short len={len(data)}")
        except Exception:
            pass
        return None, None
    aes_size = struct.unpack("<I", data[6:10])[0]
    if aes_size <= 0 or 15 + aes_size + 16 > len(data):
        try:
            _media_logger.detailed(
                "media", f"V2解密失败 bad-aes-size aes_size={aes_size} len={len(data)}")
        except Exception:
            pass
        return None, None
    head = AES.new(aes_key, AES.MODE_ECB).decrypt(data[15: 15 + aes_size])
    ext, ctype = _image_sig(head)
    if ext is None:
        try:
            _media_logger.detailed(
                "media", f"V2解密失败 head-not-image head={head[:8].hex()}")
        except Exception:
            pass
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
            try:
                _media_logger.detailed("media", f"缓存密钥格式非法 wx={wx_clean}")
            except Exception:
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
        # 审计 §4.4：hardlink 链路不可用此前全静默
        try:
            _media_logger.detailed(
                "media", f"hardlink不可用 db={hl.is_file()} md5_len={len(md5 or '')}")
        except Exception:
            pass
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
        # ValueStdStr 可能为 NULL：uuid_row=(None,) 时直接 .split 会崩
        storage_root = (uuid_row[0] or "").split("_", 2)[-1] if uuid_row else ""
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
    """该账号的 cache/ 根目录列表（进程内 TTL memo）。

    旧实现每张图都调一次 find_wechat_data_dirs()：26 个盘符 is_dir() + Users
    一层 iterdir，而且 discover 每次都无条件 detailed 打一条扫描日志——实测
    logs/siwx.log 里 42% 的行都是这条，把 UI 的 5000 行环形缓冲反复冲掉。
    """
    now = time.time()
    hit = _CACHE_ROOTS_MEMO.get(wxid_full)
    if hit is not None and now - hit[0] < _CACHE_ROOTS_TTL:
        return hit[1]
    from siwx.discover import find_wechat_data_dirs
    out = []
    for wxid, db in find_wechat_data_dirs():
        if wxid != wxid_full:
            continue
        cache = Path(db).parent / "cache"
        if cache.is_dir():
            out.append(cache)
    _CACHE_ROOTS_MEMO[wxid_full] = (now, out)
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
    # 实测两种文件名形态：<md5>_b.dat 与 <local_id>_<ts>_b.dat。local_id 分支
    # 必须带下划线边界并带上 ts：旧写法 f"{local_id}*.dat" 里 local_id=9 会命中
    # 91_…、9abc… 等同目录的兄弟文件，先选错再缓存 → 同一张图反复显示错图。
    patterns = []
    for md5v in (bubble_md5, xml_md5):
        if md5v:
            patterns.append(f"{md5v}*.dat")
    if local_id:
        patterns.append(f"{local_id}_{ts}_*.dat" if ts else f"{local_id}_*.dat")
    out = []
    for cache_root in _wechat_cache_roots(wxid_full):
        for pat in patterns:
            for f in cache_root.glob(f"*/Message/{target}/Bubble/{pat}"):
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
    """wxgf 统一转码为浏览器可显示格式；转不出来返回 None。

    旧实现把转码失败的原始 wxgf 字节透传（200 + image/wxgf），浏览器解码
    失败被前端误判为"原图未下载"，且坏结果进了缓存导致重试永远失败 ——
    现在失败统一返回 None，由调用方落到下一级候选或按失败处理。
    """
    if ext == "wxgf":
        converted = convert_wxgf(body)
        if not converted:
            return None
        ext, ctype = _image_sig(converted) or (None, None)
        if ext is None:
            return None
        body = converted
    return body, ext, ctype


def _plausible_image(body: bytes) -> bool:
    """解密产物完整性快检：头部签名已验，补尾部特征。

    拦掉半截下载或解密错尾的坏文件（实测例：白熊图 _h.dat 头部 JPEG 完好、
    尾部缺失，浏览器无法解码）。尾部窗口放宽到 64B：VoipEngine 转码产物
    会在 FFD9 后附一小段填充（实测 26B），不能按"绝对末尾"判。wxgf 不做
    强校验，由转码环节把关。
    """
    if body.startswith(b"\xff\xd8\xff"):
        return body.rfind(b"\xff\xd9") >= len(body) - 64
    if body.startswith(b"\x89PNG"):
        return b"IEND" in body[-64:]
    if body.startswith(b"GIF8"):
        return b"\x3b" in body[-8:]
    return True


def _decrypt_any(data: bytes, wxid: str):
    """按文件头分派解密 → (bytes, ctype) 或 (None, None)。"""
    if not data:
        # 0 字节 .dat（下载中断/磁盘满）：下面 V0 分支的 data[0] 会 IndexError，
        # 网页端没有 try 兜 → 500 + 完整 traceback 回显
        return None, None
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
    # 缓存键必须带 ts：同一 (chat, local_id) 在不同 ts 下是不同消息，
    # 缺 ts 时先命中的那张图会粘住后续请求（错图 + 缓存放大错误）
    cache_key = f"{account}:{chat}:{local_id}:{ts}:{md5}:{bubble_md5}:{hq}"
    with _IMG_CACHE_LOCK:
        if cache_key in _IMG_CACHE:
            _IMG_CACHE.move_to_end(cache_key)
            b, ct = _IMG_CACHE[cache_key]
            return b, ct

    wxid = account
    last_err = "未找到文件"
    label = f"{(chat or '')[:12]}… local_id={local_id} md5={(md5 or '')[:8]}… bm={(bubble_md5 or '')[:8]}…"

    def _emit(body: bytes, ext: str):
        """转码 + 缓存。转码失败返回 (None, None)，调用方落到下一级候选；
        失败结果绝不进缓存（旧实现缓存坏 wxgf 导致重试永远失败）。"""
        fin = _finalize(body, ext, f"image/{ext}")
        if fin is None:
            return None, None
        body, _ext, ctype = fin
        with _IMG_CACHE_LOCK:
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
            if body and not _plausible_image(body):
                event(f"attach 产物不完整，跳过({path.name[:24]}): {label} ({len(body)}B)")
                last_err = "解密产物不完整"
                continue
            if body:
                event(f"图片attach命中({path.name[:24]}): {label} ({len(body)}B)")
                r = _emit(body, ctype.split("/")[1])
                if r[0] is not None:
                    return r
                last_err = "wxgf 转码失败"
                continue
            last_err = "attach 解密失败"

    # ① hardlink 原图
    if md5 and len(md5) == 32:
        for path in resolve_image_path(acc_out_dir, md5, wxid):
            if not path.is_file():
                last_err = f"文件不存在: {path.name}"
                continue
            data = path.read_bytes()
            if not data:
                last_err = "文件为空"
                continue
            head = data[:6]
            if head == V2_MAGIC:
                for aes_key, xor_key in candidate_keys(wxid):
                    body, ctype = decrypt_v2_body(data, aes_key, xor_key)
                    if body and not _plausible_image(body):
                        event(f"hardlink 产物不完整，跳过: {label} ({len(body)}B)")
                        last_err = "解密产物不完整"
                        continue
                    if body:
                        _remember_key(wxid, aes_key, xor_key)
                        event(f"图片原图命中 hardlink: {label} ({len(body)}B, {ctype})")
                        r = _emit(body, ctype.split("/")[1])
                        if r[0] is not None:
                            return r
                        last_err = "wxgf 转码失败"
                        continue
                last_err = "V2 密钥未命中（请确认微信已登录过该账号）"
            elif head == V1_MAGIC:
                body, ctype = decrypt_v2_body(data, V1_FIXED_KEY, DEFAULT_XOR)
                if body and _plausible_image(body):
                    r = _emit(body, ctype.split("/")[1])
                    if r[0] is not None:
                        return r
                last_err = "V1 解密失败"
            else:
                # V0：单字节 XOR 自动检测（按已知图像首字节推导）
                for known, ext, ctype in _IMAGE_SIGS:
                    xk = data[0] ^ known[0]
                    body = data.translate(_xor_table(xk))
                    ext2, _ct = _image_sig(body)
                    if ext2 and _plausible_image(body):
                        r = _emit(body, ext2)
                        if r[0] is not None:
                            return r
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
                    if body and not _plausible_image(body):
                        event(f"气泡产物不完整，跳过: {label} ({len(body)}B)")
                        last_err = "解密产物不完整"
                        continue
                    if body:
                        _remember_key(wxid, aes_key, xor_key)
                        event(f"图片气泡命中: {label} ← {f.name[:20]}… ({len(body)}B)")
                        r = _emit(body, ctype.split("/")[1])
                        if r[0] is not None:
                            return r
                        last_err = "wxgf 转码失败"
                        continue
                last_err = "Bubble V2 密钥未命中"
            else:
                ext, ctype = _image_sig(data)
                if ext and _plausible_image(data):
                    event(f"图片气泡命中(明文): {label} ← {f.name[:20]}…")
                    r = _emit(data, ext)
                    if r[0] is not None:
                        return r
                last_err = "Bubble 未知格式"

    # ③ Thumb 明文缩略图
    if chat and local_id and ts:
        thumbs = thumb_paths(wxid, chat, local_id, ts)
        if not thumbs:
            event(f"图片三级来源全部未命中: {label}")
        for f in thumbs:
            data = f.read_bytes()
            ext, ctype = _image_sig(data)
            if ext and _plausible_image(data):
                event(f"缩略图命中: {label} ← {f.name[:20]}…")
                r = _emit(data, ext)
                if r[0] is not None:
                    return r
        last_err = "本地无原图/气泡/缩略图"
    return None, last_err


def _remember_key(wxid_full: str, aes_key: bytes, xor_key: int) -> None:
    """把验证成功的派生 key 记下来（只存密钥，不存明文）。

    写缓存失败绝不能影响图片本身：静态目录里多个导出进程/请求线程会同时
    读改写这个文件，旧实现把异常一路上抛——已经成功解密的图片被记成
    ok=False（导出）或直接 500（网页端）。这里吞掉 OSError 只留痕。
    线程内用锁串行化"读-改-写"，跨进程靠唯一临时名（_save_key_cache）。
    """
    try:
        wx_clean = clean_wxid(wxid_full)
        rec = {"aes": aes_key.hex(), "xor": f"{xor_key:02x}"}
        with _KEY_CACHE_LOCK:
            cache = _load_key_cache()
            if cache.get(wx_clean) == rec:
                return
            cache[wx_clean] = rec
            _save_key_cache(cache)
    except OSError as e:
        try:
            _media_logger.warn("media", f"派生密钥缓存写入失败（本次解密不受影响）: "
                                        f"{type(e).__name__}: {e}")
        except Exception:
            pass


def extract_md5_from_xml(text: str):
    # 左边界：真实 XML 里并存 originsourcemd5 / androidmd5 / cdnthumbmd5 等属性，
    # 裸 `md5\s*=` 只靠"真实 md5 属性恰好排在前面"才不误命中（实测 4343 条 0 例，
    # 但那是运气）。加左边界后与属性顺序无关。
    m = re.search(r'(?<![0-9A-Za-z_])md5\s*=\s*["\']([0-9a-fA-F]{32})["\']', text)
    return m.group(1).lower() if m else None
