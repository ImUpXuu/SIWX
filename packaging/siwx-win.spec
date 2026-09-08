# -*- mode: python ; coding: utf-8 -*-
# Windows 构建：单文件 exe（stories-in-wx）
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
