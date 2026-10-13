/* 设置页 —— 版本与更新 / 数据源管理 / 缓存总览 / 清除 / 隐私与免责声明 */
import { createDropdown } from '/widgets.js?v=2026100601';

const { esc, fetchJSON, copyText, toast, startJob, renderLog } = window.SX;

function el(id) { return id ? document.getElementById(id) : null; }

let logLevelDrop = null;
const plgChoiceDrop = new Map();   // 挂载元素 → choice 类插件设置的下拉实例

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
    // 切页后视图已卸载，在飞回调直接放弃（否则对 null 赋值报错）
    if (!status || !el('s-auto-sync-enabled')) return;
    el('s-auto-sync-enabled').checked = !!cfg.enabled;
    el('s-auto-sync-interval').value = cfg.interval_minutes || 30;
    const ok = cfg.last_ok === null || cfg.last_ok === undefined ? '' : (cfg.last_ok ? ' · 上次成功' : ' · 上次失败');
    // 从未运行时 last_message 只是重复「未运行」，不再拼接
    const lm = cfg.last_run && cfg.last_message ? ` · ${cfg.last_message}` : '';
    status.textContent = `状态：${cfg.enabled ? '已开启' : '未开启'} · 间隔 ${cfg.interval_minutes || 30} 分钟 · 上次：${fmtLastRun(cfg.last_run)}${ok}${lm}`;
  } catch (e) {
    status.textContent = e.message;
  }
}

