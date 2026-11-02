import base64,io,json,socket,sqlite3,tempfile,unittest,zipfile
from pathlib import Path
from unittest.mock import patch
from siwx_safe.live import Engine
from siwx_safe.sns_live import Moments,_check_url
from siwx_safe.vendor import sns_cdn as cdn
from siwx_safe.vendor.wexport.model import Contact
PNG=base64.b64decode('iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+jXyQAAAAASUVORK5CYII=')
def signed_tid(ts,index=0):
 n=(ts*1000<<23)+index
 return n-(1<<64) if n>=1<<63 else n
class MomentSource:
 def __init__(self):
  self._conns={'sns':sqlite3.connect(':memory:',check_same_thread=False)};self._contacts={'author_example':Contact('author_example','合成发布者甲'),'friend_example':Contact('friend_example','合成好友乙')};self.load_errors=[];self._keys={};self.closed=False
  con=self._conns['sns'];con.execute('CREATE TABLE SnsTimeLine(tid INTEGER PRIMARY KEY,user_name TEXT,content TEXT)')
  for i in range(25):
   ts=1704067200+i*86400;tid=signed_tid(ts,i);author='author_example' if i%2==0 else 'friend_example'
   xml=f'<SnsDataItem><TimelineObject><id>{i}</id><username>{author}</username><createTime>{ts}</createTime><contentDesc>合成动态 {i} &lt;script&gt;不可执行&lt;/script&gt;</contentDesc><ContentObject><type>2</type><mediaList><media><id>{i}</id><type>2</type><url key="12345" token="synthetic-media-token">https://mmbiz.qpic.cn/example/{i}/0</url><size width="100" height="100"/></media></mediaList></ContentObject></TimelineObject><LocalExtraInfo><user_comment><username>friend_example</username><nickname>合成好友乙</nickname><content>合成评论</content><type>2</type></user_comment><user_comment><username>friend_example</username><nickname>合成好友乙</nickname></user_comment></LocalExtraInfo></SnsDataItem>'
   con.execute('INSERT INTO SnsTimeLine VALUES(?,?,?)',(tid,author,xml))
  con.commit()
 def conversations(self):return []
 def close(self):
  for c in self._conns.values():c.close()
  self._conns.clear();self.closed=True
