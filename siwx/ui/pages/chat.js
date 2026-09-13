/* 聊天查看 —— 微信风格气泡 + 时间轴 + 消息选择/复制 */
const { esc, fetchJSON, fmtTs } = window.SX;

let account = null;
let sessions = [];
let currentChat = null;
let officialOpen = false;
let earliest = 0;
let hasMore = false;
let loading = false;
let loadedMessages = [];
let timelineDays = [];
let selectionMode = false;
let selectedKeys = new Set();
let rangeAnchorKey = null;

function el(id) { return document.getElementById(id); }

async function init() {
  const sel = el('c-account');
  try {
    const { accounts } = await fetchJSON('/api/chat/accounts');
    if (!accounts.length) {
      el('c-sessions').innerHTML =
        '<div class="c-empty">还没有解密产物 — 请先在"引导设置"完成解密</div>';
      return;
    }
    sel.innerHTML = accounts.map(a =>
      `<option value="${esc(a.wxid)}">${esc(a.wxid)}</option>`).join('');
    account = accounts[0].wxid;
    sel.addEventListener('change', () => {
      account = sel.value;
      currentChat = null;
      resetChatView();
      loadSessions();
    });
    await loadSessions();
  } catch (e) {
    el('c-sessions').innerHTML = `<div class="c-empty">${esc(e.message)}</div>`;
  }

  el('c-search').addEventListener('input', () => renderSessions(el('c-search').value));
  el('c-back').addEventListener('click', () => el('chat-wrap').classList.remove('open'));
  el('c-refresh').addEventListener('click', async () => {
    const btn = el('c-refresh');
    btn.disabled = true;
    el('c-sessions').innerHTML = '<div class="c-loading">增量同步中…</div>';
    try {
      const acc = el('c-account')?.value || account;
      const r = await fetch('/api/run', {
        method: 'POST', headers: {'content-type': 'application/json'},
        body: JSON.stringify({ mode: 'sync', export_opts: { account: acc } }),
      });
      if (r.status === 409) { el('c-sessions').innerHTML = '<div class="c-empty">已有任务在运行</div>'; return; }
      for (let i = 0; i < 300; i++) {
        await new Promise(res => setTimeout(res, 1000));
        const j = await (await fetch('/api/job')).json();
        if (!j.running) break;
        if (j.logs.length) {
          const last = j.logs[j.logs.length - 1][1];
          el('c-sessions').innerHTML = `<div class="c-loading">${esc(last.substring(0, 60))}</div>`;
        }
      }
      await loadSessions();
      if (currentChat) await Promise.all([loadMessages(true), loadTimeline()]);
    } catch (e) {
      el('c-sessions').innerHTML = `<div class="c-empty">刷新失败: ${esc(e.message)}</div>`;
    } finally {
      btn.disabled = false;
    }
  });
  el('c-select').addEventListener('click', () => setSelectionMode(true));
  el('c-cancel-select').addEventListener('click', () => setSelectionMode(false));
  el('c-copy-selected').addEventListener('click', copySelectedMessages);
  el('c-stats').addEventListener('click', openStatsModal);
  el('c-stats-close').addEventListener('click', closeStatsModal);
  el('c-stats-modal').addEventListener('click', (ev) => {
    if (ev.target === el('c-stats-modal')) closeStatsModal();
  });
  el('c-jump-date').addEventListener('change', () => {
    if (!el('c-jump-date').value || !currentChat) return;
    jumpToTime(Math.floor(new Date(el('c-jump-date').value + 'T00:00:00').getTime() / 1000));
  });

  // 跳转导出页并预选当前会话
  el('c-export').addEventListener('click', () => {
    if (!currentChat) { window.alert('先在左侧打开一个会话'); return; }
    sessionStorage.setItem('siwx-export-preset', JSON.stringify({
      account, chat: currentChat.username, display: currentChat.display,
    }));
    location.hash = '#/export';
  });

  // 消息区事件委托：复制单条、选择/范围选择
  el('c-msgs').addEventListener('click', async (ev) => {
    const copyBtn = ev.target.closest('.m-copy');
    if (copyBtn) {
      ev.stopPropagation();
      const row = copyBtn.closest('.m-row');
      const msg = messageByKey(row?.dataset.key);
      if (msg) await copyText(messageCopyText(msg), copyBtn, '已复制');
      return;
    }
    const row = ev.target.closest('.m-row[data-key]');
    if (!row || !selectionMode) return;
    toggleMessageSelection(row.dataset.key, ev.shiftKey);
  });

  // 滚动到顶部自动加载更早消息
  el('c-msgs').addEventListener('scroll', async () => {
    const box = el('c-msgs');
    if (box.scrollTop <= 4 && hasMore && !loading && currentChat) {
      loading = true;
      const keep = box.scrollHeight;
      box.insertAdjacentHTML('afterbegin', '<div class="c-loading">加载更早消息…</div>');
      await loadMessages(false);
      const tip = box.querySelector('.c-loading');
      if (tip) tip.remove();
      box.scrollTop = box.scrollHeight - keep;
      loading = false;
    }
  });
}