async function saveAutoSync() {
  const enabled = el('s-auto-sync-enabled').checked;
  const raw = Number(el('s-auto-sync-interval').value || 30);
  const interval = Math.max(1, Math.min(raw, 1440));
  if (raw !== interval) toast(`间隔超出范围，已按 ${interval} 分钟保存`);
  const cfg = await fetchJSON('/api/settings/auto-sync', {
    method: 'POST', headers: { 'content-type': 'application/json' },
    body: JSON.stringify({ enabled, interval_minutes: interval }),
  });
  if (!el('s-auto-sync-interval')) return;   // 切页后视图已卸载
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
        lines.push(`<div class="ov-row"><button class="btn btn-primary" id="s-do-update">更新到 v${esc(rv)}</button></div>`);
      } else {
        lines.push(`<div class="ov-row"><span class="dim">源码运行模式，请手动 git pull 更新</span></div>`);
      }
    } else {
      lines.push(`<div class="ov-row"><span>状态</span><span class="pill pill-green">已是最新</span></div>`);
    }
    box.innerHTML = lines.join('');
    meta.textContent = '';               // 运行模式/平台已在下方逐行展示，不再重复开发者术语

    const btn = el('s-do-update');
    if (btn) {
      btn.addEventListener('click', async () => {
        if (!window.confirm('确定更新？应用将自动重启。')) return;
        btn.disabled = true;
        btn.textContent = '更新中…';
        try {
          const r = await fetchJSON('/api/update/do', {
            method: 'POST', headers: { 'content-type': 'application/json' },
            body: JSON.stringify(chk.remote || {}),
          });
          if (r.ok) {
            toast(r.message || '更新已启动，应用将重启');
          } else {
            toast('更新失败：' + (r.message || '未知错误'), true);
            btn.disabled = false;
            btn.textContent = `更新到 v${esc(chk.remote?.version || '')}`;
          }
        } catch (e) {
          toast('更新失败：' + e.message, true);
          btn.disabled = false;
          btn.textContent = `更新到 v${chk.remote?.version || ''}`;
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
              data-type="bool" id="${id}"${val ? ' checked' : ''}> <span data-plg-bool-label>${val ? '已开启' : '已关闭'}</span></label>`;
  }
  if (it.type === 'choice') {
    // 先渲染占位挂载点，loadPluginSettings 渲染完 innerHTML 后统一水合成自绘下拉
    return `<div class="f-input sxw-mount" id="${id}" data-plg="${esc(plugin)}" data-key="${esc(it.key)}"
              data-type="choice" data-choices="${esc(JSON.stringify(it.choices || []))}"
              data-value="${esc(val ?? '')}"></div>`;
  }
  if (it.type === 'int' || it.type === 'float') {
    const step = it.type === 'float' ? ' step="any"' : '';
    const mn = it.min === null || it.min === undefined ? '' : ` min="${it.min}"`;
    const mx = it.max === null || it.max === undefined ? '' : ` max="${it.max}"`;
    return `<input class="f-input" type="number"${step}${mn}${mx} id="${id}"
              data-plg="${esc(plugin)}" data-key="${esc(it.key)}" data-type="${it.type}"
              value="${esc(val ?? '')}">`;
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
    else if (t === 'choice') {
      const drop = plgChoiceDrop.get(inp);
      out[inp.dataset.key] = drop ? drop.value : (inp.dataset.value ?? '');
    }
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
          <div class="s-inline plg-save-row">
            <button class="btn btn-primary" data-plg-save="${esc(p.plugin)}">保存</button>
            <span class="dim" data-plg-status="${esc(p.plugin)}"></span>
          </div>
        </div>`;
      }).join('')}
    </div>`;

  // choice 类插件配置水合为自绘下拉
  box.querySelectorAll('.sxw-mount[data-type="choice"]').forEach(mount => {
    let choices = [];
    try { choices = JSON.parse(mount.dataset.choices || '[]'); } catch (e) { /* 忽略 */ }
    const drop = createDropdown({
      options: choices.map(c => ({ value: c, label: c })),
      value: mount.dataset.value || null,
      label: mount.dataset.key,
    });
    plgChoiceDrop.set(mount, drop);
    mount.appendChild(drop.el);
    drop.onChange = () => {
      const st = box.querySelector(`[data-plg-status="${CSS.escape(mount.dataset.plg)}"]`);
      if (st) st.textContent = '';
    };
  });

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
        status.textContent = '已保存';
        status.style.color = 'var(--accent-fg)';
        // 回填：服务端可能对越界值做了夹紧
        Object.entries(r.values || {}).forEach(([k, v]) => {
          const inp = box.querySelector(`[data-plg="${CSS.escape(plugin)}"][data-key="${CSS.escape(k)}"]`);
          if (!inp) return;
          const t = inp.dataset.type;
          if (t === 'bool') { inp.checked = !!v; }
          else if (t === 'choice') { const drop = plgChoiceDrop.get(inp); if (drop) drop.value = v; }
          else { inp.value = v != null ? v : ''; }
        });
      } catch (e) {
        status.textContent = '保存失败：' + e.message;
        status.style.color = 'var(--danger)';
      } finally {
        btn.disabled = false;
      }
    });
  });

  // bool 开关的文案跟随状态；数字/文本改动后清除上次提示
  box.querySelectorAll('[data-plg]').forEach(inp => {
    inp.addEventListener('change', () => {
      if (inp.dataset.type === 'bool') {
        const label = inp.parentElement.querySelector('[data-plg-bool-label]');
        if (label) label.textContent = inp.checked ? '已开启' : '已关闭';
      }
      const st = box.querySelector(`[data-plg-status="${CSS.escape(inp.dataset.plg)}"]`);
      if (st) st.textContent = '';
    });
  });
}

/* ── 数据源管理：账号列表 / 手动目录 / 重新提取（原引导页的维护能力）── */

let dsRunning = false;
let dsStop = null;                 // startJob 的 stop 句柄，destroy 时停掉轮询

function dsBadge(cached, total) {
  if (!total) return '<span class="badge badge-none">无数据</span>';
  if (cached >= total) return `<span class="badge badge-full">缓存 ✓ ${cached}/${total}</span>`;
  if (cached > 0) return `<span class="badge badge-part">缓存 ${cached}/${total}</span>`;
  return '<span class="badge badge-none">未提取</span>';
}

async function loadDatasources() {
  const box = el('s-ds-accounts');
  if (!box) return;
  try {
    const s = await fetchJSON('/api/status');
    const accs = s.accounts || [];
    if (!accs.length) {
      box.innerHTML = '<div class="empty">未发现数据目录 —— 可用下方「手动添加数据目录」指定</div>';
      return;
    }
    box.innerHTML = accs.map((a, i) => `
      <div class="acc" data-i="${i}">
        <div class="avatar">${esc((a.wxid || '').replace(/^wxid_/, '').slice(0, 2).toUpperCase())}</div>
        <div class="acc-main"><b>${esc(a.wxid)}</b><span>${a.db_count || '?'} 个数据库</span></div>
        ${dsBadge(a.keys_cached, a.total_salts)}
        <button class="btn btn-sm" data-ds-run="${i}">重新提取</button>
      </div>`).join('');
    box.querySelectorAll('[data-ds-run]').forEach(btn => {
      btn.addEventListener('click', () => runExtract(accs[Number(btn.dataset.dsRun)], btn));
    });
  } catch (e) {
    box.innerHTML = `<div class="empty">加载失败：${esc(e.message)}</div>`;
  }
}

async function runExtract(acc, btn) {
  if (dsRunning || !acc) {
    if (dsRunning) toast('已有提取任务在运行，请等它结束', true);
    return;
  }
  dsRunning = true;
  const runBox = el('s-ds-run'), bar = el('s-ds-bar'), logBox = el('s-ds-log'), resBox = el('s-ds-result');
  runBox.hidden = false;
  bar.style.width = '0';
  logBox.innerHTML = '';
  resBox.innerHTML = '';
  btn.disabled = true;
  const oldText = btn.textContent;
  btn.textContent = '提取中…';
  try {
    await new Promise(resolve => {
      dsStop = startJob({ mode: 'auto', db_dir: acc.db_dir },
        logs => { if (el('s-ds-log')) renderLog(logBox, logs); },
        job => {
          if (job.error) {
            resBox.innerHTML = `<span class="badge badge-none">提取失败：${esc(job.error)}</span>`;
            toast('提取失败：' + job.error, true);
          } else {
            const accs = (job.report && job.report.accounts) || [];
            const mine = accs.find(a => a.db_dir === acc.db_dir) || accs[0];
            if (mine) {
              const full = mine.verified >= mine.total_salts;
              const dec = mine.decrypt;
              resBox.innerHTML =
                `<span class="badge ${full ? 'badge-full' : 'badge-part'}">密钥 ${mine.verified}/${mine.total_salts}</span>` +
                (dec ? ` <span class="badge badge-full">解密 ${dec.ok} 库（缓存 ${dec.cached || 0}）</span>` : '');
            } else {
              resBox.innerHTML = '<span class="badge badge-full">完成</span>';
            }
            toast('提取并解密完成');
          }
          resolve();
        });
    });
  } finally {
    dsRunning = false;
    dsStop = null;
    bar.style.width = '0';
    runBox.hidden = true;
    btn.disabled = false;
    btn.textContent = oldText;
    loadDatasources();                     // 刷新缓存徽标
  }
}

function bindDsManual() {
  el('s-ds-manual-toggle').addEventListener('click', () => {
    el('s-ds-manual-box').classList.toggle('hidden');
  });
  el('s-ds-refresh').addEventListener('click', loadDatasources);
  el('s-ds-manual-add').addEventListener('click', async () => {
    const input = el('s-ds-manual-path'), msg = el('s-ds-manual-msg'), add = el('s-ds-manual-add');
    const path = input.value.trim();
    if (!path) {
      msg.textContent = '请输入路径';
      msg.className = 'g-manual-msg err';
      return;
    }
    add.disabled = true;
    add.textContent = '验证中…';
    msg.textContent = '';
    try {
      const r = await fetch('/api/discover/validate', {
        method: 'POST', headers: { 'content-type': 'application/json' },
        body: JSON.stringify({ path }),
      });
      const d = await r.json();
      if (d.ok) {
        const names = (d.accounts?.length ? d.accounts.map(a => a.wxid) : [d.wxid]).join('、');
        msg.textContent = `已保存 ${d.accounts?.length || 1} 个账号：${names}`;
        msg.className = 'g-manual-msg ok';
        input.value = '';
        loadDatasources();
      } else {
        msg.textContent = d.error;
        msg.className = 'g-manual-msg err';
      }
    } catch (e) {
      msg.textContent = `验证失败: ${e.message}`;
      msg.className = 'g-manual-msg err';
    } finally {
      add.disabled = false;
      add.textContent = '验证并保存';
    }
  });
}

function confirmThen(msg, fn) {
  if (window.confirm(msg)) fn();
}

export async function init() {
  // 四个加载互不依赖，并行取（此前串行 await，卡片逐个蹦出来）
  await Promise.all([loadVersion(), loadOverview(), loadAutoSync(), loadPluginSettings()]);

  const saveBtn = el('s-auto-sync-save');
  saveBtn.addEventListener('click', async () => {
    saveBtn.disabled = true;
    try {
      await saveAutoSync();
      toast('自动刷新设置已保存');
    } catch (e) {
      toast('保存失败：' + e.message, true);
    } finally {
      if (saveBtn.isConnected) saveBtn.disabled = false;
    }
  });

  const clearOutput = el('s-clear-output');
  clearOutput.addEventListener('click', () =>
    confirmThen('确定删除全部解密输出？\n（密钥保留，重跑解密即可恢复）', async () => {
      clearOutput.disabled = true;
      try {
        await fetchJSON('/api/settings/clear', {
          method: 'POST', headers: { 'content-type': 'application/json' },
          body: JSON.stringify({ kind: 'output' }),
        });
        toast('已清除解密输出');
        loadOverview();
      } catch (e) {
        toast('清除失败：' + e.message, true);
      } finally {
        if (clearOutput.isConnected) clearOutput.disabled = false;
      }
    }));

  const clearKeys = el('s-clear-keys');
  clearKeys.addEventListener('click', () =>
    confirmThen('确定清除密钥库？\n下次提取需要微信在线重新收割。', async () => {
      clearKeys.disabled = true;
      try {
        await fetchJSON('/api/settings/clear', {
          method: 'POST', headers: { 'content-type': 'application/json' },
          body: JSON.stringify({ kind: 'keys' }),
        });
        toast('已清除密钥库');
        loadOverview();
      } catch (e) {
        toast('清除失败：' + e.message, true);
      } finally {
        if (clearKeys.isConnected) clearKeys.disabled = false;
      }
    }));

  el('s-rerun-wizard').addEventListener('click', () => {
    window.__sxOnboarding?.open('first');
  });

  el('s-view-disclaimer').addEventListener('click', () => {
    window.__sxDisclaimer?.open();
  });

  bindDsManual();
  loadDatasources();

  // ── 日志模式 ──────────────────────────────────────────
  logLevelDrop = createDropdown({
    options: [
      { value: 'rough', label: '粗略（默认）' },
      { value: 'detailed', label: '详细' },
    ],
    value: 'rough',
    label: '日志模式',
  });
  el('s-log-level').appendChild(logLevelDrop.el);
  logLevelDrop.onChange = async (v) => {
    try {
      await fetchJSON('/api/logs/settings', {
        method: 'POST', headers: { 'content-type': 'application/json' },
        body: JSON.stringify({ level: v }),
      });
      toast(`日志模式已切换为：${v === 'detailed' ? '详细' : '粗略'}`);
    } catch (e) {
      toast('切换失败：' + e.message, true);
      logLevelDrop.value = v === 'detailed' ? 'rough' : 'detailed';   // 回滚下拉视觉
    }
  };

  async function loadLogSettings() {
    try {
      const s = await fetchJSON('/api/logs/settings');
      logLevelDrop.value = s.level || 'rough';
    } catch (e) { /* ignore */ }
  }

  // ── 脱敏日志导出 ──────────────────────────────────────
  el('s-export-log').addEventListener('click', async () => {
    const btn = el('s-export-log');
    const desensitize = el('s-log-desensitize').checked;
    const url = `/api/logs/export?desensitize=${desensitize ? '1' : '0'}`;
    btn.disabled = true;
    try {
      const r = await fetch(url);
      if (!r.ok) throw new Error(`HTTP ${r.status}`);
      const blob = await r.blob();
      const a = document.createElement('a');
      a.href = URL.createObjectURL(blob);
      a.download = `siwx_log_${Date.now()}.txt`;
      a.click();
      URL.revokeObjectURL(a.href);
    } catch (e) {
      toast('导出失败：' + e.message, true);
    } finally {
      btn.disabled = false;
    }
  });

  // ── 复制环境信息（提 issue 用）────────────────────────
  el('s-copy-env').addEventListener('click', async () => {
    const status = el('s-copy-env-status');
    status.textContent = '';
    try {
      const d = await fetchJSON('/api/settings/env');
      const ok = await copyText(d.text || '');
      status.textContent = ok ? '已复制，可直接粘贴到 issue' : '复制失败，请手动选择';
      if (ok) toast('环境信息已复制');
    } catch (e) {
      status.textContent = '失败：' + e.message;
    }
  });

  // ── 全量媒体备份（media_backup）──────────────────────
  await initBackupCard();
  await loadLogSettings();
}

/* 全量媒体备份卡片：账号下拉 → 启动 → 轮询状态 → 结果统计。 */
let mbPoll = null;

function renderBackupResult(st) {
  el('s-mb-status').textContent = st.ok ? '备份完成' : `备份失败：${st.error || '未知错误'}`;
  const r = st.report;
  if (!r) return;
  el('s-mb-result').innerHTML = `
    <div>图片：可预览 ${r.images?.ok_viewable ?? 0} · wxgf 留档 ${r.images?.ok_wxgf_preserved ?? 0} · 失败 ${r.images?.failed ?? 0}（共 ${r.images?.total ?? 0}）</div>
    <div>视频：成功 ${r.videos?.ok ?? 0} · 跳过 ${r.videos?.skipped ?? 0} · 失败 ${r.videos?.failed ?? 0}（共 ${r.videos?.total ?? 0}）</div>
    <div>文件：成功 ${r.files?.ok ?? 0} · 跳过 ${r.files?.skipped ?? 0} · 失败 ${r.files?.failed ?? 0}（共 ${r.files?.total ?? 0}）</div>`;
  el('s-mb-open-row').classList.remove('hidden');
}

function pollBackup() {
  if (mbPoll) clearInterval(mbPoll);
  const logBox = el('s-mb-log');
  logBox.classList.remove('hidden');
  mbPoll = setInterval(async () => {
    try {
      const st = await fetchJSON('/api/media/backup/status');
      logBox.textContent = (st.logs || []).slice(-12).join('\n');
      logBox.scrollTop = logBox.scrollHeight;
      if (!st.running) {
        clearInterval(mbPoll);
        mbPoll = null;
        renderBackupResult(st);
        el('s-mb-start').disabled = false;
      }
    } catch (_) { /* 轮询失败下轮再试 */ }
  }, 1000);
}

async function initBackupCard() {
  const sel = el('s-mb-account');
  const startBtn = el('s-mb-start');
  try {
    const s = await fetchJSON('/api/status');
    const accs = s.accounts || [];
    sel.innerHTML = accs.length
      ? accs.map(a => `<option value="${esc(a.wxid)}">${esc(a.wxid)}</option>`).join('')
      : '<option value="">（无已解密账号）</option>';
    startBtn.disabled = !accs.length;
  } catch (e) {
    sel.innerHTML = '<option value="">账号列表加载失败</option>';
    startBtn.disabled = true;
  }
  startBtn.addEventListener('click', async () => {
    const account = sel.value;
    if (!account) return;
    startBtn.disabled = true;
    el('s-mb-result').innerHTML = '';
    el('s-mb-open-row').classList.add('hidden');
    el('s-mb-log').textContent = '';
    try {
      await fetchJSON('/api/media/backup/start', {
        method: 'POST',
        headers: { 'content-type': 'application/json' },
        body: JSON.stringify({ account }),
      });
      el('s-mb-status').textContent = '备份运行中…';
      pollBackup();
    } catch (e) {
      el('s-mb-status').textContent = '启动失败：' + e.message;
      startBtn.disabled = false;
    }
  });
  el('s-mb-open').addEventListener('click', async () => {
    try {
      await fetchJSON('/api/media/backup/open', { method: 'POST' });
    } catch (e) {
      toast('打开失败：' + e.message, true);
    }
  });
  // 页面刷新后恢复在跑任务的轮询
  try {
    const st = await fetchJSON('/api/media/backup/status');
    if (st.running) {
      el('s-mb-status').textContent = '备份运行中…';
      startBtn.disabled = true;
      pollBackup();
    } else if (st.done) {
      renderBackupResult(st);
    }
  } catch (_) { /* 首次无状态 */ }
}

export function destroy() {
  if (dsStop) dsStop();                    // 切页后停止在飞的提取轮询
  dsStop = null;
  if (mbPoll) { clearInterval(mbPoll); mbPoll = null; }
  plgChoiceDrop.clear();                   // 释放已脱离 DOM 的下拉挂载引用
  if (logLevelDrop) { logLevelDrop.destroy(); logLevelDrop = null; }
}
