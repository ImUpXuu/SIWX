/* 设置页 —— 版本与更新 / 缓存总览 / 清除 / 重新运行引导 */
const { esc, fetchJSON, go, resetSetup } = window.SX;

function el(id) { return id ? document.getElementById(id) : null; }

async function loadOverview() {
  const box = el('s-overview');
  try {
    const o = await fetchJSON('/api/settings/overview');
    const rows = o.outputs.length ? o.outputs.map(x => `
      <div class="ov-row">
        <span>${esc(x.wxid)}</span>
        <span class="dim">${x.size_mb} MB · ${x.manifest ? '解密缓存 ✓' : '无缓存清单'}</span>
        <span class="pill pill-gray">明文产物</span>
      </div>`).join('')
      : '<div class="empty">还没有解密输出</div>';
    box.innerHTML = `
      <div class="ov-row">
        <span>密钥库（DPAPI 加密）</span>
        <span class="dim">${esc(o.keystore.path)}</span>
        <span class="pill pill-green">${o.keystore.count} 条密钥</span>
      </div>
      ${rows}`;
  } catch (e) {
    box.innerHTML = `<div class="empty">${esc(e.message)}</div>`;
  }
}

function fmtLastRun(ts) {
  if (!ts) return '未运行';
  const d = new Date(Number(ts) * 1000);
  return Number.isNaN(d.getTime()) ? '未运行' : d.toLocaleString();
}

async function loadAutoSync() {
  const status = el('s-auto-sync-status');
  try {
    const cfg = await fetchJSON('/api/settings/auto-sync');
    el('s-auto-sync-enabled').checked = !!cfg.enabled;
    el('s-auto-sync-interval').value = cfg.interval_minutes || 30;
    const ok = cfg.last_ok === null || cfg.last_ok === undefined ? '' : (cfg.last_ok ? ' · 上次成功' : ' · 上次失败');
    status.textContent = `状态：${cfg.enabled ? '已开启' : '未开启'} · 间隔 ${cfg.interval_minutes || 30} 分钟 · 上次：${fmtLastRun(cfg.last_run)}${ok} · ${cfg.last_message || ''}`;
  } catch (e) {
    status.textContent = e.message;
  }
}

async function saveAutoSync() {
  const enabled = el('s-auto-sync-enabled').checked;
  const interval = Math.max(1, Math.min(Number(el('s-auto-sync-interval').value || 30), 1440));
  const cfg = await fetchJSON('/api/settings/auto-sync', {
    method: 'POST', headers: { 'content-type': 'application/json' },
    body: JSON.stringify({ enabled, interval_minutes: interval }),
  });
  el('s-auto-sync-interval').value = cfg.interval_minutes || interval;
  await loadAutoSync();
}

async function loadVersion() {
  const box = el('s-version');
  const meta = el('s-version-meta');
  try {
    const cur = await fetchJSON('/api/update/current');
    const chk = await fetchJSON(`/api/update/check?_=${Date.now()}`);
    const lines = [];
    lines.push(`<div class="ov-row"><span>当前版本</span><span class="pill pill-green">v${esc(cur.version)}</span></div>`);
    lines.push(`<div class="ov-row"><span>运行模式</span><span class="dim">${cur.frozen ? '打包产物' : '源码运行'}</span></div>`);
    lines.push(`<div class="ov-row"><span>平台</span><span class="dim">${esc(cur.platform)}</span></div>`);

    if (chk.has_update && chk.remote) {
      const rv = chk.remote.version;
      const notes = esc((chk.remote.notes || "").slice(0, 200));
      lines.push(`<div class="ov-row"><span>最新版本</span><span class="pill pill-amber">v${esc(rv)} ↗</span></div>`);
      lines.push(`<div class="ov-row"><span>更新内容</span><span class="dim">${notes}</span></div>`);
      if (chk.update_available) {
        lines.push(`<div class="ov-row"><button class="btn btn-primary" id="s-do-update">⬆ 更新到 v${esc(rv)}</button></div>`);
      } else {
        lines.push(`<div class="ov-row"><span class="dim">源码运行模式，请手动 git pull 更新</span></div>`);
      }
    } else {
      lines.push(`<div class="ov-row"><span>状态</span><span class="pill pill-green">✓ 已是最新</span></div>`);
    }
    box.innerHTML = lines.join('');
    meta.textContent = `打包产物: ${cur.frozen} · 平台: ${cur.platform}`;

    const btn = el('s-do-update');
    if (btn) {
      btn.addEventListener('click', async () => {
        if (!window.confirm('确定更新？应用将自动重启。')) return;
        btn.disabled = true;
        btn.textContent = '⏳ 更新中...';
        try {
          const r = await fetchJSON('/api/update/do', {
            method: 'POST', headers: { 'content-type': 'application/json' },
            body: JSON.stringify(chk.remote || {}),
          });
          if (r.ok) {
            window.alert(r.message || '更新已启动，应用将重启');
          } else {
            window.alert('更新失败: ' + (r.message || '未知错误'));
            btn.disabled = false;
            btn.textContent = `⬆ 更新到 v${esc(chk.remote?.version || '')}`;
          }
        } catch (e) {
          window.alert('更新失败: ' + e.message);
          btn.disabled = false;
        }
      });
    }
  } catch (e) {
    box.innerHTML = `<div class="empty">${esc(e.message)}</div>`;
    meta.textContent = '版本检测失败';
  }
}

