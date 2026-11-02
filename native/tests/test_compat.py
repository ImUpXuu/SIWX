"""Two edition data compatibility, synthetic-only and non-overwrite contracts."""
from pathlib import Path
from contextlib import closing
import hashlib,json,os,sqlite3,sys,tempfile,unittest
from unittest.mock import patch
R=Path(__file__).resolve().parents[1];sys.path[:0]=[str(R),str(R/'_build')]
import compat

class CompatibilityTests(unittest.TestCase):
 def setUp(self):
  self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup);self.base=Path(self.temp.name).resolve();self.home=self.base/'native';self.old=self.base/'original';self.home.mkdir();self.old.mkdir()
  self.env=patch.dict(os.environ,SIWX_NATIVE_HOME=str(self.home),SIWX_COMPAT_ROOTS=json.dumps([str(self.old)]),SIWX_ROOT=str(self.home),SIWX_DATA_DIR=str(self.home/'data'));self.env.start();self.addCleanup(self.env.stop)
  compat.STATE.update(running=False,copied=0,existing=0,failed=0,keys_added=0,paths_added=0)
 def test_sqlite_snapshot_with_wal_and_existing_native_wins(self):
  source=self.old/'output/wxid_example_01/message/message_0.db';source.parent.mkdir(parents=True)
  with closing(sqlite3.connect(source)) as conn:
   conn.execute('PRAGMA journal_mode=WAL');conn.execute('CREATE TABLE msgs(content TEXT)');conn.execute('INSERT INTO msgs VALUES(?)',('synthetic WAL record',));conn.commit()
   original_hash=hashlib.sha256(source.read_bytes()).hexdigest();compat.import_outputs(self.home,[self.old]);target=self.home/source.relative_to(self.old)
   with closing(sqlite3.connect(target)) as copy:self.assertEqual('synthetic WAL record',copy.execute('SELECT content FROM msgs').fetchone()[0])
   self.assertEqual(original_hash,hashlib.sha256(source.read_bytes()).hexdigest())
   conn.execute('INSERT INTO msgs VALUES(?)',('later original record',));conn.commit();compat.import_outputs(self.home,[self.old])
   with closing(sqlite3.connect(target)) as copy:self.assertEqual(1,copy.execute('SELECT count(*) FROM msgs').fetchone()[0])
  self.assertEqual(1,compat.STATE['copied']);self.assertGreaterEqual(compat.STATE['existing'],1);self.assertEqual(0,compat.STATE['failed'])
  journal=json.loads((self.home/'data/compat-imports.json').read_text());self.assertEqual('output/wxid_example_01/message/message_0.db',journal[0]['target'])
 def test_configuration_merges_missing_without_modifying_original(self):
  from siwx import keystore
  (self.old/'data').mkdir();(self.home/'data').mkdir()
  native={'a'*32:{'key':'b'*64,'strategy':'synthetic-native'}};old={'a'*32:{'key':'c'*64,'strategy':'synthetic-old'},'d'*32:{'key':'e'*64,'strategy':'synthetic-old'}}
  keystore.save(native);oldkey=self.old/'data/keystore.bin';oldkey.write_bytes(keystore._protect(json.dumps(old).encode()));before=oldkey.read_bytes()
  (self.home/'data/wechat_paths.json').write_text(json.dumps({'paths':['synthetic-native']}));oldpaths=self.old/'data/wechat_paths.json';oldpaths.write_text(json.dumps({'paths':['synthetic-old','synthetic-native']}));paths_before=oldpaths.read_bytes()
  compat.merge_configuration(self.home,[self.old]);store=keystore.load();self.assertEqual(native['a'*32],store['a'*32]);self.assertEqual(old['d'*32],store['d'*32]);self.assertEqual(before,oldkey.read_bytes());self.assertEqual(paths_before,oldpaths.read_bytes());self.assertEqual(['synthetic-native','synthetic-old'],json.loads((self.home/'data/wechat_paths.json').read_text())['paths'])
 def test_original_and_native_exports_same_name_are_separate_and_bounded(self):
  from flask import Flask
  import export_history
  for root,text in [(self.home,'synthetic native'),(self.old,'synthetic original')]:
   (root/'exports').mkdir();(root/'exports/same.txt').write_text(text)
  app=Flask('compat_synthetic');app.register_blueprint(export_history.bp);client=app.test_client();rows=client.get('/api/native/exports').json['files'];self.assertEqual(2,len(rows));self.assertEqual({'native','original-0'},{r['source'] for r in rows})
  for row in rows:
   response=client.get(row['download']);self.assertEqual(200,response.status_code);self.assertIn('original' if row['source']=='original-0' else 'native',response.data.decode());response.close()
  self.assertEqual(404,client.get('/api/native/exports/download?path=same.txt&source=unknown').status_code)
  self.assertEqual(404,client.get('/api/native/exports/download?path=../same.txt&source=original-0').status_code)
  with patch.dict(os.environ,SIWX_COMPAT_ROOTS='[]'):self.assertEqual([],compat.original_roots())
 def test_linked_original_or_target_never_copied(self):
  source=self.old/'output/wxid_example_01';source.mkdir(parents=True);outside=self.base/'outside';outside.mkdir();(outside/'record.txt').write_text('synthetic')
  try:(source/'escape').symlink_to(outside,target_is_directory=True)
  except OSError:self.skipTest('Symlink unavailable')
  compat.import_outputs(self.home,[self.old]);self.assertFalse((self.home/'output/wxid_example_01/escape').exists())
