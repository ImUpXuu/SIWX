/* MCP 配置页 —— 服务器信息 / 工具开关 / 调用日志 */
const { esc, fetchJSON, toast } = window.SX;

function el(id) { return document.getElementById(id); }

let pollTimer = null;

async function load() {
  try {
    const info = await fetchJSON('/api/mcp/info');
    const cmd = info.command;
    el('m-cmd').textContent = `"${cmd.command}" ${cmd.args.join(' ')}`;
    el('m-json').textContent = info.client_config;
    el('m-path').textContent = info.config_path;
    el('m-tools').innerHTML = info.tools.length
      ? info.tools.map(t => `
      <label class="m-tool">
        <input type="checkbox" data-t="${esc(t.name)}" ${t.enabled ? 'checked' : ''}>
        <div><b>${esc(t.name)}</b><span>${esc(t.description)}</span></div>
      </label>`).join('')
      : '<div class="empty">暂无注册工具</div>';
  } catch (e) {
    el('m-tools').innerHTML = `<div class="empty">${esc(e.message)}</div>`;
  }
  el('m-save').addEventListener('click', save);
  el('m-all').addEventListener('click', () => setAllTools(true));
  el('m-none').addEventListener('click', () => setAllTools(false));
  document.querySelectorAll('.copy-btn').forEach(b => {
    b.addEventListener('click', () => copy(b.dataset.target, b));
  });

  // 启动 MCP 调用日志轮询
  pollLogs();
}

function setAllTools(on) {
  el('m-tools').querySelectorAll('input[type=checkbox]').forEach(cb => {
    cb.checked = on;
  });
}

async function save() {
  const tools = {};
  el('m-tools').querySelectorAll('input[type=checkbox]').forEach(cb => {
    tools[cb.dataset.t] = cb.checked;
  });
  const btn = el('m-save');
  btn.disabled = true;
  try {
    await fetchJSON('/api/mcp/config', {
      method: 'POST', headers: { 'content-type': 'application/json' },
      body: JSON.stringify({ tools }),
    });
    const msg = el('m-save-msg');
    if (msg) {
      msg.textContent = '已保存';
      setTimeout(() => { if (msg.isConnected) msg.textContent = ''; }, 3000);
    }
    toast('MCP 工具配置已保存');
  } catch (e) {
    toast('保存失败：' + e.message, true);
  } finally {
    btn.disabled = false;
  }
}

async function copy(target, btn) {
  const text = el(target)?.textContent ?? '';
  const ok = await window.SX.copyText(text);
  if (!btn.isConnected) return;
  if (ok) {
    btn.textContent = '已复制';
    setTimeout(() => { if (btn.isConnected) btn.textContent = '复制'; }, 1500);
  } else {
    toast('复制失败，请手动选择', true);
  }
}

// ── MCP 调用日志 ───────────────────────────────────────────────

async function pollLogs() {
  if (pollTimer) clearInterval(pollTimer);
  const box = el('m-log');
  const render = (lines) => {
    // 用户上翻阅读时不再强制拽回底部（距底 >40px 视为正在阅读）
    const stick = box.scrollHeight - box.scrollTop - box.clientHeight < 40;
    box.innerHTML = lines.map(ln => {
      let cls = 'log-line';
      if (ln.includes('[ERROR]')) cls += ' err';
      else if (ln.includes('完成')) cls += ' ok';
      return `<div class="${cls}">${esc(ln)}</div>`;
    }).join('') || '<div class="log-line dim">暂无日志…</div>';
    if (stick) box.scrollTop = box.scrollHeight;
  };
  const fetchLogs = async () => {
    if (document.hidden) return;             // 后台标签页暂停轮询
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