/* ── 插件设置：由 /api/plugins/settings 下发 schema，前端自动渲染 ────── */

const PLUGIN_TYPE_LABEL = {
  str: '文本', int: '整数', float: '小数', bool: '开关',
  choice: '单选', text: '多行文本', list: '列表',
};

/** 单个设置项的输入控件（按 type 分派） */
function pluginControl(plugin, it) {
  const id = `s-plg-${plugin}-${it.key}`;
  const val = it.value === undefined || it.value === null ? it.default : it.value;

  if (it.type === 'bool') {
    return `<label class="f-chk"><input type="checkbox" data-plg="${esc(plugin)}" data-key="${esc(it.key)}"
              data-type="bool" id="${id}"${val ? ' checked' : ''}> ${val ? '已开启' : '已关闭'}</label>`;
  }
  if (it.type === 'choice') {
    const opts = (it.choices || []).map(c =>
      `<option value="${esc(c)}"${String(c) === String(val) ? ' selected' : ''}>${esc(c)}</option>`).join('');
    return `<select class="f-input" id="${id}" data-plg="${esc(plugin)}" data-key="${esc(it.key)}"
              data-type="choice">${opts}</select>`;
  }
  if (it.type === 'int' || it.type === 'float') {
    const step = it.type === 'float' ? ' step="any"' : '';
    const mn = it.min === null || it.min === undefined ? '' : ` min="${it.min}"`;
    const mx = it.max === null || it.max === undefined ? '' : ` max="${it.max}"`;
    return `<input class="f-input" type="number"${step}${mn}${mx} id="${id}"
              data-plg="${esc(plugin)}" data-key="${esc(it.key)}" data-type="${it.type}"
              value="${esc(val ?? '')}" style="width:auto;min-width:110px">`;
  }
  if (it.type === 'text') {
    return `<textarea class="f-input" id="${id}" data-plg="${esc(plugin)}" data-key="${esc(it.key)}"
              data-type="text" rows="3">${esc(val ?? '')}</textarea>`;
  }
  return `<input class="f-input" type="text" id="${id}" data-plg="${esc(plugin)}"
            data-key="${esc(it.key)}" data-type="str" value="${esc(val ?? '')}">`;
}

/** 读取某插件表单当前值 */
function readPluginValues(plugin) {
  const out = {};
  document.querySelectorAll(`[data-plg="${CSS.escape(plugin)}"]`).forEach(inp => {
    const t = inp.dataset.type || 'str';
    if (t === 'bool') out[inp.dataset.key] = inp.checked;
    else if (t === 'int') out[inp.dataset.key] = parseInt(inp.value, 10);
    else if (t === 'float') out[inp.dataset.key] = parseFloat(inp.value);
    else out[inp.dataset.key] = inp.value;
  });
  return out;
}

