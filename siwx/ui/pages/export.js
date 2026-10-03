/* 导出页 —— 多选会话导出：左侧勾选列表 + 右侧配置 + 底部日志/进度 */
import { createDropdown, createDatePicker } from '/widgets.js?v=2026100203';

const { esc, fetchJSON, fmtTs, fmtListTs } = window.SX;

function el(id) { return document.getElementById(id); }

let account = null;
let sessions = [];
let selected = new Set();       // 已勾选 username 集合
let chatPreset = null;
let chatRange = null;           // 聊天页多选带入的范围 {account, chat, display, start, end, count}
let running = false;
let pollTimer = null;
let accDrop = null;
let fmtDrop = null;
let packDrop = null;
let startPick = null;
let endPick = null;

function match(s, kw) {
  const k = (kw || '').toLowerCase();
  return !k || s.display.toLowerCase().includes(k) || s.username.toLowerCase().includes(k);
}

/** 聊天页多选范围的展示条：显示带入的范围，可一键清除；
 *  用户手动改了日期视为接管，范围自动失效。 */
function renderRangeBanner() {
  const box = el('e-range-banner');
  if (!box) return;
  const applies = !!chatRange && selected.has(chatRange.chat);
  if (!applies) { box.classList.add('hidden'); box.innerHTML = ''; return; }
  box.classList.remove('hidden');
  box.innerHTML = '';
  const text = document.createElement('span');
  text.textContent = `聊天页多选已带入：${chatRange.display || chatRange.chat} · ${chatRange.count} 条消息 · ${chatRange.start} ~ ${chatRange.end}（按此日期范围导出）`;
  const btn = document.createElement('button');
  btn.className = 'mini-btn';
  btn.textContent = '清除';
  btn.addEventListener('click', () => {
    chatRange = null;
    sessionStorage.removeItem('siwx-export-range');
    startPick.value = null;
    endPick.value = null;
    renderRangeBanner();
  });
  box.append(text, btn);
}

/** 手动改动日期 = 用户接管范围；与带入范围不一致时自动解除 */
function onManualDate() {
  if (chatRange) {
    const same = startPick.value === chatRange.start && endPick.value === chatRange.end;
    if (!same) {
      chatRange = null;
      sessionStorage.removeItem('siwx-export-range');
    }
  }
  renderRangeBanner();
}

/** 导出格式下拉 = 内置 + 插件（插件追加在内置之后，并标注来源） */
async function loadFormats() {
  try {
    const { formats } = await fetchJSON('/api/export/formats');
    if (Array.isArray(formats) && formats.length) {
      fmtDrop.options = formats.map(f => ({
        value: f.fmt,
        label: `${f.label}${f.owner ? `（插件 ${f.owner}）` : ''}`,
      }));
    }
  } catch (e) { /* 保留默认项 */ }
}

async function init() {
  accDrop = createDropdown({ options: [], placeholder: '选择账号', label: '微信账号' });
  el('e-account').appendChild(accDrop.el);
  fmtDrop = createDropdown({
    options: [
      { value: 'json', label: 'JSON（结构化全量）' },
      { value: 'html', label: 'HTML（自包含网页）' },
      { value: 'txt', label: 'TXT（纯文本）' },
      { value: 'csv', label: 'CSV（表格）' },
      { value: 'xlsx', label: 'XLSX（Excel）' },
      { value: 'markdown', label: 'Markdown' },
      { value: 'toml', label: 'TOML' },
      { value: 'sqlite', label: 'SQLite 数据库' },
    ],
    value: 'json',
    label: '导出格式',
  });
  el('e-fmt').appendChild(fmtDrop.el);
  packDrop = createDropdown({
    options: [
      { value: 'folder', label: '仅文件夹（每会话一个子文件夹）' },
      { value: 'single', label: '单个 ZIP（全部会话打包一个）' },
      { value: 'each', label: '每会话一个 ZIP' },
    ],
    value: 'folder',
    label: '打包方式',
  });
  el('e-pack').appendChild(packDrop.el);
  startPick = createDatePicker({ placeholder: '不限', label: '开始日期', todayText: '今天', clearText: '不限' });
  el('e-start').appendChild(startPick.el);
  endPick = createDatePicker({ placeholder: '不限', label: '结束日期', todayText: '今天', clearText: '不限' });
  el('e-end').appendChild(endPick.el);
  startPick.onChange = onManualDate;
  endPick.onChange = onManualDate;

  loadFormats();
  try { chatPreset = JSON.parse(sessionStorage.getItem('siwx-export-preset') || 'null'); }
  catch (e) { chatPreset = null; }
  // 聊天页带过来的预选 + 多选范围：预选是一次性的，范围持续到清除为止
  if (chatPreset) {
    if (chatPreset.range) {
      chatRange = { ...chatPreset.range, account: chatPreset.account, chat: chatPreset.chat, display: chatPreset.display };
      sessionStorage.setItem('siwx-export-range', JSON.stringify(chatRange));
    } else {
      sessionStorage.removeItem('siwx-export-range');
      chatRange = null;
    }
    sessionStorage.removeItem('siwx-export-preset');
  } else {
    try { chatRange = JSON.parse(sessionStorage.getItem('siwx-export-range') || 'null'); }
    catch (e) { chatRange = null; }
  }
  try {
    const { accounts } = await fetchJSON('/api/chat/accounts');
    accDrop.options = accounts.map(a => ({ value: a.wxid, label: a.wxid }));
    if (accounts.length) {
      if (chatPreset && accounts.some(a => a.wxid === chatPreset.account)) {
        accDrop.value = chatPreset.account;
      }
      account = accDrop.value;
      accDrop.onChange = (v) => {
        account = v;
        selected.clear();
        loadSessions();
      };
      await loadSessions();
    } else {
      el('e-sessions').innerHTML = '<div class="c-empty">还没有解密产物 — 请先完成引导</div>';
    }
  } catch (e) {
    el('e-sessions').innerHTML = `<div class="c-empty">${esc(e.message)}</div>`;
  }
  el('e-search').addEventListener('input', renderSessions);
  el('e-selall').addEventListener('change', (e) => {
    const kw = el('e-search').value;
    for (const s of sessions) {
      if (match(s, kw)) {
        if (e.target.checked) selected.add(s.username);
        else selected.delete(s.username);
      }
    }
    renderSessions();
    updateCount();
  });
  el('e-run').addEventListener('click', run);
}

