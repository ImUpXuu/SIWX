# -*- mode: python ; coding: utf-8 -*-
# macOS 构建：.app（功能同 Windows 版，运行时提示仅支持 Windows）
import sys
from pathlib import Path

ROOT = Path(SPECPATH).parent


def _app_version():
    """版本唯一来源：siwx/__init__.py 的 __version__（不 import，避免拉入依赖）。"""
    try:
        for line in (ROOT / "siwx" / "__init__.py").read_text(encoding="utf-8").splitlines():
            if line.startswith("__version__"):
                return line.split("=", 1)[1].strip().strip('"').strip("'")
    except OSError:
        pass
    return "0.0.0"


APP_VERSION = _app_version()


def _silk_decoder_bins():
    root = ROOT / "siwx" / "vendor" / "silk-decoder"
    if not root.is_dir():
        return []
    names = {"silk_v3_decoder", "silk-decoder", "silk_decoder", "decoder"}
    out = []
    for p in root.rglob("*"):
        if p.is_file() and p.name in names:
            out.append((str(p), str(Path("vendor") / "silk-decoder" / p.parent.relative_to(root))))
    return out


a = Analysis(
    [str(ROOT / "run.py")],
    pathex=[str(ROOT)],
    datas=[
        (str(ROOT / "siwx" / "ui"), "ui"),
        (str(ROOT / "version.json"), "."),
    ],
    hiddenimports=["pilk"],
    binaries=_silk_decoder_bins(),
    noarchive=False,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    name="stories-in-wx",
    console=True,
    upx=False,
)
app = BUNDLE(
    exe,
    name="stories-in-wx.app",
    info_plist={
        "CFBundleName": "stories-in-wx",
        "CFBundleDisplayName": "stories-in-wx",
        "CFBundleIdentifier": "com.storiesinwx.app",
        "CFBundleShortVersionString": APP_VERSION,
        "NSHighResolutionCapable": True,
    },
)
