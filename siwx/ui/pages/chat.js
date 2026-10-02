/* 聊天查看 —— 微信风格气泡 + 时间轴 + 消息选择/复制 */
import { createDropdown, openMenu, closeMenu } from '/widgets.js?v=2026100203';

const { esc, fetchJSON, fmtTs, fmtListTs } = window.SX;

let account = null;
let sessions = [];
let currentChat = null;
let officialOpen = false;
let earliest = 0;
let hasMore = false;
let loading = false;
let loadedMessages = [];
// 请求代数守卫：fresh 加载（打开会话/跳转/刷新）递增序号，使仍在飞的
// 旧请求（滚动加载/上一次跳转）返回后直接丢弃，避免旧响应覆盖新页面
let loadSeq = 0;
let timelineMonths = [];
let timelineDayCache = new Map();
let selectionMode = false;
let selectedKeys = new Set();
let rangeAnchorKey = null;
let accDrop = null;
let bottomFollow = null;

function el(id) { return document.getElementById(id); }

async function init() {
  accDrop = createDropdown({ options: [], placeholder: '选择账号', label: '微信账号' });
  el('c-account').appendChild(accDrop.el);
  try {
    const { accounts } = await fetchJSON('/api/chat/accounts');
    if (!accounts.length) {
      el('c-sessions').innerHTML =
        '<div class="c-empty">还没有解密产物 — 请先在"引导设置"完成解密</div>';
      return;
    }
    accDrop.options = accounts.map(a => ({ value: a.wxid, label: a.wxid }));
    account = accDrop.value;
    accDrop.onChange = (v) => {
      account = v;
      currentChat = null;
      resetChatView();
      loadSessions();
    };
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
      const acc = accDrop?.value || account;
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
  el('c-timeline-trigger').addEventListener('click', () => {
    el('c-timeline').classList.toggle('pinned');
  });

  // 跳转导出页并预选当前会话；若处于多选状态，把所选范围一并带过去
  el('c-export').addEventListener('click', () => {
    if (!currentChat) { window.alert('先在左侧打开一个会话'); return; }
    const preset = { account, chat: currentChat.username, display: currentChat.display };
    if (selectionMode && selectedKeys.size) {
      const tss = [...selectedKeys]
        .map(k => messageByKey(k)).filter(Boolean)
        .map(m => m.ts || 0).filter(Boolean);
      if (tss.length) {
        const day = (ts) => {
          const d = new Date(ts * 1000);
          return `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, '0')}-${String(d.getDate()).padStart(2, '0')}`;
        };
        preset.range = {
          start: day(Math.min(...tss)),
          end: day(Math.max(...tss)),
          count: selectedKeys.size,
        };
      }
    }
    sessionStorage.setItem('siwx-export-preset', JSON.stringify(preset));
    location.hash = '#/export';
  });

  // 消息区事件委托：复制单条、选择/范围选择、多选范围菜单（右键 / 长按）
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
  el('c-msgs').addEventListener('contextmenu', (ev) => {
    if (!selectionMode) return;
    const row = ev.target.closest('.m-row[data-key]');
    if (!row) return;
    ev.preventDefault();
    openRangeMenu(row.dataset.key, ev.clientX, ev.clientY);
  });
  // 触屏长按 500ms = 右键
  let pressTimer = null;
  el('c-msgs').addEventListener('touchstart', (ev) => {
    if (!selectionMode) return;
    const row = ev.target.closest('.m-row[data-key]');
    if (!row) return;
    const touch = ev.touches[0];
    pressTimer = setTimeout(() => {
      pressTimer = null;
      openRangeMenu(row.dataset.key, touch.clientX, touch.clientY);
    }, 500);
  }, { passive: true });
  const cancelPress = () => { if (pressTimer) { clearTimeout(pressTimer); pressTimer = null; } };
  el('c-msgs').addEventListener('touchmove', cancelPress, { passive: true });
  el('c-msgs').addEventListener('touchend', cancelPress);

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
  timelineMonths = [];
  timelineDayCache.clear();
  earliest = 0;
  hasMore = false;
  setSelectionMode(false);
  el('c-title').textContent = '微信';
  el('c-sub').textContent = '选择一个会话查看聊天记录';
  el('c-msgs').innerHTML = '<div class="c-tip">选择一个会话查看聊天记录</div>';
  el('c-timeline-list').innerHTML = '<div class="tl-empty">打开会话后可按月份跳转</div>';
  el('c-timeline').classList.remove('pinned');
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
      <span class="tm">${fmtListTs(s.last_time)}</span>
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
        <span class="tm">${fmtListTs(latest)}</span>
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
  loadSeq++;               // 使切换会话前仍在飞的旧请求失效
  loadedMessages = [];
  setSelectionMode(false);
  el('chat-wrap').classList.add('open');
  el('c-title').textContent = s.display;
  el('c-sub').textContent = s.is_group ? '群聊 · 加载中' : '私聊 · 加载中';
  el('c-msgs').innerHTML = '<div class="c-loading">加载消息…</div>';
  el('c-select').disabled = false;
  el('c-stats').disabled = false;
  await Promise.all([loadMessages(true, { stick: true }), loadTimeline()]);
}

/** 让消息区立即回到底部。
 *  #c-msgs 有 scroll-behavior:smooth，直接赋值 scrollTop 会被 CSS
 *  拖成一场漫长的平滑滚动——看起来就是"没跳到底部"。这里临时
 *  关掉平滑行为瞬时贴底。 */
function jumpToBottom(box) {
  const prev = box.style.scrollBehavior;
  box.style.scrollBehavior = 'auto';
  box.scrollTop = box.scrollHeight;
  box.style.scrollBehavior = prev;
}

/** 打开会话后的"贴底跟随"：图片懒加载就位后内容高度还会持续增长，
 *  每次都把视口按回底部；用户一旦明显向上滚动（距底超过 2/3 视口）
 *  或发出滚轮/触摸手势，立即停止跟随让位给用户。 */
function startBottomFollow(box) {
  stopBottomFollow();
  let following = true;
  let pinning = false;
  const pin = () => {
    if (!following) return;
    pinning = true;
    jumpToBottom(box);
    requestAnimationFrame(() => { pinning = false; });
  };
  const onScroll = () => {
    if (!following || pinning) return;
    const dist = box.scrollHeight - box.scrollTop - box.clientHeight;
    if (dist > box.clientHeight * 0.66) stopBottomFollow();
  };
  const onLoad = (ev) => { if (ev.target && ev.target.tagName === 'IMG') pin(); };
  const onGesture = () => stopBottomFollow();
  box.addEventListener('scroll', onScroll);
  box.addEventListener('load', onLoad, true);   // load 不冒泡，捕获阶段接
  box.addEventListener('wheel', onGesture, { passive: true });
  box.addEventListener('touchstart', onGesture, { passive: true });
  bottomFollow = {
    stop() {
      following = false;
      box.removeEventListener('scroll', onScroll);
      box.removeEventListener('load', onLoad, true);
      box.removeEventListener('wheel', onGesture);
      box.removeEventListener('touchstart', onGesture);
    },
  };
}

function stopBottomFollow() {
  if (bottomFollow) { bottomFollow.stop(); bottomFollow = null; }
}

async function loadMessages(fresh, opts = {}) {
  const s = currentChat;
  if (!s) return false;
  if (fresh) loadSeq++;               // fresh 加载使所有在飞旧请求失效
  const seq = loadSeq;
  const before = opts.before || (!fresh && earliest ? earliest : 0);
  const limit = opts.limit || 80;
  const url = `/api/chat/messages?account=${encodeURIComponent(account)}` +
    `&chat=${encodeURIComponent(s.username)}` +
    (before ? `&before=${before}` : '') +
    `&limit=${limit}`;
  const data = await fetchJSON(url);
  if (seq !== loadSeq || s !== currentChat) return false;   // 过期响应，丢弃
  const box = el('c-msgs');
  if (fresh) {
    selectedKeys.clear();
    rangeAnchorKey = null;
    loadedMessages = data.messages.slice();
    box.innerHTML = renderMessageList(loadedMessages) || '<div class="c-empty">这个会话没有消息</div>';
    if (opts.stick) {
      startBottomFollow(box);
      jumpToBottom(box);
      // 首屏图片尚未就位时高度还会变，下一帧再校准一次
      requestAnimationFrame(() => { if (bottomFollow) jumpToBottom(box); });
    } else {
      stopBottomFollow();
    }
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
  list.innerHTML = '<div class="tl-empty">正在生成月份索引…</div>';
  timelineDayCache.clear();
  try {
    const data = await fetchJSON(`/api/chat/timeline?account=${encodeURIComponent(account)}&chat=${encodeURIComponent(currentChat.username)}`);
    timelineMonths = data.months || [];
    renderTimelineMonths();
  } catch (e) {
    list.innerHTML = `<div class="tl-empty">时间轴加载失败：${esc(e.message)}</div>`;
  }
}

function renderTimelineMonths() {
  const list = el('c-timeline-list');
  if (!timelineMonths.length) {
    list.innerHTML = '<div class="tl-empty">这个会话暂无可跳转消息</div>';
    return;
  }
  list.innerHTML = timelineMonths.map(m => `
    <section class="tl-month" data-month="${esc(m.month)}">
      <button class="tl-month-btn" type="button" title="展开 ${esc(m.month)}">
        <span class="tl-dot"></span><span class="tl-month-label">${esc(monthLabel(m.month))}</span>
        <span class="tl-count">${m.count}</span><span class="tl-chevron">›</span>
      </button>
      <div class="tl-days"><div class="tl-days-inner"></div></div>
    </section>`).join('');

  list.querySelectorAll('.tl-month').forEach(section => {
    const open = () => expandTimelineMonth(section);
    section.querySelector('.tl-month-btn').addEventListener('click', () => {
      if (section.classList.contains('open')) section.classList.remove('open');
      else open();
    });
    section.addEventListener('mouseenter', open, { once: false });
  });
}

async function expandTimelineMonth(section) {
  if (!currentChat || !section) return;
  const month = section.dataset.month;
  document.querySelectorAll('.tl-month.open').forEach(n => {
    if (n !== section && !el('c-timeline').classList.contains('pinned')) n.classList.remove('open');
  });
  section.classList.add('open');
  const inner = section.querySelector('.tl-days-inner');
  if (timelineDayCache.has(month)) {
    renderTimelineDays(inner, timelineDayCache.get(month));
    return;
  }
  if (section.dataset.loading === '1') return;
  section.dataset.loading = '1';
  inner.innerHTML = '<div class="tl-empty">加载日期…</div>';
  try {
    const data = await fetchJSON(`/api/chat/timeline?account=${encodeURIComponent(account)}&chat=${encodeURIComponent(currentChat.username)}&month=${encodeURIComponent(month)}`);
    const days = data.days || [];
    timelineDayCache.set(month, days);
    renderTimelineDays(inner, days);
  } catch (e) {
    inner.innerHTML = `<div class="tl-empty">加载失败</div>`;
  } finally {
    section.dataset.loading = '0';
  }
}

function renderTimelineDays(inner, days) {
  inner.innerHTML = days.length ? days.map(d => `
    <button class="tl-day" type="button" data-ts="${d.first_ts || 0}" data-day="${esc(d.day)}" title="${esc(d.day)} · ${d.count} 条">
      <span>${esc(d.day.slice(8))}日</span><b>${d.count}</b>
    </button>`).join('') : '<div class="tl-empty">该月没有消息</div>';
  inner.querySelectorAll('.tl-day').forEach(btn => {
    // 跳转目标传日期字符串而非 first_ts——first_ts 是"当天第一条消息"时刻，
    // 以它 +24h 当窗口终点会把次日上午的消息算进窗口
    btn.addEventListener('click', () => jumpToTime(btn.dataset.day || ''));
  });
}

function monthLabel(month) {
  const [year, mon] = String(month || '').split('-');
  return `${year}年${Number(mon)}月`;
}

async function jumpToTime(day) {
  if (!currentChat || !day) return;
  // 由日期字符串计算次日零点（本机时区，与服务端 strftime localtime 语义
  // 一致）作为跳转窗口终点。原实现 endOfDay = first_ts + 24h 实际是
  // "当天第一条消息时刻 + 24 小时"：当天首条消息若发于 10:00，窗口会覆盖
  // 次日凌晨到上午的消息，次日上午消息多时当天内容被整个挤出窗口
  // （new Date 对 dd+1 的进月/进年由 Date 自动处理）。
  const [yy, mm, dd] = String(day).split('-').map(Number);
  if (!yy || !mm || !dd) return;
  const endOfDay = Math.floor(new Date(yy, mm - 1, dd + 1).getTime() / 1000);
  // 跳转期间置 loading 并清 hasMore，阻止滚动监听器在此期间再发出带旧
  // 游标的加载请求，与跳转请求形成三方竞态
  loading = true;
  hasMore = false;
  el('c-msgs').innerHTML = '<div class="c-loading">正在跳转到所选时间…</div>';
  earliest = 0;
  try {
    await loadMessages(true, { before: endOfDay, limit: 80 });
  } finally {
    loading = false;
  }
  const target = [...el('c-msgs').querySelectorAll('.m-row[data-ts]')]
    .find(n => Number(n.dataset.ts || 0) >= tsOfTarget(day)) || el('c-msgs').querySelector('.m-row[data-ts]');
  if (target) {
    target.classList.add('jump-hit');
    target.scrollIntoView({ behavior: 'smooth', block: 'center' });
    setTimeout(() => target.classList.remove('jump-hit'), 1400);
    recenterAfterImages(target);   // 图片懒加载会挤偏落点，加载后重新居中
  }
}

/** 当天零点（本地时区）时间戳，跳转定位用：窗口按"次日零点前"取，
 *  命中的第一条应 >= 当天零点。 */
function tsOfTarget(day) {
  const [yy, mm, dd] = String(day).split('-').map(Number);
  return Math.floor(new Date(yy, mm - 1, dd).getTime() / 1000);
}

/** 跳转定位靠一次性 scrollIntoView，而消息图片懒加载完成后内容高度
 *  持续变化，滚动结束时目标行已被挤离原位。这里对目标行附近尚未加载
 *  的图片改为立即加载，每次加载完成后把目标行重新滚回视野中心
 *  （与打开会话的贴底跟随同思路）；4 秒后停止。 */
function recenterAfterImages(target) {
  const box = el('c-msgs');
  const recenter = () => target.scrollIntoView({ block: 'center', behavior: 'auto' });
  const rows = [target];
  let sib = target.previousElementSibling;
  for (let n = 0; sib && n < 8; n++) { rows.push(sib); sib = sib.previousElementSibling; }
  sib = target.nextElementSibling;
  for (let n = 0; sib && n < 8; n++) { rows.push(sib); sib = sib.nextElementSibling; }
  let pending = 0;
  const timer = setTimeout(stop, 4000);
  function stop() { clearTimeout(timer); recenter(); }
  rows.forEach(row => {
    row.querySelectorAll('img[loading="lazy"]').forEach(img => {
      img.loading = 'eager';
      if (!img.complete) {
        pending++;
        img.addEventListener('load', onImg, { once: true });
        img.addEventListener('error', onImg, { once: true });
      }
    });
  });
  function onImg() {
    recenter();
    if (--pending <= 0) stop();
  }
  if (!pending) stop();
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
  // 多选状态下把导出按钮变成「导出所选」，让范围联动可见
  const ex = el('c-export');
  if (ex) ex.textContent = (selectionMode && n) ? `导出所选 ${n}` : '导出';
}

/* ── 多选范围菜单（微信式「选择以上 / 以下」）────────────────── */
function rangeSelect(key, dir, add) {
  const idx = loadedMessages.findIndex(m => msgKey(m) === key);
  if (idx < 0) return;
  const lo = dir === 'above' ? 0 : idx;
  const hi = dir === 'above' ? idx : loadedMessages.length - 1;
  for (let i = lo; i <= hi; i++) {
    const k = msgKey(loadedMessages[i]);
    if (add) selectedKeys.add(k);
    else selectedKeys.delete(k);
  }
  rangeAnchorKey = key;
  refreshSelectionUI();
}

function openRangeMenu(key, x, y) {
  const idx = loadedMessages.findIndex(m => msgKey(m) === key);
  if (idx < 0) return;
  const picked = selectedKeys.has(key);
  const items = [
    { label: picked ? '取消选择这条' : '选择这条', onClick: () => toggleMessageSelection(key, false) },
    { label: '选择以上（含这条）', disabled: idx === 0, onClick: () => rangeSelect(key, 'above', true) },
    { label: '选择以下（含这条）', disabled: idx === loadedMessages.length - 1, onClick: () => rangeSelect(key, 'below', true) },
    { label: '取消以上', disabled: idx === 0, onClick: () => rangeSelect(key, 'above', false) },
    { label: '取消以下', disabled: idx === loadedMessages.length - 1, onClick: () => rangeSelect(key, 'below', false) },
    { label: `全选已载入（${loadedMessages.length} 条）`, onClick: () => {
        loadedMessages.forEach(m => selectedKeys.add(msgKey(m)));
        refreshSelectionUI();
      } },
  ];
  if (selectedKeys.size) {
    items.push({ label: '清空已选', onClick: () => { selectedKeys.clear(); rangeAnchorKey = null; refreshSelectionUI(); } });
  }
  if (hasMore) {
    items.push({ note: '更早的消息还没载入，范围仅对已载入部分生效；向上滚动可加载更早消息。' });
  }
  openMenu(items, x, y);
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

export function destroy() {
  stopBottomFollow();
  closeMenu();
}
export { init };
