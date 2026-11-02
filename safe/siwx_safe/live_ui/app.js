'use strict';
// Original SIWX page structure and design tokens; adapter for the retained APIs.
const $ = id => document.getElementById(id);
const state = {accounts: [], account: null, pendingAccount: null, opened: false, chats: [], chat: null,
  rows: [], offset: 0, total: 0, months: [], selecting: false, selected: new Set(), selectedRows: new Map(),
  exportChats: new Set(), exportScope: null, exportFilter: 'all', rosterFilter: 'all',
  busy: false, extracting: false, stopped: false, download: null, log: [], logPaused: false};
let messageFilter = {query: '', since: '', until: '', kind: ''};
let toastTimer;
let wizardScreen='intro', wizardActive=true, snsLoaded=false, snsMounted=false, logsPaused=false, logsRows=[], tourIndex=-1;
function node(tag, cls, text) { const n = document.createElement(tag); if (cls) n.className = cls; if (text !== undefined) n.textContent = text; return n; }
function toast(text) { $('toast').textContent = text; $('toast').hidden = false; clearTimeout(toastTimer); toastTimer = setTimeout(() => {$('toast').hidden = true;}, 5000); }
async function api(path, body, signal) {
  const res = await fetch(path, {method: body === undefined ? 'GET' : 'POST',
    headers: {'Content-Type': 'application/json'}, body: body === undefined ? undefined : JSON.stringify(body),
    credentials: 'omit', cache: 'no-store', signal});
  if (!res.ok) { let data; try {data = await res.json();} catch {data = {};} throw new Error(data.error || '请求未完成，请重试。'); }
  return res;
}
async function json(path, body) { return (await api(path, body)).json(); }
async function run(task) {
  if (state.busy || state.stopped) return;
  state.busy = true; $('view').setAttribute('aria-busy', 'true'); syncButtons();
  try { await task(); } catch (error) { if (error.name !== 'AbortError') { toast(error.message); if (!$('account-modal').hidden) $('ob-status').textContent = error.message; } }
  finally { state.busy = false; syncButtons(); }
}
function syncButtons() {
  document.querySelectorAll('button,input,select').forEach(el => {if(!el.closest('#sns-page'))el.disabled = state.busy || state.stopped;});
  if (state.busy || state.stopped) { if(state.extracting)$('ob-cancel').disabled=false; if (state.download) $('e-cancel').disabled = false; return; }
  for (const id of ['c-refresh','close-data']) $(id).disabled = !state.opened;
  for (const id of ['c-msg-search','c-msg-q','c-since','c-until','c-select']) $(id).disabled = !state.chat;
  $('c-export').disabled = !state.chat || !state.total || (state.selecting && !state.selected.size);
  $('c-copy-selected').disabled = !state.selected.size;
  $('c-prev').disabled = !state.chat || state.offset <= 0;
  $('c-next').disabled = !state.chat || state.offset + state.rows.length >= state.total;
  $('ob-to-extract').disabled=state.pendingAccount===null||!$('ob-consent').checked;
  $('ob-run').disabled=state.pendingAccount===null;
  $('ob-open').disabled = state.pendingAccount === null || !$('ob-consent').checked;
  $('e-run').disabled = !state.opened || !exportSelection().length;
  $('e-roster-btn').disabled = !state.opened;
  $('view').setAttribute('aria-busy', 'false');
}
function route() {
  const requested=location.hash.replace('#/','');
  const page=['chat','sns','export','logs','settings'].includes(requested)?requested:'chat';
  for(const name of ['chat','sns','export','logs','settings'])$(name+'-page').hidden=name!==page;
  document.querySelectorAll('#menu a').forEach(a=>{a.classList.toggle('active',a.dataset.page===page);a.setAttribute('aria-current',a.dataset.page===page?'page':'false');});
  if(page==='export')renderExport();
  if(page==='settings')renderSettings();
  if(page==='logs')refreshLogs();
  if(page==='sns')mountSns();
  else if(snsMounted){window.SnsUI.destroy();snsMounted=false;}
}
async function mountSns(){
  try{
    if(!snsLoaded){await new Promise((resolve,reject)=>{const script=node('script');script.src='/sns.js';script.onload=resolve;script.onerror=()=>reject(new Error('朋友圈页面加载失败'));document.head.append(script);});snsLoaded=true;}
    if($('sns-page').hidden)return;
    if(snsMounted)window.SnsUI.destroy();
    $('sns-mount').replaceChildren();snsMounted=true;await window.SnsUI.init($('sns-mount'));
  }catch(error){$('sns-mount').replaceChildren(node('div','empty',error.message));}
}