async function loadSessions() {
  el('e-sessions').innerHTML = '<div class="c-loading">加载会话…</div>';
  const { sessions: ss } = await fetchJSON(
    `/api/chat/sessions?account=${encodeURIComponent(account)}`);
  sessions = ss;
  // 聊天页跳转预选
  if (chatPreset && chatPreset.account === account && sessions.some(s => s.username === chatPreset.chat)) {
    selected.add(chatPreset.chat);
    sessionStorage.removeItem('siwx-export-preset');
    chatPreset = null;
  }
  // 聊天页多选范围：预选会话 + 预填日期
  if (chatRange && chatRange.account === account && sessions.some(s => s.username === chatRange.chat)) {
    selected.add(chatRange.chat);
    startPick.value = chatRange.start;
    endPick.value = chatRange.end;
  }
  renderSessions();
  updateCount();
}

function renderSessions() {
  const kw = el('e-search').value;
  const list = sessions.filter(s => match(s, kw));
  el('e-sessions').innerHTML = list.length ? list.map(s => `
    <label class="e-sess ${selected.has(s.username) ? 'sel' : ''}">
      <input type="checkbox" data-u="${esc(s.username)}" ${selected.has(s.username) ? 'checked' : ''}>
      <div class="e-body">
        <span class="name">${esc(s.display)}</span>
        <span class="prev">${esc(s.preview || '点击查看详情')}</span>
      </div>
      <span class="tm">${fmtListTs(s.last_time)}</span>
    </label>`).join('')
    : '<div class="c-empty">没有匹配的会话</div>';
  el('e-sessions').querySelectorAll('input[type=checkbox]').forEach(cb => {
    cb.addEventListener('change', () => {
      if (cb.checked) selected.add(cb.dataset.u);
      else selected.delete(cb.dataset.u);
      cb.closest('.e-sess').classList.toggle('sel', cb.checked);
      updateCount();
    });
  });
}

function updateCount() {
  el('e-hint').textContent = `已选 ${selected.size} 个会话`;
  el('e-run').disabled = running || selected.size === 0;
  renderRangeBanner();
}

async function run() {
  if (running || !selected.size) return;
  const chats = sessions.filter(s => selected.has(s.username))
    .map(s => ({ chat: s.username, display: s.display }));
  const body = {
    mode: 'export',
    export_opts: {
      account,
      chats,
      format: fmtDrop.value,
      pack: packDrop.value,
      start: startPick.value || null,
      end: endPick.value || null,
      messages: el('e-msg').checked,
      media: el('e-media').checked,
      voice: el('e-voice').checked,
      avatars: el('e-ava').checked,
    },
  };
  running = true;
  updateCount();
  el('e-log').innerHTML = '';
  el('e-result').innerHTML = '';
  el('e-progress-card').style.display = 'block';
  el('e-bar').style.width = '0%';
  el('e-bar').style.animation = '';
  try {
    const r = await fetch('/api/run', {
      method: 'POST', headers: { 'content-type': 'application/json' },
      body: JSON.stringify(body),
    });
    if (r.status === 409) throw new Error('已有任务在运行');
    if (!r.ok) {
      const j = await r.json().catch(() => ({}));
      throw new Error(j.error || '启动失败');
    }
    poll();
  } catch (e) {
    running = false;
    updateCount();
    el('e-log').innerHTML = `<div class="log-line">❌ ${esc(e.message)}</div>`;
  }
}

