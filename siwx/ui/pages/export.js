/* 导出页 —— 左侧多选会话列表 + 右侧配置/日志/结果，全部走全局设计 token。
   账号来自全局账号卡（SX.getAccount）；聊天页带入的预选若属于其他账号，
   会先经 SX.setAccount 切过去再加载。 */
import { createDropdown, createDatePicker } from '/widgets.js?v=2026100601';

const { esc, fetchJSON, fmtListTs, toast, getAccount, setAccount, go } = window.SX;

function el(id) { return document.getElementById(id); }

/** 页面存活哨兵：视图被下一次导航换掉后，仍在飞的异步回调必须就此止步 */
function gone() { return !document.getElementById('e-sessions'); }

let account = null;
let sessions = [];
let selected = new Set();       // 已勾选 username 集合
let chatPreset = null;
let chatRange = null;           // 聊天页多选带入的范围 {account, chat, display, start, end, count}
let running = false;
let pollTimer = null;
let fmtDrop = null;
let packDrop = null;
let tplDrop = null;
let startPick = null;
let endPick = null;
// 头像 404 标记：失败过的头像不再反复请求（与聊天页同一策略）
const avaFailed = new Set();

function avatarImg(username) {
  if (avaFailed.has(username)) return '';
  const src = `/api/chat/avatar?account=${encodeURIComponent(account)}` +
              `&username=${encodeURIComponent(username)}`;
  // 失败由 #e-sessions 的捕获级 error 委托处理（记入 avaFailed 并移除）
  return `<img loading="lazy" src="${src}" data-ava="${esc(username)}" alt="">`;
}

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

/** HTML 模板下拉：仅在勾选 html 格式时有意义；加载失败保持默认项 */
async function loadTemplates() {
  try {
    const { templates } = await fetchJSON('/api/export/templates');
    if (Array.isArray(templates) && templates.length) {
      tplDrop.options = templates.map(t => ({
        value: t.name,
        label: `${t.label}${t.builtin ? '' : '（自定义）'}`,
      }));
    }
  } catch (e) { /* 保留默认项 */ }
}

