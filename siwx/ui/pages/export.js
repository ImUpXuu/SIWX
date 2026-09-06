/* 导出页 —— 选聊天/时间范围/勾选内容/格式 → 进度 → 结果 + 日志 */
const { esc, fetchJSON, startJob } = window.SX;

function el(id) { return document.getElementById(id); }

let chatPreset = null;

async function init() {
  try { chatPreset = JSON.parse(sessionStorage.getItem('siwx-export-preset') || 'null'); }
  catch (e) { chatPreset = null; }
  try {
    const { accounts } = await fetchJSON('/api/chat/accounts');
    el('e-account').innerHTML = accounts.map(a =>
      `<option value="${esc(a.wxid)}">${esc(a.wxid)}</option>`).join('');
    if (chatPreset && accounts.some(a => a.wxid === chatPreset.account)) {
      el('e-account').value = chatPreset.account;
    }
    if (accounts.length) await loadChats();
    el('e-account').addEventListener('change', () => { chatPreset = null; loadChats(); });
  } catch (e) {
    el('e-chat').innerHTML = `<option>${esc(e.message)}</option>`;
  }
  el('e-search').addEventListener('input', () => applyChatFilter(el('e-search').value));
  el('e-run').addEventListener('click', run);
}

async function loadChats() {
  const acc = el('e-account').value;
  el('e-chat').innerHTML = '<option>加载会话…</option>';
  const { sessions } = await fetchJSON(
    `/api/chat/sessions?account=${encodeURIComponent(acc)}`);
  window._eSessions = sessions;
  applyChatFilter(el('e-search').value);
}

function applyChatFilter(kw) {
  const kwL = (kw || '').toLowerCase();
  const ss = (window._eSessions || []).filter(s =>
    !kwL || s.display.toLowerCase().includes(kwL) || s.username.toLowerCase().includes(kwL));
  el('e-chat').innerHTML = ss.length ? ss.map(s =>
    `<option value="${esc(s.username)}" data-display="${esc(s.display)}">` +
    `${esc(s.display)}（${s.msg_count} 条）</option>`).join('')
    : '<option>没有匹配的会话</option>';
  if (chatPreset && ss.some(s => s.username === chatPreset.chat)) {
    el('e-chat').value = chatPreset.chat;
    sessionStorage.removeItem('siwx-export-preset');
    chatPreset = null;
  }
}

async function run() {
  const chatSel = el('e-chat');
  const opt = chatSel.selectedOptions[0];
  if (!opt || !chatSel.value) { window.alert('请先选择聊天'); return; }
  const body = {
    mode: 'export',
    export_opts: {
      account: el('e-account').value,
      chat: chatSel.value,
      display: opt.dataset.display || chatSel.value,
      format: el('e-fmt').value,
      pack: el('e-pack').value,
      start: el('e-start').value || null,
      end: el('e-end').value || null,
      messages: el('e-msg').checked,
      media: el('e-media').checked,
      avatars: el('e-ava').checked,
    },
  };
  el('e-run').disabled = true;
  el('e-progress').classList.remove('hidden');
  el('e-log').classList.remove('hidden');
  el('e-log').innerHTML = '';
  el('e-result').innerHTML = '';
  startJob(body, (logs) => renderLog(logs), (job) => {
    el('e-run').disabled = false;
    el('e-progress').classList.add('hidden');
    renderResult(job);
  });
}

function renderLog(logs) {
  const box = el('e-log');
  box.innerHTML = logs.map(([, m]) => `<div class="log-line">${esc(m)}</div>`)
    .join('') || '<div class="log-line dim">…</div>';
  box.scrollTop = box.scrollHeight;
}

function renderResult(job) {
  const box = el('e-result');
  const lines = [];

  if (job.error) {
    // 失败：显示错误 + 全部日志
    lines.push(`<div class="res-row res-err">❌ 导出失败：${esc(job.error)}</div>`);
    lines.push(`<div class="res-logs"><b>运行日志：</b></div>`);
    for (const [, m] of job.logs || []) {
      lines.push(`<div class="log-line dim">${esc(m)}</div>`);
    }
    box.innerHTML = lines.join('');
    return;
  }

  const r = job.report || {};
  if (!r || !r.format) {
    // 没有报告（异常）
    lines.push(`<div class="res-row res-err">⚠️ 无导出结果</div>`);
    for (const [, m] of job.logs || []) {
      lines.push(`<div class="log-line dim">${esc(m)}</div>`);
    }
    box.innerHTML = lines.join('');
    return;
  }

  // 成功
  const dur = r.duration_ms ?? '?';
  lines.push(`<div class="res-row res-ok">✔ 导出完成（${esc(String(dur))} ms）</div>`);
  lines.push(`<div class="res-meta">`);
  lines.push(`消息 ${esc(String(r.message_count ?? 0))} 条`);
  if (r.media_count) lines.push(` · 媒体 ${esc(String(r.media_count))} 张`);
  if (r.avatar_count) lines.push(` · 头像 ${esc(String(r.avatar_count))} 个`);
  lines.push(` · 格式 ${esc(r.format.toUpperCase())}</div>`);
  if (r.export_dir) lines.push(`<div class="res-meta">目录：<span class="mono">${esc(r.export_dir)}</span></div>`);
  if (r.zip) {
    const dl = encodeURIComponent(r.zip);
    lines.push(`<div class="res-actions"><a href="/api/export/download?path=${dl}">📥 下载 ZIP</a> · ` +
      `<a href="#" onclick="openDir('${esc(r.export_dir)}');return false">📂 打开目录</a></div>`);
  }
  // 始终显示日志（含媒体解密详情）
  lines.push(`<div class="res-logs"><b>运行日志：</b></div>`);
  for (const [, m] of job.logs || []) {
    lines.push(`<div class="log-line dim">${esc(m)}</div>`);
  }
  box.innerHTML = lines.join('');
}

function openDir(path) {
  fetch('/api/export/open', {
    method: 'POST', headers: { 'content-type': 'application/json' },
    body: JSON.stringify({ path }),
  });
}

export function destroy() { /* 无常驻定时器 */ }
export { init };
