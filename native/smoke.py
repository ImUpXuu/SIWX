"""Frozen Windows runtime smoke with isolated synthetic SQLite, never user data."""
from pathlib import Path
import json,os,socket,subprocess,sys,tempfile,time,urllib.request
import psutil
ROOT=Path(__file__).resolve().parent
sys.path.insert(0,str(ROOT/'tests'))
from fixture import make_fixture,ACCOUNT,CHAT
exe=Path(sys.argv[1]).resolve()
with tempfile.TemporaryDirectory(prefix='siwx_frozen_synthetic_') as tmp:
 home=Path(tmp);original=home/'synthetic-original';original.mkdir();make_fixture(original)
 exports=home/'exports';exports.mkdir(exist_ok=True);(exports/'restart-proof.json').write_text('{"synthetic":true}',encoding='utf-8')
 old_exports=original/'exports';old_exports.mkdir();(old_exports/'restart-proof.json').write_text('{"original_synthetic":true}',encoding='utf-8')
 env=dict(os.environ,SIWX_COMPAT_ROOTS=json.dumps([str(original)]),SIWX_NATIVE_HOME=str(home),SIWX_NO_PLUGINS='1',PYTHONUTF8='1')
 with socket.socket() as listener:listener.bind(('127.0.0.1',0));port=listener.getsockname()[1]
 with (home/'console.log').open('wb') as log:
  process=subprocess.Popen([str(exe),'--port',str(port),'--no-open'],env=env,cwd=home,stdout=log,stderr=log)
  try:
   base='http://127.0.0.1:'+str(port);deadline=time.monotonic()+45
   while True:
    try:
     with urllib.request.urlopen(base+'/',timeout=2) as response:page=response.read().decode()
     break
    except OSError:
     if process.poll() is not None or time.monotonic()>deadline:raise RuntimeError('Frozen runtime did not start in synthetic home')
     time.sleep(.2)
   listeners=[c.laddr.ip for c in psutil.net_connections(kind='tcp') if c.pid==process.pid and c.status==psutil.CONN_LISTEN and c.laddr.port==port]
   assert listeners==['127.0.0.1'],listeners
   assert 'stories-in-wx' in page and 'href="#/stats"' not in page
   with urllib.request.urlopen(base+'/api/status',timeout=15) as response:assert 'accounts' in json.load(response)
   while True:
    with urllib.request.urlopen(base+'/api/native/compatibility',timeout=10) as response:compat=json.load(response)
    if not compat['running']:break
    if time.monotonic()>deadline:raise RuntimeError('Compatibility import timeout')
    time.sleep(.1)
   assert compat['copied']>=4 and compat['failed']==0,compat
   with urllib.request.urlopen(base+'/api/chat/sessions?account='+ACCOUNT,timeout=10) as response:assert json.load(response)['sessions'][0]['username']==CHAT
   with urllib.request.urlopen(base+'/api/chat/messages?account='+ACCOUNT+'&chat='+CHAT+'&limit=80',timeout=10) as response:
    messages=json.load(response)['messages'];assert len(messages)==80 and '原生合成消息' in json.dumps(messages,ensure_ascii=False)
   with urllib.request.urlopen(base+'/api/native/version',timeout=10) as response:assert json.load(response)['version']=='1.7.0'
   with urllib.request.urlopen(base+'/api/native/source',timeout=10) as response:assert response.read(4)==b'PK\x03\x04'
   with urllib.request.urlopen(base+'/api/native/exports',timeout=10) as response:
    history=json.load(response);assert history['total']==2 and sum(row['source']=='native' for row in history['files'])==1 and sum(row['source'].startswith('original-') for row in history['files'])==1
   with urllib.request.urlopen(base+history['files'][0]['download'],timeout=10) as response:assert json.load(response) in [{'synthetic':True},{'original_synthetic':True}]
   with urllib.request.urlopen(base+'/export-history.js',timeout=10) as response:assert b'e-history' in response.read()
   print('PASS: frozen runtime default listen socket is 127.0.0.1 only; original runtime serves original UI, sessions and 80 actual synthetic SQLite messages')
  except BaseException:
   log.flush()
   print("Synthetic frozen runtime diagnostic:", (home/"console.log").read_text(encoding="utf-8",errors="replace"),flush=True)
   raise
  finally:
   process.terminate()
   try:process.wait(timeout=5)
   except subprocess.TimeoutExpired:process.kill();process.wait(timeout=5)

 with (home/'restart.log').open('wb') as log:
  process=subprocess.Popen([str(exe),'--port',str(port),'--no-open'],env=env,cwd=home,stdout=log,stderr=log)
  try:
   deadline=time.monotonic()+45
   while True:
    try:
     with urllib.request.urlopen(base+'/api/native/exports',timeout=2) as response:history=json.load(response)
     break
    except OSError:
     if process.poll() is not None or time.monotonic()>deadline:raise RuntimeError('Restart failed')
     time.sleep(.2)
   assert history['total']==2 and sum(row['source']=='native' for row in history['files'])==1 and sum(row['source'].startswith('original-') for row in history['files'])==1
   with urllib.request.urlopen(base+history['files'][0]['download'],timeout=10) as response:assert json.load(response) in [{'synthetic':True},{'original_synthetic':True}]
   assert (original/'output'/ACCOUNT/'message/message_0.db').is_file()
   print('PASS: frozen compatibility imports original synthetic SQLite, retains original files, lists and downloads both edition exports')
   print('PASS: frozen actual process restart preserves exported file listing and download')
  finally:
   process.terminate()
   try:process.wait(timeout=5)
   except subprocess.TimeoutExpired:process.kill();process.wait(timeout=5)
