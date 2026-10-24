/* 加密备份页 —— 一级入口「新建备份 / 恢复备份」。
   新建：与导出页同一套会话列表（e-list/e-sess + 头像），模式/过滤/密码双输，
        微信在跑则两段式 allowKill 确认。
   恢复：选包 → 输密码 → analyze（只读 diff 报告）→ 确认 → apply（任务槽）。
        消息合入专用恢复分片，不碰微信数据目录。 */
import { createDatePicker } from '/widgets.js?v=2026100601';

const { esc, fetchJSON, fmtListTs, toast, getAccount } = window.SX;

function el(id) { return document.getElementById(id); }

function gone() { return !document.getElementById('b-entry'); }

let account = null;
let sessions = [];
let selected = new Set();
let startPick = null;
let endPick = null;
let pollTimer = null;
let pendingPayload = null;      // 等待 kill 确认的请求体
let rStage = null;              // 恢复：analyze 返回的 stage_id
let rPollTimer = null;
const avaFailed = new Set();

/* ── 公共 ─────────────────────────────────────────── */

function avatarImg(username) {
  if (avaFailed.has(username)) return '';
  const src = `/api/chat/avatar?account=${encodeURIComponent(account)}` +
              `&username=${encodeURIComponent(username)}`;
  return `<img loading="lazy" src="${src}" data-ava="${esc(username)}" alt="">`;
}

function fmtSize(n) {
  if (n >= 1048576) return (n / 1048576).toFixed(1) + ' MB';
  if (n >= 1024) return (n / 1024).toFixed(1) + ' KB';
  return n + ' B';
}

function action() {
  const r = document.querySelector('input[name="b-action"]:checked');
  return r ? r.value : 'create';
}

function applyAction() {
  const create = action() === 'create';
  el('b-create-panel').classList.toggle('hidden', !create);
  el('b-restore-panel').classList.toggle('hidden', create);
}

/* ── 新建备份 ─────────────────────────────────────── */

function mode() {
  const r = document.querySelector('input[name="b-mode"]:checked');
  return r ? r.value : 'selective';
}

function applyMode() {
  const full = mode() === 'full';
  el('b-list').classList.toggle('b-disabled', full);
  el('b-filter-block').classList.toggle('hidden', full);
  el('b-hint').textContent = full ? '整账号全量备份'
    : `已选 ${selected.size} 个会话${selected.size ? '' : '（不选=全部）'}`;
}

function match(s, kw) {
  const k = (kw || '').toLowerCase();
  return !k || (s.display || '').toLowerCase().includes(k)
    || (s.username || '').toLowerCase().includes(k);
}

function renderSessions() {
  const kw = el('e-search').value;
  const list = sessions.filter(s => match(s, kw));
  el('e-sessions').innerHTML = list.length ? list.map(s => {
    const initials = (s.display || s.username || '?').slice(0, 2).toUpperCase();
    return `
    <label class="e-sess ${selected.has(s.username) ? 'sel' : ''}">
      <input type="checkbox" data-u="${esc(s.username)}" ${selected.has(s.username) ? 'checked' : ''}>
      <div class="ava"><span>${esc(initials)}</span>${avatarImg(s.username)}</div>
      <div class="e-body">
        <span class="name">${esc(s.display)}</span>
        <span class="prev">${esc(s.preview || '')}</span>
      </div>
      <span class="tm">${fmtListTs(s.last_time)}</span>
    </label>`;
  }).join('') : '<div class="e-empty">没有匹配的会话</div>';
}

