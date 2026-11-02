"""Same-user original-edition compatibility; original files are never replaced/deleted."""
from collections import deque
from contextlib import closing
import hashlib,json,os,shutil,sqlite3,tempfile,threading,time
from pathlib import Path

STATE={'running':False,'copied':0,'existing':0,'failed':0,'keys_added':0,'paths_added':0}
_LOCK=threading.Lock()

def linked(path):
 return path.is_symlink() or bool(getattr(path,'is_junction',lambda:False)())

def plain(path):
 path=Path(path)
 if not path.is_absolute() or str(path).startswith(('\\\\','//')):return False
 return not any(linked(p) for p in [path,*path.parents])

def original_roots():
 # Explicit isolated native homes do not touch any real host data by default.
 explicit=os.environ.get('SIWX_COMPAT_ROOTS')
 if explicit is not None:
  try:values=json.loads(explicit)
  except ValueError:return []
  if not isinstance(values,list):return []
 elif os.environ.get('SIWX_NATIVE_HOME'):return []
 else:
  local=Path(os.environ.get('LOCALAPPDATA',Path.home()))
  values=[local/'stories-in-wx-console',local/'stories-in-wx']
  if os.environ.get('USERPROFILE'):values.append(Path(os.environ['USERPROFILE'])/'stories-in-wx')
  if os.environ.get('SystemRoot'):values.append(Path(os.environ['SystemRoot'])/'SIWX')
 roots=[];seen=set()
 for value in values:
  if not isinstance(value,(str,Path)):continue
  p=Path(value)
  try:
   if not plain(p) or not p.is_dir():continue
   p=p.resolve();key=str(p).casefold()
   if key not in seen:seen.add(key);roots.append(p)
  except OSError:continue
 return roots

def source_id(root):
 return "original-"+hashlib.sha256(str(root.resolve()).casefold().encode("utf-8")).hexdigest()[:16]

def export_sources():
 from siwx import paths
 sources=[('native','修改版',paths.exports_root())]
 for index,root in enumerate(original_roots()):
  path=root/'exports'
  try:
   if plain(path) and path.is_dir() and path.resolve()!=sources[0][2].resolve():sources.append((source_id(root),'原版',path))
  except OSError:continue
 return sources

def atomic_json(path,data):
 path.parent.mkdir(parents=True,exist_ok=True)
 if not plain(path):raise OSError('Linked destination refused')
 fd,tmp=tempfile.mkstemp(dir=path.parent,prefix='.compat-',suffix='.tmp')
 try:
  with os.fdopen(fd,'w',encoding='utf-8') as stream:json.dump(data,stream,ensure_ascii=False)
  os.replace(tmp,path)
 finally:Path(tmp).unlink(missing_ok=True)

def copy_missing(source,target,timeout=120):
 """Exclusive publish: even a concurrently created target is never overwritten."""
 if not plain(source) or not plain(target):raise OSError('Linked path refused')
 if target.exists():return None
 target.parent.mkdir(parents=True,exist_ok=True)
 if not plain(target):raise OSError('Linked destination refused')
 if shutil.disk_usage(target.parent).free<source.stat().st_size+64*1024*1024:raise OSError('Insufficient free space')
 fd,tmp=tempfile.mkstemp(dir=target.parent,prefix='.compat-',suffix='.tmp');os.close(fd);tmp=Path(tmp)
 try:
  if source.suffix.lower()=='.db':
   deadline=time.monotonic()+timeout
   def progress(*_):
    if time.monotonic()>deadline:raise TimeoutError('SQLite snapshot timeout')
   with closing(sqlite3.connect(source.as_uri()+'?mode=ro',uri=True,timeout=1)) as src,closing(sqlite3.connect(tmp)) as dst:
    src.backup(dst,pages=256,progress=progress,sleep=.03)
    if dst.execute('PRAGMA quick_check').fetchone()[0]!='ok':raise ValueError('Invalid SQLite snapshot')
  else:shutil.copyfile(source,tmp)
  with tmp.open('rb') as stream:digest=hashlib.file_digest(stream,'sha256').hexdigest()
  # Hard-link publish is atomic and fails if the target already exists (Windows NTFS included).
  try:os.link(tmp,target)
  except FileExistsError:return None
  return digest
 finally:tmp.unlink(missing_ok=True)

