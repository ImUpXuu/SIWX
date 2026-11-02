"""Synthetic slow extraction, cancellation and SQLite thread regressions."""
import importlib, http.client, json, sqlite3, sys, tempfile, threading, time, unittest
from pathlib import Path
from types import SimpleNamespace, ModuleType
from unittest.mock import patch
from siwx_safe.live import Engine
from siwx_safe.local_web import LocalServer
from siwx_safe.vendor.wexport.model import Account
from test_live import FakeSource

class AsyncReadTests(unittest.TestCase):
 def setUp(self):
  self.engine=Engine([Account('example','win4')]);self.server=LocalServer(self.engine,0,'127.0.0.1');self.thread=threading.Thread(target=self.server.serve_forever);self.thread.start();self.addCleanup(self.cleanup)
 def cleanup(self):
  self.engine.read_progress.cancelled.set();self.server.shutdown();self.thread.join(3);self.server.server_close()
 def request(self,path,body=None):
  c=http.client.HTTPConnection('127.0.0.1',self.server.server_port,timeout=2);c.request('POST' if body is not None else 'GET',path,json.dumps(body) if body is not None else None,{'Content-Type':'application/json'});r=c.getresponse();status,data=r.status,r.read();c.close();return status,json.loads(data)
 def finished(self):
  deadline=time.monotonic()+3
  while time.monotonic()<deadline:
   status,data=self.request('/api/open/progress');self.assertEqual(200,status)
   if data['state']!='running' and not self.server.reading:return data
   time.sleep(.01)
  self.fail('Synthetic task did not finish')
 def test_responsive_conflicts_cancel_and_retry(self):
  entered=threading.Event()
  def slow(_):
   self.engine.read_progress.update('正在解密数据库（1/2）');entered.set()
   while True:self.engine.read_progress.check();time.sleep(.005)
  self.engine.opener=slow
  self.assertEqual(400,self.request('/api/open/start',{'account':0,'consent':False})[0]);self.assertEqual(202,self.request('/api/open/start',{'account':0,'consent':True})[0]);self.assertTrue(entered.wait(1));self.assertIn('1/2',self.request('/api/open/progress')[1]['message'])
  for path in ['/api/logs','/api/status','/api/accounts']:self.assertEqual(200,self.request(path)[0])
  self.assertEqual(400,self.request('/api/open/start',{'account':0,'consent':True})[0])
  for path,body in [('/api/chats',None),('/api/close',{}),('/api/accounts/refresh',{}),('/api/open',{'account':0,'consent':True})]:self.assertEqual(409,self.request(path,body)[0])
  self.assertEqual(200,self.request('/api/open/cancel',{})[0]);self.assertEqual('cancelled',self.finished()['state']);self.engine.opener=lambda _:FakeSource();self.assertEqual(202,self.request('/api/open/start',{'account':0,'consent':True})[0]);self.assertEqual('done',self.finished()['state']);self.assertEqual(200,self.request('/api/chats')[0])
 def test_sqlite_thread_affinity_and_error_cleanup(self):
  identities=[];sources=[]
  class ThreadSource(FakeSource):
   def __init__(self):super().__init__();self.conn=sqlite3.connect(':memory:');identities.append(threading.get_ident())
   def conversations(self):self.conn.execute('SELECT 1').fetchone();identities.append(threading.get_ident());return super().conversations()
   def close(self):self.conn.close();identities.append(threading.get_ident());super().close()
  def opened(_):sources.append(ThreadSource());return sources[-1]
  self.engine.opener=opened;self.assertEqual(202,self.request('/api/open/start',{'account':0,'consent':True})[0]);self.assertEqual('done',self.finished()['state']);self.assertEqual(200,self.request('/api/chats')[0]);self.assertEqual(200,self.request('/api/close',{})[0]);self.assertEqual(1,len(set(identities)));self.assertNotEqual(threading.get_ident(),identities[0]);self.assertEqual({},sources[0]._keys)
  self.engine.opener=lambda _:(_ for _ in ()).throw(RuntimeError('synthetic-private-secret'));self.request('/api/open/start',{'account':0,'consent':True});data=self.finished();self.assertEqual('error',data['state']);self.assertNotIn('synthetic-private-secret',json.dumps(data));self.assertIsNone(self.engine.source)
 def test_cooperative_deadline(self):
  self.engine.read_progress.timeout=.01
  def slow(_):
   while True:self.engine.read_progress.check();time.sleep(.005)
  self.engine.opener=slow;self.request('/api/open/start',{'account':0,'consent':True});data=self.finished();self.assertEqual('error',data['state']);self.assertIn('15分钟',data['message'])

class ScanReadTests(unittest.TestCase):
 def test_chunk_overlap_gap_native_addresses_and_header_only(self):
  module='siwx_safe.vendor.wexport.win.memory';fake=ModuleType(module);fake.ProcessMemory=object
  def find_all(data,needle):
   start=0
   while True:
    index=data.find(needle,start)
    if index<0:return
    yield index;start=index+1
  fake.find_all=find_all;name='siwx_safe.vendor.wexport.win.wcdb_keys';previous=sys.modules.pop(name,None)
  try:
   processes=ModuleType('siwx_safe.vendor.wexport.win.processes');processes.find_pids=lambda *args:[]
   with patch.dict(sys.modules,{module:fake,'siwx_safe.vendor.wexport.win.processes':processes}):scanner=importlib.import_module(name)
   chunk=4<<20;needle=b'NEEDLE';base=0x10000;blob=b'a'*(chunk-2)+needle+b'z'*10
   class Memory:
    def regions(self):return [SimpleNamespace(base=base,size=len(blob))]
    def read(self,addr,size):return blob[addr-base:addr-base+size]
   checks=[];self.assertEqual([base+chunk-2],list(scanner._search(Memory(),needle,lambda:checks.append(1))));self.assertEqual(2,len(checks))
   class Gap:
    def regions(self):return [SimpleNamespace(base=base,size=chunk*2+9)]
    def read(self,addr,size):
     if addr==base:return b'a'*(chunk-3)+b'NEE'
     if addr==base+chunk:return None
     return b'DLE'+needle
   self.assertEqual([base+chunk*2+3],list(scanner._search(Gap(),needle)))
   with tempfile.TemporaryDirectory() as tmp:
    root=Path(tmp);db=root/'db_storage/message/message_0.db';db.parent.mkdir(parents=True)
    with db.open('wb') as stream:stream.write(b'synthetic header');stream.truncate(64<<20)
    calls=[]
    def header(path,offset,size):calls.append((offset,size));return b'synthetic header'
    with patch.object(scanner,'read_shared_range',header),patch.object(Path,'read_bytes',side_effect=AssertionError('whole file read')):self.assertEqual(['message/message_0.db'],list(scanner.db_files(root)))
    self.assertEqual([(0,16)],calls)
  finally:
   sys.modules.pop(name,None)
   if previous is not None:sys.modules[name]=previous
