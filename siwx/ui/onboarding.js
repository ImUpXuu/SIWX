/* 首启全屏引导向导（壳层组件，由 app.js 调起）
 *
 * 两种模式：
 *   first —— 3 步：同意条款 → 选号 → 提取并解密；未完成配置前不可关闭，
 *            壳层导航由 app.js 的 overlayOpen 守卫冻结。
 *   add   —— 2 步：选号 → 提取并解密；可随时关闭（右上角 × / Esc），
 *            已配置用户从账号下拉「＋ 添加账号」或各页空态入口进入。
 *
 * 逻辑迁移自原 guide 页（状态检测 / 账号选择 / 手动路径 / 一键提取解密），
 * 样式（.ob-* 与共用账号卡）在 app.css，壳层组件不加载独立 CSS。
 */
const { esc, fetchJSON, startJob, renderLog, setSetupDone } = window.SX;

let ov = null;              // 遮罩根元素（存在即向导打开中）
let card = null;
let body = null;
let onCloseCb = null;
let disclaimerVersion = '';
let closable = false;
let state = null;
let escHandler = null;

function chipBar() {
  const labels = state.mode === 'first' ? ['同意条款', '选号', '提取并解密'] : ['选号', '提取并解密'];
  const cur = state.screen === 'disclaimer' ? 1
            : state.screen === 'accounts' ? (state.mode === 'first' ? 2 : 1)
            : (state.mode === 'first' ? 3 : 2);
  return labels.map((l, i) =>
    `<span class="g-chip${i + 1 === cur ? ' on' : (i + 1 < cur ? ' done' : '')}">${i + 1} ${esc(l)}</span>`).join('');
}

function renderHead() {
  card.querySelector('.ob-steps').innerHTML = chipBar();
  card.querySelector('.ob-close').hidden = !closable;
}

/* ── 屏 1（仅 first 模式）：欢迎 + 免责声明 ───────────────────── */
function showDisclaimer() {
  state.screen = 'disclaimer';
  renderHead();
  body.innerHTML = `
    <h2 class="g-title">欢迎使用 stories-in-wx</h2>
    <p class="dim g-desc">三步完成配置：同意条款 → 选择微信号 → 一键提取密钥并解密，然后进入主界面。<br>
       全程只读扫描微信进程，无需管理员；密钥经 DPAPI 加密保存，结果全部缓存，只跑一次。</p>
    <div class="ob-disc disc-body">加载声明…</div>
    <div class="ob-actions">
      <button class="btn" id="ob-decline">不同意并退出</button>
      <button class="btn btn-primary" id="ob-agree" disabled>请先阅读声明…</button>
    </div>`;
  const agree = body.querySelector('#ob-agree');
  const disc = body.querySelector('.ob-disc');
  fetch(`/pages/disclaimer.html?v=${Date.now()}`)
    .then(r => { if (!r.ok) throw new Error(`HTTP ${r.status}`); return r.text(); })
    .then(html => {
      if (!body.contains(disc)) return;      // 向导已切屏/关闭
      disc.innerHTML = html;
      agree.disabled = false;
      agree.textContent = '我已阅读并同意 →';
    })
    .catch(e => {
      if (!body.contains(disc)) return;
      disc.innerHTML = `<div class="empty">免责声明加载失败：${esc(e.message)}<br>请刷新页面重试。</div>`;
      agree.disabled = false;
      agree.textContent = '我已阅读并同意 →';
    });
  body.querySelector('#ob-decline').addEventListener('click', () => {
    window.close();                          // 仅对脚本打开的窗口有效
    body.querySelector('.ob-actions').remove();
    body.insertAdjacentHTML('beforeend',
      '<div class="card"><h2>你未同意免责声明</h2>' +
      '<p class="dim">已停止提供服务，请关闭本页并删除本工具。若为误点，请刷新页面重新阅读。</p></div>');
  });
  agree.addEventListener('click', () => {
    if (disclaimerVersion) localStorage.setItem('siwx-disclaimer-ack', disclaimerVersion);
    showAccounts();
  });
}

/* ── 账号列表（向导 / 设置页共用的徽标逻辑）──────────────────── */
function badge(cached, total) {
  if (!total) return '<span class="badge badge-none">无数据</span>';
  if (cached >= total) return `<span class="badge badge-full">缓存 ✓ ${cached}/${total}</span>`;
  if (cached > 0) return `<span class="badge badge-part">缓存 ${cached}/${total}</span>`;
  return '<span class="badge badge-none">未提取</span>';
}

