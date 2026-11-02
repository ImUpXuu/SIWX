# Minimal live edition; intentionally retains read-only Windows extraction.
from pathlib import Path
import json

ROOT = Path(SPECPATH)
a = Analysis([str(ROOT / "live_app.py")], pathex=[str(ROOT)], binaries=[],
             datas=[(str(ROOT / "siwx_safe/live_ui"), "siwx_safe/live_ui"),
                    (str(ROOT / "siwx_safe/vendor/wexport/LICENSE"), "licenses/wexport")],
             hiddenimports=[], excludes=["siwx", "wexport", "flask", "tkinter", "pyzstd"],
             noarchive=False)
allowed = {"siwx_safe.vendor.wexport", "siwx_safe.vendor.wexport.model",
           "siwx_safe.vendor.wexport.errors", "siwx_safe.vendor.wexport.parse",
           "siwx_safe.vendor.wexport.parse.content", "siwx_safe.vendor.wexport.sources",
           "siwx_safe.vendor.wexport.sources.base", "siwx_safe.vendor.wexport.sources.win4",
           "siwx_safe.vendor.wexport.crypto", "siwx_safe.vendor.wexport.crypto.aes",
           "siwx_safe.vendor.wexport.crypto.sqlcipher", "siwx_safe.vendor.wexport.crypto.zstd",
           "siwx_safe.vendor.wexport.win", "siwx_safe.vendor.wexport.win.processes",
           "siwx_safe.vendor.wexport.win.memory", "siwx_safe.vendor.wexport.win.wcdb_keys"}
for name, source, *_ in list(a.pure) + list(a.binaries):
    if name.split(".")[0] in {"siwx", "wexport", "flask", "tkinter"}:
        raise RuntimeError("Legacy/unneeded module: " + name)
    if name.startswith("siwx_safe.vendor.wexport") and name not in allowed:
        raise RuntimeError("Module outside the selected wexport subset: " + name)
    if name.startswith("siwx_safe.") and any(p in name for p in
        (".stats", ".backup", ".mcp", ".plugins", ".auto_update", ".linux_hook", ".macos_lldb", ".deep_scan", ".image_key")):
        raise RuntimeError("Removed capability: " + name)
    p = Path(source).resolve()
    if p.is_relative_to(ROOT.parent.resolve()) and not p.is_relative_to(ROOT.resolve()):
        raise RuntimeError("Module outside the isolated edition: " + name)
Path("live-module-manifest.json").write_text(json.dumps({
    "pure": sorted(e[0] for e in a.pure), "binaries": sorted(e[0] for e in a.binaries),
    "capabilities": ["existing read-only current-user WeChat 4.x extraction", "command-line launch", "anonymous browser controls on 0.0.0.0:8787 by default", "text exports", "original four-step setup wizard/settings/operation logs", "read-only opened SNS timeline/media/export"],
    "removed": ["chat statistics", "encrypted backup", "MCP", "injection", "deep scan", "background installation", "automatic browser launch", "token authentication", "auto update"]
}, indent=2), encoding="utf-8")
pyz = PYZ(a.pure)
exe = EXE(pyz, a.scripts, [], exclude_binaries=True, name="siwx-lite",
          console=True, strip=False, upx=False, uac_admin=False)
coll = COLLECT(exe, a.binaries, a.datas, strip=False, upx=False, name="siwx-lite")