function resetChatView() {
  loadedMessages = [];
  timelineDays = [];
  earliest = 0;
  hasMore = false;
  setSelectionMode(false);
  el('c-title').textContent = '微信';
  el('c-sub').textContent = '选择一个会话查看聊天记录';
  el('c-msgs').innerHTML = '<div class="c-tip">选择一个会话查看聊天记录</div>';
  el('c-timeline-list').innerHTML = '<div class="tl-empty">打开会话后可按日期跳转</div>';
  el('c-jump-date').disabled = true;
  el('c-select').disabled = true;
  el('c-stats').disabled = true;
}

async function loadSessions() {
  el('c-sessions').innerHTML = '<div class="c-loading">加载会话…</div>';
  officialOpen = false;
  const t0 = performance.now();
  const { sessions: ss } = await fetchJSON(
    `/api/chat/sessions?account=${encodeURIComponent(account)}`);
  sessions = ss;
  renderSessions('');
  const ms = Math.round(performance.now() - t0);
  if (el('c-sub') && !currentChat) el('c-sub').textContent = `${ss.length} 个会话 · ${ms}ms`;
}

function isOfficial(s) {
  return !!(s && (s.is_official || s.kind === 'official' || /^gh_/.test(s.username || '')));
}

function ownerUsername() {
  return /^wxid_.+_\d+$/.test(account || '') ? account.replace(/_\d+$/, '') : account;
}

function avatarImg(username) {
  const src = `/api/chat/avatar?account=${encodeURIComponent(account)}` +
              `&username=${encodeURIComponent(username)}`;
  return `<img loading="lazy" src="${src}" onerror="this.remove()">`;
}

function sessionRow(s, extra = '') {
  const initials = (s.display || s.username || '?').slice(0, 2).toUpperCase();
  const isGrp = s.is_group;
  const off = isOfficial(s);
  const selected = currentChat && currentChat.username === s.username;
  return `
    <div class="sess ${extra} ${selected ? 'sel' : ''}" data-u="${esc(s.username)}">
      <div class="ava"><span>${esc(initials)}</span>${avatarImg(s.username)}</div>
      <span class="name">${esc(s.display)}</span>
      <span class="prev">${esc(s.preview || '点击查看详情')}</span>
      <span class="tm">${fmtTs(s.last_time)}</span>
      <span class="tag">${off ? '公众号' : (isGrp ? '群聊' : '')}</span>
    </div>`;
}

function renderSessions(kw) {
  const kwL = (kw || '').toLowerCase();
  const visible = sessions.filter(s =>
    !kwL || s.display.toLowerCase().includes(kwL) || s.username.toLowerCase().includes(kwL));
  const normal = visible.filter(s => !isOfficial(s));
  const official = visible.filter(s => isOfficial(s));
  const lines = [];
  if (official.length) {
    const open = officialOpen || !!kwL;
    const latest = Math.max(...official.map(s => s.last_time || 0));
    lines.push(`
      <div class="sess sess-group ${open ? 'open' : ''}" data-group="official">
        <div class="ava"><span>公</span></div>
        <span class="name">公众号</span>
        <span class="prev">${official.length} 个公众号会话 · 点击${open ? '收起' : '展开'}</span>
        <span class="tm">${fmtTs(latest)}</span>
        <span class="tag">${open ? '收起' : '展开'}</span>
      </div>`);
    if (open) lines.push(...official.map(s => sessionRow(s, 'sess-official')));
  }
  lines.push(...normal.map(s => sessionRow(s)));
  el('c-sessions').innerHTML = lines.length ? lines.join('')
    : '<div class="c-empty">没有匹配的会话</div>';
  el('c-sessions').querySelectorAll('.sess').forEach(n => {
    n.addEventListener('click', () => {
      if (n.dataset.group === 'official') {
        officialOpen = !officialOpen;
        renderSessions(el('c-search').value);
        return;
      }
      const s = sessions.find(x => x.username === n.dataset.u);
      if (s) openChat(s);
    });
  });
}

