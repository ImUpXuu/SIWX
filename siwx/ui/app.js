/* stories-in-wx 壳：hash 路由 + 模块化页面加载器（pages/<name>.html/js/css） */
(function () {
  const PAGES = ['guide', 'chat', 'export', 'mcp', 'logs', 'settings'];
  let current = null;      // { name, mod }
  let loadedCss = {};

  function defaultRoute() {
    return window.SX.setupDone() ? 'chat' : 'guide';
  }

  const UI_VERSION = '2026091201';

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

  async function navigate() {
    let name = (location.hash || '').replace(/^#\/?/, '');
    if (!PAGES.includes(name)) {
      name = defaultRoute();
      if (location.hash !== `#/${name}`) { location.hash = `#/${name}`; return; }
    }
    markMenu(name);
    const view = document.getElementById('view');
    try {
      const res = await fetch(`/pages/${name}.html`);
      if (!res.ok) throw new Error(`页面加载失败 (${res.status})`);
      view.innerHTML = await res.text();
    } catch (e) {
      view.innerHTML = `<div class="card"><h2>页面加载失败</h2><p class="dim">${e.message}</p></div>`;
      return;
    }
    ensureCss(`/pages/${name}.css`);
    if (current && current.mod && current.mod.destroy) {
      try { current.mod.destroy(); } catch (e) { /* 忽略 */ }
    }
    current = null;
    try {
      const mod = await import(`/pages/${name}.js?v=${Date.now()}`);
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

  initTheme();
  document.getElementById('side-theme').addEventListener('click', () => {
    const el = document.documentElement;
    el.dataset.theme = el.dataset.theme === 'dark' ? 'light' : 'dark';
    localStorage.setItem('siwx-theme', el.dataset.theme);
  });
  window.addEventListener('hashchange', navigate);
  sideStatus();
  setInterval(sideStatus, 5000);
  navigate();
})();
