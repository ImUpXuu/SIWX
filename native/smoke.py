"""Frozen Windows runtime smoke with isolated synthetic SQLite, never user data."""
from pathlib import Path
import json,os,socket,subprocess,sys,tempfile,time,urllib.request
ROOT=Path(__file__).resolve().parent
sys.path.insert(0,str(ROOT/'tests'))
from fixture import make_fixture,ACCOUNT,CHAT
exe=Path(sys.argv[1]).resolve()
with tempfile.TemporaryDirectory(prefix='siwx_frozen_synthetic_') as tmp:
 home=Path(tmp);make_fixture(home)
 env=dict(os.environ,SIWX_NATIVE_HOME=str(home),SIWX_NO_PLUGINS='1',PYTHONUTF8='1')
 with socket.socket() as listener:listener.bind(('127.0.0.1',0));port=listener.getsockname()[1]
 with (home/'console.log').open('wb') as log:
  process=subprocess.Popen([str(exe),'--host','127.0.0.1','--port',str(port),'--no-open'],env=env,cwd=home,stdout=log,stderr=log)
  try:
   base='http://127.0.0.1:'+str(port);deadline=time.monotonic()+45
   while True:
    try:
     with urllib.request.urlopen(base+'/',timeout=2) as response:page=response.read().decode()
     break
    except OSError:
     if process.poll() is not None or time.monotonic()>deadline:raise RuntimeError('Frozen runtime did not start in synthetic home')
     time.sleep(.2)
   assert 'stories-in-wx' in page and 'href="#/stats"' not in page
   with urllib.request.urlopen(base+'/api/chat/sessions?account='+ACCOUNT,timeout=10) as response:assert json.load(response)['sessions'][0]['username']==CHAT
   with urllib.request.urlopen(base+'/api/chat/messages?account='+ACCOUNT+'&chat='+CHAT+'&limit=80',timeout=10) as response:
    messages=json.load(response)['messages'];assert len(messages)==80 and '原生合成消息' in json.dumps(messages,ensure_ascii=False)
   with urllib.request.urlopen(base+'/api/native/version',timeout=10) as response:assert json.load(response)['version']=='1.5.0'
   with urllib.request.urlopen(base+'/api/native/source',timeout=10) as response:assert response.read(4)==b'PK\x03\x04'
   print('PASS: frozen original runtime serves original UI, sessions and 80 actual synthetic SQLite messages')
  except BaseException:
   log.flush()
   print("Synthetic frozen runtime diagnostic:", (home/"console.log").read_text(encoding="utf-8",errors="replace"),flush=True)
   raise
  finally:
   process.terminate()
   try:process.wait(timeout=5)
   except subprocess.TimeoutExpired:process.kill();process.wait(timeout=5)
