/* 导出页 —— 左侧多选会话列表 + 右侧配置/日志/结果，全部走全局设计 token。
   账号来自全局账号卡（SX.getAccount）；聊天页带入的预选若属于其他账号，
   会先经 SX.setAccount 切过去再加载。
   排除体系三层叠加：正则（按账号持久化）→ 一键排除公众号（按账号持久化）
   → 名单弹窗里的手动勾选覆盖（正则/公众号开关判定后的逐人改判，同样持久化），
   手动勾选优先级最高，且只在与默认判定不同时才落盘。 */
import { createDropdown, createDatePicker } from '/widgets.js?v=2026100601';

const { esc, fetchJSON, fmtListTs, toast, getAccount, setAccount, go, copyText } = window.SX;

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
// 名单模态框：排除正则（null = 没配；excludeInvalid = 输入了非法正则）
let excludeRe = null;
let excludeInvalid = false;
let overrides = new Map();      // 手动勾选覆盖：username → 是否导出；仅存与默认判定不同的项
let rosterFilter = 'all';       // 名单筛选：all / in（将导出）/ ex（被排除）
let rosterKeyHandler = null;   // Esc 关闭；页面销毁时移除，避免监听器越积越多
// 左侧列表：类型快捷筛选（all/chat/group/official）+ 公众号折叠态
let listFilter = 'all';
let officialOpen = false;
// 控制台：暂停时不渲染但仍缓存最新日志，恢复后一次刷齐
let logPaused = false;
let lastLogs = [];
// 本次运行的勾选/排除回显（页面刷新后丢失，仅当次会话展示）
let lastRunStats = null;
// 最近一次导出/转换的目录与模板：结果面板"补生成格式"（方案 A）要用
let lastExportDir = null;
let lastTemplate = '';
// 头像 404 标记：失败过的头像不再反复请求（与聊天页同一策略）
const avaFailed = new Set();

function isOfficial(s) {
  return !!(s && (s.is_official || s.kind === 'official' || /^gh_/.test(s.username || '')));
}

function store(k, v) {
  try {
    if (v == null) localStorage.removeItem(k);
    else localStorage.setItem(k, v);
  } catch (e) { /* localStorage 不可用则仅本次会话内生效 */ }
}
function loadStore(k) {
  try { return localStorage.getItem(k); } catch (e) { return null; }
}

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

function kindMatch(s, f) {
  if (f === 'all') return true;
  if (f === 'official') return isOfficial(s);
  if (f === 'group') return !!s.is_group && !isOfficial(s);
  return !s.is_group && !isOfficial(s);
}

/** 当前筛选条件下的可见会话（搜索框 + 类型快捷筛选） */
function visibleSessions() {
  const kw = el('e-search').value;
  return sessions.filter(s => match(s, kw) && kindMatch(s, listFilter));
}

/* ── 排除判定：正则 / 公众号开关 → 默认判定；手动覆盖优先 ─────── */

function defaultExcluded(s) {
  const reHit = excludeRe instanceof RegExp &&
    (excludeRe.test(s.display || '') || excludeRe.test(s.username || ''));
  return reHit || (el('e-noofficial')?.checked && isOfficial(s));
}