async function openChat(s) {
  currentChat = s;
  earliest = 0;
  hasMore = false;
  loadedMessages = [];
  setSelectionMode(false);
  el('chat-wrap').classList.add('open');
  el('c-title').textContent = s.display;
  el('c-sub').textContent = s.is_group ? '群聊 · 加载中' : '私聊 · 加载中';
  el('c-msgs').innerHTML = '<div class="c-loading">加载消息…</div>';
  el('c-select').disabled = false;
  el('c-stats').disabled = false;
  await Promise.all([loadMessages(true), loadTimeline()]);
}

async function loadMessages(fresh, opts = {}) {
  const s = currentChat;
  if (!s) return false;
  const before = opts.before || (!fresh && earliest ? earliest : 0);
  const limit = opts.limit || 80;
  const url = `/api/chat/messages?account=${encodeURIComponent(account)}` +
    `&chat=${encodeURIComponent(s.username)}` +
    (before ? `&before=${before}` : '') +
    `&limit=${limit}`;
  const data = await fetchJSON(url);
  const box = el('c-msgs');
  if (fresh) {
    selectedKeys.clear();
    rangeAnchorKey = null;
    loadedMessages = data.messages.slice();
    box.innerHTML = renderMessageList(loadedMessages) || '<div class="c-empty">这个会话没有消息</div>';
    box.scrollTo({ top: box.scrollHeight, behavior: opts.smooth ? 'smooth' : 'auto' });
  } else {
    loadedMessages = mergeMessages(data.messages, loadedMessages);
    box.insertAdjacentHTML('afterbegin', renderMessageList(data.messages, true));
    refreshRenderedPositions();
    refreshSelectionUI();
  }
  earliest = loadedMessages.length ? loadedMessages[0].ts : earliest;
  hasMore = data.has_more;
  updateSubtitle();
  return true;
}

function mergeMessages(a, b) {
  const seen = new Set();
  return [...a, ...b].filter(m => {
    const k = msgKey(m);
    if (seen.has(k)) return false;
    seen.add(k);
    return true;
  }).sort((x, y) => (x.ts || 0) - (y.ts || 0) || (x.id || 0) - (y.id || 0));
}

function renderMessageList(msgs) {
  return msgs.map((m, i) => {
    const prev = i > 0 ? msgs[i - 1] : null;
    const showTime = !prev || (m.ts || 0) - (prev.ts || 0) > 300;
    return (showTime ? `<div class="m-time-chip">${fmtTs(m.ts)}</div>` : '') + bubble(m);
  }).join('');
}

function refreshRenderedPositions() {
  const order = new Map(loadedMessages.map((m, i) => [msgKey(m), i]));
  el('c-msgs').querySelectorAll('.m-row[data-key]').forEach(row => {
    row.dataset.pos = String(order.get(row.dataset.key) ?? 0);
  });
}

function avaHtml(username, letters) {
  return `<div class="m-ava"><span>${esc(letters)}</span>` + avatarImg(username) + `</div>`;
}

function voiceDuration(v) {
  const ms = Number(v && v.durationMs || 0);
  return ms > 0 ? `${(ms / 1000).toFixed(1)} 秒` : '语音';
}

function msgKey(m) {
  return `${m.ts || 0}:${m.id || 0}:${m.platformMessageId || ''}`;
}

function messageByKey(key) {
  return loadedMessages.find(m => msgKey(m) === key);
}

