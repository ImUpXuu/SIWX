/* 导出页 —— 选聊天/时间范围/勾选内容/格式 → 进度 → 下载 */
const { esc, fetchJSON, startJob, renderLog: renderJobLog } = window.SX;

function el(id) { return document.getElementById(id); }

let chatPreset = null;   // 从聊天页“📦 导出”跳转时带来的预选

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
    `<option value="${esc(s.username)}" data-display="${esc(s.display)}">" +
    "${esc(s.display)}（${s.msg_count} 条）</option>`).join('')
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
  startJob(body, (logs) => renderJobLog(el, logs), (job) => {
    el('e-run').disabled = false;
    el('e-progress').classList.add('hidden');
    if (job.error) {
      el('e-result').innerHTML = `<div class="empty">❌ ${esc(job.error)}</div>`;
      return;
    }
    const r = job.report || {};
    const dl = r.zip ? encodeURIComponent(r.zip) : '';
    const openPath = encodeURIComponent(r.export_dir || '');
    const openBtn = `<a href="#" onclick="fetch('/api/export/open',{method:'POST',headers:{'content-type':'application/json'},body:JSON.stringify({path:'${openPath}'})});return false"> OPEN DIR </a>`;
    el('e-result').innerHTML = `
      <div class="e-done">
        ✔ 导出完成（${r.duration_ms} ms）<br>
        消息 ${r.message_count} 条 · 媒体 ${r.media_count} 张 · 头像 ${r.avatar_count} 个<br>
        目录：<span class="mono">${esc(r.export_dir)}</span><br>
        <a href="/api/export/download?path=${dl}">📥 下载 ZIP</a>
      </div>`;
  });
}

function renderExportLog(el_, logs) {
  const box = el('e-log');
  box.innerHTML = logs.map(([t, m]) => {
    const p = m.match(/\[(export|harvest|keystore|cipher)\] (\d+)%/);
    return `<div class="log-line">${esc(m)}</div>`;
  }).join('') || '<div class="log-line dim">…</div>';
  box.scrollTop = box.scrollHeight;
}

export function destroy() { /* 无常驻定时器 */ }
export { init };
