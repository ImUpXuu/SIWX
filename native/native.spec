# Original SIWX edition with a recorded subtractive source overlay.
from pathlib import Path
import json,hashlib
ROOT=Path(SPECPATH)
BUILD=ROOT/'_build'
assert (BUILD/'native-source-manifest.json').is_file(),'Run prepare.py first'
a=Analysis([str(ROOT/'app.py')],pathex=[str(BUILD),str(ROOT)],
 datas=[(str(BUILD/'siwx/ui'),'ui'),(str(BUILD/'siwx/assets'),'siwx/assets'),
        (str(BUILD/'siwx/templates'),'siwx/templates'),(str(BUILD/'native-source-manifest.json'),'.')],
 hiddenimports=['pilk'],
 excludes=['siwx.api_stats','siwx.api_backup','siwx.api_mcp','siwx.api_update','siwx.mcp_server','siwx.backup','siwx.auto_update','siwx_safe','wexport','tkinter'],
 noarchive=False)
source_sha256={}
native_source_sha256={}
for name,source,*_ in a.pure:
 if name in {"export_history","compat"}:
  assert Path(source).resolve()==(ROOT/(name+".py")).resolve()
  native_source_sha256[name]=hashlib.sha256(Path(source).read_bytes()).hexdigest()
 if name=="siwx" or name.startswith("siwx."):
  path=Path(source).resolve()
  if not path.is_relative_to((BUILD/"siwx").resolve()):raise RuntimeError("Wrong upstream copy: "+name)
  source_sha256[name]=hashlib.sha256(path.read_bytes()).hexdigest()
 if name.startswith(('siwx_safe','wexport')) or name in {'siwx.api_stats','siwx.api_backup','siwx.api_mcp','siwx.api_update','siwx.mcp_server','siwx.backup','siwx.auto_update'}:raise RuntimeError('Removed module: '+name)
Path('native-module-manifest.json').write_text(json.dumps({'pure':sorted(e[0] for e in a.pure),'binaries':sorted(e[0] for e in a.binaries),'edition':'Original SIWX 5.3.0 modules; no adapter','source_sha256':source_sha256, 'native_source_sha256':native_source_sha256, 'removed':['statistics','backup','MCP','online update','client authentication']},indent=2))
pyz=PYZ(a.pure)
exe=EXE(pyz,a.scripts,[],exclude_binaries=True,name='siwx-lite',console=True,strip=False,upx=False,uac_admin=False)
coll=COLLECT(exe,a.binaries,a.datas,strip=False,upx=False,name='siwx-lite')
