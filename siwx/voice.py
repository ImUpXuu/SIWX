"""微信语音消息读取与轻量转码。

微信 4.x 解密后的语音数据位于 message/media_*.db 的 VoiceInfo 表：
  - Name2Id.rowid -> chat_name_id
  - VoiceInfo.local_id / svr_id / create_time 与 Msg_* 表中的消息对应
  - VoiceInfo.voice_data 通常已是明文 SILK 数据；有些记录在 #!SILK_V3 前带
    1 个控制字节，需要剥掉前缀后再作为 .silk 导出/下载。

转码策略默认使用 pilk：SILK 先由 pilk 解为裸 PCM，再用 Python 标准库 wave
封装为 WAV，不要求 ffmpeg。若 pilk 不可用，可通过 SIWX_SILK_DECODER、项目内置
解码器或 PATH 中已有的 silk_v3_decoder/silk-decoder/decoder 兜底；仍不可转码时
保留原始 SILK 下载/导出，并给出明确原因。
"""
import os
import re
import shlex
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import wave
from pathlib import Path


SILK_MAGIC = b"#!SILK_V3"
WAV_MAGIC = b"RIFF"
DEFAULT_SAMPLE_RATE = 24000
DEFAULT_CHANNELS = 1
DEFAULT_SAMPLE_WIDTH = 2      # 16-bit PCM


_AUDIO_SIGS = (
    (b"RIFF", "wav", "audio/wav"),
    (b"ID3", "mp3", "audio/mpeg"),
    (b"\xff\xfb", "mp3", "audio/mpeg"),
    (b"OggS", "ogg", "audio/ogg"),
    (b"#!AMR", "amr", "audio/amr"),
    (SILK_MAGIC, "silk", "audio/silk"),
)


def parse_voice_meta(text: str) -> dict | None:
    """从 <voicemsg .../> XML 中提取展示元数据。"""
    if not text or "<voicemsg" not in text:
        return None
    m = re.search(r"<voicemsg\b([^>]*)/?>", text, re.S)
    if not m:
        return None
    attrs = dict(re.findall(r'([a-zA-Z_][\w:-]*)="([^"]*)"', m.group(1)))

    def as_int(name, default=0):
        try:
            return int(attrs.get(name) or default)
        except (TypeError, ValueError):
            return default

    return {
        "durationMs": as_int("voicelength"),
        "size": as_int("length"),
        "voiceFormat": as_int("voiceformat"),
        "clientMsgId": attrs.get("clientmsgid", ""),
        "fromUsername": attrs.get("fromusername", ""),
        "voiceMd5": attrs.get("voicemd5", ""),
        "silkLength": as_int("silklength"),
    }


def _clean_voice_data(data: bytes) -> tuple[bytes, int]:
    """返回可写出的语音数据与 SILK magic 的偏移。"""
    if not data:
        return b"", -1
    pos = data.find(SILK_MAGIC)
    if pos >= 0:
        return data[pos:], pos
    return data, -1


def detect_audio(data: bytes) -> tuple[str, str]:
    """识别常见音频封装，返回 (ext, mimetype)。"""
    head = data[:16] if data else b""
    if head.startswith(WAV_MAGIC) and len(head) >= 12 and head[8:12] == b"WAVE":
        return "wav", "audio/wav"
    for sig, ext, ctype in _AUDIO_SIGS:
        if head.startswith(sig):
            return ext, ctype
    return "bin", "application/octet-stream"


def pcm_to_wav(pcm: bytes, sample_rate: int = DEFAULT_SAMPLE_RATE,
               channels: int = DEFAULT_CHANNELS,
               sample_width: int = DEFAULT_SAMPLE_WIDTH) -> bytes:
    """用标准库把裸 PCM 封装成 WAV。"""
    import io

    buf = io.BytesIO()
    with wave.open(buf, "wb") as wf:
        wf.setnchannels(channels)
        wf.setsampwidth(sample_width)
        wf.setframerate(sample_rate)
        wf.writeframes(pcm)
    return buf.getvalue()


def _split_command(spec: str) -> list[str]:
    """把环境变量里的命令拆成 argv；纯路径即使含空格也保留为一个参数。"""
    spec = (spec or "").strip()
    if not spec:
        return []
    if Path(spec).is_file():
        return [spec]
    return shlex.split(spec, posix=(os.name != "nt"))


def _resource_roots() -> list[Path]:
    """项目源码/打包运行时可能存放内置解码器的根目录。"""
    roots: list[Path] = []
    meipass = getattr(sys, "_MEIPASS", None)
    if meipass:
        roots.append(Path(meipass))
    if getattr(sys, "frozen", False):
        roots.append(Path(sys.executable).resolve().parent)
    here = Path(__file__).resolve().parent
    roots.extend([here, here.parent, Path.cwd()])

    uniq: list[Path] = []
    seen = set()
    for root in roots:
        try:
            key = str(root.resolve())
        except OSError:
            key = str(root)
        if key not in seen:
            seen.add(key)
            uniq.append(root)
    return uniq


