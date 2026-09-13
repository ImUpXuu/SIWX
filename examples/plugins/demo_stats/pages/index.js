/* 插件页脚本：导出 init/destroy，与内置页面的模块契约完全一致 */
let timer = null;

export async function init(view) {
  const refreshBtn = view.querySelector('#ds-refresh');
  const when = view.querySelector('#ds-when');
  const tbody = view.querySelector('#ds-accounts tbody');
  const cfgEl = view.querySelector('#ds-config');

  async function load() {
    try {
      const s = await (await fetch('/api/status')).json();
      const rows = s.accounts || [];
      tbody.innerHTML = rows.length
        ? rows.map(a => `<tr><td>${a.wxid}</td><td>${a.db_count}</td><td>${a.keys_cached}</td></tr>`).join('')
        : '<tr><td colspan="3" class="dim">暂无账号</td></tr>';
      if (when) when.textContent = '更新于 ' + new Date().toLocaleTimeString();
    } catch (e) {
      tbody.innerHTML = `<tr><td colspan="3" class="dim">读取失败：${e.message}</td></tr>`;
    }
  }

  async function loadConfig() {
    try {
      const j = await (await fetch('/api/plugins/config?plugin=demo_stats')).json();
      cfgEl.textContent = JSON.stringify(j.values || {}, null, 2);
    } catch (e) {
      cfgEl.textContent = '读取失败：' + e.message;
    }
  }

  refreshBtn.addEventListener('click', load);
  await Promise.all([load(), loadConfig()]);

  // 按插件设置里的间隔自动刷新（默认 30s）
  let every = 30;
  try {
    const j = await (await fetch('/api/plugins/config?plugin=demo_stats')).json();
    every = Number(j.values?.refresh_seconds) || 30;
  } catch (e) { /* 用默认值 */ }
  timer = setInterval(load, Math.max(5, every) * 1000);
}

export function destroy() {
  if (timer) { clearInterval(timer); timer = null; }
}
