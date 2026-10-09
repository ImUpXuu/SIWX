"""全量媒体备份 —— 按文件系统扫描，不依赖消息解析。

exporter.py 的导出路径以**消息**为中心：先扫消息拿到 (md5, chat, local_id) 等
引用，再反查磁盘文件。这条路径简洁，但任何消息解析的边界情况（跨分片
local_id 撞号、消息体解析失败、引用信息缺失……）都可能导致个别文件漏导。

本模块反过来：直接枚举 msg/attach、msg/video、msg/file 下**磁盘上实际
存在**的文件，逐个解密/复制，不要求能定位到具体消息或会话。用于"删除
微信本地数据前先确认媒体已安全导出"这类场景——保证的是文件级 100% 覆盖，
而不是消息级的可追溯性（丢失的是"这个文件属于哪条消息"，不会丢文件本身）。

三类产物：
- 图片（msg/attach/**/*.dat，V0/V1/V2 混合，见 docs/media-decryption-principles.md）
  解密后按内容类型写出；wxgf 格式（微信自研图片容器，不止用于动画表情）
  会再尝试转码为可直接预览的格式（Windows 走 media.convert_wxgf() 的
  VoipEngine.dll；macOS 走 media_wxgf_macos.py 的活体微信进程 + LLDB 调用，
  见该模块顶部的详细原理说明），转码失败（微信未运行 / SIP 未关闭 /
  内部布局不匹配等）时退回保留原始 .wxgf 字节——这不算失败，数据已安全
  保留，只是暂时不能直接预览。
- 视频（msg/video/**/*.mp4）：实测是未加密的标准 MP4 容器（ISO Media /
  ftyp isom-iso2-avc1-mp41），直接复制，无需解密。
- 文件消息附件（msg/file/**，任意扩展名）：实测同样未加密、原文件名
  和扩展名原样保留在磁盘上（PDF/APK/PNG 等头部签名完好），直接复制。
"""
import platform
import shutil
from pathlib import Path

from siwx import media


def _account_root(wxid_full: str) -> Path | None:
    """账号的 xwechat_files/<wxid> 根目录（cache/ 的上一级）。"""
    roots = media._wechat_cache_roots(wxid_full)
    if not roots:
        return None
    return roots[0].parent


def backup_images(wxid_full: str, out_dir: Path, log=print) -> dict:
    """解密 msg/attach 下全部 .dat → out_dir，保留原有目录结构（便于溯源）。

    两段式：第一遍只做解密分类，wxgf 格式先原样写出（不在逐文件循环里
    转码——macOS 的转码要附加到活的微信进程，每张图单独 attach/detach
    对几千张图片来说慢到不可用）；第二遍对所有 wxgf 一次性批量转码
    （同一次 attach 会话处理一批，开销只摊销一次）。

    返回统计：total / ok_viewable（可直接预览的格式，含转码成功的 wxgf）/
    ok_wxgf_preserved（转码失败或当前平台不支持，原始 wxgf 字节原样保留，
    不算失败）/ failed（真正的失败：密钥未命中或数据损坏）/ bytes_written /
    failures（失败原因样本，最多保留 50 条）。
    """
    stats = {"total": 0, "ok_viewable": 0, "ok_wxgf_preserved": 0, "failed": 0,
             "bytes_written": 0, "failures": []}
    root = _account_root(wxid_full)
    if root is None:
        return stats
    attach_root = root / "msg" / "attach"
    if not attach_root.is_dir():
        return stats
    out_dir.mkdir(parents=True, exist_ok=True)
    files = list(attach_root.rglob("*.dat"))
    stats["total"] = len(files)
    wxgf_pending = []  # [(待转码文件路径, 最终 heic 路径)]
    for i, f in enumerate(files):
        try:
            data = f.read_bytes()
            body, ctype = media._decrypt_any(data, wxid_full)
        except Exception as e:
            stats["failed"] += 1
            if len(stats["failures"]) < 50:
                stats["failures"].append(f"{f.name}: {type(e).__name__}: {e}")
            continue
        if not body:
            stats["failed"] += 1
            if len(stats["failures"]) < 50:
                stats["failures"].append(f"{f.name}: 密钥未命中或解密失败")
            continue
        rel = f.relative_to(attach_root)
        if ctype == "image/wxgf":
            dst = out_dir / rel.with_suffix(".wxgf")
            dst.parent.mkdir(parents=True, exist_ok=True)
            dst.write_bytes(body)
            wxgf_pending.append((dst, dst.with_suffix(".heic")))
        else:
            ext = (ctype.split("/")[-1] if ctype else "bin") or "bin"
            dst = out_dir / rel.with_suffix(f".{ext}")
            dst.parent.mkdir(parents=True, exist_ok=True)
            dst.write_bytes(body)
            stats["ok_viewable"] += 1
        stats["bytes_written"] += len(body)
        if log and (i + 1) % 2000 == 0:
            log(f"[media_backup] 图片 {i + 1}/{len(files)}")

    if wxgf_pending:
        _convert_pending_wxgf(wxgf_pending, stats, log)
    return stats