async function loadSessions() {
  if (!account) {
    el('e-sessions').innerHTML =
      '<div class="e-empty">还没有解密产物<br>' +
      '<button class="btn btn-primary" id="b-go-guide" type="button">去添加账号</button></div>';
    el('b-go-guide').addEventListener('click', () => window.__sxOnboarding?.open('add'));
    return;
  }
  el('e-sessions').innerHTML = '<div class="e-loading">加载会话…</div>';
  try {
    const { sessions: ss } = await fetchJSON(
      `/api/chat/sessions?account=${encodeURIComponent(account)}`);
    if (gone()) return;
    sessions = ss || [];
    renderSessions();
    applyMode();
  } catch (e) {
    if (gone()) return;
    el('e-sessions').innerHTML = `<div class="e-empty">${esc(e.message)}</div>`;
  }
}

function selectedTypes() {
  return [...el('b-types').querySelectorAll('input:checked')]
    .map(i => parseInt(i.value, 10)).filter(n => !Number.isNaN(n));
}

function buildPayload(allowKill) {
  const m = mode();
  const chats = m === 'selective' && selected.size ? [...selected] : null;
  const types = m === 'selective' ? selectedTypes() : null;
  return {
    account,
    mode: m,
    chats,
    types: types && types.length ? types : null,
    start: m === 'selective' ? (startPick.value || null) : null,
    end: m === 'selective' ? (endPick.value || null) : null,
    password: el('b-pw1').value,
    allowKill: !!allowKill,
  };
}

function showKillDialog(pids) {
  el('b-kill-pids').textContent = pids.join(', ');
  el('b-kill-mask').classList.remove('hidden');
}

function hideKillDialog() {
  el('b-kill-mask').classList.add('hidden');
  pendingPayload = null;
}

async function startBackup(allowKill) {
  const payload = allowKill && pendingPayload
    ? { ...pendingPayload, allowKill: true }
    : buildPayload(false);
  if (allowKill) pendingPayload = null;
  try {
    const r = await fetchJSON('/api/backup/start', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(payload),
    });
    if (r.needKill) {
      pendingPayload = payload;
      showKillDialog(r.pids || []);
      return;
    }
    hideKillDialog();
    el('b-progress-card').classList.remove('hidden');
    el('b-log').innerHTML = '';
    el('b-result').innerHTML = '';
    startPolling();
  } catch (e) {
    toast(`备份启动失败：${e.message}`, true);
  }
}

function onRun() {
  const p1 = el('b-pw1').value, p2 = el('b-pw2').value;
  if (p1.length < 8) { toast('备份密码至少 8 位', true); return; }
  if (p1 !== p2) { toast('两次输入的密码不一致', true); return; }
  startBackup(false);
}

function renderLogLine(line) {
  const div = document.createElement('div');
  div.className = 'log-line';
  div.textContent = line;
  el('b-log').appendChild(div);
  el('b-log').scrollTop = el('b-log').scrollHeight;
}

function renderResult(res) {
  if (!res) return;
  const st = res.stats || {};
  const parts = [`${fmtSize(res.size)} · ${res.files} 个文件 · ${res.mode === 'full' ? '全量' : '自定义'}`];
  if (st.chats_count != null) parts.push(`${st.chats_count} 个会话 / ${st.messages || 0} 条消息`);
  const wal = (res.wal || []).length;
  el('b-result').innerHTML = `
    <div class="b-ok">✓ 备份完成：${esc(res.path.split(/[\\/]/).pop())}</div>
    <div class="hint">${esc(parts.join(' · '))}${wal ? ` · ⚠ ${wal} 个 -wal 残留（最新消息可能未包含）` : ''}</div>`;
}

async function poll() {
  if (gone()) { clearInterval(pollTimer); return; }
  try {
    const st = await fetchJSON('/api/backup/status');
    if (gone()) { clearInterval(pollTimer); return; }
    el('b-bar').style.width = (st.progress || 0) + '%';
    el('b-pct').textContent = `${st.progress || 0}% ${st.msg || ''}`;
    const lines = st.logs || [];
    const box = el('b-log');
    if (box.childElementCount !== lines.length) {
      box.innerHTML = '';
      for (const l of lines) renderLogLine(l);
    } else if (lines.length) {
      const last = box.lastElementChild;
      if (last && last.textContent !== lines[lines.length - 1]) renderLogLine(lines[lines.length - 1]);
    }
    if (st.done) {
      clearInterval(pollTimer);
      pollTimer = null;
      if (st.ok) renderResult(st.result);
      else el('b-result').innerHTML = `<div class="b-fail">✗ ${esc(st.error || '备份失败')}</div>`;
      el('b-pw1').value = '';
      el('b-pw2').value = '';
      loadPacks();
    }
  } catch (e) { /* 轮询失败下轮重试 */ }
}