function bubble(m) {
  if (m.type === 10000 || m.type === 10002) {
    return `<div class="m-row sys" data-key="${esc(msgKey(m))}" data-ts="${m.ts || 0}"><div class="m-bubble">${esc(m.text)}</div><button class="m-copy">复制</button></div>`;
  }
  const who = m.is_me ? 'me' : '';
  const avaUser = m.is_me ? ownerUsername() : (m.sender_wxid || currentChat.username);
  const ava = `<div class="cell-ava">${avaHtml(avaUser, (m.sender_name || '?').slice(0, 2).toUpperCase())}</div>`;
  const name = (!m.is_me && currentChat && currentChat.is_group && m.sender_name)
    ? `<div class="m-name">${esc(m.sender_name)}</div>` : '';
  let inner = '';
  if (m.render) {
    inner = SX.renderNodes(m.render);
  } else if (m.kind === 'quote' && m.quote) {
    inner = `<div class="m-quote"><div class="m-quote-n">${esc(m.quote.displayname)}</div>` +
            `<div class="m-quote-t">${esc(m.quote.content)}</div></div>` + esc(m.text);
  } else if (m.kind === 'link' && m.link) {
    const url = m.link.url || '';
    const full = url.startsWith('http') ? url : 'https://' + url;
    let host = '';
    try { host = new URL(full).hostname; } catch (e) {}
    inner = `<div class="m-link"><a href="${esc(full)}" target="_blank" rel="noreferrer">${esc(m.link.title)}</a>` +
            (host ? `<div class="m-link-host">${esc(host)}</div>` : '') + `</div>`;
  } else if (m.kind === 'voice') {
    const base = `/api/chat/media/voice?account=${encodeURIComponent(account)}` +
                 `&chat=${encodeURIComponent(currentChat.username)}` +
                 `&local_id=${m.id || 0}&svr_id=${encodeURIComponent(m.platformMessageId || '')}&ts=${m.ts || 0}`;
    const wav = base + '&format=wav';
    const silk = base + '&format=silk';
    inner = `<div class="m-voice"><span>${esc(voiceDuration(m.voice))}</span>` +
            `<audio controls preload="none" src="${wav}"></audio>` +
            `<a href="${silk}" download="voice_${m.id || 'msg'}.silk">下载 SILK</a></div>`;
  } else if (m.kind === 'image') {
    const src = `/api/chat/media/image?account=${encodeURIComponent(account)}` +
                (m.md5 ? `&md5=${encodeURIComponent(m.md5)}` : '') +
                (m.bubble_md5 ? `&bubble_md5=${encodeURIComponent(m.bubble_md5)}` : '') +
                `&chat=${encodeURIComponent(currentChat.username)}` +
                `&local_id=${m.id || 0}&ts=${m.ts || 0}`;
    const retrySrc = src + '&r=' + Date.now();
    inner = `<img class="m-img" loading="lazy" src="${src}" alt="图片"
              onclick="window.open(this.src + '&hq=1')"
              onerror="SX.imgFallback(this, '${esc(retrySrc)}')">`;
  } else if (m.kind === 'sticker') {
    inner = '[动画表情]';
  } else {
    inner = esc(m.text);
  }
  let quoteHtml = '';
  if (m.quote && m.kind !== 'quote') {
    quoteHtml = `<div class="m-quote"><div class="m-quote-n">${esc(m.quote.displayname)}</div>` +
                `<div class="m-quote-t">${esc(m.quote.content)}</div></div>`;
  }
  const selected = selectedKeys.has(msgKey(m));
  return `
    <div class="m-row ${who} ${selected ? 'selected' : ''}" data-key="${esc(msgKey(m))}" data-ts="${m.ts || 0}">
      <div class="m-check">✓</div>
      ${ava}
      <div class="cell-body">
        ${name}
        <div class="m-bubble">${quoteHtml}${inner}</div>
        <div class="m-time">${fmtTs(m.ts)}</div>
      </div>
      <button class="m-copy" title="复制这条消息">复制</button>
    </div>`;
}