function renderAccount() {
  const account = state.accounts.find(a => a.id === state.account);
  $('acc-name').textContent = account ? account.name : (state.accounts.length ? '选择微信号' : '暂无账号');
  $('acc-wxid').textContent = state.opened ? '已提取' : '点击选择账号';
  $('acc-initial').textContent = account ? account.name.slice(0, 1) : '…';
  $('side-wx').textContent = state.stopped ? '程序已停止' : (state.opened ? '微信数据已打开' : '请选择账号并提取');
  const pop = $('acc-pop'); pop.replaceChildren(node('div', 'acc-pop-head', '切换微信号'));
  for (const a of state.accounts) {
    const row = node('button', 'acc-item' + (a.id === state.account ? ' on' : ''));
    row.type = 'button'; row.setAttribute('role', 'option'); row.setAttribute('aria-selected', String(a.id === state.account));
    row.append(node('span', 'acc-ava', a.name.slice(0, 1)), node('span', 'acc-meta', a.name));
    row.onclick = () => {pop.hidden = true; $('acc-card').setAttribute('aria-expanded', 'false'); showAccountModal(a.id);}; pop.append(row);
  }
  if (!state.accounts.length) pop.append(node('div', 'acc-pop-head', '未发现微信账号'));
}
function showAccountModal(index = state.account, full=false) {
  if (state.busy || state.stopped) return;
  wizardActive=full;
  state.pendingAccount = index ?? state.accounts[0]?.id ?? null;
  $('ob-consent').checked = false; $('ob-status').textContent = ''; $('account-modal').hidden = false;
  renderAccountChoices();showWizardScreen(full?'intro':'accounts');syncButtons();$('ob-close').focus();
}
function renderAccountChoices() {
  const list = $('ob-accounts'); list.replaceChildren(); $('ob-empty').hidden = !!state.accounts.length;
  for (const a of state.accounts) {
    const card = node('button', 'acc-box' + (a.id === state.pendingAccount ? ' selected' : ''));
    card.type = 'button'; card.setAttribute('aria-pressed', String(a.id === state.pendingAccount));
    card.append(node('b', '', a.name), node('span', 'dim', a.id === state.account && state.opened ? '当前账号' : '微信账号'));
    card.onclick = () => {state.pendingAccount = a.id; renderAccountChoices(); syncButtons();}; list.append(card);
  }
}
function clearData() {
  if(snsMounted){window.SnsUI.destroy();snsMounted=false;}
  state.opened = false; state.chats = []; state.chat = null; state.rows = []; state.total = 0;
  state.offset = 0; state.months = []; state.selected.clear(); state.selectedRows.clear(); state.selecting = false;
  state.exportChats.clear(); state.exportScope = null;
  $('chat-wrap').classList.remove('open', 'selecting');
  $('c-title').textContent = '微信'; $('c-sub').textContent = '选择一个会话查看聊天记录';
  $('c-page').textContent = ''; $('c-msgs').replaceChildren(node('div', 'c-tip', '选择账号并提取后查看聊天记录'));
  $('c-timeline-list').replaceChildren(node('div', 'tl-empty', '打开会话后可按月份跳转'));
  renderChats(); renderExport(); renderAccount(); renderSettings(); updateSelection();
}
async function waitExtraction() {
  state.extracting=true;$('ob-cancel').hidden=false;$('ob-cancel').textContent='停止本次提取';syncButtons();
  try {
    for (;;) {
      const res=await api('/api/open/progress',undefined,AbortSignal.timeout(15000));
      const progress=await res.json();
      $('ob-log').hidden=false;
      $('ob-log').textContent=progress.logs.join('\n')+'\n已用时 '+progress.elapsed+' 秒';
      $('ob-log').scrollTop=$('ob-log').scrollHeight;
      $('ob-status').textContent=progress.message;
      if(progress.state==='done')return progress.result;
      if(progress.state!=='running')throw new Error(progress.message);
      await new Promise(resolve=>setTimeout(resolve,700));
    }
  } catch(error) {
    if(error.name==='TimeoutError'||error.name==='TypeError')throw new Error('进度连接中断。后台可能仍在读取，请刷新页面查看当前阶段，避免重复启动。');
    throw error;
  } finally {state.extracting=false;$('ob-cancel').hidden=true;}
}
$('ob-cancel').onclick=async()=>{
  $('ob-cancel').disabled=true;
  try{await json('/api/open/cancel',{});$('ob-cancel').textContent='正在停止…';}
  catch(error){toast(error.message);$('ob-cancel').disabled=false;}
};
async function extract(index, resume=false) {
  clearData(); $('ob-status').textContent = '正在提取并读取，请保持微信登录…';
  if(!resume)await json('/api/open/start', {account: index, consent: true});
  const result = await waitExtraction();
  state.account = index; state.opened = true; state.chats = await json('/api/chats');$('toast').hidden=true;
  if(!wizardActive)$('account-modal').hidden=true;renderAccount();renderChats();renderExport();renderSettings();
  if(!wizardActive&&!$('sns-page').hidden)await mountSns();
  if (result.warnings) toast('部分数据库未能读取，当前展示可用记录。');
}
function matches(text, pattern) { if (!pattern) return true; try {return new RegExp(pattern, 'i').test(text);} catch {return text.toLowerCase().includes(pattern.toLowerCase());} }
function renderChats() {
  const list = $('c-sessions'); list.replaceChildren();
  const chats = state.chats.filter(c => matches(c.name, $('c-search').value));
  for (const chat of chats) {
    const row = node('div', 'sess' + (chat.id === state.chat ? ' sel' : '')); row.tabIndex = 0; row.setAttribute('role','button'); row.dataset.chat = chat.id;
    row.append(node('span', 'ava', chat.name.slice(0, 1)), node('b', 'name', chat.name),
      node('span', 'prev', chat.kind === 'group' ? '群聊' : chat.kind === 'official' ? '公众号' : '聊天记录'));
    if (chat.lastTime) row.append(node('span', 'tm', new Date(chat.lastTime * 1000).toLocaleDateString('zh-CN', {month:'2-digit', day:'2-digit'})));
    if (chat.count) row.append(node('span', 'tag', chat.count + ' 条'));
    row.onclick = () => run(async () => {state.chat = chat.id; messageFilter = readChatFilter(); state.selecting = false; state.selected.clear(); state.selectedRows.clear(); renderChats(); await loadMessages(0); $('chat-wrap').classList.add('open');});
    row.onkeydown = e => {if (e.key === 'Enter' || e.key === ' ') {e.preventDefault(); row.click();}}; list.append(row);
  }
  if (!chats.length) {const empty = node('div', 'c-empty', state.opened ? '没有匹配的会话' : '请选择账号并提取'); if (!state.opened) {const b=node('button','btn btn-primary','选择微信号'); b.onclick=()=>showAccountModal(); empty.append(b);} list.append(empty);}
}
function readChatFilter() {return {query:$('c-msg-q').value, since:$('c-since').value, until:$('c-until').value, kind:document.querySelector('#c-kind-chips .on')?.dataset.kind || ''};}
async function loadMessages(offset) {
  const data = await json('/api/messages', {chat:state.chat, ...messageFilter, offset});
  state.rows=data.messages; state.total=data.total; state.offset=data.offset; state.months=data.months;
  const chat=state.chats.find(c=>c.id===state.chat); $('c-title').textContent=chat?.name || '微信';
  $('c-sub').textContent=state.total ? state.total + ' 条聊天记录' : '没有匹配的聊天记录';
  $('c-page').textContent=state.total ? `${state.offset+1}–${state.offset+state.rows.length} / ${state.total}` : '0 条';
  const filtered=Object.values(messageFilter).some(Boolean); $('c-filter-exit').classList.toggle('hidden',!filtered);
  $('c-filter-info').classList.toggle('hidden',!filtered); $('c-filter-info').textContent=filtered ? '当前筛选结果 · '+state.total+' 条' : '';
  renderMessages(); renderTimeline(); updateSelection();
}
function renderMessages() {
  const list=$('c-msgs');list.replaceChildren();
  for(const m of state.rows){
    const row=node('article','m-row'+(m.isSender?' me':'')+(m.typeName==='system'?' sys':''));row.dataset.key=String(m.id);
    row.classList.toggle('selected',state.selected.has(m.id));row.setAttribute('aria-selected',String(state.selected.has(m.id)));
    const avatar=node('div','cell-ava'),body=node('div','cell-body');avatar.append(node('span','m-ava',m.senderDisplayName.slice(0,1)));
    const bubble=node('div','m-bubble',m.content);body.append(node('div','m-name',m.senderDisplayName),bubble,node('div','m-time',m.createTime));
    const copy=node('button','m-copy','复制');copy.type='button';copy.onclick=e=>{e.stopPropagation();copyText(m.content);};
    row.append(avatar,body,node('span','m-check','✓'),copy);
    row.onclick=()=>{if(!state.selecting||state.busy)return;if(state.selected.has(m.id)){state.selected.delete(m.id);state.selectedRows.delete(m.id);}else{state.selected.add(m.id);state.selectedRows.set(m.id,m);}row.classList.toggle('selected',state.selected.has(m.id));row.setAttribute('aria-selected',String(state.selected.has(m.id)));updateSelection();syncButtons();};list.append(row);
  }
  if(!state.rows.length)list.append(node('div','c-tip','没有匹配的聊天记录'));
  list.scrollTop=0;
}
function renderTimeline(){
  const list=$('c-timeline-list');list.replaceChildren();
  for(const item of state.months){const b=node('button','tl-month',item.month.replace('-','年')+'月');b.type='button';b.onclick=()=>run(async()=>{await loadMessages(item.offset);$('c-timeline').classList.remove('pinned');});list.append(b);}
  if(!state.months.length)list.append(node('div','tl-empty','暂无可跳转月份'));
}
function updateSelection(){
  $('chat-wrap').classList.toggle('selecting',state.selecting);
  $('c-select').classList.toggle('hidden',state.selecting);$('c-copy-selected').classList.toggle('hidden',!state.selecting);$('c-cancel-select').classList.toggle('hidden',!state.selecting);
  $('c-copy-selected').textContent=state.selected.size?'复制所选（'+state.selected.size+'）':'复制所选';
  $('c-export').textContent=state.selecting&&state.selected.size?'导出所选':'导出';
}
async function copyText(text){
  try{if(navigator.clipboard&&window.isSecureContext)await navigator.clipboard.writeText(text);else{const input=node('textarea','copy-buffer');input.value=text;document.body.append(input);input.select();const ok=document.execCommand('copy');input.remove();if(!ok)throw new Error();}toast('已复制');}catch{toast('复制未完成，请手动选择文字复制。');}
}
function copySelected(){const rows=[...state.selectedRows.values()].sort((a,b)=>a.id-b.id);copyText(rows.map(m=>m.senderDisplayName+'  '+m.createTime+'\n'+m.content).join('\n\n'));}
function clearScope(){state.exportScope=null;$('e-range-banner').classList.add('hidden');}
function excluded(c){return ($('e-noofficial').checked&&c.kind==='official') || ($('e-roster-re').value&&matches(c.name+' '+c.id,$('e-roster-re').value));}
function exportSelection(){return state.chats.filter(c=>state.exportChats.has(c.id)&&!excluded(c));}
function renderExport(){
  const list=$('e-sessions');list.replaceChildren();
  const visible=state.chats.filter(c=>matches(c.name,$('e-search').value)&&(state.exportFilter==='all'||c.kind===state.exportFilter));
  for(const c of visible){const row=node('label','e-sess'+(state.exportChats.has(c.id)?' sel':'')+(excluded(c)?' is-excl':''));const box=node('input');box.type='checkbox';box.checked=state.exportChats.has(c.id);box.setAttribute('aria-label','选择 '+c.name);box.onchange=()=>{clearScope();box.checked?state.exportChats.add(c.id):state.exportChats.delete(c.id);renderExport();syncButtons();};const body=node('span','e-body');body.append(node('b','name',c.name),node('span','prev',excluded(c)?'已排除':'聊天记录'));row.append(box,node('span','ava',c.name.slice(0,1)),body,node('span','kind-tag',c.kind==='group'?'群聊':c.kind==='official'?'公众号':'好友'));list.append(row);}
  if(!visible.length)list.append(node('div','e-empty',state.opened?'没有匹配的会话':'请先选择账号并提取'));
  const count=exportSelection().length;$('e-hint').textContent='已选 '+count+' 个会话';$('e-selall').checked=!!visible.length&&visible.every(c=>state.exportChats.has(c.id));
  $('e-range-banner').classList.toggle('hidden',!state.exportScope?.ids?.length);
  if(state.exportScope?.ids?.length){$('e-range-banner').replaceChildren(node('b','', '仅导出所选 '+state.exportScope.ids.length+' 条消息'));const b=node('button','mini-btn','改为全部筛选结果');b.onclick=()=>{clearScope();renderExport();};$('e-range-banner').append(b);}
  if(!$('e-roster-modal').classList.contains('hidden'))renderRoster();
}
function renderRoster(){
  const list=$('e-roster-list');list.replaceChildren();let shown=0;
  for(const c of state.chats){const included=state.exportChats.has(c.id)&&!excluded(c);if(!matches(c.name,$('e-roster-search').value)||state.rosterFilter==='in'&&!included||state.rosterFilter==='ex'&&included)continue;
    const row=node('label','e-rost'),box=node('input');box.type='checkbox';box.checked=state.exportChats.has(c.id);box.onchange=()=>{clearScope();box.checked?state.exportChats.add(c.id):state.exportChats.delete(c.id);renderExport();syncButtons();};row.append(box,node('span','nm',c.name),node('span','tag'+(included?' back':''),included?'将导出':'已排除'));list.append(row);shown++;}
  $('e-roster-hint').textContent=$('e-roster-re').value?'命中排除规则的会话不会导出':'';$('e-roster-count').textContent='将导出 '+exportSelection().length+' 个会话 · 当前显示 '+shown+' 个';
}
function log(text){state.log.push(new Date().toLocaleTimeString('zh-CN')+'  '+text);if(state.log.length>200)state.log.shift();if(!state.logPaused)$('e-log').textContent=state.log.join('\n');}
async function downloadExport(){
  const chats=exportSelection();if(!chats.length)return;
  const filters={query:$('e-query').value,since:$('e-start').value,until:$('e-end').value,kind:$('e-kind').value},fmt=$('e-fmt').value;
  const body=state.exportScope?.ids?.length?{chat:state.exportScope.chat,...filters,ids:state.exportScope.ids,format:fmt}:
    chats.length===1&&$('e-pack').value==='auto'?{chat:chats[0].id,...filters,format:fmt}:{chats:chats.map(c=>c.id),...filters,format:fmt};
  if(!body.chats&&$('e-pack').value==='zip')body.pack='zip';
  const zip=!!body.chats||body.pack==='zip';state.download=new AbortController();$('e-progress-card').classList.remove('hidden');$('e-bar').classList.remove('done');$('e-bar').classList.add('working');$('e-cancel').classList.remove('hidden');$('e-cancel').disabled=false;
  $('e-prog-text').textContent='正在生成导出文件…';$('e-result').replaceChildren();log('开始导出 '+chats.length+' 个会话');
  try{const res=await api('/api/export',body,state.download.signal),blob=await res.blob();const url=URL.createObjectURL(blob),a=node('a');a.href=url;a.download=zip?'chat-exports.zip':'chat-export.'+fmt;document.body.append(a);a.click();a.remove();setTimeout(()=>URL.revokeObjectURL(url),60000);$('e-bar').classList.add('done');$('e-prog-text').textContent='导出完成';$('e-result').append(node('div','res-row res-ok','导出完成，请在浏览器下载中查看。'));log('导出完成');}
  catch(error){if(error.name==='AbortError'){log('已取消本次下载');$('e-prog-text').textContent='已取消下载';}else{log('导出未完成：'+error.message);$('e-prog-text').textContent='导出未完成';throw error;}}
  finally{state.download=null;$('e-bar').classList.remove('working');$('e-cancel').classList.add('hidden');}
}
$('acc-card').onclick=()=>{if(state.busy||state.stopped)return;$('acc-pop').hidden=!$('acc-pop').hidden;$('acc-card').setAttribute('aria-expanded',String(!$('acc-pop').hidden));};
document.addEventListener('click',e=>{if(!$('acc-card').contains(e.target)&&!$('acc-pop').contains(e.target)){$('acc-pop').hidden=true;$('acc-card').setAttribute('aria-expanded','false');}});
$('ob-close').onclick=()=>{$('account-modal').hidden=true;};$('ob-consent').onchange=syncButtons;
$('ob-open').onclick=()=>runWizardExtraction();
$('c-search').oninput=renderChats;$('c-back').onclick=()=>{$('chat-wrap').classList.remove('open');};
$('c-refresh').onclick=()=>run(async()=>{if(state.account!==null)await extract(state.account);});
function searchMessages(){return run(async()=>{if(!state.chat)return;messageFilter=readChatFilter();state.selected.clear(); state.selectedRows.clear();await loadMessages(0);});}
$('c-msg-search').onclick=searchMessages;$('c-msg-q').onkeydown=e=>{if(e.key==='Enter')searchMessages();};
for(const b of document.querySelectorAll('#c-kind-chips button'))b.onclick=()=>{if(state.busy)return;document.querySelectorAll('#c-kind-chips button').forEach(x=>x.classList.toggle('on',x===b));searchMessages();};
$('c-filter-exit').onclick=()=>{$('c-msg-q').value='';$('c-since').value='';$('c-until').value='';document.querySelectorAll('#c-kind-chips button').forEach(b=>b.classList.toggle('on',b.dataset.kind===''));searchMessages();};
$('c-prev').onclick=()=>run(()=>loadMessages(Math.max(0,state.offset-200)));$('c-next').onclick=()=>run(()=>loadMessages(state.offset+state.rows.length));
$('c-timeline-trigger').onclick=()=>{$('c-timeline').classList.toggle('pinned');};
$('c-select').onclick=()=>{state.selecting=true;updateSelection();syncButtons();};$('c-cancel-select').onclick=()=>{state.selecting=false;state.selected.clear(); state.selectedRows.clear();renderMessages();updateSelection();syncButtons();};$('c-copy-selected').onclick=copySelected;
$('c-export').onclick=()=>{if(!state.chat)return;state.exportChats=new Set([state.chat]);state.exportScope=state.selecting&&state.selected.size?{chat:state.chat,ids:[...state.selected].sort((a,b)=>a-b)}:null;
  $('e-query').value=messageFilter.query;$('e-start').value=messageFilter.since;$('e-end').value=messageFilter.until;$('e-kind').value=messageFilter.kind;location.hash='#/export';renderExport();syncButtons();};