function startPolling() {
  clearInterval(pollTimer);
  pollTimer = setInterval(poll, 900);
  poll();
}

async function loadWechatHint() {
  try {
    const st = await fetchJSON('/api/backup/wechat');
    let line = st.running
      ? '⚠ 微信正在运行：开始备份时会请求确认终止微信进程。'
      : '✓ 微信未运行，可以直接开始备份。';
    const cands = (st.accounts || []).filter(a => a.wxid === account)
      .sort((a, b) => b.keys - a.keys || b.dbs - a.dbs);
    const cur = cands[0];
    if (cands.length > 1) {
      line += `（该账号有 ${cands.length} 个数据目录，已选密钥最全的）`;
    }
    if (cur && cur.dbs > 0) {
      if (cur.keys >= cur.dbs) {
        line += ` 密钥 ${cur.keys}/${cur.dbs} ✓`;
      } else {
        line += ` 该账号密钥 ${cur.keys}/${cur.dbs}` +
          (st.running ? '——开始备份时会自动提取（只对当前登录账号有效）。'
                      : '——需先启动微信并登录该账号，备份才能自动提取密钥。');
      }
    }
    el('b-wechat').textContent = line;
  } catch (e) { /* 提示失败不影响主流程 */ }
}

/* ── 恢复备份 ─────────────────────────────────────── */

async function loadPacks() {
  try {
    const { packs } = await fetchJSON('/api/backup/list');
    if (gone()) return;
    el('b-packs').innerHTML = (packs && packs.length)
      ? packs.map(p => `
        <div class="b-pack" data-path="${esc(p.path)}">
          <div class="b-pack-main">
            <span class="b-pack-name">${esc(p.name)}</span>
            <span class="hint">${fmtSize(p.size)} · ${fmtListTs(p.mtime)}</span>
          </div>
          <button class="mini-btn" data-peek="${esc(p.path)}">信息</button>
          <button class="btn btn-sm" data-analyze="${esc(p.path)}">分析</button>
        </div>`).join('')
      : '<div class="b-empty-list">暂无备份包——先在「新建备份」创建一个，或把别处导出的 .siwxpack 放进 exports/ 目录</div>';
  } catch (e) {
    if (gone()) return;
    el('b-packs').innerHTML = `<div class="b-empty-list">备份列表加载失败：${esc(e.message)}</div>`;
  }
}

async function onPeek(path, btn) {
  try {
    const { info } = await fetchJSON('/api/backup/peek', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ path }),
    });
    const k = info.kdf || {};
    btn.textContent = `v${info.format} · ${k.alg || '?'}`;
    btn.disabled = true;
  } catch (e) {
    toast(`读取失败：${e.message}`, true);
  }
}

async function onAnalyze(path, btn) {
  const pw = el('r-pw').value;
  if (!pw) { toast('请先输入备份密码', true); return; }
  btn.disabled = true;
  btn.textContent = '分析中…';
  try {
    const rep = await fetchJSON('/api/backup/restore/analyze', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ path, password: pw }),
    });
    renderReport(rep, path);
  } catch (e) {
    toast(`分析失败：${e.message}`, true);
  } finally {
    btn.disabled = false;
    btn.textContent = '分析';
  }
}

