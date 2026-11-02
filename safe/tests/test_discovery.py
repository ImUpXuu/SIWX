import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch
from siwx_safe.live import Engine,discover_accounts,_accounts_at,_windows_roots
class DiscoveryTests(unittest.TestCase):
 def setUp(self):
  self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup);self.root=Path(self.tmp.name).resolve()
 def account(self,path):
  (path/'db_storage/message').mkdir(parents=True);return path
 def test_redirected_drive_and_direct_account_roots(self):
  home=self.root/'profile';home.mkdir();drive=self.root/'drive';drive.mkdir();a=self.account(drive/'xwechat_files/user_example');redirected=self.root/'redirected';redirected.mkdir();b=self.account(redirected/'xwechat_files/user_second');direct=self.account(self.root/'custom/user_direct')
  with patch('siwx_safe.live.Path.home',return_value=home),patch('siwx_safe.live.sys.platform','win32'),patch('siwx_safe.live._windows_roots',return_value=[drive/'xwechat_files',redirected,direct,direct/'db_storage']):found=discover_accounts()
  self.assertEqual({a,b,direct},{x.root for x in found});self.assertEqual(3,len(found))
 def test_default_explicit_normalization_and_no_recursive_walk(self):
  home=self.root/'profile';a=self.account(home/'Documents/xwechat_files/user_example');db=a/'db_storage/message/example.db';db.write_bytes(b'synthetic-not-opened')
  for path in (home,home/'Documents',home/'Documents/xwechat_files',a,a/'db_storage',a/'db_storage/message',db):self.assertEqual([a],[x.root for x in discover_accounts(path)])
  self.account(home/'unrelated/deeper/xwechat_files/user_hidden')
  with patch('siwx_safe.live.Path.home',return_value=home),patch('siwx_safe.live.sys.platform','darwin'):self.assertEqual([a],[x.root for x in discover_accounts()])
  self.assertEqual([], _accounts_at('https://example.invalid/data'));self.assertEqual([], _accounts_at('//example.invalid/share'))
 def test_container_and_refresh_stable_id_open_source(self):
  a=self.account(self.root/'WeChat Files/xwechat_files/user_example');b=self.account(self.root/'new/xwechat_files/user_second');self.assertEqual([a],[x.root for x in discover_accounts(self.root)])
  engine=Engine(discover_accounts(a),discoverer=lambda:discover_accounts(b)+discover_accounts(a));marker=object();engine.source=marker;engine.account_index=0;result=engine.refresh_accounts();self.assertEqual(1,result['added']);self.assertIs(marker,engine.source);self.assertEqual(0,engine.account_index);self.assertEqual([0,1],[x['id'] for x in result['accounts']]);self.assertEqual(0,engine.refresh_accounts()['added']);self.assertNotIn(str(self.root),json.dumps(list(engine.events)))
 def test_symlink_account_and_registry_settings(self):
  a=self.account(self.root/'target/user_example');links=self.root/'xwechat_files';links.mkdir();(links/'linked').symlink_to(a,target_is_directory=True);self.assertEqual([], _accounts_at(links))
  class Key:
   def __init__(self,name):self.name=name
   def __enter__(self):return self
   def __exit__(self,*_):pass
  registry=SimpleNamespace(HKEY_CURRENT_USER=1,OpenKey=lambda _,name:Key(name),QueryValueEx=lambda key,value:(str(a) if value=='FileSavePath' else str(self.root/'redirected'),1))
  import ctypes
  fake=SimpleNamespace(kernel32=SimpleNamespace(GetLogicalDrives=lambda:8,GetDriveTypeW=lambda drive:3))
  with patch.dict('sys.modules',{'winreg':registry}),patch.object(ctypes,'windll',fake,create=True):roots=_windows_roots()
  self.assertIn(a,roots);self.assertTrue(any(str(x).startswith('D:') for x in roots))
