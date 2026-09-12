# -*- mode: python ; coding: utf-8 -*-
# Windows 构建：单文件 exe（stories-in-wx）
import sys
from pathlib import Path

ROOT = Path(SPECPATH).parent


def _silk_decoder_bins():
    root = ROOT / "siwx" / "vendor" / "silk-decoder"
    if not root.is_dir():
        return []
    names = {"silk_v3_decoder.exe", "silk-decoder.exe", "silk_decoder.exe", "decoder.exe"}
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
    hiddenimports=[],
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
