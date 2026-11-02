"""Original message/session/SNS SQLite fixtures; no adapter or real user files."""
from pathlib import Path
from contextlib import closing
import hashlib,sqlite3
ACCOUNT='wxid_demo_a';CHAT='wxid_demo_b'

def make_fixture(root):
 acc=Path(root)/'output'/ACCOUNT
 table='Msg_'+hashlib.md5(CHAT.encode()).hexdigest()
 db=acc/'message/message_0.db';db.parent.mkdir(parents=True)
 with closing(sqlite3.connect(db)) as conn, conn:
  conn.execute(f'CREATE TABLE [{table}](local_id INTEGER PRIMARY KEY,server_id INTEGER,local_type INTEGER,create_time INTEGER,origin_source INTEGER,real_sender_id INTEGER,message_content BLOB,packed_info_data BLOB)')
  conn.execute('CREATE TABLE Name2Id(user_name TEXT)');conn.execute('INSERT INTO Name2Id VALUES(?)',(CHAT,))
  for i in range(180):conn.execute(f'INSERT INTO [{table}] VALUES(?,?,?,?,?,?,?,?)',(i+1,1000+i,1,1700000000+i,0,1,('原生合成消息 '+str(i)).encode(),None))
 db=acc/'session/session.db';db.parent.mkdir()
 with closing(sqlite3.connect(db)) as conn, conn:
  conn.execute('CREATE TABLE SessionTable(username TEXT,summary TEXT,sort_timestamp INTEGER)');conn.execute('INSERT INTO SessionTable VALUES(?,?,?)',(CHAT,'原生合成消息 179',1700000179))
 db=acc/'contact/contact.db';db.parent.mkdir()
 with closing(sqlite3.connect(db)) as conn, conn:
  conn.execute('CREATE TABLE contact(username TEXT,remark TEXT,nick_name TEXT,alias TEXT)');conn.execute('INSERT INTO contact VALUES(?,?,?,?)',(CHAT,'合成联系人','合成联系人',''))
 db=acc/'sns/sns.db';db.parent.mkdir()
 with closing(sqlite3.connect(db)) as conn, conn:
  conn.execute('CREATE TABLE SnsTimeLine(tid INTEGER PRIMARY KEY DESC,user_name TEXT,content TEXT,pack_info_buf TEXT)')
  xml='<SnsDataItem><TimelineObject><id>123</id><username>'+CHAT+'</username><createTime>1700000000</createTime><contentDesc>原生合成朋友圈</contentDesc><ContentObject><type>1</type><mediaList/></ContentObject></TimelineObject></SnsDataItem>'
  conn.execute('INSERT INTO SnsTimeLine VALUES(?,?,?,?)',(123,CHAT,xml,''))
 return acc
