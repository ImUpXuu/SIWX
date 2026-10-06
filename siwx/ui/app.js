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
  const PAGES_BUILTIN = ['chat', 'sns', 'stats', 'export', 'mcp', 'logs', 'settings'];
  const UI_VERSION = '2026100623';
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

  function initTheme() {
    const saved = localStorage.getItem('siwx-theme');
    if (saved === 'dark' || (!saved && matchMedia('(prefers-color-scheme: dark)').matches)) {
      document.documentElement.dataset.theme = 'dark';
    }
  }

  /* ── 首启引导向导（onboarding.js）────────────────────────
   * 未完成配置时全屏接管；已配置用户从账号下拉 / 各页空态进入 add 模式。
   * overlayOpen 与免责声明弹层共用：任一打开期间 navigate() 冻结。 */
  let onboardingMod = null;

  function openOnboarding(mode) {
    if (overlayOpen) return;
    if (mode === 'add' && !SX.setupDone()) mode = 'first';   // 未配置完不允许跳过完整向导
    overlayOpen = true;
    (async () => {
      try {
        if (!onboardingMod) onboardingMod = await import(`/onboarding.js?v=${UI_VERSION}`);
        onboardingMod.open({
          mode,
          disclaimerVersion: DISCLAIMER_VERSION,
          closable: SX.setupDone(),
          onClose: async (res) => {
            overlayOpen = false;
            if (res && res.finished) {
              await loadAccounts();
              if (res.account && accounts.some(a => a.wxid === res.account)) {
                setAccount(res.account, { reload: false });
              }
              if (currentPageName() !== 'chat') location.hash = '#/chat';
              else navigate();
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
    sub.textContent = mode === 'gate'
      ? '首次使用需阅读并确认本声明后才能进入控制台；条款更新后会再次弹出。'
      : '以下是本声明的全文。';

    const body = document.createElement('div');
    body.className = 'disc-body';
    body.textContent = '加载中…';

    const actions = document.createElement('div');
    actions.className = 'disc-actions';
    if (mode === 'gate') {
      const decline = document.createElement('button');
      decline.className = 'btn';
      decline.textContent = '不同意并退出';
      decline.addEventListener('click', () => {
        window.close();                       // 仅对脚本打开的窗口有效
        body.innerHTML = '';
        body.insertAdjacentHTML('beforeend',
          '<div class="card"><h2>你未同意免责声明</h2>' +
          '<p class="dim">已停止提供服务，请关闭本页并删除本工具。若为误点，请刷新页面重新阅读。</p></div>');
        actions.innerHTML = '';
      });
      const accept = document.createElement('button');
      accept.className = 'btn btn-primary';
      accept.textContent = '我已阅读并同意';
      accept.addEventListener('click', () => {
        localStorage.setItem('siwx-disclaimer-ack', DISCLAIMER_VERSION);
        ov.remove();
        overlayOpen = false;
        navigate();
      });
      actions.append(decline, accept);
    } else {
      const close = document.createElement('button');
      close.className = 'btn btn-primary';
      close.textContent = '关闭';
      close.addEventListener('click', () => { ov.remove(); overlayOpen = false; });
      actions.append(close);
    }

    card.append(head, sub, body, actions);
    ov.appendChild(card);
    document.body.appendChild(ov);
    try {
      const r = await fetch(`/pages/disclaimer.html?v=${UI_VERSION}`);
      if (!r.ok) throw new Error(`HTTP ${r.status}`);
      body.innerHTML = await r.text();
    } catch (e) {
      body.innerHTML = '';
      body.insertAdjacentHTML('beforeend',
        '<div class="card"><h2>免责声明加载失败</h2>' +
        `<p class="dim">${SX.esc(String(e))}</p>` +
        '<p class="dim">请前往 GitHub 仓库（github.com/ImUpXuu/SIWX）阅读 README 中的完整免责声明后，刷新本页重试。</p></div>');
    }
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
  sideStatus();
  setInterval(sideStatus, 5000);

  /* 启动顺序：账号 → 插件菜单 → 首启向导 / 条款更新确认 / 路由。
   * 首启（未完成配置）由全屏向导接管；条款版本更新时对老用户补一次声明确认。 */
  loadAccounts().finally(() => {
    loadPluginMenu();
    if (!SX.setupDone()) {
      openOnboarding('first');
    } else if (!disclaimerAcked()) {
      openDisclaimer('gate');
    } else {
      navigate();
    }
  });
})();
