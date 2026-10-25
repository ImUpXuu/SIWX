/* stories-in-wx 壳：hash 路由 + 模块化页面加载器（pages/<name>.html/js/css）
 *
 * 页面来源有两类：
 *   内置：/pages/<name>.*        （PAGES_BUILTIN 固定顺序）
 *   插件：/plugin-pages/<plugin>/<file>.*（由 /api/plugins/pages 动态下发）
 * 插件菜单项**追加在内置之后**，与内置项完全平级（同样式、同高亮逻辑）。
 *
 * 全局账号：微信号是身份而不是筛选器。账号卡是全站唯一切换入口，
 * 选中值经 SX.getAccount() 对所有页面可见；切换后当前页面自动重载。
 */
(function () {
  const PAGES_BUILTIN = ['chat', 'sns', 'stats', 'export', 'backup', 'mcp', 'logs', 'settings'];
  // 静态资源缓存戳（同时作为页面模块的 ?v=）。改了 ui/ 下的 js/css 就 +1，
  // 并同步 siwx/ui/index.html 的 /common.js?v= 与 /app.js?v=（tests/test_version_sync.py 断言两者一致）。
  const UI_VERSION = '2026102404';
  // 免责声明条款版本：条款有实质更新时改此值，控制台会要求重新确认
  const DISCLAIMER_VERSION = '20260925';

  let pluginPages = [];            // 服务端已按显示条件过滤
  let current = null;              // { name, mod }
  let loadedCss = {};

  /** 全部可用页面名（内置 + 插件） */
  function allPages() {
    return PAGES_BUILTIN.concat(pluginPages.map(p => p.id));
  }

  function findPage(name) {
    return pluginPages.find(p => p.id === name) || null;
  }

  function defaultRoute() {
    // 首启引导由全屏向导（onboarding.js）承担，不再有引导路由页
    return 'chat';
  }

  function ensureCss(href) {
    const key = `${href}?v=${UI_VERSION}`;
    if (loadedCss[key]) return;
    loadedCss[key] = true;
    const link = document.createElement('link');
    link.rel = 'stylesheet';
    link.href = key;
    document.head.appendChild(link);
  }

  /* ── 全局账号状态 ───────────────────────────────────────────
   * 所有页面 init 时从 SX.getAccount() 读取；切换账号由壳层统一
   * 重载当前页面，页面内不再各自维护账号下拉。 */
  let accounts = [];               // [{ wxid, ... }]
  let account = null;              // 当前 wxid 或 null
  let accountReady = false;

  function cleanWxid(wxid) {
    return String(wxid || '').replace(/_[0-9a-f]{4}$/i, '');
  }

  function renderAccCard() {
    const nameEl = document.getElementById('acc-name');
    const wxidEl = document.getElementById('acc-wxid');
    const initialEl = document.getElementById('acc-initial');
    const img = document.getElementById('acc-img');
    if (!nameEl) return;
    if (!account) {
      nameEl.textContent = accounts.length ? '选择微信号' : '暂无账号';
      wxidEl.textContent = accounts.length ? '' : '完成引导后出现在这里';
      initialEl.textContent = '…';
      img.hidden = true;
      return;
    }
    nameEl.textContent = cleanWxid(account);
    wxidEl.textContent = account;
    initialEl.textContent = (cleanWxid(account) || '?').slice(0, 1).toUpperCase();
    img.hidden = true;             // 先藏，加载成功再显示（避免破图闪烁）
    img.onload = () => { img.hidden = false; };
    img.onerror = () => { img.hidden = true; };
    img.src = `/api/chat/avatar?account=${encodeURIComponent(account)}` +
              `&username=${encodeURIComponent(account)}`;
  }

  /** 下拉项头像：复用聊天页的头像接口；加载失败时退回首字母占位 */
  function accPopAva(wxid, initial) {
    const src = `/api/chat/avatar?account=${encodeURIComponent(wxid)}` +
                `&username=${encodeURIComponent(wxid)}`;
    return `<span class="acc-ava"><span>${SX.esc(initial)}</span>` +
           `<img data-acc-ava src="${src}" alt="" hidden></span>`;
  }

  function renderAccPop() {
    const pop = document.getElementById('acc-pop');
    if (!pop) return;
    if (!accounts.length) {
      pop.innerHTML = '<div class="acc-pop-head">还没有已解密的微信号</div>' +
        '<div class="acc-foot"><button type="button" data-acc-add>去添加账号</button></div>';
    } else {
      pop.innerHTML = '<div class="acc-pop-head">切换微信号</div>' + accounts.map(a => `
        <div class="acc-item ${a.wxid === account ? 'on' : ''}" data-acc="${SX.esc(a.wxid)}" role="option" tabindex="0" aria-selected="${a.wxid === account}">
          ${accPopAva(a.wxid, (cleanWxid(a.wxid) || '?').slice(0, 1).toUpperCase())}
          <span class="acc-meta"><b>${SX.esc(cleanWxid(a.wxid))}</b><span>${SX.esc(a.wxid)}</span></span>
        </div>`).join('') +
        '<div class="acc-foot"><button type="button" data-acc-add>＋ 添加账号</button></div>';
    }
    // 头像加载成功才显示（否则保留首字母占位）；编程式监听，兼容 CSP no-unsafe-inline
    pop.querySelectorAll('img[data-acc-ava]').forEach(img => {
      img.onload = () => { img.hidden = false; };
      img.onerror = () => { img.remove(); };
    });
    pop.querySelectorAll('[data-acc]').forEach(n => {
      const pick = () => { closeAccPop(); SX.setAccount(n.dataset.acc); };
      n.addEventListener('click', pick);
      // 补键盘激活：role=option 但无 tabindex/键盘处理时选项根本不可达
      n.addEventListener('keydown', e => {
        if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); pick(); }
      });
    });
    pop.querySelectorAll('[data-acc-add]').forEach(n => {
      n.addEventListener('click', () => { closeAccPop(); openOnboarding('add'); });
    });
  }

  function openAccPop() {
    const pop = document.getElementById('acc-pop');
    const card = document.getElementById('acc-card');
    if (!pop || !card) return;
    renderAccPop();
    pop.hidden = false;
    card.setAttribute('aria-expanded', 'true');
    const onDocDown = (ev) => {
      if (!pop.contains(ev.target) && !card.contains(ev.target)) closeAccPop();
    };
    const onKey = (ev) => { if (ev.key === 'Escape') { closeAccPop(); ev.preventDefault(); } };
    pop._close = () => {
      pop.hidden = true;
      card.setAttribute('aria-expanded', 'false');
      document.removeEventListener('pointerdown', onDocDown, true);
      document.removeEventListener('keydown', onKey, true);
      pop._close = null;
    };
    document.addEventListener('pointerdown', onDocDown, true);
    document.addEventListener('keydown', onKey, true);
  }
  function closeAccPop() {
    const pop = document.getElementById('acc-pop');
    if (pop && pop._close) pop._close();
  }

  async function loadAccounts() {
    try {
      const { accounts: list } = await SX.fetchJSON('/api/chat/accounts');
      accounts = Array.isArray(list) ? list : [];
    } catch (e) {
      accounts = [];
    }
    const saved = localStorage.getItem('siwx-account');
    account = (saved && accounts.some(a => a.wxid === saved)) ? saved
            : (accounts.length ? accounts[0].wxid : null);
    if (account) localStorage.setItem('siwx-account', account);
    accountReady = true;
    renderAccCard();
  }

  /** 切账号：持久化 → 刷新账号卡 → 重载当前页面（页面会读到新值） */
  function setAccount(wxid, opts = {}) {
    if (!accounts.some(a => a.wxid === wxid) || wxid === account) return;
    account = wxid;
    localStorage.setItem('siwx-account', wxid);
    renderAccCard();
    if (opts.reload !== false) reloadPage();
  }

  /* 对页面暴露（挂在 SX 上，见 common.js 末尾的挂载约定） */
  window.__sxAccount = {
    get: () => account,
    list: () => accounts.slice(),
    set: setAccount,
    ready: () => accountReady,
    clean: cleanWxid,
  };

  /* ── 页面导航 ─────────────────────────────────────────────── */
  function pageBase(name) {
    const p = findPage(name);
    if (!p) {
      return { html: `/pages/${name}.html`, css: `/pages/${name}.css`, js: `/pages/${name}.js` };
    }
    const dir = `/plugin-pages/${encodeURIComponent(p.plugin)}/${p.entry || 'index'}`;
    return { html: `${dir}.html`, css: `${dir}.css`, js: `${dir}.js` };
  }

  /** 页面标题（浏览器标签页联动） */
  const PAGE_TITLES = {};
  function syncTitle(name) {
    if (!PAGE_TITLES[name]) {
      const a = document.querySelector(`#menu a[data-page="${CSS.escape(name)}"]`);
      PAGE_TITLES[name] = a ? a.textContent.trim() : name;
    }
    document.title = `${PAGE_TITLES[name]} · stories-in-wx`;
  }

  function markMenu(name) {
    document.querySelectorAll('#menu a').forEach(a => {
      const on = a.dataset.page === name;
      a.classList.toggle('active', on);
      if (on) a.setAttribute('aria-current', 'page');
      else a.removeAttribute('aria-current');
    });
  }

  function currentPageName() {
    return (location.hash || '').replace(/^#\/?/, '');
  }

  /** 用户开了「减少动态效果」时全部动画降级为直接切换 */
  function prefersReducedMotion() {
    return matchMedia('(prefers-reduced-motion: reduce)').matches;
  }

  let navSeq = 0;                  // 路由代数守卫：快速连点菜单时旧导航作废

  async function navigate() {
    if (overlayOpen) return;                 // 免责声明弹层打开期间冻结导航
    const seq = ++navSeq;
    let name = currentPageName();
    if (!allPages().includes(name)) {
      name = defaultRoute();
      if (location.hash !== `#/${name}`) { location.hash = `#/${name}`; return; }
    }
    markMenu(name);
    syncTitle(name);
    const view = document.getElementById('view');
    const base = pageBase(name);
    // 先把新页面取回来再切换：旧实现 fetch 后直接 innerHTML，
    // 取页期间旧内容还在，塞入瞬间白屏——这是「切页生硬」的来源之一
    let html;
    try {
      const res = await fetch(base.html);
      if (!res.ok) throw new Error(`页面加载失败 (${res.status})`);
      html = await res.text();
    } catch (e) {
      if (seq !== navSeq) return;
      view.innerHTML = `<div class="card"><h2>页面加载失败</h2><p class="dim">${e.message}</p></div>`;
      return;
    }
    if (seq !== navSeq) return;              // 取页期间用户又点了别的页面
    ensureCss(base.css);
    const doSwap = () => {
      if (seq !== navSeq) return;            // 视图过渡期间又发起了新导航
      if (current && current.mod && current.mod.destroy) {
        try { current.mod.destroy(); } catch (e) { /* 忽略 */ }
      }
      current = null;
      view.innerHTML = html;
    };
    // 切页过渡：View Transitions API（Chrome/Edge 原生，零依赖）；
    // 不支持 / 用户要求减少动态效果时，降级为 CSS 淡入。
    if (document.startViewTransition && !prefersReducedMotion()) {
      try {
        const vt = document.startViewTransition(doSwap);
        vt.ready?.catch?.(() => {});
        vt.finished?.catch?.(() => {});
        await vt.updateCallbackDone;
      } catch (e) {
        doSwap();
      }
    } else {
      doSwap();
      if (seq !== navSeq) return;
      view.classList.remove('view-enter');
      void view.offsetWidth;                 // 强制 reflow 重启动画
      view.classList.add('view-enter');
    }
    if (seq !== navSeq) return;
    try {
      const mod = await import(`${base.js}?v=${Date.now()}`);
      if (seq !== navSeq) return;            // 模块加载期间导航已切换
      current = { name, mod };
      if (mod.init) await mod.init(view);
    } catch (e) {
      view.insertAdjacentHTML('beforeend',
        `<div class="card"><h2>页面脚本错误</h2><p class="dim">${e.message}</p></div>`);
    }
  }

  /** 账号切换后重跑当前页。
   *
   * 走完整 navigate()：它会 destroy → 重新取模板灌进 view → 重新 import → init。
   * 旧实现只 destroy 再 init，页面骨架 DOM（#c-msgs、#s-log-level…）和 init 里
   * 绑的监听器全都留在原地，于是聊天页/设置页监听器双绑——一次点击打两次
   * POST /api/run、一次确认弹两个框、设置页出现两个下拉。
   */
  async function reloadPage() {
    return navigate();
  }

  /* ── 插件菜单注入（平级追加在内置之后）──────────────────────── */
  function renderPluginMenu() {
    const menu = document.getElementById('menu');
    menu.querySelectorAll('a[data-plugin="1"]').forEach(a => a.remove());
    pluginPages.forEach(p => {
      const a = document.createElement('a');
      a.href = `#/${p.id}`;
      a.dataset.page = p.id;
      a.dataset.plugin = '1';
      if (p.group) a.dataset.group = p.group;
      const ico = document.createElement('span');
      ico.className = 'ico';
      ico.textContent = p.icon || '🧩';
      const label = document.createElement('span');
      label.textContent = p.title || p.id;
      a.append(ico, label);
      if (p.badge) {
        const b = document.createElement('span');
        b.className = 'nav-badge';
        b.textContent = p.badge;
        a.appendChild(b);
      }
      if (p.tip) a.title = p.tip;
      menu.appendChild(a);
    });
    markMenu(currentPageName());
  }

  async function loadPluginMenu() {
    try {
      const r = await fetch(`/api/plugins/pages?v=${Date.now()}`);
      const j = await r.json();
      pluginPages = Array.isArray(j.pages) ? j.pages : [];
    } catch (e) {
      pluginPages = [];                 // 插件不可用不应影响宿主
    }
    renderPluginMenu();
    // 插件可能在首屏路由之后才就绪，若当前 hash 指向插件页则补一次导航
    if (findPage(currentPageName())) navigate();
  }

  /* 侧栏微信状态 */
  async function sideStatus() {
    if (document.hidden) return;             // 后台标签页暂停轮询
    try {
      const s = await (await fetch('/api/status')).json();
      document.getElementById('side-wx').textContent =
        s.wechat_running ? `微信运行中 · ${s.pids.length} 进程` : '微信未运行';
    } catch (e) {
      document.getElementById('side-wx').textContent = '后端离线';
    }
  }

  /* ── 侧栏任务指示（审计 §4.1：全局任务槽只有一个，壳层却不显示）──
   * 只读展示：与 sideStatus 共用 5s 轮询，GET /api/job 显示运行中任务的
   * 类型与进度；点「查看」跳到对应页面（导出/补生成 → 导出页）。本模块
   * 不写任何页面状态、不发起任何写请求，与 export.js 的任务恢复逻辑互不
   * 抢状态；任务结束只在亲眼观察到 running→结束 的那一拍短暂显示终态，
   * 页面刚打开时服务端残留的历史 done 不回放。 */
  const JOB_MODE_LABELS = {
    keys: '密钥提取', decrypt: '解密', auto: '提取并解密', sync: '增量同步',
    export: '导出', convert: '补生成', sns_export: '朋友圈导出',
  };
  // 「查看」跳转目标：对齐各 mode 的发起页；未登记的一律去导出页
  const JOB_MODE_PAGE = { export: 'export', convert: 'export', sync: 'chat', sns_export: 'sns' };
  let jobWasRunning = false;
  let jobLastMode = '';
  let jobDoneTimer = null;

  function jobLabel(mode) { return JOB_MODE_LABELS[mode] || mode || '任务'; }

  function ensureSideJob() {
    let btn = document.getElementById('side-job');
    if (btn) return btn;
    btn = document.createElement('button');
    btn.type = 'button';
    btn.id = 'side-job';
    btn.hidden = true;
    btn.addEventListener('click', () => {
      const target = JOB_MODE_PAGE[jobLastMode] || 'export';
      if (currentPageName() !== target) location.hash = `#/${target}`;   // 已在目标页时不重复导航
    });
    const side = document.querySelector('.side');
    const foot = side && side.querySelector('.side-foot');
    if (!side || !foot) return btn;
    side.insertBefore(btn, document.getElementById('side-tour') || foot);
    return btn;
  }

  async function sideJob() {
    if (document.hidden) return;             // 后台标签页暂停轮询（与 sideStatus 一致）
    const btn = ensureSideJob();
    if (!btn.isConnected) return;            // 侧栏尚未挂载
    let j;
    try {
      j = await (await fetch('/api/job')).json();
    } catch (e) {
      btn.hidden = true;                     // 后端离线：sideStatus 已有提示，不留过期指示
      return;
    }
    if (j.running) {
      jobWasRunning = true;
      jobLastMode = j.mode || '';
      clearTimeout(jobDoneTimer);
      const pct = j.progress && Number(j.progress.pct) > 0 ? ` · ${j.progress.pct}%` : '';
      btn.className = 'side-job';
      btn.innerHTML = '<span class="dot run"></span>' +
        `<span class="side-job-label">${SX.esc(jobLabel(j.mode))}${pct} · 运行中</span>` +
        '<span class="side-job-go">查看</span>';
      btn.hidden = false;
      return;
    }
    if (!jobWasRunning) return;              // 未见运行中的空闲拍（含历史 done）：不打扰
    jobWasRunning = false;                   // 亲眼看到 running → 结束：短暂显示一次终态
    const cancelled = !!(j.report && j.report.cancelled);
    const ok = !!j.ok && !j.error && !cancelled;
    const cls = ok ? 'ok' : (cancelled ? 'warn' : 'err');
    const suffix = ok ? ' · 已完成' : (cancelled ? ' · 已取消' : ' · 已失败');
    btn.className = 'side-job' + (ok ? ' done' : (cancelled ? ' cancel' : ' fail'));
    btn.innerHTML = `<span class="dot ${cls}"></span>` +
      `<span class="side-job-label">${SX.esc(jobLabel(jobLastMode))}${suffix}</span>`;
    btn.hidden = false;
    clearTimeout(jobDoneTimer);
    jobDoneTimer = setTimeout(() => { btn.hidden = true; }, 6000);
  }

  function initTheme() {
    const saved = localStorage.getItem('siwx-theme');
    if (saved === 'dark' || (!saved && matchMedia('(prefers-color-scheme: dark)').matches)) {
      document.documentElement.dataset.theme = 'dark';
    }
  }

  /* ── 首启引导向导（onboarding.js）────────────────────────
   * 未完成配置时全屏接管；已配置用户从账号下拉 / 各页空态进入 add 模式。
   * overlayOpen 与免责声明弹层共用：任一打开期间 navigate() 冻结。
   * 跳过过引导的用户（siwx-setup-skipped）例外：向导以可关闭的 add 模式
   * 续跑且 completable（完成提取即计为完成配置），不再被不可关闭向导锁死。 */
  let onboardingMod = null;

  function openOnboarding(mode) {
    if (overlayOpen) return;
    const done = SX.setupDone();
    const skipped = setupSkipped();
    if (mode === 'add' && !done && !skipped) mode = 'first';   // 未配置完不允许跳过完整向导
    if (mode === 'first' && skipped) mode = 'add';             // 跳过过引导：不再进不可关闭的完整向导
    overlayOpen = true;
    (async () => {
      try {
        if (!onboardingMod) onboardingMod = await import(`/onboarding.js?v=${UI_VERSION}`);
        onboardingMod.open({
          mode,
          completable: !done,              // 跳过后续跑：完成提取即视为完成配置
          disclaimerVersion: DISCLAIMER_VERSION,
          closable: done || skipped,
          onClose: async (res) => {
            overlayOpen = false;
            if (res && res.finished) {
              localStorage.removeItem(SETUP_SKIPPED_KEY);   // 完成配置：清除跳过痕迹
            }
            updateSetupHint();
            if (res && res.finished) {
              await loadAccounts();
              if (res.account && accounts.some(a => a.wxid === res.account)) {
                setAccount(res.account, { reload: false });
              }
              if (currentPageName() !== 'chat') location.hash = '#/chat';
              else navigate();
              // 本次向导真正完成了配置（first 完整向导 / 跳过后的续跑）：
              // 首次进入主界面放一轮分步导览（看过或跳过即不再自动弹）
              if (!done && !tourDone()) maybeStartTour(true);
            } else {
              navigate();          // 关闭向导后刷新当前页（账号列表可能已变化）
            }
          },
        });
      } catch (e) {
        overlayOpen = false;
        const view = document.getElementById('view');
        if (view) {
          view.innerHTML = `<div class="card"><h2>引导向导加载失败</h2><p class="dim">${SX.esc(String(e))}</p></div>`;
        }
      }
    })();
  }

  window.__sxOnboarding = { open: openOnboarding };
  window.__sxDisclaimer = { open: () => openDisclaimer('review') };

  /* ── 侧栏「引导未完成」常驻提示 ─────────────────────────────
   * 首启提取失败可「暂时跳过」（onboarding.js 写 siwx-setup-skipped）。
   * 跳过不是完成：siwx-setup-done 仍为空，重启不再弹全屏向导，但本入口
   * 常驻侧栏，点击以 add 模式续跑向导；完成配置后跳过痕迹一并清除。 */
  const SETUP_SKIPPED_KEY = 'siwx-setup-skipped';
  function setupSkipped() { return localStorage.getItem(SETUP_SKIPPED_KEY) === '1'; }

  function updateSetupHint() {
    let btn = document.getElementById('side-setup-hint');
    if (!btn) {
      const sideFoot = document.querySelector('.side .side-foot');
      if (!sideFoot) return;
      btn = document.createElement('button');
      btn.type = 'button';
      btn.id = 'side-setup-hint';
      btn.className = 'setup-hint';
      btn.hidden = true;
      btn.innerHTML = '<span class="dot warn"></span><span><b>引导未完成</b>，点击继续配置</span>';
      btn.addEventListener('click', () => {
        if (overlayOpen) return;
        if (!disclaimerAcked()) { openDisclaimer('gate'); return; }  // 条款先行：跳过不豁免门禁
        openOnboarding('add');
      });
      sideFoot.parentElement.insertBefore(btn, sideFoot);
    }
    btn.hidden = SX.setupDone() || !setupSkipped();
  }

  /* 弹层（向导/免责声明）打开期间壳层不可聚焦、不可交互：补齐导航冻结
   * 之外的焦点门禁，键盘 Tab 也进不去被遮住的菜单。旧浏览器忽略 inert，
   * 无副作用。onboarding.js 对自身生命周期做同样处理。 */
  function shellInert(on) {
    const shell = document.querySelector('.shell');
    if (!shell) return;
    if (on) shell.setAttribute('inert', '');
    else shell.removeAttribute('inert');
  }

  /* ── 免责声明弹层 ─────────────────────────────────────────
   * 常驻全文入口在「设置 → 隐私与免责声明」（window.__sxDisclaimer）。
   * 首启由全屏向导的第 1 步承担；仅当条款版本更新（siwx-disclaimer-ack
   * 落后于 DISCLAIMER_VERSION）时，对已配置用户再弹一次本声明要求确认。 */
  let overlayOpen = false;

  function disclaimerAcked() {
    return localStorage.getItem('siwx-disclaimer-ack') === DISCLAIMER_VERSION;
  }

  async function openDisclaimer(mode) {   // 'gate' 需确认 | 'review' 重读
    if (overlayOpen) return;
    overlayOpen = true;
    const ov = document.createElement('div');
    ov.className = 'disc-overlay';
    const card = document.createElement('div');
    card.className = 'disc-card';

    const head = document.createElement('div');
    head.className = 'disc-head';
    const h2 = document.createElement('h2');
    h2.textContent = '免责声明 · 法律声明';
    const ver = document.createElement('span');
    ver.className = 'hint';
    ver.textContent = `v${DISCLAIMER_VERSION}`;
    head.append(h2, ver);

    const sub = document.createElement('div');
    sub.className = 'disc-sub';
    // gate 只对已配置用户弹出（首启确认由向导第 1 步承担），故措辞按「再确认」
    sub.textContent = mode === 'gate'
      ? '条款已更新，请阅读并重新确认本声明后继续使用。'
      : '以下是本声明的全文。';

    const body = document.createElement('div');
    body.className = 'disc-body';
    body.textContent = '加载中…';

    const actions = document.createElement('div');
    actions.className = 'disc-actions';
    let accept = null;
    if (mode === 'gate') {
      const decline = document.createElement('button');
      decline.className = 'btn';
      decline.textContent = '不同意并退出';
      decline.addEventListener('click', () => {
        window.close();                       // 仅对脚本打开的窗口有效
        actions.style.display = 'none';
        body.innerHTML = '';
        body.insertAdjacentHTML('beforeend',
          '<div class="card"><h2>你未同意免责声明</h2>' +
          '<p class="dim">已停止提供服务，请关闭本页并删除本工具。若为误点，可返回重新阅读。</p></div>');
        const back = document.createElement('button');
        back.className = 'btn';
        back.textContent = '← 返回重新阅读';
        back.style.marginTop = '10px';
        back.addEventListener('click', () => {
          actions.style.display = '';
          loadBody();
        });
        body.appendChild(back);
      });
      accept = document.createElement('button');
      accept.className = 'btn btn-primary';
      accept.textContent = '我已阅读并同意';
      accept.disabled = true;                 // 条款加载完成前禁止确认（fail-open 修复）
      accept.addEventListener('click', () => {
        localStorage.setItem('siwx-disclaimer-ack', DISCLAIMER_VERSION);
        localStorage.setItem('siwx-disclaimer-ack-ts', String(Date.now()));  // 前端留痕：确认时间戳
        ov.remove();
        overlayOpen = false;
        shellInert(false);
        navigate();
      });
      actions.append(decline, accept);
    } else {
      const close = document.createElement('button');
      close.className = 'btn btn-primary';
      close.textContent = '关闭';
      close.addEventListener('click', () => { ov.remove(); overlayOpen = false; shellInert(false); });
      actions.append(close);
    }

    card.append(head, sub, body, actions);
    ov.appendChild(card);
    document.body.appendChild(ov);
    shellInert(true);                         // 弹层打开期间壳层不可聚焦/不可交互

    /* 全文加载（初始 + 重试共用）；gate 模式下加载失败保持「同意」禁用，
       条款一字未显示不得完成法律确认。 */
    async function loadBody() {
      body.textContent = '加载中…';
      try {
        const r = await fetch(`/pages/disclaimer.html?v=${UI_VERSION}`);
        if (!r.ok) throw new Error(`HTTP ${r.status}`);
        body.innerHTML = await r.text();
        if (accept) accept.disabled = false;
      } catch (e) {
        body.innerHTML = '';
        body.insertAdjacentHTML('beforeend',
          '<div class="card"><h2>免责声明加载失败</h2>' +
          `<p class="dim">${SX.esc(String(e))}</p>` +
          '<p class="dim">请检查后端服务是否正常；也可前往 GitHub 仓库（github.com/ImUpXuu/SIWX）阅读 README 中的完整免责声明。</p></div>');
        if (accept) accept.disabled = true;
        const retry = document.createElement('button');
        retry.className = 'btn';
        retry.textContent = '重新加载';
        retry.style.marginTop = '10px';
        retry.addEventListener('click', loadBody);
        body.appendChild(retry);
      }
    }
    loadBody();
  }

  /* ── 首进导览（分步遮罩，壳层组件；审计 §2.5：核心用法链零覆盖）──
   * 半透明遮罩挖孔高亮目标元素 + 说明气泡；每步「下一步 / 跳过」，Esc 退出。
   * 看完或跳过都写 siwx-tour-done：自动播放只出现一次；侧栏常驻
   * 「使用导览」入口对所有人（含跳过过引导的用户与老用户）可重看。
   * 目标元素不在当前页（如导出页的开始按钮）时先导航过去等它出现；等不到
   * 就退化高亮侧栏对应菜单项，再不行气泡居中——导览绝不因元素变动报错卡死。 */
  const TOUR_DONE_KEY = 'siwx-tour-done';
  const TOUR_ICON = '<svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="12" r="9"/><path d="M9.5 9.3a2.5 2.5 0 1 1 3.9 2.1c-.8.5-1.4 1-1.4 1.9"/><path d="M12 17h.01"/></svg>';

  const TOUR_STEPS = [
    { sel: '#menu', title: '侧栏导航：各页各管一摊',
      text: '聊天查看浏览记录、朋友圈看动态、统计看数据、导出拿文件、日志排障、设置调选项——点菜单随时切换。' },
    { sel: '#e-run', page: 'export', fb: '#menu a[data-page="export"]',
      title: '导出三步',
      text: '进入「导出」页：勾选要导出的会话 → 选格式与打包方式 → 点绿色「开始导出」按钮（进页后高亮的那个）。' },
    { sel: '#e-result', page: 'export', fb: '#menu a[data-page="export"]',
      title: '完成后拿文件',
      text: '导出完成后，运行日志卡底部的结果区会出现「📂 打开目录」按钮，点它直接打开导出文件夹。' },
    { sel: '#side-wx', fb: '#side-foot', title: '任务与状态',
      text: '这里显示微信连接状态；导出、同步等任务运行时，上方会出现任务进度条和「查看」入口，点了直达对应页面。' },
    { sel: '#side-tour', title: '随时重看',
      text: '忘了怎么用？点侧栏的「使用导览」随时重看本轮导览。' },
  ];

  function tourDone() { return localStorage.getItem(TOUR_DONE_KEY) === '1'; }
  function markTourDone() { localStorage.setItem(TOUR_DONE_KEY, '1'); }

  function tourElVisible(el) {
    return !!el && !!el.getBoundingClientRect && el.getBoundingClientRect().width > 0;
  }

  function tourWaitFor(sel, timeoutMs) {
    return new Promise((resolve) => {
      const t0 = Date.now();
      const tick = () => {
        const el = document.querySelector(sel);
        if (tourElVisible(el)) { resolve(el); return; }
        if (Date.now() - t0 >= timeoutMs) { resolve(null); return; }
        setTimeout(tick, 120);
      };
      tick();
    });
  }

  async function tourResolveTarget(step) {
    if (step.page && currentPageName() !== step.page) {
      location.hash = `#/${step.page}`;
      const found = await tourWaitFor(step.sel, 3000);
      if (tourElVisible(found)) return found;
    }
    const el = document.querySelector(step.sel);
    if (tourElVisible(el)) return el;
    const fb = step.fb ? document.querySelector(step.fb) : null;
    return tourElVisible(fb) ? fb : null;
  }

  /* 挖孔矩形：目标 rect 外扩一点；过小/不可见的目标（如空结果区）给最小
   * 可见尺寸，并整体夹在视口内，窄窗口也不出界。 */
  function tourRectFor(el) {
    const r = el.getBoundingClientRect();
    const pad = 6;
    const w = Math.min(Math.max(r.width, 72), innerWidth - 16);
    const h = Math.min(Math.max(r.height, 44), innerHeight - 16);
    const cx = Math.min(Math.max(r.left + r.width / 2, 0), innerWidth);
    const cy = Math.min(Math.max(r.top + r.height / 2, 0), innerHeight);
    return {
      left: Math.min(Math.max(cx - w / 2 - pad, 8), Math.max(8, innerWidth - w - pad * 2)),
      top: Math.min(Math.max(cy - h / 2 - pad, 8), Math.max(8, innerHeight - h - pad * 2)),
      width: Math.min(w + pad * 2, innerWidth - 16),
      height: Math.min(h + pad * 2, innerHeight - 16),
    };
  }

  function tourPlace() {
    if (!tour) return;
    if (tour.el && tour.el.isConnected && tourElVisible(tour.el)) {
      const rect = tourRectFor(tour.el);
      Object.assign(tour.spot.style, {
        display: 'block',
        left: rect.left + 'px', top: rect.top + 'px',
        width: rect.width + 'px', height: rect.height + 'px',
      });
      // 气泡：优先放目标下方，放不下放上方，再不行就近贴放；左右夹在视口内
      const bw = tour.bubble.offsetWidth, bh = tour.bubble.offsetHeight;
      let top;
      if (rect.top + rect.height + bh + 14 < innerHeight) top = rect.top + rect.height + 12;
      else if (rect.top - bh - 14 > 0) top = rect.top - bh - 12;
      else top = Math.min(Math.max(12, rect.top + rect.height + 12), innerHeight - bh - 12);
      tour.bubble.style.top = Math.max(12, top) + 'px';
      tour.bubble.style.left =
        Math.min(Math.max(12, rect.left + rect.width / 2 - bw / 2), innerWidth - bw - 12) + 'px';
      tour.bubble.classList.remove('center');
    } else {
      tour.spot.style.display = 'none';       // 无可高亮目标：遮罩退化为居中说明
      tour.bubble.classList.add('center');
      tour.bubble.style.top = '';
      tour.bubble.style.left = '';
    }
  }

  async function tourShow(idx) {
    if (!tour) return;
    tour.idx = idx;
    // 每步在此统一解析目标（可能跨页导航等待元素）；等待期间导览被关闭则放弃
    tour.el = await tourResolveTarget(TOUR_STEPS[idx]);
    if (!tour) return;
    const step = TOUR_STEPS[idx];
    const last = idx === TOUR_STEPS.length - 1;
    tour.bubble.innerHTML =
      `<div class="tour-step-tag">${idx + 1} / ${TOUR_STEPS.length}</div>` +
      `<h3>${SX.esc(step.title)}</h3><p>${SX.esc(step.text)}</p>` +
      '<div class="tour-actions">' +
      '<button type="button" class="btn tour-skip">跳过导览</button>' +
      '<span class="tour-grow"></span>' +
      `<button type="button" class="btn btn-primary tour-next">${last ? '完成 ✓' : '下一步 →'}</button>` +
      '</div>';
    tour.bubble.querySelector('.tour-skip').addEventListener('click', tourClose);
    tour.bubble.querySelector('.tour-next').addEventListener('click', () => {
      if (last) { tourClose(); return; }
      const nextBtn = tour && tour.bubble.querySelector('.tour-next');
      if (nextBtn) nextBtn.disabled = true;   // 等待跨页定位期间防连点
      tourShow(idx + 1);
    });
    tour.bubble.querySelector('.tour-next').focus();
    tourPlace();
  }

  let tour = null;   // { idx, root, spot, bubble, el, onKey, onMove }

  function tourStart() {
    if (tour || overlayOpen) return;
    const root = document.createElement('div');
    root.className = 'tour-overlay';
    const spot = document.createElement('div');
    spot.className = 'tour-spot';
    const bubble = document.createElement('div');
    bubble.className = 'tour-bubble';
    bubble.setAttribute('role', 'dialog');
    bubble.setAttribute('aria-label', '使用导览');
    root.append(spot, bubble);
    document.body.appendChild(root);

    tour = { idx: 0, root, spot, bubble, el: null, onKey: null, onMove: null };
    tour.onKey = (ev) => { if (ev.key === 'Escape') { tourClose(); ev.preventDefault(); } };
    tour.onMove = () => {
      if (tour && tour.el && !tour.el.isConnected) tour.el = null;   // 目标被切页移除：收起挖孔
      tourPlace();
    };
    document.addEventListener('keydown', tour.onKey, true);
    window.addEventListener('resize', tour.onMove);
    document.addEventListener('scroll', tour.onMove, true);
    tourShow(0);
  }

  function tourClose() {
    if (!tour) return;
    markTourDone();                           // 看完/跳过都记一次，不再自动弹
    document.removeEventListener('keydown', tour.onKey, true);
    window.removeEventListener('resize', tour.onMove);
    document.removeEventListener('scroll', tour.onMove, true);
    tour.root.remove();
    tour = null;
  }

  /* 自动播放门槛：配置完成 + 非跳过用户 + 从未看过。跳过过引导的用户可能
   * 还没有可用数据，不自动放；老用户升级后首次进入主界面放一次。 */
  function maybeStartTour(auto) {
    if (overlayOpen || tour) return;
    if (auto && (tourDone() || setupSkipped() || !SX.setupDone())) return;
    tourStart();
  }

  /* 侧栏常驻「使用导览」入口：新用户、跳过过引导的用户与老用户都能重看 */
  function ensureSideTour() {
    let btn = document.getElementById('side-tour');
    if (btn) return btn;
    btn = document.createElement('button');
    btn.type = 'button';
    btn.id = 'side-tour';
    btn.className = 'side-tour';
    btn.title = '使用导览：分步高亮认识各页面入口';
    btn.innerHTML = `<span class="side-tour-ico" aria-hidden="true">${TOUR_ICON}</span>` +
      '<span>使用导览</span>';
    btn.addEventListener('click', () => { if (!overlayOpen) tourStart(); });
    const side = document.querySelector('.side');
    const foot = side && side.querySelector('.side-foot');
    if (!side || !foot) return btn;
    side.insertBefore(btn, foot);
    return btn;
  }

  initTheme();
  const themeBtn = document.getElementById('side-theme');
  const svg = (inner) =>
    `<svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round">${inner}</svg>`;
  const ICON_MOON = svg('<path d="M21 12.8A9 9 0 1 1 11.2 3a7 7 0 0 0 9.8 9.8z"/>');
  const ICON_SUN = svg('<circle cx="12" cy="12" r="4"/><path d="M12 2v2M12 20v2M4.9 4.9l1.4 1.4M17.7 17.7l1.4 1.4M2 12h2M20 12h2M6.3 17.7l-1.4 1.4M19.1 4.9l-1.4 1.4"/>');
  const syncThemeIcon = () => {
    themeBtn.innerHTML = document.documentElement.dataset.theme === 'dark' ? ICON_SUN : ICON_MOON;
  };
  syncThemeIcon();
  themeBtn.addEventListener('click', () => {
    const el = document.documentElement;
    el.dataset.theme = el.dataset.theme === 'dark' ? 'light' : 'dark';
    localStorage.setItem('siwx-theme', el.dataset.theme);
    syncThemeIcon();
  });
  const accCardBtn = document.getElementById('acc-card');  accCardBtn.addEventListener('click', () => {
    const pop = document.getElementById('acc-pop');
    if (pop && !pop.hidden && pop._close) closeAccPop();
    else openAccPop();
  });
  window.addEventListener('hashchange', navigate);
  ensureSideTour();
  sideStatus();
  sideJob();
  setInterval(() => { sideStatus(); sideJob(); }, 5000);

  /* 启动顺序：账号 → 插件菜单 → 首启向导 / 跳过续跑 / 条款更新确认 / 路由。
   * 首启（未完成配置）由全屏向导接管；跳过过引导的用户放行进主界面排障，
   * 但条款门禁照常生效，且侧栏常驻「引导未完成」续跑入口；条款版本更新时
   * 对老用户补一次声明确认；配置完成且未看过导览时，进入主界面放一轮
   * 分步导览（看完/跳过写 siwx-tour-done，只出现一次）。 */
  loadAccounts().finally(() => {
    loadPluginMenu();
    if (!SX.setupDone()) {
      if (setupSkipped()) {
        updateSetupHint();
        if (!disclaimerAcked()) openDisclaimer('gate');   // 跳过不豁免条款门禁
        else navigate();
      } else {
        openOnboarding('first');
      }
    } else if (!disclaimerAcked()) {
      openDisclaimer('gate');
    } else {
      navigate();
      maybeStartTour(true);       // 老用户升级后首次进入：一次性分步导览
    }
  });
})();