function renderReport(rep, path) {
  rStage = rep.stage_id;
  el('r-report-card').classList.remove('hidden');
  el('r-apply-card').classList.add('hidden');
  const meta = `${Path_name(path)} · ${rep.pack_mode === 'full' ? '全量' : '自定义'}包 · ${rep.pack_created || ''}`;
  el('r-report-meta').textContent = meta;

  if (rep.fresh) {
    el('r-report-summary').innerHTML = `
      <div class="b-ok">目标账号（${esc(rep.account.slice(0, 6))}***）在本机还没有缓存库</div>
      <div class="hint">确认后将<b>全新安装</b>：包内 ${rep.total_pack} 条消息与全部会话/联系人直接落地为可浏览的档案账号。</div>`;
    el('r-report-chats').innerHTML = '';
    return;
  }

  el('r-report-summary').innerHTML = `
    <div class="b-report-line">
      包内消息 <b>${rep.total_pack}</b> 条 ·
      <span class="b-dup">已存在（跳过）${rep.total_dup}</span> ·
      <span class="b-new">将恢复 ${rep.total_insert}</span> ·
      新会话行 ${rep.sessions_new} · 新联系人 ${rep.contacts_new}
    </div>
    ${rep.total_insert === 0
      ? '<div class="hint">包内消息已全部存在于缓存库——无需恢复（重复执行是安全的，结果不变）。</div>'
      : '<div class="hint">确认后：消息合入专用恢复分片（重新解密不会覆盖），会话摘要与联系人按用户名合并；被改写文件先自动冷备。</div>'}`;

  const rows = (rep.chats || []).slice(0, 30);
  el('r-report-chats').innerHTML = rows.length ? `
    <div class="b-report-head">会话（按将恢复条数排序，前 ${rows.length}/${rep.chats_count}）</div>
    <div class="b-report-row b-report-row-head">
      <span>会话</span><span>包内</span><span>已有</span><span>将恢复</span>
    </div>` + rows.map(c => `
    <div class="b-report-row">
      <span class="b-report-name">${esc(c.display)}</span>
      <span>${c.pack_rows}</span>
      <span class="b-dup">${c.dup}</span>
      <span class="b-new">${c.to_insert}</span>
    </div>`).join('') : '';
}

function Path_name(p) {
  return String(p).split(/[\\/]/).pop();
}

function onCancelRestore() {
  rStage = null;
  el('r-report-card').classList.add('hidden');
}

async function onApply() {
  if (!rStage) return;
  try {
    const r = await fetchJSON('/api/backup/restore/apply', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ stage_id: rStage }),
    });
    if (!r.started) throw new Error(r.error || '启动失败');
    el('r-apply-card').classList.remove('hidden');
    el('r-log').innerHTML = '';
    el('r-result').innerHTML = '';
    startRestorePolling();
  } catch (e) {
    toast(`恢复启动失败：${e.message}`, true);
  }
}

function rRenderLog(line) {
  const div = document.createElement('div');
  div.className = 'log-line';
  div.textContent = line;
  el('r-log').appendChild(div);
  el('r-log').scrollTop = el('r-log').scrollHeight;
}

async function rPoll() {
  if (gone()) { clearInterval(rPollTimer); return; }
  try {
    const st = await fetchJSON('/api/backup/restore/status');
    if (gone()) { clearInterval(rPollTimer); return; }
    el('r-bar').style.width = (st.progress || 0) + '%';
    el('r-pct').textContent = `${st.progress || 0}% ${st.msg || ''}`;
    const lines = st.logs || [];
    const box = el('r-log');
    if (box.childElementCount !== lines.length) {
      box.innerHTML = '';
      for (const l of lines) rRenderLog(l);
    } else if (lines.length) {
      const last = box.lastElementChild;
      if (last && last.textContent !== lines[lines.length - 1]) rRenderLog(lines[lines.length - 1]);
    }
    if (st.done) {
      clearInterval(rPollTimer);
      rPollTimer = null;
      rStage = null;
      el('r-report-card').classList.add('hidden');
      const res = st.result || {};
      if (st.ok) {
        el('r-result').innerHTML = `
          <div class="b-ok">✓ 恢复完成（${res.mode === 'install' ? `全新安装 ${res.files} 个文件` : `消息 +${res.inserted}（跳过重复 ${res.dup}）· 会话 ${res.sessions} · 联系人 +${res.contacts}`}）</div>
          <div class="hint">聊天查看页刷新后即可看到恢复的会话。</div>`;
        el('r-pw').value = '';
      } else {
        el('r-result').innerHTML = `<div class="b-fail">✗ ${esc(st.error || '恢复失败')}</div>`;
      }
    }
  } catch (e) { /* 下轮重试 */ }
}