def _bundled_decoder_candidates() -> list[tuple[str, list[str]]]:
    """查找随项目源码或 PyInstaller 产物一起携带的 SILK 解码器。"""
    exe_names = ["silk_v3_decoder", "silk-decoder", "silk_decoder", "decoder"]
    if os.name == "nt":
        exe_names = [name + ".exe" for name in exe_names] + exe_names
    subdirs = (
        Path("vendor") / "silk-decoder" / "windows",
        Path("vendor") / "silk-decoder" / "win32",
        Path("vendor") / "silk-decoder" / "bin",
        Path("vendor") / "silk-decoder",
        Path("vendor"),
        Path("bin"),
        Path("."),
    )
    out: list[tuple[str, list[str]]] = []
    seen = set()
    for root in _resource_roots():
        for sub in subdirs:
            base = root / sub
            for name in exe_names:
                p = base / name
                if not p.is_file():
                    continue
                key = str(p.resolve())
                if key in seen:
                    continue
                seen.add(key)
                out.append((f"bundled:{p.name}", [str(p)]))
    return out


def _decoder_candidates() -> list[tuple[str, list[str]]]:
    """返回可尝试的 SILK 解码器命令；项目内置优先，其次用户环境。"""
    out: list[tuple[str, list[str]]] = []
    out.extend(_bundled_decoder_candidates())
    env = os.environ.get("SIWX_SILK_DECODER")
    if env:
        parts = _split_command(env)
        if parts:
            out.append(("SIWX_SILK_DECODER", parts))
    for name in ("silk_v3_decoder", "silk-decoder", "silk_decoder", "decoder"):
        exe = shutil.which(name)
        if exe:
            out.append((name, [exe]))
    return out


def _run_decoder_command(parts: list[str], silk_path: Path, pcm_path: Path,
                         sample_rate: int) -> tuple[bytes | None, str]:
    """执行本地解码器，约定输出裸 PCM。"""
    tokens = []
    has_placeholder = False
    for p in parts:
        if any(x in p for x in ("{input}", "{output}", "{rate}")):
            has_placeholder = True
            p = (p.replace("{input}", str(silk_path))
                   .replace("{output}", str(pcm_path))
                   .replace("{rate}", str(sample_rate)))
        tokens.append(p)
    if not has_placeholder:
        tokens.extend([str(silk_path), str(pcm_path)])

    flags = 0
    if os.name == "nt" and hasattr(subprocess, "CREATE_NO_WINDOW"):
        flags = subprocess.CREATE_NO_WINDOW
    try:
        proc = subprocess.run(tokens, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                              timeout=20, check=False, creationflags=flags)
    except Exception as e:
        return None, str(e)
    if proc.returncode != 0:
        err = (proc.stderr or proc.stdout or b"").decode("utf-8", errors="replace")
        return None, err.strip()[:300] or f"退出码 {proc.returncode}"
    if pcm_path.is_file() and pcm_path.stat().st_size > 0:
        return pcm_path.read_bytes(), ""
    if proc.stdout:
        return proc.stdout, ""
    return None, "解码器未输出 PCM"


def _decode_silk_to_pcm_with_pilk(data: bytes) -> tuple[bytes | None, str, str]:
    """默认用 pilk 解码 SILK；pilk 是项目依赖，不需要 ffmpeg。"""
    try:
        import pilk  # type: ignore
    except Exception:
        return None, "", ""
    try:
        with tempfile.TemporaryDirectory(prefix="siwx_voice_") as td:
            silk_path = Path(td) / "input.silk"
            pcm_path = Path(td) / "output.pcm"
            silk_path.write_bytes(data)
            pilk.decode(str(silk_path), str(pcm_path))
            if pcm_path.is_file() and pcm_path.stat().st_size > 0:
                return pcm_path.read_bytes(), "", "pilk"
    except Exception as e:
        return None, str(e), "pilk"
    return None, "pilk 未输出 PCM", "pilk"


def _decode_silk_to_pcm_with_command(data: bytes, sample_rate: int) -> tuple[bytes | None, str, str]:
    """尝试使用本机已有命令行解码器把 SILK 解成 PCM。"""
    candidates = _decoder_candidates()
    if not candidates:
        return None, "未找到本地 SILK 解码器（可设置 SIWX_SILK_DECODER）", ""

    last_err = ""
    with tempfile.TemporaryDirectory(prefix="siwx_voice_") as td:
        silk_path = Path(td) / "input.silk"
        pcm_path = Path(td) / "output.pcm"
        silk_path.write_bytes(data)
        for name, parts in candidates:
            pcm, err = _run_decoder_command(parts, silk_path, pcm_path, sample_rate)
            if pcm:
                return pcm, "", name
            last_err = err or last_err
            try:
                if pcm_path.exists():
                    pcm_path.unlink()
            except OSError:
                pass
    return None, last_err or "SILK 解码失败", ""