async function loadTimeline() {
  if (!currentChat) return;
  const list = el('c-timeline-list');
  list.innerHTML = '<div class="tl-empty">正在生成时间轴…</div>';
  el('c-jump-date').disabled = true;
  try {
    const data = await fetchJSON(`/api/chat/timeline?account=${encodeURIComponent(account)}&chat=${encodeURIComponent(currentChat.username)}`);
    timelineDays = data.days || [];
    renderTimeline(data);
  } catch (e) {
    list.innerHTML = `<div class="tl-empty">时间轴加载失败：${esc(e.message)}</div>`;
  }
}

function renderTimeline(data) {
  const list = el('c-timeline-list');
  if (!timelineDays.length) {
    list.innerHTML = '<div class="tl-empty">这个会话暂无可跳转消息</div>';
    return;
  }
  const max = Math.max(...timelineDays.map(d => d.count || 0), 1);
  el('c-jump-date').disabled = false;
  el('c-jump-date').min = localDateValue(data.ts_min || timelineDays[0].first_ts);
  el('c-jump-date').max = localDateValue(data.ts_max || timelineDays[timelineDays.length - 1].last_ts);
  list.innerHTML = timelineDays.map(d => {
    const pct = Math.max(8, Math.round((d.count || 0) * 100 / max));
    return `<button class="tl-item" data-ts="${d.first_ts || 0}" title="${esc(d.day)} · ${d.count} 条">
      <span class="tl-dot"></span><span class="tl-date">${esc(d.day.slice(5))}</span>
      <span class="tl-bar"><i style="width:${pct}%"></i></span><span class="tl-count">${d.count}</span>
    </button>`;
  }).join('');
  list.querySelectorAll('.tl-item').forEach(btn => {
    btn.addEventListener('click', () => jumpToTime(Number(btn.dataset.ts || 0)));
  });
}

async function jumpToTime(ts) {
  if (!currentChat || !ts) return;
  const endOfDay = ts + 24 * 3600;
  el('c-msgs').innerHTML = '<div class="c-loading">正在跳转到所选时间…</div>';
  earliest = 0;
  await loadMessages(true, { before: endOfDay, limit: 80, smooth: true });
  const target = [...el('c-msgs').querySelectorAll('.m-row[data-ts]')]
    .find(n => Number(n.dataset.ts || 0) >= ts) || el('c-msgs').querySelector('.m-row[data-ts]');
  if (target) {
    target.classList.add('jump-hit');
    target.scrollIntoView({ behavior: 'smooth', block: 'center' });
    setTimeout(() => target.classList.remove('jump-hit'), 1400);
  }
}

function localDateValue(ts) {
  if (!ts) return '';
  const d = new Date(ts * 1000);
  const y = d.getFullYear();
  const m = String(d.getMonth() + 1).padStart(2, '0');
  const day = String(d.getDate()).padStart(2, '0');
  return `${y}-${m}-${day}`;
}

function setSelectionMode(on) {
  selectionMode = !!on;
  if (!selectionMode) {
    selectedKeys.clear();
    rangeAnchorKey = null;
  }
  el('chat-wrap')?.classList.toggle('selecting', selectionMode);
  el('c-select')?.classList.toggle('hidden', selectionMode);
  el('c-copy-selected')?.classList.toggle('hidden', !selectionMode);
  el('c-cancel-select')?.classList.toggle('hidden', !selectionMode);
  refreshSelectionUI();
}

function toggleMessageSelection(key, range) {
  if (!key) return;
  if (range && rangeAnchorKey) {
    const a = loadedMessages.findIndex(m => msgKey(m) === rangeAnchorKey);
    const b = loadedMessages.findIndex(m => msgKey(m) === key);
    if (a >= 0 && b >= 0) {
      const [lo, hi] = a < b ? [a, b] : [b, a];
      for (let i = lo; i <= hi; i++) selectedKeys.add(msgKey(loadedMessages[i]));
    }
  } else if (selectedKeys.has(key)) {
    selectedKeys.delete(key);
    rangeAnchorKey = key;
  } else {
    selectedKeys.add(key);
    rangeAnchorKey = key;
  }
  refreshSelectionUI();
}