function isExcluded(s) {
  if (overrides.has(s.username)) return !overrides.get(s.username);
  return defaultExcluded(s);
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

/** HTML 模板只在勾了 html 格式时显示，其余格式下隐藏整行 */
function syncTplVisibility() {
  const fmts = Array.isArray(fmtDrop.value) ? fmtDrop.value : [fmtDrop.value];
  el('e-tpl').closest('label')?.classList.toggle('hidden', !fmts.includes('html'));
}

/* ── 导出名单模态框 ───────────────────────────────────────────
 * 查看本次将导出的联系人：正则/公众号开关给出默认判定，名单里每行
 * 一个复选框可逐人改判（勾回被排除的、剔掉想保留的），改判按账号
 * 持久化，只存与默认判定不同的项。支持名单内搜索与三态筛选，
 * 并可一键复制整个名单（每行一个昵称）。 */
function rosterList() { return sessions.filter(s => selected.has(s.username)); }

function renderRoster() {
  const list = rosterList();
  const kw = (el('e-roster-search').value || '').trim().toLowerCase();
  const rows = list.filter(s =>
    !kw || (s.display || '').toLowerCase().includes(kw) ||
    (s.username || '').toLowerCase().includes(kw))
    .filter(s => rosterFilter === 'all' ||
      (rosterFilter === 'in' && !isExcluded(s)) ||
      (rosterFilter === 'ex' && isExcluded(s)));
  el('e-roster-list').innerHTML = rows.length ? rows.map(s => {
    const ex = isExcluded(s);
    const over = overrides.has(s.username);
    const kindTag = isOfficial(s) ? '公众号' : (s.is_group ? '群聊' : '');
    let state = '';
    if (ex) state = '<span class="tag">已排除</span>';
    else if (over && defaultExcluded(s)) state = '<span class="tag back">已勾回</span>';
    return `<label class="e-rost${ex ? ' excl' : ''}">` +
      `<input type="checkbox" data-u="${esc(s.username)}" ${ex ? '' : 'checked'}>` +
      `<span class="nm">${esc(s.display || s.username)}</span>` +
      `<span class="uid">${esc(s.username)}</span>` +
      (kindTag ? `<span class="kind">${kindTag}</span>` : '') +
      state +
      '</label>';
  }).join('') : `<div class="e-empty">${list.length ? '没有匹配的联系人' : '先在左侧勾选要导出的会话'}</div>`;
  el('e-roster-list').querySelectorAll('input[type=checkbox]').forEach(cb => {
    cb.addEventListener('change', () => {
      const s = list.find(x => x.username === cb.dataset.u);
      if (!s) return;
      // 只存与默认判定不同的覆盖项：勾回默认状态时自动移除，避免 Map 越积越大
      if (cb.checked !== !defaultExcluded(s)) overrides.set(s.username, cb.checked);
      else overrides.delete(s.username);
      store(overrideStoreKey(), JSON.stringify([...overrides]));
      renderRoster();
      updateCount();
    });
  });
  const excludedN = list.filter(isExcluded).length;
  const overN = overrides.size;
  el('e-roster-count').textContent =
    `共 ${list.length} 人 · 将导出 ${list.length - excludedN} 人` +
    (excludedN ? ` · 排除 ${excludedN} 人` : '') +
    (overN ? ` · 手动改判 ${overN} 人` : '');
  const hint = el('e-roster-hint');
  hint.textContent = excludeInvalid ? '正则语法有误，暂不生效'
    : (excludeRe ? '排除正则已生效' : '');
  hint.classList.toggle('bad', excludeInvalid);
}

function excludeStoreKey() { return `siwx-export-exclude:${account || ''}`; }
function overrideStoreKey() { return `siwx-export-override:${account || ''}`; }
function officialStoreKey() { return `siwx-export-noofficial:${account || ''}`; }
function cfgStoreKey() { return `siwx-export-cfg:${account || ''}`; }

/** 编译输入框里的排除正则并刷新列表；顺带持久化到 localStorage */
function applyExclude() {
  const input = el('e-roster-re');
  const src = input.value.trim();
  excludeInvalid = false;
  excludeRe = null;
  if (src) {
    try { excludeRe = new RegExp(src, 'i'); }
    catch (e) { excludeInvalid = true; }
  }
  store(excludeStoreKey(), src && !excludeInvalid ? src : null);
  renderRoster();
  updateCount();
}

function setupRosterModal() {
  const modal = el('e-roster-modal');
  el('e-roster-btn').addEventListener('click', () => {
    renderRoster();
    modal.classList.remove('hidden');
  });
  const close = () => modal.classList.add('hidden');
  el('e-roster-close').addEventListener('click', close);
  modal.addEventListener('click', (ev) => { if (ev.target === modal) close(); });
  rosterKeyHandler = (ev) => {
    if (ev.key === 'Escape' && !modal.classList.contains('hidden')) close();
  };
  document.addEventListener('keydown', rosterKeyHandler);
  el('e-roster-re').value = loadStore(excludeStoreKey()) || '';
  el('e-roster-re').addEventListener('input', applyExclude);
  el('e-roster-search').addEventListener('input', renderRoster);
  el('e-roster-chips').querySelectorAll('.chip').forEach(c => {
    c.addEventListener('click', () => {
      rosterFilter = c.dataset.f;
      el('e-roster-chips').querySelectorAll('.chip')
        .forEach(x => x.classList.toggle('on', x === c));
      renderRoster();
    });
  });
  el('e-roster-reset').addEventListener('click', () => {
    if (!overrides.size) { toast('没有手动改判记录'); return; }
    overrides.clear();
    store(overrideStoreKey(), null);
    renderRoster();
    updateCount();
    toast('已清除全部手动改判，恢复默认判定');
  });
  el('e-roster-copy').addEventListener('click', async () => {
    const lines = rosterList().filter(s => !isExcluded(s))
      .map(s => s.display || s.username);
    if (!lines.length) { toast('名单为空，先在左侧勾选会话', true); return; }
    const ok = await copyText(lines.join('\n'));
    toast(ok ? `已复制 ${lines.length} 个联系人（每行一个）` : '复制失败', !ok);
  });
  try { overrides = new Map(JSON.parse(loadStore(overrideStoreKey()) || '[]')); }
  catch (e) { overrides = new Map(); }
  applyExclude();  // 恢复上次的正则并渲染计数
}

/** 公众号开关：按账号持久化，改动即刻反映到名单与左侧标记 */
function applyNoOfficial() {
  const cb = el('e-noofficial');
  const saved = loadStore(officialStoreKey());
  cb.checked = saved == null ? true : saved === '1';
  cb.addEventListener('change', () => {
    store(officialStoreKey(), cb.checked ? '1' : '0');
    renderSessions();
    renderRoster();
    updateCount();
  });
  // 恢复持久化值发生在名单首次渲染之后，这里补一次刷新让判定与开关同步
  renderRoster();
  updateCount();
}

/** 上次导出配置记忆：格式/打包/内容勾选按账号恢复（日期范围属临时意图，不记忆） */
function restoreCfg() {
  let cfg = null;
  try { cfg = JSON.parse(loadStore(cfgStoreKey()) || 'null'); } catch (e) { cfg = null; }
  if (!cfg) return;
  try {
    if (Array.isArray(cfg.formats) && cfg.formats.length) fmtDrop.value = cfg.formats;
    if (cfg.pack) packDrop.value = cfg.pack;
    if (typeof cfg.msgs === 'boolean') el('e-msg').checked = cfg.msgs;
    if (typeof cfg.media === 'boolean') el('e-media').checked = cfg.media;
    if (typeof cfg.voice === 'boolean') el('e-voice').checked = cfg.voice;
    if (typeof cfg.ava === 'boolean') el('e-ava').checked = cfg.ava;
  } catch (e) { /* 配置残缺就保持默认 */ }
  syncTplVisibility();
}

function saveCfg() {
  store(cfgStoreKey(), JSON.stringify({
    formats: Array.isArray(fmtDrop.value) ? fmtDrop.value : [fmtDrop.value],
    pack: packDrop.value,
    msgs: el('e-msg').checked,
    media: el('e-media').checked,
    voice: el('e-voice').checked,
    ava: el('e-ava').checked,
  }));
}

async function init() {
  account = getAccount();
  try { chatPreset = JSON.parse(sessionStorage.getItem('siwx-export-preset') || 'null'); }
  catch (e) { chatPreset = null; }
  // 预选会话属于另一个微信号：全局账号是身份，切过去（不触发页面重载）。
  // 必须先切账号再初始化名单/开关，排除相关的按账号持久化才能落到正确账号。
  if (chatPreset && chatPreset.account && chatPreset.account !== account) {
    setAccount(chatPreset.account, { reload: false });
    account = chatPreset.account;
  }
  setupRosterModal();
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
  fmtDrop.onChange = syncTplVisibility;
  restoreCfg();
  syncTplVisibility();
  applyNoOfficial();

  loadFormats();
  loadTemplates();
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
    for (const s of visibleSessions()) {
      if (e.target.checked) selected.add(s.username);
      else selected.delete(s.username);
    }
    renderSessions();
    updateCount();
  });
  el('e-filter-chips').querySelectorAll('.chip').forEach(c => {
    c.addEventListener('click', () => {
      listFilter = c.dataset.f;
      el('e-filter-chips').querySelectorAll('.chip')
        .forEach(x => x.classList.toggle('on', x === c));
      renderSessions();
    });
  });
  el('e-log-pause').addEventListener('click', () => {
    logPaused = !logPaused;
    const b = el('e-log-pause');
    b.textContent = logPaused ? '继续' : '暂停';
    b.setAttribute('aria-pressed', String(logPaused));
    if (!logPaused) renderLogs(lastLogs);
  });
  el('e-cancel').addEventListener('click', requestCancel);
  el('e-log-copy').addEventListener('click', async () => {
    const text = lastLogs.map(entry => entry.length >= 4 ? entry[3] : entry[1]).join('\n');
    if (!text) { toast('日志为空', true); return; }
    const ok = await copyText(text);
    toast(ok ? '已复制运行日志' : '复制失败', !ok);
  });
  el('e-run').addEventListener('click', run);
}