$('e-search').oninput=()=>{renderExport();syncButtons();};
for(const b of document.querySelectorAll('#e-filter-chips button'))b.onclick=()=>{state.exportFilter=b.dataset.f;document.querySelectorAll('#e-filter-chips button').forEach(x=>x.classList.toggle('on',x===b));renderExport();syncButtons();};
$('e-selall').onchange=()=>{clearScope();for(const c of state.chats.filter(c=>matches(c.name,$('e-search').value)&&(state.exportFilter==='all'||c.kind===state.exportFilter)))$('e-selall').checked?state.exportChats.add(c.id):state.exportChats.delete(c.id);renderExport();syncButtons();};
$('e-noofficial').onchange=()=>{renderExport();syncButtons();};
for(const id of ['e-query','e-start','e-end','e-kind'])$(id).onchange=()=>{clearScope();renderExport();};
$('e-roster-btn').onclick=()=>{$('e-roster-modal').classList.remove('hidden');renderRoster();};$('e-roster-close').onclick=()=>{$('e-roster-modal').classList.add('hidden');};
$('e-roster-search').oninput=renderRoster;$('e-roster-re').oninput=()=>{clearScope();renderExport();syncButtons();};
for(const b of document.querySelectorAll('#e-roster-chips button'))b.onclick=()=>{state.rosterFilter=b.dataset.f;document.querySelectorAll('#e-roster-chips button').forEach(x=>x.classList.toggle('on',x===b));renderRoster();};
$('e-roster-reset').onclick=()=>{clearScope();state.exportChats=new Set(state.chats.filter(c=>!excluded(c)).map(c=>c.id));renderExport();syncButtons();};
$('e-roster-copy').onclick=()=>copyText(exportSelection().map(c=>c.name).join('\n'));
$('e-run').onclick=()=>run(downloadExport);$('e-cancel').onclick=()=>state.download?.abort();
$('e-log-pause').onclick=()=>{state.logPaused=!state.logPaused;$('e-log-pause').textContent=state.logPaused?'继续':'暂停';$('e-log-pause').setAttribute('aria-pressed',String(state.logPaused));if(!state.logPaused)$('e-log').textContent=state.log.join('\n');};$('e-log-copy').onclick=()=>copyText(state.log.join('\n'));
$('close-data').onclick=()=>run(async()=>{await api('/api/close',{});clearData();toast('当前数据已关闭');});
$('stop').onclick=()=>run(async()=>{if(!window.confirm('停止程序并关闭当前数据？'))return;await api('/api/stop',{});state.stopped=true;clearData();$('account-modal').hidden=true;toast('程序已停止，可以关闭此网页。');});
function theme(value){if($('s-theme'))$('s-theme').value=value;document.documentElement.dataset.theme=value;try{localStorage.setItem('siwx-theme',value);}catch{}$('side-theme').textContent=value==='dark'?'☀':'☾';}
$('side-theme').onclick=()=>theme(document.documentElement.dataset.theme==='dark'?'light':'dark');
try{theme(localStorage.getItem('siwx-theme')==='dark'?'dark':'light');}catch{theme('light');}
window.addEventListener('hashchange',route);document.addEventListener('keydown',e=>{if(e.key==='Escape'&&!state.busy){$('account-modal').hidden=true;$('acc-pop').hidden=true;$('e-roster-modal').classList.add('hidden');$('c-timeline').classList.remove('pinned');}});

