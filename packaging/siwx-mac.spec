# -*- mode: python ; coding: utf-8 -*-
# macOS 构建：.app（功能同 Windows 版，运行时提示仅支持 Windows）
import sys
from pathlib import Path

ROOT = Path(SPECPATH).parent

a = Analysis(
    [str(ROOT / "run.py")],
    pathex=[str(ROOT)],
    datas=[
        (str(ROOT / "siwx" / "ui"), "ui"),
        (str(ROOT / "version.json"), "."),
    ],
    hiddenimports=[],
    binaries=[],
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
        "CFBundleShortVersionString": "0.2.0",
        "NSHighResolutionCapable": True,
    },
)