def transcode_voice(data: bytes, target: str = "wav",
                    sample_rate: int = DEFAULT_SAMPLE_RATE) -> tuple[bytes | None, str | dict]:
    """把语音数据转成目标格式。

    返回：
      - 成功: (body, {"format", "mimetype", "ext", "engine"})
      - 失败: (None, reason)

    支持：
      1. 已是 WAV 时直接返回；
      2. SILK 通过 pilk（默认）或项目内置/本机可选 decoder → PCM → 标准库 WAV；
      3. target=silk/raw 时返回清理后的原始数据。
    """
    body, _offset = _clean_voice_data(data)
    if not body:
        return None, "语音数据为空"
    ext, ctype = detect_audio(body)
    target = (target or "wav").lower()
    if target in ("silk", "raw", "original"):
        return body, {"format": ext, "mimetype": ctype, "ext": ext, "engine": "original"}
    if target not in ("wav", "wave"):
        return None, f"暂不支持的语音格式: {target}"
    if ext == "wav":
        return body, {"format": "wav", "mimetype": "audio/wav", "ext": "wav", "engine": "passthrough"}
    if ext != "silk":
        return None, f"当前只能将 SILK 转为 WAV，实际格式为 {ext}"

    pcm, err, engine = _decode_silk_to_pcm_with_pilk(body)
    if not pcm:
        pcm, err, engine = _decode_silk_to_pcm_with_command(body, sample_rate)
    if not pcm:
        return None, err or "未找到可用的 SILK 解码器"
    if len(pcm) % DEFAULT_SAMPLE_WIDTH:
        pcm = pcm[:-1]
    if not pcm:
        return None, "解码器输出的 PCM 为空"
    wav = pcm_to_wav(pcm, sample_rate=sample_rate)
    return wav, {"format": "wav", "mimetype": "audio/wav", "ext": "wav", "engine": engine or "decoder"}


def _chat_id(conn, chat: str):
    if not chat:
        return None
    try:
        row = conn.execute("SELECT rowid FROM Name2Id WHERE user_name=?", (chat,)).fetchone()
        return int(row[0]) if row else None
    except (sqlite3.Error, TypeError, ValueError):
        return None


def _candidate_queries(chat_id, local_id, svr_id, ts):
    """按可信度生成 VoiceInfo 查询条件。"""
    if chat_id is not None and svr_id:
        yield "chat_name_id=? AND svr_id=?", (chat_id, svr_id)
    if chat_id is not None and local_id and ts:
        yield "chat_name_id=? AND local_id=? AND create_time=?", (chat_id, local_id, ts)
    if chat_id is not None and local_id:
        yield "chat_name_id=? AND local_id=?", (chat_id, local_id)
    if svr_id:
        yield "svr_id=?", (svr_id,)
    if local_id and ts:
        yield "local_id=? AND create_time=?", (local_id, ts)
    if local_id:
        yield "local_id=?", (local_id,)


def get_voice(acc_dir: Path, chat: str = "", local_id: int = 0,
              svr_id: int = 0, ts: int = 0) -> tuple[bytes | None, dict | str]:
    """读取一条语音消息。

    返回 (data, info)。data 为清理过前缀的 SILK/原始语音数据；找不到时
    返回 (None, reason)。
    """
    msg_dir = Path(acc_dir) / "message"
    if not msg_dir.is_dir():
        return None, "message 目录不存在"

    for db in sorted(msg_dir.glob("media_*.db"), reverse=True):
        try:
            conn = sqlite3.connect(db)
            try:
                if not conn.execute(
                        "SELECT name FROM sqlite_master WHERE type='table' AND name='VoiceInfo'"
                ).fetchone():
                    continue
                cid = _chat_id(conn, chat)
                for where, params in _candidate_queries(cid, local_id, svr_id, ts):
                    row = conn.execute(
                        "SELECT chat_name_id, create_time, local_id, svr_id, voice_data, data_index "
                        f"FROM VoiceInfo WHERE {where} ORDER BY create_time DESC LIMIT 1",
                        params).fetchone()
                    if not row or not row[4]:
                        continue
                    raw, offset = _clean_voice_data(bytes(row[4]))
                    if not raw:
                        continue
                    ext, ctype = detect_audio(raw)
                    return raw, {
                        "db": db.name,
                        "chatNameId": row[0],
                        "createTime": row[1],
                        "localId": row[2],
                        "serverId": str(row[3] or ""),
                        "dataIndex": row[5] or "",
                        "size": len(raw),
                        "rawSize": len(row[4]),
                        "silkOffset": offset,
                        "format": ext,
                        "mimetype": ctype,
                    }
            finally:
                conn.close()
        except sqlite3.Error:
            continue
    return None, "语音数据不存在或尚未同步"