function poll() {
  pollTimer = setInterval(async () => {
    try {
      const job = await (await fetch('/api/job')).json();
      renderLogs(job.logs || []);
      // 进度条：解析最后一条 [export] N%
      let pct = 0;
      for (const entry of (job.logs || [])) {
        const msg = entry.length >= 4 ? entry[3] : entry[1];
        const mt = /\[export\] (\d+)%/.exec(msg);
        if (mt) pct = +mt[1];
      }
      el('e-bar').style.width = pct + '%';
      if (!job.running) {
        clearInterval(pollTimer);
        running = false;
        updateCount();
        el('e-bar').style.width = '100%';
        el('e-bar').style.animation = 'none';
        setTimeout(() => { el('e-progress-card').style.display = 'none'; }, 600);
        renderResult(job);
      }
    } catch (e) { /* 忽略单次轮询失败 */ }
  }, 600);
}

function renderLogs(logs) {
  const box = el('e-log');
  box.innerHTML = logs.map(entry => {
    const msg = entry.length >= 4 ? entry[3] : entry[1];
    return `<div class="log-line">${esc(msg)}</div>`;
  }).join('') || '<div class="log-line dim">…</div>';
  box.scrollTop = box.scrollHeight;
}

function renderResult(job) {
  const box = el('e-result');
  const lines = [];
  if (job.error) {
    lines.push(`<div class="res-row res-err">❌ 导出失败：${esc(job.error)}</div>`);
    const logs = job.logs || [];
    lines.push(`<details class="res-logs-detail" open><summary>📋 运行日志 (${logs.length} 条)</summary>`);
    for (const entry of logs) {
      const msg = entry.length >= 4 ? entry[3] : entry[1];
      lines.push(`<div class="log-line dim">${esc(msg)}</div>`);
    }
    lines.push('</details>');
    box.innerHTML = lines.join('');
    return;
  }
  const r = job.report || {};
  if (!r.sessions) {
    lines.push(`<div class="res-row res-err">⚠️ 无导出结果</div>`);
    box.innerHTML = lines.join('');
    return;
  }
  const dur = ((r.duration_ms || 0) / 1000).toFixed(1);
  lines.push(`<div class="res-row res-ok">✔ 导出完成：${r.ok_count}/${r.sessions.length} 个会话，`
    + `共 ${r.message_count} 条消息，图片 ${r.image_count || 0}，语音 ${r.voice_count || 0}，头像 ${r.avatar_count}（${dur}s）</div>`);
  if (r.total_dir) {
    lines.push(`<div class="res-meta">目录：<span class="mono">${esc(r.total_dir)}</span></div>`);
    lines.push(`<div class="res-actions"><a href="#" onclick="SX.openPath('${esc(r.total_dir)}');return false">📂 打开目录</a></div>`);
  }
  if (r.zip) {
    lines.push(`<div class="res-actions"><a href="/api/export/download?path=${encodeURIComponent(r.zip)}">📥 下载整体 ZIP</a></div>`);
  }
  if (r.zips && r.zips.length) {
    lines.push(`<div class="res-meta">共 ${r.zips.length} 个会话 ZIP（位于导出目录内）</div>`);
  }
  lines.push('<details class="res-logs-detail" open><summary>各会话结果</summary>');
  for (const s of r.sessions) {
    if (s.error) {
      lines.push(`<div class="res-meta res-err">✗ ${esc(s.display)}：${esc(s.error)}</div>`);
    } else {
      const dl = s.file ? ` · <a href="/api/export/download?path=${encodeURIComponent(s.file)}">下载文件</a>` : '';
      const media = (s.image_count || s.voice_count) ? ` · 图片 ${s.image_count || 0} · 语音 ${s.voice_count || 0}` : '';
      lines.push(`<div class="res-meta">✓ ${esc(s.display)} — ${s.message_count} 条消息${media}${dl}</div>`);
    }
  }
  lines.push('</details>');
  const logs = job.logs || [];
  lines.push(`<details class="res-logs-detail"><summary>📋 运行日志 (${logs.length} 条)</summary>`);
  for (const entry of logs) {
    const msg = entry.length >= 4 ? entry[3] : entry[1];
    lines.push(`<div class="log-line dim">${esc(msg)}</div>`);
  }
  lines.push('</details>');
  box.innerHTML = lines.join('');
}

export function destroy() {
  if (pollTimer) clearInterval(pollTimer);
}
export { init };