def _convert_pending_wxgf(pending: list, stats: dict, log) -> None:
    """对第一遍收集到的 wxgf 文件批量转码，更新 stats；跨平台分流。

    log=None 等价于"不要日志"（与本模块其余函数的既有约定一致），这里统一
    归一化成空操作，避免再到处写 `if log and ...` 判断，也避免把裸 None
    传给 media_wxgf_macos.convert_wxgf_batch() 导致它内部调用时炸掉。
    """
    if log is None:
        log = lambda *_a, **_k: None
    log(f"[media_backup] {len(pending)} 张 wxgf 待转码为可预览格式…")
    if platform.system() == "Darwin":
        from siwx import media_wxgf_macos
        results = media_wxgf_macos.convert_wxgf_batch(pending, log=log)
        for wxgf_path, heic_path in pending:
            r = results.get(str(wxgf_path))
            if r and r.get("ok") and heic_path.is_file():
                stats["ok_viewable"] += 1
                stats["bytes_written"] += r.get("bytes", 0)
                try:
                    wxgf_path.unlink()
                except OSError:
                    pass
            else:
                stats["ok_wxgf_preserved"] += 1
        return
    # Windows：convert_wxgf() 内部缓存了 VoipEngine.dll 句柄，逐张调用开销
    # 不像 macOS 的 LLDB attach 那么大，不需要额外批处理。
    converted = 0
    for wxgf_path, heic_path in pending:
        try:
            data = wxgf_path.read_bytes()
            out = media.convert_wxgf(data)
        except Exception as e:
            log(f"[media_backup] wxgf 转码异常 {wxgf_path.name}: {e}")
            out = None
        if out:
            ext, ctype = media._image_sig(out) or ("gif", "image/gif")
            final_path = wxgf_path.with_suffix(f".{ext}")
            final_path.write_bytes(out)
            stats["ok_viewable"] += 1
            stats["bytes_written"] += len(out)
            try:
                wxgf_path.unlink()
            except OSError:
                pass
            converted += 1
        else:
            stats["ok_wxgf_preserved"] += 1
    log(f"[media_backup] wxgf 转码完成: {converted}/{len(pending)}")


def backup_videos(wxid_full: str, out_dir: Path, log=print) -> dict:
    """复制 msg/video 下全部 .mp4（未加密）→ out_dir，保留原有目录结构。

    Bug 修复：shutil.copy2() 连元数据一起复制，而微信自己落盘的源视频文件
    是只读的（-r--r--r--）——第一次备份时这个只读位就跟着拷到了目标文件
    上。重新运行备份（新消息到达后）时对已存在的同名目标文件再次
    shutil.copy2() 等于对一个只读文件发起覆盖写，当场 PermissionError；
    实测 1926 个视频里 1913 个（全部"已备份过的"）都这样"失败"，其实
    数据从头到尾都在、根本没真的丢。改用 copyfile()（只拷数据不拷
    元数据，目标文件按进程 umask 创建、天然可写）+ 目标已存在且大小
    相同就跳过（视频内容不会变，省去整段重复 IO，也顺带避开这个坑）。
    """
    stats = {"total": 0, "ok": 0, "skipped": 0, "failed": 0, "bytes_written": 0,
             "failures": []}
    root = _account_root(wxid_full)
    if root is None:
        return stats
    video_root = root / "msg" / "video"
    if not video_root.is_dir():
        return stats
    out_dir.mkdir(parents=True, exist_ok=True)
    files = list(video_root.rglob("*.mp4"))
    stats["total"] = len(files)
    for i, f in enumerate(files):
        try:
            rel = f.relative_to(video_root)
            dst = out_dir / rel
            src_size = f.stat().st_size
            if dst.is_file() and dst.stat().st_size == src_size:
                stats["skipped"] += 1
                continue
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(f, dst)
            stats["ok"] += 1
            stats["bytes_written"] += src_size
        except Exception as e:
            stats["failed"] += 1
            if len(stats["failures"]) < 50:
                stats["failures"].append(f"{f.name}: {type(e).__name__}: {e}")
        if log and (i + 1) % 200 == 0:
            log(f"[media_backup] 视频 {i + 1}/{len(files)}")
    return stats


def backup_files(wxid_full: str, out_dir: Path, log=print) -> dict:
    """复制 msg/file 下全部文件消息附件（未加密，原文件名/扩展名原样
    保留——实测 PDF/APK/PNG 等头部签名完好）→ out_dir，保留原有目录结构。

    与 backup_videos() 同一套坑、同一套修法：源文件只读
    （-r--r--r--），用 copyfile()（不拷元数据）而非 copy2()，且目标
    已存在且大小相同就跳过，避免重复运行时覆盖写只读目标文件炸
    PermissionError，也省去重复 IO。.DS_Store 等 Finder 元数据文件
    (以 "." 开头) 不是聊天附件，跳过。
    """
    stats = {"total": 0, "ok": 0, "skipped": 0, "failed": 0, "bytes_written": 0,
             "failures": []}
    root = _account_root(wxid_full)
    if root is None:
        return stats
    file_root = root / "msg" / "file"
    if not file_root.is_dir():
        return stats
    out_dir.mkdir(parents=True, exist_ok=True)
    files = [f for f in file_root.rglob("*") if f.is_file() and not f.name.startswith(".")]
    stats["total"] = len(files)
    for i, f in enumerate(files):
        try:
            rel = f.relative_to(file_root)
            dst = out_dir / rel
            src_size = f.stat().st_size
            if dst.is_file() and dst.stat().st_size == src_size:
                stats["skipped"] += 1
                continue
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(f, dst)
            stats["ok"] += 1
            stats["bytes_written"] += src_size
        except Exception as e:
            stats["failed"] += 1
            if len(stats["failures"]) < 50:
                stats["failures"].append(f"{f.name}: {type(e).__name__}: {e}")
        if log and (i + 1) % 200 == 0:
            log(f"[media_backup] 文件 {i + 1}/{len(files)}")
    return stats


def backup_all(wxid_full: str, out_dir: Path, log=print) -> dict:
    """images/ + videos/ + files/ 一次性全量备份，返回合并统计。"""
    img_stats = backup_images(wxid_full, out_dir / "images", log=log)
    vid_stats = backup_videos(wxid_full, out_dir / "videos", log=log)
    file_stats = backup_files(wxid_full, out_dir / "files", log=log)
    return {"images": img_stats, "videos": vid_stats, "files": file_stats}