/* ── 屏 2：选号 ──────────────────────────────────────────────── */
function showAccounts() {
  state.screen = 'accounts';
  state.account = null;
  renderHead();
  body.innerHTML = `
    <h2 class="g-title">选择微信号</h2>
    <p class="dim g-desc">已列出本机全部数据目录；带「缓存 ✓」的账号之前配置过，重跑也是秒回。</p>
    <div id="ob-check" class="g-check">正在检测环境…</div>
    <div id="ob-accounts" class="accounts"><div class="empty">检测中…</div></div>
    <div class="ob-manual-entry">
      <button class="btn btn-sm" id="ob-manual-toggle">＋ 手动指定数据目录</button>
      <div class="g-manual" id="ob-manual-box" hidden>
        <div class="g-manual-title">直接指定微信数据目录</div>
        <p class="dim g-manual-hint">可填写 xwechat_files 根目录、账号目录、db_storage、其内部子目录或具体 .db 文件；根目录下有多个账号也会一起识别。<br>
           微信内获取路径：微信设置 → 文件管理 → 打开文件夹 → 复制地址栏路径。</p>
        <div class="g-manual-row">
          <input id="ob-manual-path" class="g-input" type="text" placeholder="例如: D:\\xwechat_files\\wxid_xxx\\db_storage">
          <button class="btn btn-primary" id="ob-manual-add">验证并保存</button>
        </div>
        <div id="ob-manual-msg" class="g-manual-msg"></div>
      </div>
    </div>
    <div class="ob-actions">
      ${state.mode === 'first' ? '<button class="btn" id="ob-back">← 上一步</button>' : ''}
      <button class="btn btn-primary" id="ob-next" disabled>下一步 →</button>
    </div>`;
  body.querySelector('#ob-back')?.addEventListener('click', showDisclaimer);
  body.querySelector('#ob-next').addEventListener('click', () => {
    if (state.account) showExtract();
  });
  body.querySelector('#ob-manual-toggle').addEventListener('click', () => {
    body.querySelector('#ob-manual-box').hidden = !body.querySelector('#ob-manual-box').hidden;
  });
  body.querySelector('#ob-manual-add').addEventListener('click', addManualPath);
  checkEnv();
}

async function checkEnv() {
  const box = body.querySelector('#ob-check');
  if (!box) return;
  box.textContent = '正在检测环境…';
  try {
    const s = await fetchJSON('/api/status');
    if (!body.contains(box)) return;
    state.accounts = s.accounts || [];
    state.wechatRunning = s.wechat_running;
    const wx = s.wechat_running
      ? `<div><span class="dot ok"></span>微信运行中（${s.pids.length} 进程）</div>`
      : `<div><span class="dot warn"></span>微信未运行 —— 提取密钥需要微信在线，请先启动并登录</div>`;
    const accs = state.accounts.length
      ? `<div><span class="dot ok"></span>发现 ${state.accounts.length} 个微信号数据目录</div>`
      : `<div><span class="dot warn"></span>未自动找到微信数据目录 —— 可在下方手动指定路径</div>`;
    const ks = s.stored_salts
      ? `<div><span class="dot ok"></span>密钥库已有 ${s.stored_salts} 条缓存</div>`
      : `<div><span class="dot dim"></span>密钥库为空（首次运行）</div>`;
    box.innerHTML = wx + accs + ks;
    renderAccountList();
  } catch (e) {
    if (!body.contains(box)) return;
    box.innerHTML = `<div><span class="dot err"></span>后端离线：${esc(e.message)}</div>`;
    const retry = document.createElement('button');
    retry.className = 'btn btn-sm';
    retry.textContent = '重新检测';
    retry.style.marginTop = '8px';
    retry.addEventListener('click', checkEnv);
    box.appendChild(retry);
  }
}

function renderAccountList() {
  const el0 = body.querySelector('#ob-accounts');
  if (!el0) return;
  const seen = new Set();
  const all = [...(state.accounts || []), ...(state.manualAccounts || [])].filter(a => {
    const key = String(a.db_dir || '').toLowerCase();
    if (seen.has(key)) return false;
    seen.add(key);
    return true;
  });
  if (!all.length) {
    el0.innerHTML = '<div class="empty">未找到账号 — 可用下方「手动指定数据目录」添加</div>';
    return;
  }
  el0.innerHTML = all.map((a, i) => {
    const isManual = !!a.manual;
    const isSel = state.account && state.account.db_dir === a.db_dir;
    return `
    <div class="acc ${isSel ? 'sel' : ''}" data-i="${i}">
      <div class="avatar">${esc((a.wxid || '').replace(/^wxid_/, '').slice(0, 2).toUpperCase())}</div>
      <div class="acc-main"><b>${esc(a.wxid)}</b><span>${a.db_count || '?'} 个数据库${isManual ? ' · 手动' : ''}</span></div>
      ${isManual ? '<span class="pill pill-gray">手动</span>' : badge(a.keys_cached, a.total_salts)}
    </div>`;
  }).join('');
  el0.querySelectorAll('.acc').forEach(node => {
    node.addEventListener('click', () => {
      state.account = all[Number(node.dataset.i)];
      el0.querySelectorAll('.acc').forEach(c => c.classList.remove('sel'));
      node.classList.add('sel');
      body.querySelector('#ob-next').disabled = false;
    });
  });
  // 只有一个账号时自动选中，省一次点击
  if (!state.account && all.length === 1) {
    state.account = all[0];
    el0.querySelector('.acc')?.classList.add('sel');
    body.querySelector('#ob-next').disabled = false;
  }
}

