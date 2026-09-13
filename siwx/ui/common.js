/* stories-in-wx 共享工具（window.SX）—— 页面模块通过它复用，不直接碰 DOM 全局 */
(function () {
  function esc(s) {
    const d = document.createElement('div');
    d.textContent = s == null ? '' : String(s);
    return d.innerHTML;
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
  async function fetchJSON(url, opts) {
    const r = await fetch(url, opts);
    const j = await r.json().catch(() => ({}));
    if (!r.ok) throw new Error(j.error || r.statusText);
    return j;
  }

  /* 任务：POST /api/run + 轮询 /api/job；返回 stop()。 */
  function startJob(body, onLog, onDone) {
    let stop = false;
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
          try { job = await (await fetch('/api/job')).json(); } catch (e) { return; }
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
  function resetSetup() { localStorage.removeItem('siwx-setup-done'); }

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

  /* 打开导出目录/文件（资源管理器），仅限 exports 根内 */
  function openPath(path) {
    fetch('/api/export/open', {
      method: 'POST', headers: { 'content-type': 'application/json' },
      body: JSON.stringify({ path }),
    });
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

  window.SX = { esc, timeStr, fmtTs, fetchJSON, startJob, renderLog, go,
                setupDone, setSetupDone, resetSetup, imgFallback, openPath,
                renderNodes };
})();