async function init() {
  account = getAccount();
  if (!account) {
    el('e-sessions').innerHTML =
      '<div class="e-empty">还没有解密产物<br>' +
      '<button class="btn btn-primary" id="e-go-guide" type="button">去添加账号</button></div>';
    el('e-go-guide').addEventListener('click', () => window.__sxOnboarding?.open('add'));
    return;
  }

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
    value: ['json'],
    multi: true,
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
  tplDrop = createDropdown({
    options: [{ value: 'default', label: '默认 · 微信风格' }],
    value: 'default',
    label: 'HTML 模板',
  });
  el('e-tpl').appendChild(tplDrop.el);
  startPick = createDatePicker({ placeholder: '不限', label: '开始日期', todayText: '今天', clearText: '不限' });
  el('e-start').appendChild(startPick.el);
  endPick = createDatePicker({ placeholder: '不限', label: '结束日期', todayText: '今天', clearText: '不限' });
  el('e-end').appendChild(endPick.el);
  startPick.onChange = onManualDate;
  endPick.onChange = onManualDate;

  loadFormats();
  loadTemplates();
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
  // 预选会话属于另一个微信号：全局账号是身份，切过去（不触发页面重载）
  if (chatPreset && chatPreset.account && chatPreset.account !== account) {
    setAccount(chatPreset.account, { reload: false });
    account = chatPreset.account;
  }
  await loadSessions();

  el('e-search').addEventListener('input', renderSessions);
  // 头像加载失败：error 不冒泡，捕获阶段接管（与聊天页同一套处理）
  el('e-sessions').addEventListener('error', (ev) => {
    const img = ev.target;
    if (img && img.tagName === 'IMG' && img.dataset.ava !== undefined) {
      avaFailed.add(img.dataset.ava);
      img.remove();
    }
  }, true);
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
  el('e-sessions').innerHTML = '<div class="e-loading">加载会话…</div>';
  try {
    const { sessions: ss } = await fetchJSON(
      `/api/chat/sessions?account=${encodeURIComponent(account)}`);
    if (gone()) return;
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
  } catch (e) {
    if (gone()) return;
    el('e-sessions').innerHTML = `<div class="e-empty">${esc(e.message)}</div>`;
  }
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
        <span class="prev">${esc(s.preview || '点击查看详情')}</span>
      </div>
      <span class="tm">${fmtListTs(s.last_time)}</span>
    </label>`;
  }).join('')
    : '<div class="e-empty">没有匹配的会话</div>';
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
  // 全选框三态同步：手动逐个勾选后表头不能再显示空框
  const kw = el('e-search').value;
  const matched = sessions.filter(s => match(s, kw));
  const picked = matched.filter(s => selected.has(s.username)).length;
  const selall = el('e-selall');
  selall.checked = matched.length > 0 && picked === matched.length;
  selall.indeterminate = picked > 0 && picked < matched.length;
  renderRangeBanner();
}

async function run() {
  if (running || !selected.size) return;
  const fmts = Array.isArray(fmtDrop.value) ? fmtDrop.value : [fmtDrop.value];
  if (!fmts.length) { toast('请至少选择一种导出格式', true); return; }
  const chats = sessions.filter(s => selected.has(s.username))
    .map(s => ({ chat: s.username, display: s.display }));
  const body = {
    mode: 'export',
    export_opts: {
      account,
      chats,
      format: fmts,
      pack: packDrop.value,
      template: tplDrop.value,
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
  el('e-progress-card').classList.remove('hidden');
  el('e-bar').style.width = '0%';
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
    if (!gone()) el('e-log').innerHTML = `<div class="log-line err">${esc(e.message)}</div>`;
  }
}

function poll() {
  let failStreak = 0;                    // 连续失败计数：后端挂掉时终局报错，不无限吞错
  pollTimer = setInterval(async () => {
    if (document.hidden) return;             // 后台标签页暂停轮询
    if (gone()) { clearInterval(pollTimer); pollTimer = null; return; }
    try {
      const job = await (await fetch('/api/job')).json();
      failStreak = 0;
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
        pollTimer = null;
        running = false;
        updateCount();
        el('e-bar').style.width = '100%';
        const card = el('e-progress-card');
        setTimeout(() => { if (card.isConnected) card.classList.add('hidden'); }, 600);
        renderResult(job);
        if (job.ok) toast('导出完成');
        else toast(job.error || '导出失败', true);
      }
    } catch (e) {
      // 服务端重启/挂掉：连续失败就停下来给出错误态，进度条不再永久冻结
      if (++failStreak >= 5) {
        clearInterval(pollTimer);
        pollTimer = null;
        running = false;
        if (!gone()) {
          updateCount();
          el('e-log').insertAdjacentHTML('beforeend',
            `<div class="log-line err">与后端失去联系（${esc(e.message)}），已停止轮询，请刷新重试</div>`);
        }
      }
    }
  }, 600);
}

function logLines(logs, cls) {
  return logs.map(entry => {
    const msg = entry.length >= 4 ? entry[3] : entry[1];
    return `<div class="log-line ${cls || ''}">${esc(msg)}</div>`;
  }).join('');
}

function renderLogs(logs) {
  if (gone()) return;
  const box = el('e-log');
  box.innerHTML = logLines(logs) || '<div class="log-line dim">…</div>';
  box.scrollTop = box.scrollHeight;
}

/** 单个会话结果行：成功带下载链接，失败标红带原因 */
function sessionResultLine(s) {
  if (s.error) {
    return `<div class="res-meta res-err">✗ ${esc(s.display)}：${esc(s.error)}</div>`;
  }
  const fileList = (Array.isArray(s.files) && s.files.length)
    ? s.files : (s.file ? [s.file] : []);
  const dl = fileList.length ? ' · ' + fileList.map((f, i) =>
    `<a href="/api/export/download?path=${encodeURIComponent(f)}">下载文件${fileList.length > 1 ? `(${i + 1})` : ''}</a>`
  ).join(' / ') : '';
  const media = (s.image_count || s.voice_count) ? ` · 图片 ${s.image_count || 0} · 语音 ${s.voice_count || 0}` : '';
  return `<div class="res-meta">✓ ${esc(s.display)} — ${s.message_count} 条消息${media}${dl}</div>`;
}

function renderResult(job) {
  if (gone()) return;
  const box = el('e-result');
  const logs = job.logs || [];
  const lines = [];
  if (job.error) {
    lines.push(`<div class="res-row res-err">✗ 导出失败：${esc(job.error)}</div>`);
    lines.push(`<div class="res-actions"><a class="btn" href="#" data-retry>↻ 重试</a></div>`);
    lines.push(`<details class="res-logs-detail" open><summary>运行日志（${logs.length} 条）</summary>`);
    lines.push(logLines(logs, 'dim'));
    lines.push('</details>');
    box.innerHTML = lines.join('');
    bindResultActions(box);
    return;
  }
  const r = job.report || {};
  if (!r.sessions) {
    // 任务完成了却没有 report：多半是后端异常或任务被别的操作打断，重试可直接再来一次
    lines.push(`<div class="res-row res-err">⚠️ 无导出结果</div>`);
    lines.push(`<div class="res-actions"><a class="btn" href="#" data-retry>↻ 重试</a></div>`);
    lines.push(`<details class="res-logs-detail" open><summary>运行日志（${logs.length} 条）</summary>`);
    lines.push(logLines(logs, 'dim'));
    lines.push('</details>');
    box.innerHTML = lines.join('');
    bindResultActions(box);
    return;
  }
  const failed = r.sessions.filter(s => s.error);
  const dur = ((r.duration_ms || 0) / 1000).toFixed(1);
  lines.push(`<div class="res-row ${failed.length ? 'res-part' : 'res-ok'}">` +
    `✔ 导出完成：${r.ok_count}/${r.sessions.length} 个会话` +
    (failed.length ? `（${failed.length} 个失败）` : '') +
    `，共 ${r.message_count} 条消息，图片 ${r.image_count || 0}，语音 ${r.voice_count || 0}，头像 ${r.avatar_count}（${dur}s）</div>`);
  if (r.total_dir) {
    lines.push(`<div class="res-meta">目录：<span class="mono">${esc(r.total_dir)}</span></div>`);
    // 路径只放 data 属性，点击时再从 dataset 读取——内联 onclick 拼
    // Windows 路径会被 JS 转义吃掉反斜杠（D:\x → D:x），后端必然拒绝
    lines.push(`<div class="res-actions"><a class="btn" href="#" data-open-path="${esc(r.total_dir)}">📂 打开目录</a></div>`);
  }
  if (r.zip) {
    lines.push(`<div class="res-actions"><a class="btn" href="/api/export/download?path=${encodeURIComponent(r.zip)}">📥 下载整体 ZIP</a></div>`);
  }
  if (r.zips && r.zips.length) {
    lines.push(`<div class="res-meta">共 ${r.zips.length} 个会话 ZIP（位于导出目录内）</div>`);
  }
  // 失败的会话单独分区置顶，成功/失败不再混排
  if (failed.length) {
    lines.push(`<details class="res-logs-detail res-failed" open><summary>失败会话（${failed.length}）</summary>`);
    failed.forEach(s => lines.push(sessionResultLine(s)));
    lines.push('</details>');
  }
  const okSessions = r.sessions.filter(s => !s.error);
  if (okSessions.length) {
    lines.push('<details class="res-logs-detail" open><summary>各会话结果</summary>');
    okSessions.forEach(s => lines.push(sessionResultLine(s)));
    lines.push('</details>');
  }
  lines.push(`<details class="res-logs-detail"><summary>运行日志（${logs.length} 条）</summary>`);
  lines.push(logLines(logs, 'dim'));
  lines.push('</details>');
  box.innerHTML = lines.join('');
  bindResultActions(box);
}

/** 结果框里的按钮：重试（失败路径）与打开目录（只读 dataset，避免路径被转义吃掉） */
function bindResultActions(box) {
  box.querySelectorAll('[data-retry]').forEach(a => {
    a.addEventListener('click', (ev) => { ev.preventDefault(); run(); });
  });
  box.querySelectorAll('[data-open-path]').forEach(a => {
    a.addEventListener('click', (ev) => {
      ev.preventDefault();
      SX.openPath(a.dataset.openPath);
    });
  });
}

export function destroy() {
  if (pollTimer) { clearInterval(pollTimer); pollTimer = null; }
}
export { init };
