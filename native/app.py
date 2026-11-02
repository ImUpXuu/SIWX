"""Original SIWX desktop edition entry; retained features use original modules."""
from pathlib import Path
import argparse, json, os, platform, sys

VERSION='1.5.0'
BASE=Path(__file__).resolve().parent
if not getattr(sys,'frozen',False):sys.path.insert(0,str(BASE/'_build'))

def configure_home():
 value=os.environ.get('SIWX_NATIVE_HOME')
 root=Path(value) if value else Path(os.environ.get('LOCALAPPDATA',Path.home()))/'stories-in-wx-console'/'native'
 if not root.is_absolute() or str(root).startswith(('\\\\','//')):raise ValueError('Native home must be a local absolute directory.')
 root=root.resolve();root.mkdir(parents=True,exist_ok=True)
 os.environ['SIWX_ROOT']=str(root);os.environ['SIWX_DATA_DIR']=str(root/'data')
 return root

def make_app():
 configure_home()
 from siwx import server
 @server.app.get('/api/native/version')
 def native_version():return {'version':VERSION,'base':'5.2.0','edition':'original pages and backend'}
 # Defense against plugin reintroductions; feature removal, not caller authentication.
 @server.app.get('/api/native/source')
 def native_source():
  from flask import abort,send_file
  path=(Path(sys.executable).resolve().parent if getattr(sys,'frozen',False) else BASE)/'SOURCE.zip'
  if not path.is_file():abort(404)
  return send_file(path,as_attachment=True,download_name='SIWX-1.5.0-source.zip')
 @server.app.before_request
 def removed_features():
  from flask import abort,request
  path=request.path
  if any(path==prefix or path.startswith(prefix+'/') for prefix in ['/api/stats','/api/backup','/api/mcp','/api/update','/api/media/backup','/api/chat/stats']):abort(404)
 return server.app

def main(argv=None):
 parser=argparse.ArgumentParser(description='SIWX native edition: original pages and backend, selected features removed.')
 parser.add_argument('--version',action='version',version='SIWX '+VERSION+' (original SIWX 5.2.0)')
 parser.add_argument('command',nargs='?',choices=['probe'])
 parser.add_argument('--host',choices=['0.0.0.0','127.0.0.1'],default='0.0.0.0')
 parser.add_argument('--port',type=int,default=8787)
 parser.add_argument('--no-open',action='store_true')
 parser.add_argument('--data',type=Path)
 args=parser.parse_args(argv)
 if args.command=='probe':
  print(json.dumps({'version':VERSION,'platform':platform.system(),'edition':'original SIWX 5.2.0','host':args.host}));return 0
 if not 1<=args.port<=65535:parser.error('port out of range')
 if sys.platform=='win32':
  import psutil
  owner=psutil.Process().username().rsplit('\\',1)[-1].casefold()
  if owner in {'system','local service','network service'}:parser.error('Run in the selected logged-in desktop user session.')
 app=make_app()
 if args.data:
  from siwx.discover import add_manual_data_dir
  result=add_manual_data_dir(str(args.data));
  if not result.get('saved'):parser.error('WeChat data directory was not accepted.')
 from siwx.server import run_server
 run_server(args.host,args.port,open_browser=False,trust_lan=True)
 return 0

if __name__=='__main__':
 # Original entry normalizes console streams before importing rich/CLI/runtime.
 # Frozen Python ignores PYTHONUTF8; the relay reads UTF-8 log bytes explicitly.
 for stream in (sys.stdout,sys.stderr):
  try:
   if stream is not None:stream.reconfigure(encoding='utf-8',errors='replace')
  except (AttributeError,OSError,ValueError):pass
 from multiprocessing import freeze_support
 freeze_support()
 if len(sys.argv)>1 and sys.argv[1]=='--wxgf-worker':
  # Same original frozen media worker body; no replacement media implementation.
  from siwx.media import _WXGF_WORKER
  sys.argv=[sys.argv[0],*sys.argv[2:]]
  exec(compile(_WXGF_WORKER,'<wxgf-worker>','exec'),{'__name__':'__main__'})
 else:raise SystemExit(main())
