"""Acceptance against the generated original SIWX implementation."""
from pathlib import Path
import ast,hashlib,json,os,sys,tempfile,unittest
from unittest.mock import patch
ROOT=Path(__file__).resolve().parents[1]
sys.path[:0]=[str(ROOT),str(ROOT/'_build')]
from fixture import ACCOUNT,CHAT,make_fixture

class NativeTests(unittest.TestCase):
 @classmethod
 def setUpClass(cls):
  cls.temp=tempfile.TemporaryDirectory();cls.home=Path(cls.temp.name).resolve();os.environ['SIWX_NATIVE_HOME']=str(cls.home);os.environ['SIWX_NO_PLUGINS']='1'
  from app import make_app
  cls.app=make_app();cls.client=cls.app.test_client();cls.acc=make_fixture(cls.home)
 @classmethod
 def tearDownClass(cls):
  # File log handlers are closed before fixture deletion (Windows file ownership).
  import logging
  for handler in list(logging.getLogger('siwx').handlers):handler.close();logging.getLogger('siwx').removeHandler(handler)
  import faulthandler
  from siwx import logging_setup
  faulthandler.disable()
  if logging_setup._CRASH_FH is not None:logging_setup._CRASH_FH.close();logging_setup._CRASH_FH=None
  cls.temp.cleanup()
 def test_original_retained_code_and_removed_pages(self):
  source=ROOT.parent/'siwx';generated=ROOT/'_build/siwx'
  for name in ['extract.py','sqlcipher.py','api_export.py','api_sns.py','media.py','voice.py','sns.py','ui/onboarding.js','ui/pages/export.js','ui/pages/sns.js']:
   self.assertEqual((source/name).read_bytes(),(generated/name).read_bytes(),name)
  functions=lambda path:{n.name:ast.dump(n,include_attributes=False) for n in ast.parse(path.read_text()).body if isinstance(n,ast.FunctionDef)}
  before,after=functions(source/'api_chat.py'),functions(generated/'api_chat.py')
  for name,value in after.items():self.assertEqual(before[name],value,name)
  self.assertNotIn('conversation_stats',after)
  self.assertNotIn('siwx_safe',sys.modules)
  for name in ['stats','backup','mcp']:
   self.assertEqual(404,self.client.get('/pages/'+name+'.js').status_code)
   self.assertEqual(404,self.client.get('/api/'+name+'/info').status_code)
  for url in ['/api/update/current','/api/media/backup/status','/api/chat/stats']:
   self.assertEqual(404,self.client.get(url).status_code)
 def test_original_sessions_messages_pagination_search_timeline(self):
  response=self.client.get('/api/chat/accounts');self.assertEqual(200,response.status_code);self.assertEqual(ACCOUNT,response.json['accounts'][0]['wxid'])
  base='?account='+ACCOUNT+'&chat='+CHAT
  sessions=self.client.get('/api/chat/sessions?account='+ACCOUNT);self.assertEqual(200,sessions.status_code);self.assertEqual(CHAT,sessions.json['sessions'][0]['username'])
  response=self.client.get('/api/chat/messages'+base+'&limit=80');self.assertEqual(200,response.status_code);rows=response.json['messages'];self.assertEqual(80,len(rows));self.assertIn('原生合成消息',json.dumps(rows,ensure_ascii=False))
  response=self.client.get('/api/chat/messages'+base+'&q=179');self.assertEqual(200,response.status_code);self.assertIn('179',json.dumps(response.json,ensure_ascii=False))
  response=self.client.get('/api/chat/timeline'+base);self.assertEqual(200,response.status_code)
 def test_original_sns_and_multi_format_export_download(self):
  from siwx import exporter,paths
  response=self.client.get('/api/sns/timeline?account='+ACCOUNT);self.assertEqual(200,response.status_code);self.assertIn('原生合成朋友圈',json.dumps(response.json,ensure_ascii=False))
  report=exporter.run_export(self.acc,ACCOUNT,CHAT,'合成联系人',['json','html'],want_media=False,want_voice=False,want_avatars=False,export_root=paths.exports_root(),pack='folder')
  files=[p for p in paths.exports_root().rglob('*.json') if p.name!='manifest.json'];self.assertTrue(files);data=files[0].read_text();self.assertIn('原生合成消息',data)
  response=self.client.get('/api/export/download',query_string={'path':str(files[0])});self.assertEqual(200,response.status_code);response.close()
  self.assertTrue(list(paths.exports_root().rglob('*.html')))
 def test_no_client_auth_and_storage_separate(self):
  for headers in [{'Host':'example.invalid','Origin':'https://another.invalid','Sec-Fetch-Site':'cross-site'},{'Host':'203.0.113.1:1234'}]:
   self.assertEqual(200,self.client.get('/api/chat/accounts',headers=headers).status_code)
   self.assertEqual(200,self.client.get('/api/logs',headers=headers).status_code)
  self.assertEqual(404,self.client.post('/api/mcp/config',json={},headers={'Origin':'null','Sec-Fetch-Site':'cross-site'}).status_code)
  from siwx import paths
  self.assertEqual(self.home,paths.app_root());self.assertEqual(self.home/'data',paths.data_dir());self.assertEqual(self.home/'output',paths.out_root())
  self.assertEqual('1.7.1',self.client.get('/api/native/version').json['version'])
if __name__=='__main__':unittest.main()