def merge_configuration(home,roots):
 from siwx import keystore
 store_path=home/'data/keystore.bin'
 try:
  store=json.loads(keystore._unprotect(store_path.read_bytes()).decode()) if store_path.is_file() else {}
  if not isinstance(store,dict):raise ValueError('Invalid current store')
  added=0
  for root in roots:
   for path in [root/'keystore.bin',root/'data/keystore.bin']:
    if path==store_path or not path.is_file() or not plain(path):continue
    try:
     old=json.loads(keystore._unprotect(path.read_bytes()).decode())
     if not isinstance(old,dict):continue
     for salt,record in old.items():
      if salt not in store and isinstance(record,dict) and isinstance(record.get('key'),str):store[salt]=record;added+=1
    except (OSError,ValueError):STATE['failed']+=1
  if added:keystore.save(store);STATE['keys_added']=added
 except (OSError,ValueError):STATE['failed']+=1
 path=home/'data/wechat_paths.json'
 try:
  current=json.loads(path.read_text(encoding='utf-8')) if path.is_file() else {'paths':[]}
  if isinstance(current,list):current={'paths':current}
  if not isinstance(current,dict) or not isinstance(current.get('paths'),list):raise ValueError('Invalid current configuration')
  seen={str(p).casefold() for p in current['paths']};added=0
  for root in roots:
   for oldpath in [root/'wechat_paths.json',root/'data/wechat_paths.json']:
    if oldpath==path or not oldpath.is_file() or not plain(oldpath):continue
    try:
     raw=json.loads(oldpath.read_text(encoding='utf-8'));values=raw if isinstance(raw,list) else raw.get('paths',[])
     if not isinstance(values,list):continue
     for value in values:
      if isinstance(value,str) and value.casefold() not in seen:current['paths'].append(value);seen.add(value.casefold());added+=1
    except (OSError,ValueError,AttributeError):STATE['failed']+=1
  if added:atomic_json(path,current);STATE['paths_added']=added
 except (OSError,ValueError):STATE['failed']+=1

def import_outputs(home,roots):
 journal=home/'data/compat-imports.json'
 try:
  records=json.loads(journal.read_text(encoding='utf-8')) if journal.is_file() else []
  if not isinstance(records,list):records=[]
  for index,root in enumerate(roots):
   source=root/'output';target=home/'output'
   if not plain(source) or not source.is_dir() or source.resolve()==target.resolve():continue
   queue=deque([source])
   while queue:
    folder=queue.popleft()
    try:
     for path in folder.iterdir():
      if not plain(path):continue
      if path.is_dir():queue.append(path);continue
      if not path.is_file() or path.name.endswith(('-wal','-shm','.tmp')):continue
      destination=target/path.relative_to(source)
      if destination.exists():STATE['existing']+=1;continue
      try:
       digest=copy_missing(path,destination)
       if digest:
        records.append({'target':destination.relative_to(home).as_posix(),'source':source_id(root),'sha256':digest});STATE['copied']+=1
        if STATE['copied']%20==0:atomic_json(journal,records)
       else:STATE['existing']+=1
      except (OSError,ValueError,sqlite3.Error,TimeoutError):STATE['failed']+=1
    except OSError:STATE['failed']+=1
  atomic_json(journal,records)
 except (OSError,ValueError):STATE['failed']+=1
 finally:STATE['running']=False

def start(home):
 with _LOCK:
  roots=original_roots()
  if STATE['running'] or not roots:return
  STATE.update(running=True,copied=0,existing=0,failed=0,keys_added=0,paths_added=0)
  # Small configuration merge completes before accepting HTTP writes. Bulk DB copy is background.
  merge_configuration(home,roots)
  threading.Thread(target=import_outputs,args=(home,roots),daemon=True,name='original-data-compat').start()
