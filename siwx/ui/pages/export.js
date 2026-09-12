/* 导出页 —— 多选会话导出：左侧勾选列表 + 右侧配置 + 底部日志/进度 */
const { esc, fetchJSON, fmtTs } = window.SX;

function el(id) { return document.getElementById(id); }

let account = null;
let sessions = [];
let selected = new Set();       // 已勾选 username 集合
let chatPreset = null;
let running = false;
let pollTimer = null;

function match(s, kw) {
  const k = (kw || '').toLowerCase();
  return !k || s.display.toLowerCase().includes(k) || s.username.toLowerCase().includes(k);
}

async function init() {
  try { chatPreset = JSON.parse(sessionStorage.getItem('siwx-export-preset') || 'null'); }
  catch (e) { chatPreset = null; }
  try {
    const { accounts } = await fetchJSON('/api/chat/accounts');
    el('e-account').innerHTML = accounts.map(a =>
      `<option value="${esc(a.wxid)}">${esc(a.wxid)}</option>`).join('');
    if (accounts.length) {
      if (chatPreset && accounts.some(a => a.wxid === chatPreset.account)) {
        el('e-account').value = chatPreset.account;
      }
      account = el('e-account').value;
      await loadSessions();
    } else {
      el('e-sessions').innerHTML = '<div class="c-empty">还没有解密产物 — 请先完成引导</div>';
    }
  } catch (e) {
    el('e-sessions').innerHTML = `<div class="c-empty">${esc(e.message)}</div>`;
  }
  el('e-account').addEventListener('change', () => {
    account = el('e-account').value;
    selected.clear();
    loadSessions();
  });
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
      <span class="tm">${fmtTs(s.last_time)}</span>
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
      format: el('e-fmt').value,
      pack: el('e-pack').value,
      start: el('e-start').value || null,
      end: el('e-end').value || null,
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
