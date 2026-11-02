# Build only from the isolated safe/ source root. No legacy project dependencies.
from pathlib import Path
import json

ROOT = Path(SPECPATH)
a = Analysis([str(ROOT / "app.py")], pathex=[str(ROOT)],
             binaries=[], datas=[(str(ROOT / "siwx_safe/vendor/wexport/LICENSE"), "licenses/wexport")],
             hiddenimports=[], excludes=["siwx", "wexport", "psutil", "flask", "Crypto", "requests"],
             noarchive=False)
for entry in list(a.pure) + list(a.binaries):
    name = entry[0]
    if name.split(".")[0] in {"siwx", "wexport", "psutil", "flask", "Crypto", "requests"}:
        raise RuntimeError("Legacy or unnecessary module included: " + name)
    if name.startswith("siwx_safe.vendor.wexport.win") or name in {
        "siwx_safe.live", "siwx_safe.local_web", "siwx_safe.live_app"}:
        raise RuntimeError("Live extraction is not allowed in the offline build: " + name)
    source = Path(entry[1]).resolve()
    project = ROOT.parent.resolve()
    if source.is_relative_to(project) and not source.is_relative_to(ROOT.resolve()):
        raise RuntimeError("Module outside isolated safe source root: " + name)
Path("module-manifest.json").write_text(json.dumps(
    {"pure": sorted(e[0] for e in a.pure), "binaries": sorted(e[0] for e in a.binaries)},
    indent=2), encoding="utf-8")
pyz = PYZ(a.pure)
exe = EXE(pyz, a.scripts, [], exclude_binaries=True, name="siwx-safe",
          console=True, strip=False, upx=False, uac_admin=False)
coll = COLLECT(exe, a.binaries, a.datas, strip=False, upx=False, name="siwx-safe")