async function addManualPath() {
  const input = body.querySelector('#ob-manual-path');
  const msg = body.querySelector('#ob-manual-msg');
  const add = body.querySelector('#ob-manual-add');
  const path = input.value.trim();
  if (!path) {
    msg.textContent = '请输入路径';
    msg.className = 'g-manual-msg err';
    return;
  }
  add.disabled = true;
  const oldText = add.textContent;
  add.textContent = '验证中…';
  msg.textContent = '';
  try {
    const r = await fetch('/api/discover/validate', {
      method: 'POST',
      headers: { 'content-type': 'application/json' },
      body: JSON.stringify({ path }),
    });
    const d = await r.json();
    if (d.ok) {
      if (!state.manualAccounts) state.manualAccounts = [];
      const items = (d.accounts?.length ? d.accounts : [{ wxid: d.wxid, db_dir: d.db_dir, db_count: d.db_count }])
        .map(a => ({ wxid: a.wxid, db_dir: a.db_dir, db_count: a.db_count || 0,
                     keys_cached: 0, total_salts: 0, manual: true }));
      for (const acc of items) {
        const exists = [...(state.accounts || []), ...state.manualAccounts]
          .some(a => String(a.db_dir || '').toLowerCase() === String(acc.db_dir || '').toLowerCase());
        if (!exists) state.manualAccounts.push(acc);
      }
      state.account = items[0];
      msg.textContent = `已保存 ${items.length} 个账号：${items.map(a => a.wxid).join('、')}`;
      msg.className = 'g-manual-msg ok';
      input.value = '';
      renderAccountList();
    } else {
      msg.textContent = d.error;
      msg.className = 'g-manual-msg err';
    }
  } catch (e) {
    msg.textContent = `验证失败: ${e.message}`;
    msg.className = 'g-manual-msg err';
  } finally {
    add.disabled = false;
    add.textContent = oldText;
  }
}

/* ── 屏 3：提取并解密 ────────────────────────────────────────── */
function showExtract() {
  state.screen = 'extract';
  state.offlineConfirmed = false;
  renderHead();
  const isMac = navigator.platform === 'MacIntel' || navigator.platform.includes('Mac');
  body.innerHTML = `
    <h2 class="g-title">提取密钥并解密</h2>
    ${isMac ? '<div class="g-macos-hint">macOS 提示：若密钥未缓存，请在点击「提取并解密」前 <b>60 秒内重新登录微信</b>（退出再打开），确保密钥在内存中。</div>' : ''}
    <p class="dim g-desc" id="ob-extract-hint"></p>
    <div id="ob-wxwarn" class="ob-warn" hidden>
      <b>未检测到微信进程。</b>提取密钥需要微信正在运行并已登录；也可离线提取（仅使用已缓存的密钥）。
      <div class="ob-warn-row">
        <button class="btn btn-sm" id="ob-wx-recheck">我已启动微信，重新检测</button>
        <button class="btn btn-sm" id="ob-wx-skip">仍然继续（离线提取）</button>
      </div>
    </div>
    <div class="ob-actions">
      <button class="btn" id="ob-back2">← 上一步</button>
      <button class="btn btn-primary" id="ob-run">提取并解密</button>
      <button class="btn btn-primary hidden" id="ob-finish">完成并进入主界面</button>
    </div>
    <div id="ob-progress" class="progress hidden"><div class="bar"></div></div>
    <div id="ob-log" class="console hidden"></div>
    <div id="ob-result" class="g3-res"></div>`;
  const hint = body.querySelector('#ob-extract-hint');
  hint.textContent = state.account
    ? `对 ${state.account.wxid} 一键提取密钥并解密${state.account.keys_cached ? `（${state.account.keys_cached}/${state.account.total_salts} 已缓存）` : ''}。请保持微信在线；已有缓存则秒回。`
    : '';
  body.querySelector('#ob-back2').addEventListener('click', showAccounts);
  body.querySelector('#ob-run').addEventListener('click', tryRun);
  body.querySelector('#ob-finish').addEventListener('click', finish);
  body.querySelector('#ob-wx-recheck').addEventListener('click', async () => {
    try {
      const s = await fetchJSON('/api/status');
      state.wechatRunning = s.wechat_running;
      if (s.wechat_running) body.querySelector('#ob-wxwarn').hidden = true;
    } catch (e) { /* 保留警告框，用户可再试或离线继续 */ }
  });
  body.querySelector('#ob-wx-skip').addEventListener('click', () => {
    state.offlineConfirmed = true;
    body.querySelector('#ob-wxwarn').hidden = true;
    runExtract();
  });
}

