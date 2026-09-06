/* MCP 配置页 —— 左侧会话列表 + 右侧配置/工具开关 + 调用日志 */
const { esc, fetchJSON, fmtTs } = window.SX;

function el(id) { return document.getElementById(id); }

let account = null;
let sessions = [];
let pollTimer = null;

async function loadSessions() {
  if (!account) return;
  const { sessions: ss } = await fetchJSON(
    `/api/chat/sessions?account=${encodeURIComponent(account)}`);
  sessions = ss;
  renderSessions('');
}

function renderSessions(kw) {
  const k = (kw || '').toLowerCase();
  const list = sessions.filter(s =>
    !k || s.display.toLowerCase().includes(k) || s.username.toLowerCase().includes(k));
  el('m-sessions').innerHTML = list.length ? list.map(s => {
    const initials = (s.display || s.username || '?').slice(0, 2).toUpperCase();
    return `
    <div class="sess" data-u="${esc(s.username)}">
      <div class="ava">${esc(initials)}</div>
      <span class="name">${esc(s.display)}</span>
      <span class="prev">${esc(s.preview || '')}</span>
      <span class="tm">${fmtTs(s.last_time)}</span>
    </div>`;
  }).join('') : '<div class="c-empty">没有匹配的会话</div>';
}

async function load() {
  try {
    const { accounts } = await fetchJSON('/api/chat/accounts');
    el('m-account').innerHTML = accounts.map(a =>
      `<option value="${esc(a.wxid)}">${esc(a.wxid)}</option>`).join('');
    if (accounts.length) {
      account = accounts[0].wxid;
      await loadSessions();
    } else {
      el('m-sessions').innerHTML = '<div class="c-empty">无解密产物</div>';
    }
  } catch (e) {
    el('m-sessions').innerHTML = `<div class="c-empty">${esc(e.message)}</div>`;
  }
  el('m-account').addEventListener('change', () => { account = el('m-account').value; loadSessions(); });
  el('m-search').addEventListener('input', () => renderSessions(el('m-search').value));

  try {
    const info = await fetchJSON('/api/mcp/info');
    const cmd = info.command;
    el('m-cmd').textContent = `"${cmd.command}" ${cmd.args.join(' ')}`;
    el('m-json').textContent = info.client_config;
    el('m-path').textContent = info.config_path;
    el('m-tools').innerHTML = info.tools.map(t => `
      <label class="m-tool">
        <input type="checkbox" data-t="${esc(t.name)}" ${t.enabled ? 'checked' : ''}>
        <div><b>${esc(t.name)}</b><span>${esc(t.description)}</span></div>
      </label>`).join('');
  } catch (e) {
    el('m-tools').innerHTML = `<div class="empty">${esc(e.message)}</div>`;
  }
  el('m-save').addEventListener('click', save);
  document.querySelectorAll('.copy-btn').forEach(b => {
    b.addEventListener('click', () => copy(b.dataset.target, b));
  });

  // 启动 MCP 调用日志轮询
  pollLogs();
}

async function save() {
  const tools = {};
  el('m-tools').querySelectorAll('input[type=checkbox]').forEach(cb => {
    tools[cb.dataset.t] = cb.checked;
  });
  await fetchJSON('/api/mcp/config', {
    method: 'POST', headers: { 'content-type': 'application/json' },
    body: JSON.stringify({ tools }),
  });
  el('m-save-msg').textContent = '✓ 已保存';
  setTimeout(() => { el('m-save-msg').textContent = ''; }, 3000);
}

async function copy(target, btn) {
  const text = el(target).textContent;
  try {
    await navigator.clipboard.writeText(text);
    btn.textContent = '✓ 已复制';
    setTimeout(() => { btn.textContent = '复制'; }, 1500);
  } catch (e) { /* 忽略 */ }
}

// ── MCP 调用日志 ───────────────────────────────────────────────

async function pollLogs() {
  if (pollTimer) clearInterval(pollTimer);
  const box = el('m-log');
  const render = (lines) => {
    box.innerHTML = lines.map(ln => {
      // 级别着色
      let cls = 'log-line';
      if (ln.includes('[ERROR]')) cls += ' err';
      else if (ln.includes('完成')) cls += ' ok';
      else if (ln.includes('调用')) cls += '';
      return `<div class="${cls}">${esc(ln)}</div>`;
    }).join('') || '<div class="log-line dim">暂无日志…</div>';
    box.scrollTop = box.scrollHeight;
  };
  const fetchLogs = async () => {
    try {
      const d = await fetchJSON('/api/mcp/logs?limit=200');
      render(d.logs || []);
      el('m-log-meta').textContent = `日志文件: ${d.path || ''} · 共 ${d.total || 0} 行（最近）`;
    } catch (e) {
      box.innerHTML = `<div class="log-line">获取日志失败: ${esc(e.message)}</div>`;
    }
  };
  await fetchLogs();
  pollTimer = setInterval(fetchLogs, 2000);
}

export async function init() { await load(); }
export function destroy() { if (pollTimer) clearInterval(pollTimer); }
