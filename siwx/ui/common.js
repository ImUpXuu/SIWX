/* stories-in-wx 共享工具（window.SX）—— 页面模块通过它复用，不直接碰 DOM 全局 */
(function () {
  function esc(s) {
    const d = document.createElement('div');
    d.textContent = s == null ? '' : String(s);
    // innerHTML 序列化只转义 & < >，不转义引号；而本函数大量用于属性位
    // （data-* / onclick / href），值中出现引号即可逃出属性注入任意事件。
    // 补齐引号转义后，属性位与文本位同等安全（顺带修复 settings 插件
    // choice 型设置项 data-choices 属性被 JSON 引号截断导致选项丢失）。
    return d.innerHTML.replace(/"/g, '&quot;').replace(/'/g, '&#39;');
  }
  function timeStr(t) {
    const d = new Date(Number(t));
    const p = (n) => String(n).padStart(2, '0');
    return `${p(d.getHours())}:${p(d.getMinutes())}:${p(d.getSeconds())}`;
  }
  function fmtTs(t) {
    if (!t) return '';
    const d = new Date(Number(t) * 1000);
    const p = (n) => String(n).padStart(2, '0');
    return `${d.getFullYear()}-${p(d.getMonth() + 1)}-${p(d.getDate())} ${p(d.getHours())}:${p(d.getMinutes())}`;
  }
  /* 会话列表用的智能短时间（微信风格）：今天 → HH:MM，昨天 → 昨天，
     一周内 → 周X，今年 → MM-DD，更早 → YYYY/MM/DD。
     会话行右侧只预留了窄窄一列，全长 "YYYY-MM-DD HH:MM" 会把标题
     挤得与时间戳重叠——这里是治本的一招。 */
  function fmtListTs(t) {
    if (!t) return '';
    const d = new Date(Number(t) * 1000);
    if (Number.isNaN(d.getTime())) return '';
    const now = new Date();
    const p = (n) => String(n).padStart(2, '0');
    const dayStart = (x) => new Date(x.getFullYear(), x.getMonth(), x.getDate()).getTime();
    const days = Math.round((dayStart(now) - dayStart(d)) / 86400000);
    if (days <= 0) return `${p(d.getHours())}:${p(d.getMinutes())}`;
    if (days === 1) return '昨天';
    if (days < 7) return '周' + '日一二三四五六'[d.getDay()];
    if (d.getFullYear() === now.getFullYear()) return `${p(d.getMonth() + 1)}-${p(d.getDate())}`;
    return `${d.getFullYear()}/${p(d.getMonth() + 1)}/${p(d.getDate())}`;
  }
  async function fetchJSON(url, opts) {
    const r = await fetch(url, opts);
    const j = await r.json().catch(() => ({}));
    if (!r.ok) throw new Error(j.error || r.statusText);
    return j;
  }

  /* 任务：POST /api/run + 轮询 /api/job；返回 stop()。 */
  function startJob(body, onLog, onDone) {
    let stop = false;
    let failStreak = 0;
    (async () => {
      try {
        const r = await fetch('/api/run', {
          method: 'POST',
          headers: { 'content-type': 'application/json' },
          body: JSON.stringify(body),
        });
        const j = await r.json().catch(() => ({}));
        if (r.status === 409) { onDone && onDone({ error: '已有任务在运行' }); return; }
        if (!r.ok) { onDone && onDone({ error: j.error || '启动失败' }); return; }
        const poll = async () => {
          if (stop) return;
          let job;
          try {
            job = await (await fetch('/api/job')).json();
          } catch (e) {
            // 后端重启/瞬时网络失败时不能直接 return：续跑的 setTimeout 只在成功
            // 分支里，一次抖动就会既不再轮询、也永不回调 onDone（按钮永久卡死）。
            // 抄 export.js 的做法，连续失败到阈值才放弃并明确报错。
            if (++failStreak >= 5) {
              onDone && onDone({ error: '与后端失去联系，请刷新页面后重试' });
              return;
            }
            setTimeout(poll, 600);
            return;
          }
          failStreak = 0;
          if (onLog) onLog(job.logs || []);
          if (job.running) {
            setTimeout(poll, 600);
          } else {
            onDone && onDone(job);
          }
        };
        poll();
      } catch (e) {
        onDone && onDone({ error: String(e) });
      }
    })();
    return () => { stop = true; };
  }

  function renderLog(el, logs) {
    el.innerHTML = logs.map(entry => {
      // 兼容旧格式 [ts, msg] 和新格式 [ts, level, module, msg]
      let t, msg;
      if (entry.length >= 4) {
        [, , , msg] = entry;  // 新格式: 取第4个元素
      } else {
        [t, msg] = entry;  // 旧格式
      }
      return `<div class="log-line"><span class="t">${timeStr(entry[0])}</span>${esc(msg)}</div>`;
    }).join('') || '<div class="log-line dim">…</div>';
    el.scrollTop = el.scrollHeight;
  }

  function go(hash) { location.hash = hash; }
  function setupDone() { return localStorage.getItem('siwx-setup-done') === '1'; }
  function setSetupDone() { localStorage.setItem('siwx-setup-done', '1'); }

  /* 图片解密失败占位 → 点击重试（在微信中打开图片下载原图后再点） */
  function imgFallback(img, retrySrc) {
    const span = document.createElement('span');
    span.className = 'm-retry';
    span.textContent = '[原图未下载 · 在微信中打开该图片后点此重试]';
    span.addEventListener('click', () => {
      const img2 = document.createElement('img');
      img2.className = 'm-img';
      img2.loading = 'lazy';
      img2.src = retrySrc + '&r=' + Date.now();
      img2.onerror = () => {
        img2.replaceWith(span);
      };
      span.replaceWith(img2);
    });
    img.replaceWith(span);
  }

  /* 打开导出目录/文件（资源管理器），仅限 exports 根内；失败给出提示 */
  async function openPath(path) {
    let r;
    try {
      r = await fetch('/api/export/open', {
        method: 'POST', headers: { 'content-type': 'application/json' },
        body: JSON.stringify({ path }),
      });
    } catch (e) {
      toast('打开目录失败：' + e.message, true);
      return;
    }
    if (!r.ok) {
      const j = await r.json().catch(() => ({}));
      toast('打开目录失败：' + (j.error || `HTTP ${r.status}`), true);
    }
  }

  /* ── 插件渲染器：结构化节点树 → HTML ─────────────────────────────
   * 插件**不返回 HTML 字符串**，而是返回节点树，从根上杜绝 XSS：
   *   { t: 'text',  v: '...' }                         纯文本（自动转义）
   *   { t: 'el',    tag: 'div', cls: 'x', v: '文本',
   *                 a: { href: '...' }, c: [子节点] }   元素
   *   { t: 'img',   src: '...', alt: '...', cls: '...' } 图片（src 仅允许同源/相对）
   *   { t: 'a',     href: '...', v: '文本' }            链接（仅 http/https/相对）
   *   { t: 'raw',   v: '<b>x</b>' }                     显式 HTML —— 被忽略（安全）
   * 数组即多个节点。非法节点被静默跳过，不会破坏整条消息。
   */
  const ALLOWED_TAGS = new Set(['span', 'div', 'b', 'i', 'em', 'strong', 'code',
                                'pre', 'p', 'br', 'ul', 'ol', 'li', 'small',
                                'table', 'thead', 'tbody', 'tr', 'th', 'td']);
  const ALLOWED_ATTRS = new Set(['class', 'title', 'style', 'colspan', 'rowspan']);

  function safeUrl(u) {
    const s = String(u == null ? '' : u).trim();
    if (!s) return '';
    if (/^(https?:|\/|\.\/|\.\.\/|#)/i.test(s)) return s;
    return '';                       // 拒绝 javascript: / data: 等
  }

  function safeStyle(v) {
    // 只放行无 url()/expression 的简单声明，防 CSS 注入
    const s = String(v == null ? '' : v);
    if (/url\s*\(|expression|javascript:/i.test(s)) return '';
    return s;
  }

  function renderNodes(node) {
    if (node == null || node === false) return '';
    if (Array.isArray(node)) return node.map(renderNodes).join('');
    if (typeof node === 'string' || typeof node === 'number') return esc(node);
    if (typeof node !== 'object') return '';
    const t = node.t || (node.tag ? 'el' : 'text');
    if (t === 'text') return esc(node.v);
    if (t === 'raw') return '';                      // 显式拒绝 HTML 注入
    if (t === 'img') {
      const src = safeUrl(node.src);
      if (!src) return '';
      return `<img class="m-img ${esc(node.cls || '')}" loading="lazy" src="${esc(src)}" alt="${esc(node.alt || '')}">`;
    }
    if (t === 'a') {
      const href = safeUrl(node.href);
      const body = node.c != null ? renderNodes(node.c) : esc(node.v);
      if (!href) return `<span>${body}</span>`;
      return `<a href="${esc(href)}" target="_blank" rel="noreferrer">${body}</a>`;
    }
    // t === 'el'
    let tag = String(node.tag || 'span').toLowerCase();
    if (!ALLOWED_TAGS.has(tag)) tag = 'span';
    let attrs = '';
    const a = node.a || {};
    let hasCls = false;
    Object.keys(a).forEach(k => {
      if (k === 'href' || k === 'src' || k.indexOf('on') === 0) return;   // 事件/URL 一律拒绝
      if (!ALLOWED_ATTRS.has(k)) return;
      let v = k === 'style' ? safeStyle(a[k]) : String(a[k]);
      if (!v) return;
      if (k === 'class') hasCls = true;
      attrs += ` ${k}="${esc(v)}"`;
    });
    if (node.cls && !hasCls) attrs += ` class="${esc(node.cls)}"`;
    if (tag === 'br') return '<br>';
    const body = node.c != null ? renderNodes(node.c) : esc(node.v);
    return `<${tag}${attrs}>${body}</${tag}>`;
  }

  /** 复制文本到剪贴板；非安全上下文回退到 execCommand。返回是否成功。 */
  async function copyText(text) {
    if (navigator.clipboard && window.isSecureContext) {
      await navigator.clipboard.writeText(text);
      return true;
    }
    const ta = document.createElement('textarea');
    ta.value = text;
    ta.style.position = 'fixed';
    ta.style.left = '-9999px';
    document.body.appendChild(ta);
    ta.focus();
    ta.select();
    const ok = document.execCommand('copy');
    ta.remove();
    return ok;
  }

  /* ── 全局 toast：非阻断轻反馈，替代 window.alert ─────────────
   * 统一挂在壳层，任何页面都能 SX.toast('已复制')；err=true 用危险色。 */
  let _toastTimer = null;
  function toast(text, err) {
    let box = document.getElementById('sx-toast-box');
    if (!box) {
      box = document.createElement('div');
      box.id = 'sx-toast-box';
      box.setAttribute('aria-live', 'polite');
      document.body.appendChild(box);
    }
    box.innerHTML = '';
    const t = document.createElement('div');
    t.className = 'sx-toast' + (err ? ' err' : '');
    t.textContent = text;
    box.appendChild(t);
    clearTimeout(_toastTimer);
    _toastTimer = setTimeout(() => {
      t.classList.add('out');
      setTimeout(() => t.remove(), 200);
    }, 2400);
  }

  /* ── 全局账号（状态本体在 app.js，这里做惰性转发）──────────────
   * 页面用法：init 时 const account = SX.getAccount()；账号变化由壳层
   * 统一重载页面，页面内不需要订阅事件。 */
  function getAccount() { return window.__sxAccount ? window.__sxAccount.get() : null; }
  function listAccounts() { return window.__sxAccount ? window.__sxAccount.list() : []; }
  function setAccount(wxid, opts) { if (window.__sxAccount) window.__sxAccount.set(wxid, opts); }

  window.SX = { esc, timeStr, fmtTs, fmtListTs, fetchJSON, startJob, renderLog, go,
                setupDone, setSetupDone, imgFallback, openPath,
                renderNodes, copyText, toast, getAccount, listAccounts, setAccount };
})();
