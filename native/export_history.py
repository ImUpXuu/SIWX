"""Read-only persistent export inventory; no database/user-message content reads."""
from collections import deque
from datetime import datetime
import os
from pathlib import Path,PurePosixPath,PureWindowsPath
import stat
from urllib.parse import urlencode
from flask import Blueprint,abort,jsonify,request,send_file
from siwx import paths
from compat import export_sources,plain

bp=Blueprint('native_export_history',__name__,url_prefix='/api/native/exports')
FORMATS={'.zip','.json','.html','.htm','.txt','.md','.csv','.xlsx','.pdf','.docx','.toml','.sqlite','.db'}
MAX_ENTRIES=50000
MAX_DEPTH=5

def linked(path):
 return path.is_symlink() or bool(getattr(path,'is_junction',lambda:False)())

def export_file(relative,source="native"):
 if not isinstance(relative,str) or not relative or len(relative)>4096 or '\\' in relative or ':' in relative or '\x00' in relative:abort(404)
 parts=relative.split('/')
 if any(p in ('','.','..') for p in parts) or PurePosixPath(relative).is_absolute() or PureWindowsPath(relative).is_absolute():abort(404)
 roots={key:root for key,label,root in export_sources()}
 root=roots.get(source)
 if root is None or not plain(root):abort(404)
 root=root.resolve();target=root
 for part in parts:
  target=target/part
  if linked(target):abort(404)
 try:
  resolved=target.resolve(strict=True)
  if not resolved.is_relative_to(root) or not stat.S_ISREG(resolved.stat().st_mode) or resolved.suffix.lower() not in FORMATS or resolved.name=='manifest.json':abort(404)
 except (OSError,ValueError):abort(404)
 return resolved

@bp.get('')
def inventory():
 try:offset=int(request.args.get('offset','0'));limit=int(request.args.get('limit','50'))
 except ValueError:return jsonify(error='页码或每页数量无效。'),400
 if not 0<=offset<=MAX_ENTRIES or not 1<=limit<=100:return jsonify(error='页码或每页数量超出范围。'),400
 query=request.args.get('q','').strip().casefold()
 if len(query)>200:return jsonify(error='搜索关键词过长。'),400
 rows=[];visited=0;truncated=False
 for source,label,root in export_sources():
  if not plain(root) or not root.is_dir():continue
  root=root.resolve();queue=deque([(root,0)])
  while queue:
   directory,depth=queue.popleft()
   try:
    with os.scandir(directory) as entries:
     for entry in entries:
      visited+=1
      if visited>MAX_ENTRIES:truncated=True;break
      path=Path(entry.path)
      if entry.is_symlink() or linked(path):continue
      if entry.is_dir(follow_symlinks=False):
       if depth<MAX_DEPTH and entry.name.casefold() not in {'media','assets','images','avatars','voice','video','stickers','_media'}:queue.append((path,depth+1))
       elif depth>=MAX_DEPTH:truncated=True
       continue
      if not entry.is_file(follow_symlinks=False) or path.suffix.lower() not in FORMATS or path.name=='manifest.json':continue
      relative=path.relative_to(root).as_posix()
      if query and query not in relative.casefold() and query not in label.casefold():continue
      info=entry.stat(follow_symlinks=False)
      rows.append({'name':path.name,'source':source,'source_label':label,'directory':str(path.parent),'path':relative,'batch':relative.split('/')[0] if '/' in relative else '直接导出文件','format':path.suffix[1:].upper(),'bytes':info.st_size,'modified':datetime.fromtimestamp(info.st_mtime).isoformat(timespec='seconds'),'modified_ns':info.st_mtime_ns,'download':'/api/native/exports/download?'+urlencode({'path':relative,'source':source})})
   except OSError:continue
   if visited>MAX_ENTRIES:break
  if visited>MAX_ENTRIES:break
 rows.sort(key=lambda row:(-row['modified_ns'],row['path']))
 for row in rows:row.pop('modified_ns')
 return jsonify(files=rows[offset:offset+limit],offset=offset,limit=limit,total=len(rows),has_more=offset+limit<len(rows),truncated=truncated)

@bp.get('/download')
def download():
 return send_file(export_file(request.args.get('path'),request.args.get('source','native')),as_attachment=True)
