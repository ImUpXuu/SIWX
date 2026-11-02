"""Unreachable Windows drive/Users fault containment, without accessing real drives."""
from pathlib import Path
import os,sys,tempfile,unittest
from types import SimpleNamespace
from unittest.mock import patch
R=Path(__file__).resolve().parents[1];sys.path[:0]=[str(R),str(R/'_build')]
from siwx import discover

class DiscoveryFaultTests(unittest.TestCase):
 def test_each_oserror_candidate_is_skipped_without_leaking_credentials(self):
  for number in [1326,5,21,53]:
   path=unittest.mock.Mock();path.is_dir.side_effect=OSError(number,'synthetic credential-sensitive detail')
   with patch.object(discover._slog,'detailed') as log:
    self.assertFalse(discover._is_accessible_dir(path));self.assertNotIn('credential-sensitive',str(log.call_args))
  path=unittest.mock.Mock();path.is_dir.return_value=True;self.assertTrue(discover._is_accessible_dir(path))
 def test_bad_drive_and_users_keep_local_and_manual_accounts_and_status200(self):
  # Simulate both root-Z authentication failure and C/Users permission error.
  # Explicit platform patch targets only discover; no native Windows calls occur.
  with tempfile.TemporaryDirectory() as tmp:
   home=Path(tmp).resolve();db=home/'Documents/xwechat_files/wxid_demo_a/db_storage';db.mkdir(parents=True);manual=home/'manual/wxid_demo_b/db_storage';manual.mkdir(parents=True)
   real=Path.is_dir;seen=[]
   def is_dir(path):
    text=str(path).replace('\\','/').replace('//','/').rstrip('/')
    if text=='Z:':seen.append('drive');raise OSError(1326,'synthetic unavailable mapping')
    if text=='C:':return True
    if text=='C:/Users':seen.append('users');raise PermissionError(5,'synthetic inaccessible users')
    if len(text)==2 and text[1]==':':return False
    return real(path)
   try:
    with patch.dict(os.environ,{'USERPROFILE':str(home),'SIWX_ROOT':str(home),'SIWX_DATA_DIR':str(home/'data'),'SIWX_NO_PLUGINS':'1'}),patch.object(discover,'platform',SimpleNamespace(system=lambda:'Windows')),patch.object(Path,'is_dir',is_dir),patch.object(discover,'load_manual_data_dirs',return_value=[('wxid_demo_b',str(manual))]):
     found=discover.find_wechat_data_dirs();self.assertEqual({'wxid_demo_a','wxid_demo_b'},{name for name,_ in found});self.assertIn('drive',seen);self.assertIn('users',seen)
     from siwx import server
     with patch.object(server,'find_wechat_pids',return_value=[]),patch.object(server,'load_manual_data_dirs',return_value=[]),patch.object(server.keystore,'load',return_value={}):
      from flask import Flask
      status_app=Flask('synthetic_discovery');status_app.add_url_rule('/api/status',view_func=server.status)
      server._invalidate_status_scan();response=status_app.test_client().get('/api/status');self.assertEqual(200,response.status_code);self.assertEqual({'wxid_demo_a','wxid_demo_b'},{a['wxid'] for a in response.json['accounts']});server._invalidate_status_scan()
   finally:
    # Native test process owns any newly imported logger/faulthandler handles.
    import logging,faulthandler
    from siwx import logging_setup
    faulthandler.disable()
    if logging_setup._CRASH_FH is not None:logging_setup._CRASH_FH.close();logging_setup._CRASH_FH=None
    for handler in list(logging.getLogger('siwx').handlers):handler.close();logging.getLogger('siwx').removeHandler(handler)