window.SX={fetchJSON:async url=>json(url),copyText:async text=>{await copyText(text);return true;},
  fmtTs:ts=>new Date(Number(ts)*1000).toLocaleString('zh-CN',{hour12:false}),
  fmtDate:ts=>{const d=new Date(Number(ts)*1000);return d.getFullYear()+'-'+String(d.getMonth()+1).padStart(2,'0')+'-'+String(d.getDate()).padStart(2,'0');},
  getAccount:()=>state.opened?'current':null,getAccountName:()=>state.accounts.find(a=>a.id===state.account)?.name||'微信',toast,recordEvent:log};
function showWizardScreen(screen){
  wizardScreen=screen;
  for(const [name,id] of [['intro','ob-intro'],['accounts','ob-account-step'],['extract','ob-extract-step'],['next','ob-next-step']])$(id).hidden=name!==screen;
  $('ob-steps').replaceChildren();const labels=['确认使用','选择账号','提取并解密','接下来做什么'];
  const index=['intro','accounts','extract','next'].indexOf(screen);
  labels.forEach((label,i)=>$('ob-steps').append(node('span','g-chip'+(i===index?' on':i<index?' done':''),(i+1)+' '+label)));
  $('ob-title').textContent='选择微信号';
  $('ob-extract-account').textContent='请保持本人微信登录。当前选择：'+(state.accounts.find(a=>a.id===state.pendingAccount)?.name||'未选择账号');
}
async function wizardExtractionTask(resume=false){
  showWizardScreen('extract');$('ob-progress').hidden=false;$('ob-log').hidden=false;$('ob-log').textContent='准备读取账号数据…';$('ob-result').textContent='';$('ob-run').hidden=false;$('ob-finish').hidden=true;
  try{await extract(state.pendingAccount,resume);$('ob-result').textContent='提取完成，可以查看聊天与朋友圈。';$('ob-run').hidden=true;$('ob-finish').hidden=false;if(!wizardActive){$('account-modal').hidden=true;route();}}
  catch(error){$('ob-result').textContent=error.message;throw error;}
  finally{$('ob-progress').hidden=true;}
}
async function runWizardExtraction(){
  if(!$('ob-consent').checked||state.pendingAccount===null)return;
  await run(()=>wizardExtractionTask());
}
function finishWizard(page='chat'){$('account-modal').hidden=true;wizardActive=false;location.hash='#/'+page;route();syncButtons();}
$('side-wizard').onclick=()=>showAccountModal(state.account,true);
$('ob-start').onclick=()=>showWizardScreen('accounts');$('ob-back-intro').onclick=()=>showWizardScreen('intro');
$('ob-to-extract').onclick=()=>{if($('ob-consent').checked&&state.pendingAccount!==null){$('ob-result').textContent='';$('ob-log').hidden=true;$('ob-progress').hidden=true;$('ob-run').hidden=false;$('ob-finish').hidden=true;showWizardScreen('extract');}};
$('ob-back-account').onclick=()=>showWizardScreen('accounts');$('ob-run').onclick=()=>runWizardExtraction();
$('ob-finish').onclick=()=>{if(wizardActive)showWizardScreen('next');else finishWizard();};$('ob-enter').onclick=()=>finishWizard();
$('ob-skip').onclick=()=>finishWizard('settings');$('ob-extract-skip').onclick=()=>finishWizard('logs');
document.querySelectorAll('[data-wizard-go]').forEach(b=>b.onclick=()=>finishWizard(b.dataset.wizardGo));
$('ob-manual-toggle').onclick=()=>{$('ob-manual-box').hidden=!$('ob-manual-box').hidden;};
$('ob-manual-add').onclick=()=>run(async()=>{const before=new Set(state.accounts.map(a=>a.id));$('ob-manual-msg').textContent='验证中…';const result=await json('/api/accounts/add',{path:$('ob-manual-path').value});state.accounts=result.accounts;state.pendingAccount=state.accounts.find(a=>!before.has(a.id))?.id??state.pendingAccount;$('ob-manual-msg').textContent='账号已添加';$('ob-manual-path').value='';renderAccount();renderAccountChoices();renderSettings();});
function renderSettings(){
 $('s-current').textContent=state.opened?(state.accounts.find(a=>a.id===state.account)?.name||'已打开'):'未打开账号';
 $('s-theme').value=document.documentElement.dataset.theme==='dark'?'dark':'light';
 const list=$('s-ds-accounts');list.replaceChildren();for(const a of state.accounts){const row=node('div','acc-box');row.append(node('b','',a.name));const b=node('button','btn btn-sm',a.id===state.account&&state.opened?'重新提取':'选择并提取');b.onclick=()=>showAccountModal(a.id);row.append(b);list.append(row);}
 if(!state.accounts.length)list.append(node('div','empty','未发现账号，可从运行向导手动添加数据目录。'));
}
async function refreshLogs(){if(logsPaused)return;try{logsRows=(await json('/api/logs')).logs;renderLogs();}catch{if(!state.stopped)toast('日志读取未完成。');}}
function filteredLogs(){return logsRows.filter(row=>(!$('log-filter').value||row.level===$('log-filter').value)&&row.message.toLowerCase().includes($('log-text').value.toLowerCase()));}
function logsText(){return filteredLogs().map(row=>($('log-ts').checked?row.time+'  ':'')+'['+row.level.toUpperCase()+'] '+row.message).join('\n');}
function renderLogs(){const rows=filteredLogs();$('log-count').textContent=rows.length+' 条';$('log-box').textContent=logsText()||'暂无操作记录';if($('log-auto').checked)$('log-box').scrollTop=$('log-box').scrollHeight;}
async function downloadLogs(){try{logsRows=(await json('/api/logs')).logs;const url=URL.createObjectURL(new Blob([logsText()],{type:'text/plain;charset=utf-8'}));const a=node('a');a.href=url;a.download='siwx-operation-log.txt';document.body.append(a);a.click();a.remove();setTimeout(()=>URL.revokeObjectURL(url),60000);}catch(error){toast(error.message);}}
$('s-rerun-wizard').onclick=()=>showAccountModal(state.account,true);$('s-account').onclick=()=>showAccountModal();
async function refreshAccounts(){const result=await json('/api/accounts/refresh',{});state.accounts=result.accounts;if(state.pendingAccount===null&&state.accounts.length)state.pendingAccount=state.accounts[0].id;if(state.account===null&&state.accounts.length)state.account=state.accounts[0].id;renderAccount();renderAccountChoices();renderSettings();$('ob-detect-status').textContent=state.accounts.length?'已检测到 '+state.accounts.length+' 个账号':'未检测到账号，可手动指定微信数据目录';return result;}
$('s-refresh-accounts').onclick=()=>run(async()=>{await refreshAccounts();toast('账号检测完成');});
$('ob-refresh').onclick=()=>run(async()=>{$('ob-detect-status').textContent='正在检测微信数据目录…';await refreshAccounts();});
$('s-close-data').onclick=()=>run(async()=>{await api('/api/close',{});clearData();renderSettings();toast('当前数据已关闭');});
$('s-theme').onchange=()=>theme($('s-theme').value);$('s-view-logs').onclick=()=>{location.hash='#/logs';};$('s-export-log').onclick=downloadLogs;
$('log-filter').onchange=renderLogs;$('log-text').oninput=renderLogs;$('log-ts').onchange=renderLogs;$('log-auto').onchange=renderLogs;
$('log-pause').onclick=()=>{logsPaused=!logsPaused;$('log-pause').textContent=logsPaused?'继续':'暂停';$('log-pause').setAttribute('aria-pressed',String(logsPaused));if(!logsPaused)refreshLogs();};
$('log-clear').onclick=()=>run(async()=>{await api('/api/logs/clear',{});logsRows=[];state.log=[];renderLogs();});$('log-copy').onclick=()=>copyText(logsText());$('log-download').onclick=downloadLogs;
setInterval(()=>{if(!$('logs-page').hidden&&!state.busy&&!state.stopped)refreshLogs();},2000);
const tourSteps=[['#acc-card','选择账号','从这里切换本人的微信账号。'],['#side-wizard','运行向导','重新确认、选号、提取并打开数据。'],['#menu a[data-page=chat]','聊天查看','查找会话、筛选消息、复制或导出所选记录。'],['#menu a[data-page=sns]','朋友圈','浏览动态、互动与媒体，按发布者和日期筛选并导出。'],['#menu a[data-page=export]','导出','选择会话、日期与格式，下载文件或ZIP。'],['#menu a[data-page=logs]','运行日志','查看操作阶段、搜索、复制与下载。'],['#menu a[data-page=settings]','设置','管理账号、主题与运行向导。']];
function showTour(index){document.querySelectorAll('.tour-target').forEach(n=>n.classList.remove('tour-target'));$('tour-panel')?.remove();tourIndex=index;if(index<0||index>=tourSteps.length)return;const [selector,title,text]=tourSteps[index],target=document.querySelector(selector);target?.classList.add('tour-target');target?.scrollIntoView({block:'nearest'});const panel=node('div','tour-panel');panel.id='tour-panel';panel.setAttribute('role','dialog');panel.setAttribute('aria-label','使用导览');panel.append(node('b','',title),node('p','',text));const actions=node('div','ob-actions');for(const [label,next] of [['上一步',index-1],[index===tourSteps.length-1?'完成':'下一步',index+1],['关闭',-1]]){const b=node('button','btn btn-sm',label);b.onclick=()=>showTour(next);actions.append(b);}panel.append(actions);document.body.append(panel);}
$('side-tour').onclick=()=>showTour(0);
route();run(async()=>{state.accounts=await json('/api/accounts');if(!state.accounts.length)await refreshAccounts();const current=await json('/api/state');current.extraction=await json('/api/open/progress');state.account=current.account??state.accounts[0]?.id??null;state.opened=current.opened;if(state.opened)state.chats=await json('/api/chats');renderAccount();renderChats();renderExport();if(!state.opened){state.pendingAccount=state.account;wizardActive=true;$('account-modal').hidden=false;renderAccountChoices();showWizardScreen('intro');if(current.extraction?.state==='running'){state.pendingAccount=current.extraction.account;$('ob-consent').checked=true;await wizardExtractionTask(true);}}});