class MomentTests(unittest.TestCase):
 def setUp(self):
  self.source=MomentSource();self.sns=Moments(self.source);self.addCleanup(self.source.close)
 def test_paginated_precision_filters_comments_and_no_keys(self):
  page=self.sns.timeline({'limit':20});self.assertEqual(20,len(page['timeline']));self.assertTrue(page['has_more']);last=self.sns.timeline({'before_tid':page['next_before_tid']});self.assertEqual(5,len(last['timeline']));self.assertFalse(last['has_more']);self.assertEqual(25,len({p['tid'] for p in page['timeline']+last['timeline']}))
  post=page['timeline'][0];self.assertIsInstance(post['tid'],str);self.assertEqual(1,len(post['comments']));self.assertEqual(1,len(post['likes']));self.assertEqual(post,self.sns.detail(post['tid']));raw=json.dumps(post);self.assertNotIn('synthetic-media-token',raw);self.assertNotIn('"key"',raw);self.assertNotIn('"enc_key"',raw);self.assertIn('/_siwx_media/',post['medias'][0]['url']);self.assertEqual(13,len(self.sns.timeline({'username':'author_example'})['timeline']));self.assertEqual(1,len(self.sns.timeline({'keyword':'合成动态 24'})['timeline']));self.assertEqual(2,len(self.sns.friends()));self.assertEqual('合成发布者甲',self.sns.friends()[0]['display'])
  for options in ({'before_tid':'bad'},{'before_tid':str(1<<70)},{'start':'2025-01-01','end':'2024-01-01'}):
   with self.assertRaises(ValueError):self.sns.timeline(options)
 def test_recorded_media_only_cache_and_release(self):
  url=self.sns.timeline({})['timeline'][0]['medias'][0]['url']
  with self.assertRaises(ValueError):self.sns.media('https://127.0.0.1/private')
  with patch.object(cdn,'fetch',return_value=(PNG,{})) as fetch:
   data,mime=self.sns.media(url);self.assertEqual(PNG,data);self.assertEqual('image/png',mime);self.sns.media(url);self.assertEqual(1,fetch.call_count)
  self.sns.close();self.assertFalse(self.sns.cache);self.assertFalse(self.sns.resources)
 def test_export_media_zip_and_passive_text(self):
  body,mime,name=self.sns.export({'format':'html','keyword':'合成动态 24'});self.assertNotIn(b'<script>',body);self.assertEqual('moments.html',name)
  with patch.object(cdn,'fetch',return_value=(PNG,{})):body,mime,name=self.sns.export({'format':'html','keyword':'合成动态 24','media':True})
  self.assertEqual('application/zip',mime)
  with zipfile.ZipFile(io.BytesIO(body)) as archive:
   self.assertEqual(PNG,archive.read('media/0001.png'));self.assertIn(b'src="media/0001.png"',archive.read('moments.html'));report=json.loads(archive.read('export-report.json'));self.assertEqual(1,report['downloaded_media']);self.assertEqual(0,report['unavailable_media'])
  markdown,_,_=self.sns.export({'format':'markdown','keyword':'合成动态 24'});self.assertNotIn(b'<script>',markdown);self.assertIn(b'\\<script\\>',markdown)
  with self.assertRaises(ValueError):self.sns.export({'format':'exe'})
 def test_dtd_invalid_rows_and_destination_guards(self):
  con=self.source._conns['sns'];con.execute('INSERT INTO SnsTimeLine VALUES(?,?,?)',(signed_tid(1709251200),'author_example','<!DOCTYPE foo [<!ENTITY x "bad">]><SnsDataItem>&x;</SnsDataItem>'));con.commit();self.assertEqual(26,self.sns.count());self.assertEqual(20,len(self.sns.timeline({})['timeline']))
  for url in ('file:///tmp/example','http://127.0.0.1/example','https://mmbiz.qpic.cn:9000/a','https://user:pass@mmbiz.qpic.cn/a','https://evil.invalid/a'):
   with self.assertRaises(ValueError):_check_url(url)
  answers=[(socket.AF_INET,socket.SOCK_STREAM,6,'',('127.0.0.1',443))]
  with patch('siwx_safe.sns_live.socket.getaddrinfo',return_value=answers):
   with self.assertRaises(ValueError):_check_url('https://mmbiz.qpic.cn/a')
 def test_explicit_directory_discovery_and_fixed_stage_logs(self):
  engine=Engine([])
  with tempfile.TemporaryDirectory() as temp:
   account=Path(temp).resolve()/'xwechat_files'/'user_example';(account/'db_storage').mkdir(parents=True);result=engine.add_account_directory(str(account.parent));self.assertEqual(1,result['added']);self.assertNotIn(temp,json.dumps(list(engine.events)))
   with self.assertRaises(ValueError):engine.add_account_directory(str(account))
  for bad in ('relative/path','//example.invalid/share','https://example.invalid/data'):
   with self.assertRaises(ValueError):engine.add_account_directory(bad)
  engine.opener=lambda _:(_ for _ in ()).throw(RuntimeError('synthetic-key private-path'))
  with self.assertRaises(RuntimeError):engine.open(0,True)
  self.assertNotIn('synthetic-key',json.dumps(list(engine.events)));self.assertNotIn('private-path',json.dumps(list(engine.events)))

class EncryptedMomentTests(unittest.TestCase):
 def test_original_database_decrypts_in_memory_without_source_write(self):
  import hashlib
  from siwx_safe.live import discover_accounts
  from siwx_safe.vendor.wexport.crypto.aes import AesCbc
  from siwx_safe.vendor.wexport.sources.win4 import Win4Source
  with tempfile.TemporaryDirectory() as temp:
   root=Path(temp);db=root/'db_storage/sns/sns.db';db.parent.mkdir(parents=True)
   source=MomentSource();plain=sqlite3.connect(db);source._conns['sns'].backup(plain);plain.close();source.close()
   header=bytearray(db.read_bytes());header[20]=80;db.write_bytes(header);plain=sqlite3.connect(db);plain.execute('VACUUM');plain.close()
   raw=db.read_bytes();key=bytes(range(32));salt=bytes(range(16));iv=bytes(16);aes=AesCbc(key);encrypted=bytearray()
   try:
    for offset in range(0,len(raw),4096):
     page=raw[offset:offset+4096];encrypted.extend((salt if offset==0 else b'')+aes.encrypt(iv,page[16 if offset==0 else 0:4096-80])+iv+bytes(64))
   finally:aes.close()
   db.write_bytes(encrypted);before=hashlib.sha256(db.read_bytes()).hexdigest();account=discover_accounts(root)[0]
   def opener(a):
    src=Win4Source(a,key);src.open();return src
   engine=Engine([account],opener)
   try:engine.open(0,True);self.assertEqual(25,engine.moments.count());self.assertEqual(20,len(engine.moments.timeline({})['timeline']))
   finally:engine.close()
   self.assertEqual(before,hashlib.sha256(db.read_bytes()).hexdigest());self.assertEqual([db],list(root.rglob('*.db')))