function tryRun() {
  if (state.running || !state.account) return;
  if (state.wechatRunning === false && !state.offlineConfirmed) {
    body.querySelector('#ob-wxwarn').hidden = false;   // 卡片内联确认，替代 window.confirm
    return;
  }
  runExtract();
}

function runExtract() {
  state.running = true;
  const runBtn = body.querySelector('#ob-run');
  const finishBtn = body.querySelector('#ob-finish');
  const progress = body.querySelector('#ob-progress');
  const logBox = body.querySelector('#ob-log');
  const resBox = body.querySelector('#ob-result');
  runBtn.disabled = true;
  finishBtn.classList.add('hidden');
  progress.classList.remove('hidden');
  logBox.classList.remove('hidden');
  logBox.innerHTML = '';
  resBox.innerHTML = '';
  startJob({ mode: 'auto', db_dir: state.account.db_dir },
    logs => { if (body.contains(logBox)) renderLog(logBox, logs); },
    job => {
      state.running = false;
      progress.classList.add('hidden');
      if (job.error) {
        resBox.innerHTML = `<span class="badge badge-none">提取失败：${esc(job.error)}</span>`;
        runBtn.disabled = false;              // 失败后允许直接重试
        return;
      }
      const accs = (job.report && job.report.accounts) || [];
      const mine = accs.find(a => state.account && a.db_dir === state.account.db_dir) || accs[0];
      if (mine) {
        const full = mine.verified >= mine.total_salts;
        const dec = mine.decrypt;
        resBox.innerHTML =
          `<span class="badge ${full ? 'badge-full' : 'badge-part'}">密钥 ${mine.verified}/${mine.total_salts}</span>` +
          (dec ? ` <span class="badge badge-full">解密 ${dec.ok} 库（缓存 ${dec.cached || 0}）</span>` : '');
      }
      runBtn.classList.add('hidden');         // 成功后主按钮切换为「完成」
      finishBtn.classList.remove('hidden');
      if (body.contains(resBox)) resBox.scrollIntoView({ block: 'nearest' });
    });
}

function finish() {
  if (state.mode === 'first') setSetupDone();
  const cb = onCloseCb;
  teardown();
  cb && cb({ finished: true, account: state ? state.account?.wxid : null });
}

function close() {
  if (!closable || !ov) return;
  const cb = onCloseCb;
  const acc = state ? state.account?.wxid : null;
  teardown();
  cb && cb({ finished: false, account: acc });
}

function teardown() {
  if (escHandler) document.removeEventListener('keydown', escHandler, true);
  escHandler = null;
  if (ov) { ov.remove(); ov = null; card = null; body = null; }
}

/* ── 入口 ────────────────────────────────────────────────────── */
export function open(opts) {
  if (ov) return;
  state = {
    mode: opts.mode === 'add' ? 'add' : 'first',
    screen: null, account: null, running: false, offlineConfirmed: false,
    accounts: [], manualAccounts: [], wechatRunning: null,
  };
  disclaimerVersion = opts.disclaimerVersion || '';
  closable = !!opts.closable;
  onCloseCb = opts.onClose || null;

  ov = document.createElement('div');
  ov.className = 'ob-overlay';
  ov.innerHTML = `
    <div class="ob-card">
      <div class="ob-head">
        <div class="ob-steps"></div>
        <button class="mini-btn ob-close" title="关闭向导" aria-label="关闭向导">✕</button>
      </div>
      <div class="ob-body"></div>
    </div>`;
  document.body.appendChild(ov);
  card = ov.firstElementChild;
  body = card.querySelector('.ob-body');
  card.querySelector('.ob-close').addEventListener('click', close);
  if (closable) {
    escHandler = (ev) => { if (ev.key === 'Escape') { close(); ev.preventDefault(); } };
    document.addEventListener('keydown', escHandler, true);
  }
  if (state.mode === 'first') showDisclaimer();
  else showAccounts();
}