async function loadPluginSettings() {
  const card = el('s-plugins-card');
  const box = el('s-plugins');
  const hint = el('s-plugins-hint');
  let data;
  try {
    data = await fetchJSON('/api/plugins/settings');
  } catch (e) {
    card.hidden = true;
    return;                      // 插件系统不可用 → 整卡隐藏，不影响宿主
  }
  const plugins = (data && data.plugins) || [];
  if (!plugins.length) { card.hidden = true; return; }

  card.hidden = false;
  const total = plugins.reduce((n, p) => n + p.items.length, 0);
  hint.textContent = `${plugins.length} 个插件 · ${total} 项配置`;

  box.innerHTML = `
    <div id="s-plugins-list">
      ${plugins.map(p => {
        // 按 group 归组，保留声明顺序
        const groups = [];
        p.items.forEach(it => {
          const g = it.group || '常规';
          let bucket = groups.find(x => x.name === g);
          if (!bucket) { bucket = { name: g, items: [] }; groups.push(bucket); }
          bucket.items.push(it);
        });
        return `
        <div class="plg-block">
          <div class="plg-head">
            <b>${esc(p.plugin)}</b>
            <span class="pill pill-gray">v${esc(p.version || '?')}</span>
            <span class="dim">${esc(p.description || '')}</span>
          </div>
          ${groups.map(g => `
            ${groups.length > 1 ? `<div class="plg-group">${esc(g.name)}</div>` : ''}
            <div class="s-actions">
              ${g.items.map(it => `
                <div class="s-item">
                  <b>${esc(it.label || it.key)}
                     <span class="dim" style="font-weight:400">· ${esc(PLUGIN_TYPE_LABEL[it.type] || it.type)}</span></b>
                  ${it.help ? `<span class="dim">${esc(it.help)}</span>` : ''}
                  ${pluginControl(p.plugin, it)}
                </div>`).join('')}
            </div>`).join('')}
          <div class="s-inline" style="margin-top:10px">
            <button class="btn btn-primary" data-plg-save="${esc(p.plugin)}">保存</button>
            <span class="dim" data-plg-status="${esc(p.plugin)}"></span>
          </div>
        </div>`;
      }).join('')}
    </div>`;

  // 绑定保存
  box.querySelectorAll('[data-plg-save]').forEach(btn => {
    btn.addEventListener('click', async () => {
      const plugin = btn.dataset.plgSave;
      const status = box.querySelector(`[data-plg-status="${CSS.escape(plugin)}"]`);
      btn.disabled = true;
      try {
        const r = await fetchJSON('/api/plugins/settings', {
          method: 'POST', headers: { 'content-type': 'application/json' },
          body: JSON.stringify({ plugin, values: readPluginValues(plugin) }),
        });
        status.textContent = '✓ 已保存';
        status.style.color = 'var(--ok, #16a34a)';
        // 回填：服务端可能对越界值做了夹紧
        Object.entries(r.values || {}).forEach(([k, v]) => {
          const inp = box.querySelector(`[data-plg="${CSS.escape(plugin)}"][data-key="${CSS.escape(k)}"]`);
          if (!inp) return;
          const t = inp.dataset.type;
          if (t === 'bool') { inp.checked = !!v; }
          else if (t === 'int' || t === 'float') { inp.value = v != null ? v : ''; }
          else { inp.value = v != null ? v : ''; }
        });
      } catch (e) {
        status.textContent = '✗ ' + e.message;
        status.style.color = 'var(--danger, #dc2626)';
      } finally {
        btn.disabled = false;
      }
    });
  });

  // bool 开关的文案跟随状态；数字/文本改动后清除上次提示
  box.querySelectorAll('[data-plg]').forEach(inp => {
    inp.addEventListener('change', () => {
      if (inp.dataset.type === 'bool') {
        inp.parentElement.lastChild.textContent = inp.checked ? ' 已开启' : ' 已关闭';
      }
      const st = box.querySelector(`[data-plg-status="${CSS.escape(inp.dataset.plg)}"]`);
      if (st) st.textContent = '';
    });
  });
}

function confirmThen(msg, fn) {
  if (window.confirm(msg)) fn();
}

export async function init() {
  await loadVersion();
  await loadOverview();
  await loadAutoSync();
  await loadPluginSettings();

  el('s-auto-sync-save').addEventListener('click', async () => {
    try {
      await saveAutoSync();
      window.alert('自动刷新设置已保存');
    } catch (e) {
      window.alert('保存失败: ' + e.message);
    }
  });

  el('s-clear-output').addEventListener('click', () =>
    confirmThen('确定删除全部解密输出？\n（密钥保留，重跑解密即可恢复）', async () => {
      await fetchJSON('/api/settings/clear', {
        method: 'POST', headers: { 'content-type': 'application/json' },
        body: JSON.stringify({ kind: 'output' }),
      });
      window.alert('已清除解密输出');
      loadOverview();
    }));

  el('s-clear-keys').addEventListener('click', () =>
    confirmThen('确定清除密钥库？\n下次提取需要微信在线重新收割。', async () => {
      await fetchJSON('/api/settings/clear', {
        method: 'POST', headers: { 'content-type': 'application/json' },
        body: JSON.stringify({ kind: 'keys' }),
      });
      window.alert('已清除密钥库');
      loadOverview();
    }));

  el('s-rerun-guide').addEventListener('click', () => {
    resetSetup();
    go('#/guide');
  });

  // ── 日志模式 ──────────────────────────────────────────
  async function loadLogSettings() {
    try {
      const s = await fetchJSON('/api/logs/settings');
      el('s-log-level').value = s.level || 'rough';
    } catch (e) { /* ignore */ }
  }

  el('s-log-level').addEventListener('change', async (e) => {
    await fetchJSON('/api/logs/settings', {
      method: 'POST', headers: { 'content-type': 'application/json' },
      body: JSON.stringify({ level: e.target.value }),
    });
    window.alert(`日志模式已切换为: ${e.target.value === 'detailed' ? '详细' : '粗略'}`);
  });

  // ── 脱敏日志导出 ──────────────────────────────────────
  el('s-export-log').addEventListener('click', async () => {
    const desensitize = el('s-log-desensitize').checked;
    const url = `/api/logs/export?desensitize=${desensitize ? '1' : '0'}`;
    try {
      const r = await fetch(url);
      const blob = await r.blob();
      const a = document.createElement('a');
      a.href = URL.createObjectURL(blob);
      a.download = `siwx_log_${Date.now()}.txt`;
      a.click();
      URL.revokeObjectURL(a.href);
    } catch (e) {
      window.alert('导出失败: ' + e.message);
    }
  });

  await loadLogSettings();
}

export function destroy() { /* no timers */ }