function refreshSelectionUI() {
  el('c-msgs')?.querySelectorAll('.m-row[data-key]').forEach(row => {
    row.classList.toggle('selected', selectedKeys.has(row.dataset.key));
  });
  const n = selectedKeys.size;
  const btn = el('c-copy-selected');
  if (btn) {
    btn.textContent = n ? `复制所选 ${n}` : '复制所选';
    btn.disabled = !n;
  }
}

async function copySelectedMessages() {
  const picked = loadedMessages.filter(m => selectedKeys.has(msgKey(m)));
  if (!picked.length) return;
  await copyText(picked.map(messageCopyText).join('\n'), el('c-copy-selected'), '已复制所选');
}

function messageCopyText(m) {
  const sender = m.is_me ? '我' : (m.sender_name || currentChat?.display || '对方');
  const body = m.kind === 'image' ? '[图片]'
    : m.kind === 'voice' ? `[${voiceDuration(m.voice)}]`
    : m.kind === 'link' && m.link ? `${m.link.title || '链接'} ${m.link.url || ''}`
    : (m.text || m.content || '');
  return `[${fmtTs(m.ts)}] ${sender}: ${body}`;
}

async function copyText(text, btn, doneText) {
  try {
    if (navigator.clipboard && window.isSecureContext) {
      await navigator.clipboard.writeText(text);
    } else {
      const ta = document.createElement('textarea');
      ta.value = text;
      ta.style.position = 'fixed';
      ta.style.left = '-9999px';
      document.body.appendChild(ta);
      ta.focus();
      ta.select();
      document.execCommand('copy');
      ta.remove();
    }
    flashButton(btn, doneText || '已复制');
  } catch (e) {
    window.alert('复制失败：' + e.message);
  }
}

function flashButton(btn, text) {
  if (!btn) return;
  const old = btn.textContent;
  btn.textContent = text;
  btn.classList.add('copied');
  setTimeout(() => {
    btn.textContent = old;
    btn.classList.remove('copied');
  }, 1000);
}

function updateSubtitle() {
  if (!currentChat) return;
  const type = currentChat.is_group ? '群聊' : '私聊';
  const more = hasMore ? ' · 向上滚动加载更早' : '';
  el('c-sub').textContent = `${type} · 已载入 ${loadedMessages.length} 条${more}`;
}

async function openStatsModal() {
  if (!currentChat) return;
  const modal = el('c-stats-modal');
  const body = el('c-stats-body');
  modal.classList.remove('hidden');
  body.innerHTML = '<div class="c-loading">正在统计这个会话…</div>';
  try {
    const d = await fetchJSON(`/api/chat/stats?account=${encodeURIComponent(account)}&chat=${encodeURIComponent(currentChat.username)}`);
    body.innerHTML = renderStats(d);
  } catch (e) {
    body.innerHTML = `<div class="c-empty">统计失败：${esc(e.message)}</div>`;
  }
}

function closeStatsModal() {
  el('c-stats-modal').classList.add('hidden');
}

function renderStats(d) {
  const typeRows = (d.types || []).slice(0, 8).map(x =>
    `<div class="stat-line"><span>${esc(x.label)}</span><b>${x.count}</b></div>`).join('');
  return `
    <div class="stat-grid">
      <div class="stat-card"><span>消息总数</span><b>${d.total || 0}</b></div>
      <div class="stat-card"><span>我发送</span><b>${d.sent || 0}</b></div>
      <div class="stat-card"><span>对方/群成员</span><b>${d.received || 0}</b></div>
      <div class="stat-card"><span>活跃天数</span><b>${d.active_days || 0}</b></div>
    </div>
    <div class="stat-section">
      <div class="stat-title">时间跨度</div>
      <p>${fmtTs(d.first_ts)} — ${fmtTs(d.last_ts)}</p>
      <p>最活跃日期：${esc(d.busiest_day?.day || '-')}（${d.busiest_day?.count || 0} 条）</p>
      <p>最活跃时段：${d.busiest_hour == null ? '-' : `${d.busiest_hour}:00`} 左右</p>
    </div>
    <div class="stat-section">
      <div class="stat-title">类型分布</div>
      ${typeRows || '<p class="dim">暂无类型数据</p>'}
    </div>`;
}

export function destroy() { /* 无常驻定时器 */ }
export { init };
