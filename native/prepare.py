"""Generate a minimal subtractive edition of the checked-out upstream SIWX.
Original siwx/ remains byte-for-byte untouched. Generated files are disposable,
never user data. Every deletion/edit is recorded with input/output SHA-256.
"""
from pathlib import Path
import ast, hashlib, json, re, shutil, subprocess
ROOT=Path(__file__).resolve().parent
SOURCE=ROOT.parent/'siwx'
BUILD=ROOT/'_build'
REMOVED_MODULES={'api_stats','api_backup','api_mcp','api_update','mcp_server','backup','media_backup','auto_update'}

def sha(data):return hashlib.sha256(data).hexdigest()
def remove_nodes(text,predicate):
 lines=text.splitlines(keepends=True);spans=[]
 for node in ast.parse(text).body:
  if predicate(node):
   start=min([node.lineno]+[d.lineno for d in getattr(node,'decorator_list',[])])-1
   spans.append((start,node.end_lineno))
 for start,end in sorted(spans,reverse=True):del lines[start:end]
 return ''.join(lines)

def once(text,old,new):
 if text.count(old)!=1:raise ValueError('Upstream changed; review exact removal: '+old[:60])
 return text.replace(old,new,1)

def prepare():
 if BUILD.exists():
  assert (BUILD/'GENERATED').read_text()=='SIWX native generated copy\n'
  shutil.rmtree(BUILD)
 BUILD.mkdir();(BUILD/'GENERATED').write_text('SIWX native generated copy\n')
 shutil.copytree(SOURCE,BUILD/'siwx',ignore=shutil.ignore_patterns('__pycache__','*.pyc'))
 ui=BUILD/'siwx/ui'
 for name in REMOVED_MODULES:
  p=BUILD/'siwx'/name
  if p.is_dir():shutil.rmtree(p)
  p=p.with_suffix('.py')
  if p.exists():p.unlink()
 for name in ['stats','backup','mcp']:
  for p in (ui/'pages').glob(name+'.*'):p.unlink()
 p=BUILD/'siwx/server.py';s=p.read_text()
 s=remove_nodes(s,lambda n: isinstance(n,(ast.FunctionDef,ast.AsyncFunctionDef)) and n.name in {'_host_guard','_origin_guard'})
 for name in ['stats','backup','mcp','update']:
  s=re.sub(r'^from siwx\.api_'+name+r' import .+\n','',s,flags=re.M)
  s=once(s,'app.register_blueprint('+name+'_bp)\n','')
 # Media backup is a separate direct-route feature in the original server.
 a=s.index('# ── 全量媒体备份（');b=s.index('@app.post("/api/discover/validate")',a);s=s[:a]+s[b:]
 s=remove_nodes(s,lambda n:isinstance(n,ast.FunctionDef) and n.name=='_tail_mcp_log')
 s=once(s,'mcp_logs = _tail_mcp_log(500)','mcp_logs = []')
 p.write_text(s)
 p=BUILD/'siwx/api_chat.py';s=p.read_text();s=remove_nodes(s,lambda n:isinstance(n,ast.FunctionDef) and n.name=='conversation_stats');p.write_text(s)
 # Same-user scope is maintained; this is not an HTTP allowlist.
 p=BUILD/'siwx/discover.py';s=p.read_text();s=once(s,"def find_wechat_data_dirs():",'''def _is_accessible_dir(path):
    try:
        return path.is_dir()
    except OSError:
        # An offline/mapped/inaccessible candidate must not abort all discovery.
        _slog.detailed("discover", "跳过不可访问的扫描目录，继续检测其他位置。", ring=False)
        return False


def find_wechat_data_dirs():''');s=once(s,"if drive.is_dir():","if _is_accessible_dir(drive):");s=once(s,"                if users_dir.is_dir():","                if _is_accessible_dir(users_dir):");s=once(s,'for proc in psutil.process_iter(["pid", "name", "memory_info"]):','owner = psutil.Process().username()\n    for proc in psutil.process_iter(["pid", "name", "memory_info", "username"]):');s=once(s,'if name in [n.lower() for n in target_names]:','if name in [n.lower() for n in target_names] and (proc.info.get("username") or "").casefold() == owner.casefold():');p.write_text(s)
 p=BUILD/'siwx/env_info.py';s=p.read_text();s=once(s,'from siwx.auto_update import is_frozen\n        frozen = bool(is_frozen())','import sys\n        frozen = bool(getattr(sys, "frozen", False))');p.write_text(s)
 p=ui/'index.html';s=p.read_text()
 for name in ['stats','backup','mcp']:s=re.sub(r'^.*<a href="#/'+name+r'".*\n','',s,flags=re.M)
 p.write_text(s)
 p=ui/'app.js';s=p.read_text();s=once(s,"['chat', 'sns', 'stats', 'export', 'backup', 'mcp', 'logs', 'settings']","['chat', 'sns', 'export', 'logs', 'settings']");s=once(s,'请检查后端服务是否正常；也可前往 GitHub 仓库（github.com/ImUpXuu/SIWX）阅读 README 中的完整免责声明。','请检查后端服务是否正常，再重新加载免责声明。');p.write_text(s)
 p=ui/'pages/chat.html';s=p.read_text();s=re.sub(r'^.*id="c-stats".*\n','',s,flags=re.M);s=s[:s.index('<div class="c-modal hidden" id="c-stats-modal"')].rstrip()+'\n';p.write_text(s)
 p=ui/'pages/chat.js';s=p.read_text();a=s.index("  el('c-stats').addEventListener");b=s.index("  el('c-timeline-trigger')",a);s=s[:a]+s[b:];s=once(s,"  el('c-stats').disabled = false;\n",'');a=s.index('/* ── 会话统计弹层');b=s.index('export function destroy()',a);s=s[:a]+s[b:];s=once(s,"  if (statsModalKeyHandler) {\n    document.removeEventListener('keydown', statsModalKeyHandler);\n    statsModalKeyHandler = null;\n  }\n",'');p.write_text(s)
 p=ui/'pages/settings.html';s=p.read_text().replace('<h2>版本与更新</h2>','<h2>版本</h2>');a=s.index('  <div class="card s-card">\n    <div class="card-head"><h2>全量媒体备份');b=s.index('  <div class="card s-card" id="s-plugins-card"',a);s=s[:a]+s[b:];s=re.sub(r'^.*<div class="a-row"><span>(?:开发者|博客|源码)</span>.*\n','',s,flags=re.M);s=once(s,'      <div class="a-row"><span>许可证</span>','      <div class="a-row"><span>对应源码</span><b><a href="/api/native/source" download>下载源码</a></b></div>\n      <div class="a-row"><span>许可证</span>');p.write_text(s)
 p=ui/'pages/settings.js';s=p.read_text();a=s.index('async function loadVersion()');b=s.index('/* ── 插件设置',a) if '/* ── 插件设置' in s[a:] else -1
 if b<0:b=s.index('const PLUGIN_TYPE_LABEL',a)
 s=s[:a]+'''async function loadVersion() {
  const cur = await fetchJSON('/api/native/version');
  el('s-version').innerHTML = `<div class="ov-row"><span>当前版本</span><span class="pill pill-green">${esc(cur.version)}</span></div>`;
  el('s-version-meta').textContent = 'SIWX 原生页面';
}

'''+s[b:]
 s=once(s,'  await initBackupCard();\n','');a=s.index('/* 全量媒体备份卡片');b=s.index('export function destroy()',a);s=s[:a]+s[b:]
 for line in ['  if (mbPoll) { clearInterval(mbPoll); mbPoll = null; }\n','  if (mbDrop) { mbDrop.destroy(); mbDrop = null; }\n']:s=once(s,line,'')
 p.write_text(s)
 # Keep legal terms and license; strip contact URL promotion only.
 p=ui/'pages/disclaimer.html';s=p.read_text().replace('GitHub Issues（github.com/ImUpXuu/SIWX/issues）或作者博客（upxuu.com）','原作者公开联系渠道').replace('github.com/ImUpXuu/SIWX/issues · 完整版见仓库 README「免责声明 · 法律声明」。','请通过原作者公开联系渠道提交。');p.write_text(s)
 for p in [ui/'index.html',ui/'app.js']:
  s=p.read_text().replace('2026102404','2026110201');p.write_text(s)
 # Add one isolated history widget; original export.js/html remain exact.
 extra=ui/'export-history.js';shutil.copyfile(ROOT/'ui/export-history.js',extra)
 p=ui/'index.html';s=p.read_text();s=once(s,'</body>','<script src="/export-history.js?v=2026110202"></script>\n</body>');p.write_text(s)
 # Python sources compile, and original cryptography/extraction/export/SNS remain exact.
 changes={};files={}
 for p in sorted(SOURCE.rglob('*')):
  if not p.is_file() or '__pycache__' in p.parts or p.suffix=='.pyc':continue
  rel=p.relative_to(SOURCE).as_posix();q=BUILD/'siwx'/rel;before=p.read_bytes();after=q.read_bytes() if q.is_file() else None
  if after is not None:
   files[rel]=sha(after)
   if q.suffix=='.py':compile(after,str(q),'exec')
  if before!=after:changes[rel]={'source_sha256':sha(before),'edition_sha256':sha(after) if after is not None else None}
 for name in ['extract.py','sqlcipher.py','api_export.py','api_sns.py','sns.py','media.py','voice.py','ui/onboarding.js','ui/pages/export.js','ui/pages/sns.js']:
  assert (SOURCE/name).read_bytes()==(BUILD/'siwx'/name).read_bytes(),name
 files['ui/export-history.js']=sha(extra.read_bytes());changes['ui/export-history.js']={'source_sha256':None,'edition_sha256':files['ui/export-history.js']}
 try:source_ref=subprocess.check_output(['git','rev-parse','HEAD'],cwd=ROOT,stderr=subprocess.DEVNULL).decode().strip()
 except subprocess.CalledProcessError:source_ref=(ROOT.parent/'SOURCE-REVISION.txt').read_text().strip()
 manifest={'version':'1.7.1','base':'SIWX5.3.0','source_ref':source_ref,'changed':changes,'files':files,'removed':sorted(REMOVED_MODULES),'retained':'Original upstream chat/messages/media/export/SNS/setup/settings/logs and extract/decrypt/cache; no safe/adapter imports'}
 (BUILD/'native-source-manifest.json').write_text(json.dumps(manifest,indent=2))
 return manifest
if __name__=='__main__':
 m=prepare();print('Generated original edition:',len(m['files']),'files;',len(m['changed']),'deletions/edits')
