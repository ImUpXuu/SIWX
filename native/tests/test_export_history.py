"""Durable actual export file inventory/restart/download/path-boundary regressions."""
from pathlib import Path
import json,sys,tempfile,unittest,zipfile
from unittest.mock import patch
R=Path(__file__).resolve().parents[1];sys.path[:0]=[str(R),str(R/'_build')]
from flask import Flask
import export_history as history

class HistoryTests(unittest.TestCase):
 def setUp(self):
  self.temp=tempfile.TemporaryDirectory();self.root=Path(self.temp.name).resolve()/'exports';self.root.mkdir();self.mock=patch.object(history.paths,'exports_root',return_value=self.root);self.mock.start();self.addCleanup(self.mock.stop);self.addCleanup(self.temp.cleanup)
  self.app=Flask('synthetic_history');self.app.register_blueprint(history.bp);self.client=self.app.test_client()
 def test_disk_files_survive_fresh_app_and_download_with_pagination(self):
  batch=self.root/'合成导出';batch.mkdir();(batch/'manifest.json').write_text('{}');(batch/'record.json').write_text(json.dumps({'messages':[{'content':'合成导出文本'}]},ensure_ascii=False));(batch/'record.html').write_text('<html>合成导出文本</html>')
  with zipfile.ZipFile(self.root/'合成导出.zip','w') as z:z.write(batch/'record.json','record.json')
  for i in range(35):(batch/f'part-{i}.txt').write_text('合成')
  rows=self.client.get('/api/native/exports?limit=30').json;self.assertEqual(38,rows['total']);self.assertEqual(30,len(rows['files']));self.assertTrue(rows['has_more']);self.assertEqual(8,len(self.client.get('/api/native/exports?offset=30&limit=30').json['files']))
  fresh=Flask('synthetic_restart');fresh.register_blueprint(history.bp);client=fresh.test_client();self.assertEqual(38,client.get('/api/native/exports').json['total'])
  result=client.get('/api/native/exports?q=record.json').json;self.assertEqual(1,result['total']);response=client.get(result['files'][0]['download']);self.assertEqual(200,response.status_code);self.assertIn('attachment',response.headers['Content-Disposition']);self.assertIn('合成导出文本',response.data.decode());response.close()
  (batch/'record.json').unlink();self.assertEqual(0,client.get('/api/native/exports?q=record.json').json['total'])
 def test_download_blocks_traversal_manifest_and_links(self):
  (self.root/'record.txt').write_text('synthetic');(self.root/'manifest.json').write_text('{}');outside=self.root.parent/'outside.txt';outside.write_text('private synthetic')
  for relative in ['../outside.txt','/outside.txt','C:/outside.txt','record.txt:stream','a/../record.txt','manifest.json','a\\record.txt','missing.txt']:
   response=self.client.get('/api/native/exports/download',query_string={'path':relative});self.assertEqual(404,response.status_code,relative)
  try:(self.root/'linked.txt').symlink_to(outside)
  except OSError:pass
  else:
   self.assertEqual(1,self.client.get('/api/native/exports').json['total']);self.assertEqual(404,self.client.get('/api/native/exports/download?path=linked.txt').status_code)
  for query in ['?offset=-1','?offset=bad','?limit=0','?limit=101']:self.assertEqual(400,self.client.get('/api/native/exports'+query).status_code)
 def test_directory_links_and_attachments_not_crawled(self):
  batch=self.root/'batch';batch.mkdir();(batch/'record.csv').write_text('content\nsynthetic')
  media=batch/'media';media.mkdir();(media/'not-export.json').write_text('{}')
  outside=self.root.parent/'outside';outside.mkdir();(outside/'never.txt').write_text('synthetic')
  try:(batch/'escape').symlink_to(outside,target_is_directory=True)
  except OSError:pass
  result=self.client.get('/api/native/exports').json;self.assertEqual(['batch/record.csv'],[r['path'] for r in result['files']]);self.assertEqual(404,self.client.get('/api/native/exports/download?path=batch/escape/never.txt').status_code)
