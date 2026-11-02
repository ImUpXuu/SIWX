"""Loopback-only launch contract; keep HTTP identity guards unchanged."""
from pathlib import Path
import contextlib,io,json,sys,unittest
from unittest.mock import patch
R=Path(__file__).resolve().parents[1];sys.path[:0]=[str(R),str(R/'_build')]
import app as entry

class LoopbackTests(unittest.TestCase):
 def test_default_probe_and_server_bind_loopback(self):
  out=io.StringIO()
  with contextlib.redirect_stdout(out):self.assertEqual(0,entry.main(['probe']))
  self.assertEqual('127.0.0.1',json.loads(out.getvalue())['host'])
  with patch.object(entry,'make_app'),patch('siwx.server.run_server') as serve:
   self.assertEqual(0,entry.main([]));serve.assert_called_once_with('127.0.0.1',8787,open_browser=False,trust_lan=True)
  with patch.object(entry,'make_app'),patch('siwx.server.run_server') as serve:
   self.assertEqual(0,entry.main(['--host','127.0.0.1','--port','8789']));self.assertEqual(('127.0.0.1',8789),serve.call_args.args)
 def test_external_bind_rejected_before_opening_data_or_starting_server(self):
  for host in ['0.0.0.0','192.0.2.1','::','localhost']:
   with patch.object(entry,'make_app') as make,contextlib.redirect_stderr(io.StringIO()):
    with self.assertRaises(SystemExit) as raised:entry.main(['--host',host])
    self.assertEqual(2,raised.exception.code);make.assert_not_called()