async function loadSessions() {
  el('e-sessions').innerHTML = '<div class="e-loading">加载会话…</div>';
  officialOpen = false;
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

function sessionRowHtml(s) {
  const initials = (s.display || s.username || '?').slice(0, 2).toUpperCase();
  const off = isOfficial(s);
  const kindTag = off ? '公众号' : (s.is_group ? '群聊' : '');
  const exTag = isExcluded(s) && selected.has(s.username)
    ? '<span class="tag excl-tag">已排除</span>' : '';
  const exCls = isExcluded(s) && selected.has(s.username) ? ' is-excl' : '';
  return `
    <label class="e-sess ${selected.has(s.username) ? 'sel' : ''}${exCls}">
      <input type="checkbox" data-u="${esc(s.username)}" ${selected.has(s.username) ? 'checked' : ''}>
      <div class="ava"><span>${esc(initials)}</span>${avatarImg(s.username)}</div>
      <div class="e-body">
        <span class="name">${esc(s.display)}${exTag}</span>
        <span class="prev">${esc(s.preview || '点击查看详情')}</span>
      </div>
      <span class="tm">${fmtListTs(s.last_time)}</span>
      ${kindTag ? `<span class="tag kind-tag">${kindTag}</span>` : ''}
    </label>`;
}

function renderSessions() {
  const list = visibleSessions();
  const normal = list.filter(s => !isOfficial(s));
  const official = list.filter(s => isOfficial(s));
  const lines = [];
  // 公众号折叠分组（与聊天页同一交互）：快捷筛选专门看公众号时平铺
  if (official.length && listFilter !== 'official') {
    const latest = Math.max(...official.map(s => s.last_time || 0));
    lines.push(`
      <div class="e-sess e-grp ${officialOpen ? 'open' : ''}" data-group="official"
           tabindex="0" role="button" aria-label="公众号分组，点击${officialOpen ? '收起' : '展开'}">
        <div class="ava"><span>公</span></div>
        <div class="e-body">
          <span class="name">公众号</span>
          <span class="prev">${official.length} 个公众号会话 · 点击${officialOpen ? '收起' : '展开'}</span>
        </div>
        <span class="tm">${fmtListTs(latest)}</span>
      </div>`);
    if (officialOpen) lines.push(...official.map(sessionRowHtml));
  } else {
    lines.push(...official.map(sessionRowHtml));
  }
  lines.push(...normal.map(sessionRowHtml));
  el('e-sessions').innerHTML = lines.length ? lines.join('')
    : '<div class="e-empty">没有匹配的会话</div>';
  el('e-sessions').querySelectorAll('input[type=checkbox]').forEach(cb => {
    cb.addEventListener('change', () => {
      if (cb.checked) selected.add(cb.dataset.u);
      else selected.delete(cb.dataset.u);
      cb.closest('.e-sess').classList.toggle('sel', cb.checked);
      updateCount();
    });
  });
  const grp = el('e-sessions').querySelector('[data-group=official]');
  if (grp) {
    const toggle = () => { officialOpen = !officialOpen; renderSessions(); };
    grp.addEventListener('click', toggle);
    grp.addEventListener('keydown', (ev) => {
      if (ev.key === 'Enter' || ev.key === ' ') { ev.preventDefault(); toggle(); }
    });
  }
}

function updateCount() {
  const picked = sessions.filter(s => selected.has(s.username));
  const excludedN = picked.filter(isExcluded).length;
  const willExport = picked.length - excludedN;
  el('e-hint').textContent = `已选 ${picked.length} 个会话` +
    (excludedN ? ` · 将导出 ${willExport} 个（被排除 ${excludedN} 个）` : '');
  el('e-run').disabled = running || willExport === 0;
  // 全选框三态同步：手动逐个勾选后表头不能再显示空框
  const matched = visibleSessions();
  const sel = matched.filter(s => selected.has(s.username)).length;
  const selall = el('e-selall');
  selall.checked = matched.length > 0 && sel === matched.length;
  selall.indeterminate = sel > 0 && sel < matched.length;
  renderRangeBanner();
}

function resetCancelButton() {
  const b = el('e-cancel');
  b.classList.remove('hidden');
  b.disabled = false;
  b.textContent = '取消任务';
}

/** 协作式取消：请求受理后等任务走到最近检查点才真正停止（优雅停止，
 *  已完成会话保留）。409 = 任务刚好结束，无需处理。 */
async function requestCancel() {
  const b = el('e-cancel');
  b.disabled = true;
  b.textContent = '正在取消…';
  try {
    const r = await fetch('/api/job/cancel', { method: 'POST' });
    if (r.status === 409) { resetCancelButton(); return; }
    if (!r.ok) throw new Error('取消请求失败');
  } catch (e) {
    // 网络失败不回弹按钮：轮询很快会给终局，按钮随终局一起收起
    toast(`取消请求失败：${e.message}`, true);
  }
}

/** 方案 A：对最近一次 JSON 导出目录就地补生成格式（不重跑解密）。 */
async function runConvert(formats, overwrite = false) {
  if (running || !lastExportDir) return;
  running = true;
  updateCount();
  el('e-log').innerHTML = '';
  el('e-result').innerHTML = '';
  el('e-progress-card').classList.remove('hidden');
  el('e-bar').style.width = '0%';
  el('e-prog-text').textContent = '准备补生成…';
  resetCancelButton();
  const body = {
    mode: 'convert',
    export_opts: {
      dir: lastExportDir,
      formats,
      overwrite,
      template: lastTemplate || undefined,
    },
  };
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

async function run() {
  if (running || !selected.size) return;
  const fmts = Array.isArray(fmtDrop.value) ? fmtDrop.value : [fmtDrop.value];
  if (!fmts.length) { toast('请至少选择一种导出格式', true); return; }
  const picked = sessions.filter(s => selected.has(s.username));
  const excludedN = picked.filter(isExcluded).length;
  const chats = picked.filter(s => !isExcluded(s))
    .map(s => ({ chat: s.username, display: s.display }));
  if (excludedN) toast(`排除规则已剔除 ${excludedN} 个会话，不参与导出`);
  if (!chats.length) { toast('排除后没有可导出的会话', true); return; }
  // 大批量 + 媒体的组合可能要跑很久，先确认再开工
  const heavy = el('e-media').checked || el('e-voice').checked || el('e-ava').checked;
  if ((chats.length >= 30 && heavy) || chats.length >= 80) {
    if (!window.confirm(`即将导出 ${chats.length} 个会话${heavy ? '（含媒体处理，耗时较长）' : ''}，确定开始？`)) return;
  }
  saveCfg();
  lastRunStats = { picked: picked.length, excluded: excludedN, export: chats.length };
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
  lastTemplate = tplDrop.value || '';
  el('e-log').innerHTML = '';
  el('e-result').innerHTML = '';
  el('e-progress-card').classList.remove('hidden');
  el('e-bar').style.width = '0%';
  el('e-prog-text').textContent = '准备中…';
  resetCancelButton();
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
      lastLogs = job.logs || [];
      if (!logPaused) renderLogs(lastLogs);
      renderProgress(job);
      if (!job.running) {
        clearInterval(pollTimer);
        pollTimer = null;
        running = false;
        updateCount();
        el('e-cancel').classList.add('hidden');
        el('e-bar').style.width = '100%';
        const card = el('e-progress-card');
        setTimeout(() => { if (card.isConnected) card.classList.add('hidden'); }, 600);
        renderResult(job);
        const kind = job.report && job.report.kind;
        if (job.ok) toast(kind === 'convert' ? '格式补生成完成' : '导出完成');
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

/** 进度条 + 文字：优先用后端结构化 progress，旧后端回退到日志正则解析 */
function renderProgress(job) {
  let pct = 0, msg = '';
  if (job.progress && typeof job.progress.pct === 'number') {
    pct = job.progress.pct;
    msg = job.progress.msg || '';
  } else {
    for (const entry of lastLogs) {
      const m = /^\[export\] (\d+)% ?(.*)$/.exec(
        String(entry.length >= 4 ? entry[3] : entry[1] || ''));
      if (m) { pct = +m[1]; msg = m[2]; }
    }
  }
  el('e-bar').style.width = pct + '%';
  el('e-prog-text').textContent = msg ? `${pct}% · ${msg}` : (pct ? `${pct}%` : '');
}

/* 级别着色与日志页同一套口径：结构化条目直接用级别，文件轨按文本启发式 */
function logLevel(entry) {
  if (entry.length >= 4) return String(entry[1] || '').toUpperCase();
  const m = /^\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}[,.]?\d*\s*\[(\w+)\]/.exec(String(entry[1] || ''));
  return m ? m[1].toUpperCase() : null;
}

function logClass(entry) {
  const lv = logLevel(entry);
  if (lv === 'ERROR') return 'err';
  if (lv === 'WARN') return 'warn';
  if (lv === 'INFO') return 'ok';
  if (lv === 'DEBUG') return 'dim';
  const msg = String(entry.length >= 4 ? entry[3] : entry[1] || '');
  if (msg.includes('[错误]') || msg.includes('✗') || msg.includes('失败')) return 'err';
  if (msg.includes('✔') || msg.includes('成功') || msg.includes('完成')) return 'ok';
  if (msg.includes('⚠') || msg.includes('跳过') || msg.includes('缺失')) return 'warn';
  return 'dim';
}

function logLines(logs, cls) {
  return logs.map(entry => {
    const msg = String(entry.length >= 4 ? entry[3] : entry[1] || '');
    const d = entry[0] ? new Date(entry[0]) : null;
    const t = d && !isNaN(d) ?
      `<span class="t">${String(d.getHours()).padStart(2, '0')}:${String(d.getMinutes()).padStart(2, '0')}:${String(d.getSeconds()).padStart(2, '0')}</span>` : '';
    return `<div class="log-line ${cls || logClass(entry)}">${t}${esc(msg)}</div>`;
  }).join('');
}

function renderLogs(logs) {
  if (gone()) return;
  const box = el('e-log');
  // 贴底判定要在替换内容前做：换完 innerHTML 后 scrollHeight 已变，判断就失真了
  const stick = box.scrollHeight - box.scrollTop - box.clientHeight < 48;
  box.innerHTML = logLines(logs) || '<div class="log-line dim">…</div>';
  // 用户本就停在底部才跟随滚动；往上翻看错误时不被刷走
  if (stick) box.scrollTop = box.scrollHeight;
}

function fmtSize(n) {
  if (!n && n !== 0) return '';
  if (n >= 1048576) return (n / 1048576).toFixed(1) + ' MB';
  if (n >= 1024) return (n / 1024).toFixed(0) + ' KB';
  return n + ' B';
}

/** 单个会话结果行：成功带下载链接与耗时/大小，失败标红带原因和复制按钮 */
function sessionResultLine(s) {
  if (s.error) {
    return `<div class="res-meta res-err">✗ ${esc(s.display)}：${esc(s.error)}` +
      ` <a href="#" class="copy-err" data-err="${esc(s.display)}: ${esc(s.error)}">复制错误</a></div>`;
  }
  const fileList = (Array.isArray(s.files) && s.files.length)
    ? s.files : (s.file ? [s.file] : []);
  const dl = fileList.length ? ' · ' + fileList.map((f, i) =>
    `<a href="/api/export/download?path=${encodeURIComponent(f)}">下载文件${fileList.length > 1 ? `(${i + 1})` : ''}</a>`
  ).join(' / ') : '';
  const media = (s.image_count || s.voice_count) ? ` · 图片 ${s.image_count || 0} · 语音 ${s.voice_count || 0}` : '';
  const size = s.size_bytes ? ` · ${fmtSize(s.size_bytes)}` : '';
  const dur = s.duration_ms ? ` · ${(s.duration_ms / 1000).toFixed(1)}s` : '';
  return `<div class="res-meta">✓ ${esc(s.display)} — ${s.message_count} 条消息${media}${size}${dur}${dl}</div>`;
}

function renderResult(job) {
  if (gone()) return;
  const box = el('e-result');
  const logs = job.logs || [];
  const lines = [];
  const echo = lastRunStats
    ? `<div class="res-meta">本次勾选 ${lastRunStats.picked} · 排除 ${lastRunStats.excluded} · 实际导出 ${lastRunStats.export} 个会话</div>`
    : '';
  if (job.error) {
    lines.push(`<div class="res-row res-err">✗ 导出失败：${esc(job.error)}</div>`);
    if (echo) lines.push(echo);
    lines.push(`<div class="res-actions"><a class="btn" href="#" data-retry>↻ 重试</a></div>`);
    lines.push(`<details class="res-logs-detail" open><summary>运行日志（${logs.length} 条）</summary>`);
    lines.push(logLines(logs, 'dim'));
    lines.push('</details>');
    box.innerHTML = lines.join('');
    bindResultActions(box);
    return;
  }
  const r = job.report || {};
  if (r.kind === 'convert') {
    // 方案 A 结果：转换摘要 + 继续补其他格式/覆盖重跑的入口
    lastExportDir = r.dir || lastExportDir;
    const st = r.cancelled
      ? `<div class="res-row res-part">⏹ 已取消：本次补生成 ${r.ok} 个会话（已完成部分保留）</div>`
      : `<div class="res-row ${r.failed ? 'res-part' : 'res-ok'}">` +
        `✔ 补生成完成：${r.ok}/${r.total} 个会话` +
        (r.skipped ? `，已有跳过 ${r.skipped}` : '') +
        (r.failed ? `，失败 ${r.failed}` : '') + '</div>';
    lines.push(st);
    if (r.missing_media) {
      lines.push(`<div class="res-meta res-part">⚠ 媒体引用缺失 ${r.missing_media} 处（导出时未下载成功或目录被改动），对应位置会显示占位</div>`);
    }
    if (r.failed) {
      lines.push(`<div class="res-actions"><a class="btn" href="#" data-convert="md,html" data-convert-ow="1">↻ 覆盖重跑（含失败会话）</a></div>`);
    }
    lines.push(convertActionsHtml(['md', 'html']));
    lines.push(`<details class="res-logs-detail"><summary>运行日志（${logs.length} 条）</summary>`);
    lines.push(logLines(logs, 'dim'));
    lines.push('</details>');
    box.innerHTML = lines.join('');
    bindResultActions(box);
    return;
  }
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
  lastExportDir = r.total_dir || lastExportDir;
  lines.push(`<div class="res-row ${r.cancelled ? 'res-part' : (failed.length ? 'res-part' : 'res-ok')}">` +
    (r.cancelled ? '⏹ 已取消：' : '✔ 导出完成：') +
    `${r.ok_count}/${r.sessions.length} 个会话` +
    (failed.length ? `（${failed.length} 个失败）` : '') +
    `，共 ${r.message_count} 条消息，图片 ${r.image_count || 0}，语音 ${r.voice_count || 0}，头像 ${r.avatar_count}（${dur}s）` +
    (r.cancelled ? '（已完成部分的产物保留）' : '') + '</div>');
  if (echo) lines.push(echo);
  if (r.total_dir) {
    lines.push(`<div class="res-meta">目录：<span class="mono">${esc(r.total_dir)}</span></div>`);
    // 路径只放 data 属性，点击时再从 dataset 读取——内联 onclick 拼
    // Windows 路径会被 JS 转义吃掉反斜杠（D:\x → D:x），后端必然拒绝
    lines.push(`<div class="res-actions"><a class="btn" href="#" data-open-path="${esc(r.total_dir)}">📂 打开目录</a></div>`);
  }
  // 方案 A：本次导出含 JSON 时，结果面板可就地补生成 Markdown/HTML
  // （转换器吃的是会话 JSON；不含 JSON 的导出没有可转换对象，不显示）
  const fmts = Array.isArray(r.format) ? r.format : [r.format];
  if (r.total_dir && fmts.some(f => String(f).toLowerCase() === 'json')) {
    lines.push(convertActionsHtml(['md', 'html']));
  }
  if (r.zip) {
    lines.push(`<div class="res-actions"><a class="btn" href="/api/export/download?path=${encodeURIComponent(r.zip)}">📥 下载整体 ZIP${r.zip_size ? `（${fmtSize(r.zip_size)}）` : ''}</a></div>`);
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

/** 结果框里的按钮：重试（失败路径）、打开目录（只读 dataset，避免路径被
 *  JS 转义吃掉反斜杠）、失败信息一键复制 */
/** 方案 A 入口按钮组：从当前导出目录补生成 Markdown / HTML */
function convertActionsHtml(formats) {
  const btns = formats.map(f =>
    `<a class="btn" href="#" data-convert="${f}">${f === 'md' ? '📝 补生成 Markdown' : '🌐 补生成 HTML'}</a>`
  ).join('');
  return `<div class="res-actions">${btns}` +
    `<a class="btn" href="#" data-convert="md,html" data-convert-ow="1">↻ 覆盖重跑两种</a></div>`;
}

function bindResultActions(box) {
  box.querySelectorAll('[data-retry]').forEach(a => {
    a.addEventListener('click', (ev) => { ev.preventDefault(); run(); });
  });
  box.querySelectorAll('[data-convert]').forEach(a => {
    a.addEventListener('click', (ev) => {
      ev.preventDefault();
      runConvert(a.dataset.convert.split(','), a.dataset.convertOw === '1');
    });
  });
  box.querySelectorAll('[data-open-path]').forEach(a => {
    a.addEventListener('click', (ev) => {
      ev.preventDefault();
      SX.openPath(a.dataset.openPath);
    });
  });
  box.querySelectorAll('.copy-err').forEach(a => {
    a.addEventListener('click', (ev) => {
      ev.preventDefault();
      copyText(a.dataset.err).then(ok => toast(ok ? '已复制错误信息' : '复制失败', !ok));
    });
  });
}

export function destroy() {
  if (pollTimer) { clearInterval(pollTimer); pollTimer = null; }
  if (rosterKeyHandler) {
    document.removeEventListener('keydown', rosterKeyHandler);
    rosterKeyHandler = null;
  }
}
export { init };
