"""Run upstream regressions in isolated temp data, or native acceptance tests.
No source tree/user data writes beyond generated overlay and isolated test files.
"""
from pathlib import Path
import os,subprocess,sys,tempfile
ROOT=Path(__file__).resolve().parent
with tempfile.TemporaryDirectory(prefix='siwx_native_tests_') as tmp:
 env=dict(os.environ,SIWX_ROOT=tmp,SIWX_DATA_DIR=str(Path(tmp)/'data'),SIWX_NATIVE_HOME=tmp,SIWX_NO_PLUGINS='1',PYTHONUTF8='1')
 args=[sys.executable,'-m','pytest','tests','-q','-rs'] if '--upstream' in sys.argv else [sys.executable,'-m','unittest','discover','-s',str(ROOT/'tests'),'-v']
 raise SystemExit(subprocess.call(args,cwd=ROOT.parent,env=env))
