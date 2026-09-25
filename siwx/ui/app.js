/* stories-in-wx 壳：hash 路由 + 模块化页面加载器（pages/<name>.html/js/css）
 *
 * 页面来源有两类：
 *   内置：/pages/<name>.*        （PAGES_BUILTIN 固定顺序）
 *   插件：/plugin-pages/<plugin>/<file>.*（由 /api/plugins/pages 动态下发）
 * 插件菜单项**追加在内置之后**，与内置项完全平级（同样式、同高亮逻辑）。
 */
(function () {
  const PAGES_BUILTIN = ['guide', 'chat', 'stats', 'export', 'mcp', 'logs', 'settings'];
  const UI_VERSION = '2026092501';
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
    return window.SX.setupDone() ? 'chat' : 'guide';
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

  function markMenu(name) {
    document.querySelectorAll('#menu a').forEach(a => {
      a.classList.toggle('active', a.dataset.page === name);
    });
  }

  function currentPageName() {
    return (location.hash || '').replace(/^#\/?/, '');
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

  /* ── 页面导航 ─────────────────────────────────────────────── */
  function pageBase(name) {
    const p = findPage(name);
    if (!p) {
      return { html: `/pages/${name}.html`, css: `/pages/${name}.css`, js: `/pages/${name}.js` };
    }
    const dir = `/plugin-pages/${encodeURIComponent(p.plugin)}/${p.entry || 'index'}`;
    return { html: `${dir}.html`, css: `${dir}.css`, js: `${dir}.js` };
  }

  async function navigate() {
    if (overlayOpen) return;                 // 免责声明弹层打开期间冻结导航
    let name = currentPageName();
    if (!allPages().includes(name)) {
      name = defaultRoute();
      if (location.hash !== `#/${name}`) { location.hash = `#/${name}`; return; }
    }
    markMenu(name);
    const view = document.getElementById('view');
    const base = pageBase(name);
    try {
      const res = await fetch(base.html);
      if (!res.ok) throw new Error(`页面加载失败 (${res.status})`);
      view.innerHTML = await res.text();
    } catch (e) {
      view.innerHTML = `<div class="card"><h2>页面加载失败</h2><p class="dim">${e.message}</p></div>`;
      return;
    }
    ensureCss(base.css);
    if (current && current.mod && current.mod.destroy) {
      try { current.mod.destroy(); } catch (e) { /* 忽略 */ }
    }
    current = null;
    try {
      const mod = await import(`${base.js}?v=${Date.now()}`);
      current = { name, mod };
      if (mod.init) await mod.init(view);
    } catch (e) {
      view.insertAdjacentHTML('beforeend',
        `<div class="card"><h2>页面脚本错误</h2><p class="dim">${e.message}</p></div>`);
    }
  }

  /* 侧栏微信状态 */
  async function sideStatus() {
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

  /* ── 免责声明门 ───────────────────────────────────────────
   * 首次启动 / 条款版本更新后先展示全文并要求确认；未确认前冻结全部页面。
   * 确认状态存 localStorage（siwx-disclaimer-ack = 条款版本号），侧栏可随时重看。 */
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
  document.getElementById('side-theme').addEventListener('click', () => {
    const el = document.documentElement;
    el.dataset.theme = el.dataset.theme === 'dark' ? 'light' : 'dark';
    localStorage.setItem('siwx-theme', el.dataset.theme);
  });
  document.getElementById('side-disclaimer').addEventListener('click',
    () => openDisclaimer('review'));
  window.addEventListener('hashchange', navigate);
  sideStatus();
  setInterval(sideStatus, 5000);
  loadPluginMenu();      // 先拉插件菜单，再决定路由
  if (disclaimerAcked()) {
    navigate();
  } else {
    openDisclaimer('gate');
  }
})();
