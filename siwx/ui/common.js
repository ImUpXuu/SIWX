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
    el.innerHTML = logs.map(([t, m]) =>
      `<div class="log-line"><span class="t">${timeStr(t)}</span>${esc(m)}</div>`
    ).join('') || '<div class="log-line dim">…</div>';
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

  window.SX = { esc, timeStr, fmtTs, fetchJSON, startJob, renderLog, go,
                setupDone, setSetupDone, resetSetup, imgFallback, openPath };
})();