function startRestorePolling() {
  clearInterval(rPollTimer);
  rPollTimer = setInterval(rPoll, 900);
  rPoll();
}

/* ── init / destroy ───────────────────────────────── */

async function init() {
  account = getAccount();
  startPick = createDatePicker({ placeholder: '不限', label: '开始日期', todayText: '今天', clearText: '不限' });
  endPick = createDatePicker({ placeholder: '不限', label: '结束日期', todayText: '今天', clearText: '不限' });
  el('b-start').appendChild(startPick.el);
  el('b-end').appendChild(endPick.el);

  document.querySelectorAll('input[name="b-action"]').forEach(r =>
    r.addEventListener('change', applyAction));
  document.querySelectorAll('input[name="b-mode"]').forEach(r =>
    r.addEventListener('change', applyMode));
  el('e-sessions').addEventListener('change', (e) => {
    const u = e.target.dataset && e.target.dataset.u;
    if (!u) return;
    if (e.target.checked) selected.add(u); else selected.delete(u);
    e.target.closest('.e-sess').classList.toggle('sel', e.target.checked);
    applyMode();
  });
  el('e-sessions').addEventListener('error', (ev) => {
    const img = ev.target;
    if (img && img.tagName === 'IMG' && img.dataset.ava !== undefined) {
      avaFailed.add(img.dataset.ava);
      img.remove();
    }
  }, true);
  el('e-search').addEventListener('input', renderSessions);
  el('e-selall').addEventListener('change', (e) => {
    const kw = el('e-search').value;
    for (const s of sessions.filter(s => match(s, kw))) {
      if (e.target.checked) selected.add(s.username);
      else selected.delete(s.username);
    }
    renderSessions();
    applyMode();
  });
  el('b-run').addEventListener('click', onRun);
  el('b-kill-yes').addEventListener('click', () => { hideKillDialog(); startBackup(true); });
  el('b-kill-no').addEventListener('click', () => { hideKillDialog(); toast('请手动退出微信后再点「开始备份」'); });
  el('b-packs').addEventListener('click', (e) => {
    const ana = e.target.closest('button[data-analyze]');
    if (ana) { onAnalyze(ana.dataset.analyze, ana); return; }
    const btn = e.target.closest('button[data-peek]');
    if (btn) onPeek(btn.dataset.peek, btn);
  });
  el('r-cancel').addEventListener('click', onCancelRestore);
  el('r-apply').addEventListener('click', onApply);

  try {
    const st = await fetchJSON('/api/backup/status');
    if (st && st.running) {
      el('b-progress-card').classList.remove('hidden');
      startPolling();
    }
  } catch (e) { /* 忽略 */ }
  try {
    const st = await fetchJSON('/api/backup/restore/status');
    if (st && st.running) {
      el('r-apply-card').classList.remove('hidden');
      startRestorePolling();
    }
  } catch (e) { /* 忽略 */ }

  applyAction();
  applyMode();
  loadSessions();
  loadPacks();
  loadWechatHint();
}

function destroy() {
  clearInterval(pollTimer);
  clearInterval(rPollTimer);
  pollTimer = rPollTimer = null;
}

export { init, destroy };
