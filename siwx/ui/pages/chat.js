/* 聊天查看 —— 微信风格气泡 + 时间轴 + 消息选择/复制。
   账号来自全局账号卡（SX.getAccount），本页不再自选；
   复制/提示统一走 SX.copyText / SX.toast；图片交互用事件委托，无内联事件。 */
import { openMenu, closeMenu } from '/widgets.js?v=2026100601';

const { esc, fetchJSON, fmtTs, fmtListTs, copyText, toast, getAccount, go } = window.SX;

let account = null;
let sessions = [];
let currentChat = null;
let officialOpen = false;
let earliest = 0;
let earliestId = 0;      // 翻页游标第二分量：同一秒的消息按 local_id 续翻
let hasMore = false;
let hasNewer = false;    // 时间轴跳转后窗口之后（更新）的消息是否可继续加载
let ownerWxid = null;    // 服务端解析的本人展示 wxid（账号目录名可能带设备后缀）
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
let bottomFollow = null;
// 头像 404 标记：失败过的头像不再反复请求（几百会话 = 几百次请求的风暴源）
const avaFailed = new Set();

function el(id) { return document.getElementById(id); }

/** 页面存活哨兵：视图被下一次导航换掉后，仍在飞的异步回调必须就此止步，
 *  否则对已卸载元素 el(...).innerHTML 赋值会抛 null 错误（app.js 兜底横幅）。 */
function gone() { return !document.getElementById('chat-wrap'); }

const EMPTY_CTA = '<div class="c-empty">还没有解密产物<br>' +
  '<button class="btn btn-primary" id="c-go-guide" type="button">去添加账号</button></div>';

function bindGuideCta() {
  el('c-go-guide')?.addEventListener('click', () => window.__sxOnboarding?.open('add'));
}

async function init() {
  account = getAccount();
  if (!account) {
    el('c-sessions').innerHTML = EMPTY_CTA;
    bindGuideCta();
    return;
  }

  el('c-search').addEventListener('input', () => renderSessions(el('c-search').value));
  el('c-back').addEventListener('click', () => el('chat-wrap').classList.remove('open'));
  el('c-refresh').addEventListener('click', async () => {
    const btn = el('c-refresh');
    btn.disabled = true;
    el('c-sessions').innerHTML = '<div class="c-loading">增量同步中…</div>';
    try {
      const r = await fetch('/api/run', {
        method: 'POST', headers: {'content-type': 'application/json'},
        body: JSON.stringify({ mode: 'sync', export_opts: { account } }),
      });
      if (r.status === 409) {
        toast('已有任务在运行，稍后再试', true);
        await loadSessions();              // 恢复列表，不再停留在一行字
        return;
      }
      if (!r.ok) throw new Error('启动失败');
      for (let i = 0; i < 300; i++) {
        await new Promise(res => setTimeout(res, 1000));
        if (gone()) return;
        const j = await (await fetch('/api/job')).json();
        if (!j.running) break;
        if (j.logs.length) {
          const entry = j.logs[j.logs.length - 1];
          const msg = String(entry.length >= 4 ? entry[3] : entry[1]);
          el('c-sessions').innerHTML = `<div class="c-loading">${esc(msg.substring(0, 60))}${msg.length > 60 ? '…' : ''}</div>`;
        }
      }
      await loadSessions();
      if (currentChat) await Promise.all([loadMessages(true), loadTimeline()]);
    } catch (e) {
      if (!gone()) {
        el('c-sessions').innerHTML = `<div class="c-empty">刷新失败: ${esc(e.message)}</div>`;
        toast('刷新失败：' + (e.message || e), true);
      }
    } finally {
      if (!gone()) btn.disabled = false;
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
    if (!currentChat) { toast('先在左侧打开一个会话', true); return; }
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

  // 消息区事件委托：复制单条、图片查看、选择/范围选择、多选范围菜单（右键 / 长按）
  el('c-msgs').addEventListener('click', async (ev) => {
    if (ev.target.closest('.c-load-newer')) {
      loadNewer();
      return;
    }
    // 多选模式下点击链接只做选中，不触发跳转
    if (selectionMode && ev.target.closest('a')) ev.preventDefault();
    const copyBtn = ev.target.closest('.m-copy');
    if (copyBtn) {
      ev.stopPropagation();
      const row = copyBtn.closest('.m-row');
      const msg = messageByKey(row?.dataset.key);
      if (msg) await copyWithFlash(copyBtn, messageCopyText(msg), '已复制');
      return;
    }
    const img = ev.target.closest('.m-img');
    if (img && !selectionMode) {
      window.open(img.src + '&hq=1', '_blank', 'noopener');
      return;
    }
    const finderImg = ev.target.closest('.m-finder-cover img');
    if (finderImg) {
      // 视频号卡片不承诺播放；点击封面看大图是唯一合理的后备操作
      window.open(finderImg.src, '_blank', 'noopener');
      return;
    }
    const row = ev.target.closest('.m-row[data-key]');
    if (!row || !selectionMode) return;
    toggleMessageSelection(row.dataset.key, ev.shiftKey);
  });
  // 图片加载失败：error 不冒泡，捕获阶段接管；重试 URL 放 dataset，
  // 不再用内联 onerror（兼容 CSP no-unsafe-inline），头像失败同时记入标记
  el('c-msgs').addEventListener('error', (ev) => {
    const img = ev.target;
    if (!img || img.tagName !== 'IMG') return;
    if (img.dataset.ava !== undefined) {
      avaFailed.add(img.dataset.ava);
      img.remove();
    } else if (img.dataset.finder !== undefined) {
      // 视频号封面/头像加载失败（CDN 过期）：隐藏图片保留文字信息
      img.remove();
    } else if (img.dataset.sticker !== undefined) {
      // 动画表情加载失败（CDN 过期/离线）：回退为文字，不显示重试占位
      const span = document.createElement('span');
      span.className = 'm-sticker-text';
      span.textContent = '[动画表情]';
      img.replaceWith(span);
    } else if (img.dataset.retry) {
      SX.imgFallback(img, img.dataset.retry);
    }
  }, true);
  el('c-msgs').addEventListener('contextmenu', (ev) => {
    if (!selectionMode) return;
    const row = ev.target.closest('.m-row[data-key]');
    if (!row) return;
    ev.preventDefault();
    openRangeMenu(row.dataset.key, ev.clientX, ev.clientY);
  });
  // 触屏长按 500ms = 右键；菜单弹出后随后的 click 不再翻转选中
  let pressTimer = null;
  let suppressNextRowClick = false;
  el('c-msgs').addEventListener('touchstart', (ev) => {
    if (!selectionMode) return;
    const row = ev.target.closest('.m-row[data-key]');
    if (!row) return;
    const touch = ev.touches[0];
    pressTimer = setTimeout(() => {
      pressTimer = null;
      suppressNextRowClick = true;
      openRangeMenu(row.dataset.key, touch.clientX, touch.clientY);
    }, 500);
  }, { passive: true });
  const cancelPress = () => { if (pressTimer) { clearTimeout(pressTimer); pressTimer = null; } };
  el('c-msgs').addEventListener('touchmove', cancelPress, { passive: true });
  el('c-msgs').addEventListener('touchend', (ev) => {
    cancelPress();
    // 长按刚弹出菜单时吞掉紧随的 click，防止菜单下方消息被误选中
    if (suppressNextRowClick) {
      ev.preventDefault();
      suppressNextRowClick = false;
    }
  });

  // 滚动到顶部自动加载更早消息
  el('c-msgs').addEventListener('scroll', async () => {
    const box = el('c-msgs');
    if (box.scrollTop <= 4 && hasMore && !loading && currentChat) {
      loading = true;
      const keep = box.scrollHeight;
      box.insertAdjacentHTML('afterbegin', '<div class="c-loading">加载更早消息…</div>');
      try {
        await loadMessages(false);
      } catch (e) {
        toast('加载更早消息失败：' + (e.message || e), true);
      } finally {
        const tip = box.querySelector('.c-loading');
        if (tip) tip.remove();
        // 锚定视口：直接赋值会被 scroll-behavior:smooth 拖成动画滚动
        const prev = box.style.scrollBehavior;
        box.style.scrollBehavior = 'auto';
        box.scrollTop = box.scrollHeight - keep;
        box.style.scrollBehavior = prev;
        loading = false;
      }
    }
  });

  await loadFaces();
  await loadSessions();
}

async function loadSessions() {
  if (gone()) return;
  el('c-sessions').innerHTML = '<div class="c-loading">加载会话…</div>';
  officialOpen = false;
  try {
    const { sessions: ss } = await fetchJSON(
      `/api/chat/sessions?account=${encodeURIComponent(account)}`);
    if (gone()) return;
    sessions = ss;
    renderSessions('');
    if (el('c-sub') && !currentChat) el('c-sub').textContent = `${ss.length} 个会话`;
  } catch (e) {
    if (gone()) return;
    el('c-sessions').innerHTML = `<div class="c-empty">加载失败：${esc(e.message)}<br>` +
      '<button class="btn" id="c-sess-retry" type="button">重试</button></div>';
    el('c-sess-retry')?.addEventListener('click', loadSessions);
  }
}

function isOfficial(s) {
  return !!(s && (s.is_official || s.kind === 'official' || /^gh_/.test(s.username || '')));
}

function ownerUsername() {
  return /^wxid_.+_\d+$/.test(account || '') ? account.replace(/_\d+$/, '') : account;
}

function avatarImg(username) {
  if (avaFailed.has(username)) return '';
  const src = `/api/chat/avatar?account=${encodeURIComponent(account)}` +
              `&username=${encodeURIComponent(username)}`;
  // 失败由 #c-msgs 的捕获级 error 委托处理（记入 avaFailed 并移除）
  return `<img loading="lazy" src="${src}" data-ava="${esc(username)}" alt="">`;
}

function sessionRow(s, extra = '') {
  const initials = (s.display || s.username || '?').slice(0, 2).toUpperCase();
  const isGrp = s.is_group;
  const off = isOfficial(s);
  const selected = currentChat && currentChat.username === s.username;
  return `
    <div class="sess ${extra} ${selected ? 'sel' : ''}" data-u="${esc(s.username)}"
         tabindex="0" role="button" aria-label="会话 ${esc(s.display)}">
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
      <div class="sess sess-group ${open ? 'open' : ''}" data-group="official"
           tabindex="0" role="button" aria-label="公众号分组，点击${open ? '收起' : '展开'}">
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
    const activate = () => {
      if (n.dataset.group === 'official') {
        officialOpen = !officialOpen;
        renderSessions(el('c-search').value);
        return;
      }
      const s = sessions.find(x => x.username === n.dataset.u);
      if (s) openChat(s);
    };
    n.addEventListener('click', activate);
    // 键盘等价：Enter/Space 激活，列表行此前完全不可聚焦、无法用键盘选中
    n.addEventListener('keydown', e => {
      if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); activate(); }
    });
  });
}

async function openChat(s) {
  currentChat = s;
  earliest = 0;
  earliestId = 0;
  hasMore = false;
  hasNewer = false;
  ownerWxid = null;
  loadSeq++;               // 使切换会话前仍在飞的旧请求失效
  loadedMessages = [];
  setSelectionMode(false);
  el('chat-wrap').classList.add('open');
  el('c-title').textContent = s.display;
  el('c-sub').textContent = s.is_group ? '群聊 · 加载中' : '私聊 · 加载中';
  el('c-msgs').innerHTML = '<div class="c-loading">加载消息…</div>';
  el('c-select').disabled = false;
  el('c-stats').disabled = false;
  el('c-export').disabled = false;
  try {
    await Promise.all([loadMessages(true, { stick: true }), loadTimeline()]);
  } catch (e) {
    if (gone()) return;
    el('c-msgs').innerHTML = `<div class="c-empty">加载失败：${esc(e.message)}<br>` +
      '<button class="btn" id="c-msgs-retry" type="button">重试</button></div>';
    el('c-msgs-retry')?.addEventListener('click', () => openChat(s));
    el('c-sub').textContent = `${s.is_group ? '群聊' : '私聊'} · 加载失败`;
  }
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
  // 游标为 (ts, localId) 组合：同一秒多条消息只按时间戳 `<` 翻页会被跳过
  const beforeId = (!fresh && !opts.before && before && earliestId) ? earliestId : 0;
  // 正向翻页（时间轴跳转后继续往后看）：游标取已载入的最后一条
  const after = opts.after || 0;
  const afterId = after ? (opts.afterId || 0) : 0;
  const limit = opts.limit || 80;
  const url = `/api/chat/messages?account=${encodeURIComponent(account)}` +
    `&chat=${encodeURIComponent(s.username)}` +
    (before ? `&before=${before}` : '') +
    (beforeId ? `&before_id=${beforeId}` : '') +
    (after ? `&after=${after}` : '') +
    (afterId ? `&after_id=${afterId}` : '') +
    `&limit=${limit}`;
  const data = await fetchJSON(url);
  if (gone() || seq !== loadSeq || s !== currentChat) return false;   // 过期响应，丢弃
  const box = el('c-msgs');
  ownerWxid = data.owner || ownerWxid;
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
  } else if (after) {
    // 正向追加：新消息接在尾部，视口跳到新内容
    const prevLast = loadedMessages[loadedMessages.length - 1];
    loadedMessages = mergeMessages(data.messages, loadedMessages);
    const added = data.messages.filter(m => prevLast && (m.ts > prevLast.ts ||
      (m.ts === prevLast.ts && (m.id || 0) > (prevLast.id || 0))));
    if (added.length) {
      box.insertAdjacentHTML('beforeend', renderMessageList(added));
      dedupeTimeChips(box);
      refreshSelectionUI();
    }
  } else {
    loadedMessages = mergeMessages(data.messages, loadedMessages);
    box.insertAdjacentHTML('afterbegin', renderMessageList(data.messages));
    dedupeTimeChips(box);
    refreshSelectionUI();
  }
  if (after) {
    hasNewer = !!data.has_more;          // 正向：has_more 表示还有更新的
  } else {
    // 反向/跳转：has_more 表示还有更早的；has_newer 由服务端判定
    // （跳转窗口的次日零点游标之后是否仍有消息）
    hasMore = data.has_more;
    hasNewer = fresh ? !!data.has_newer : hasNewer;
    earliest = loadedMessages.length ? loadedMessages[0].ts : earliest;
    earliestId = loadedMessages.length ? (loadedMessages[0].id || 0) : earliestId;
  }
  refreshNewerButton();
  updateSubtitle();
  return true;
}

/** 底部"加载之后的消息"按钮：时间轴跳转只载入窗口内消息，窗口之后
 *  （更新）的消息此前无入口可达——滑到底就断了。有更晚消息时渲染按钮。 */
function refreshNewerButton() {
  const box = el('c-msgs');
  if (!box) return;
  box.querySelectorAll('.c-newer').forEach(n => n.remove());
  if (hasNewer && loadedMessages.length) {
    box.insertAdjacentHTML('beforeend',
      '<div class="c-newer"><button class="c-load-newer" type="button">加载之后的消息 ↓</button></div>');
  }
}

async function loadNewer() {
  const box = el('c-msgs');
  const last = loadedMessages[loadedMessages.length - 1];
  if (!last || !hasNewer || loading || !currentChat) return;
  loading = true;
  box.insertAdjacentHTML('beforeend',
    '<div class="c-loading" id="newer-tip">加载之后的消息…</div>');
  try {
    await loadMessages(false, { after: last.ts || 0, afterId: last.id || 0 });
    jumpToBottom(box);
  } catch (e) {
    toast('加载失败: ' + (e.message || e), true);
  } finally {
    document.getElementById('newer-tip')?.remove();
    loading = false;
  }
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

/** 相邻去重时间 chip：一条 chip 只在它与上一条消息间隔 >300s 时保留。
 *  分批追加/前插后，批次接缝处会渲染出间隔很近的两个 chip。 */
function dedupeTimeChips(box) {
  let prevTs = null;
  let pendingChip = null;
  [...box.children].forEach(n => {
    if (n.classList.contains('m-time-chip')) { pendingChip = n; return; }
    const ts = Number(n.dataset?.ts || 0);
    if (pendingChip) {
      if (prevTs != null && ts - prevTs <= 300) pendingChip.remove();
      pendingChip = null;
    }
    if (n.classList.contains('m-row')) prevTs = ts;
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

/* ── 小黄脸：与导出管线共用 siwx/wx_faces.py 素材表 ──────────────
 * 后端 /api/chat/faces 给名称表、/api/chat/face 给 PNG；
 * 正文/引用/系统消息里的已知 [表情名] 替换为内联图，未知名原样保留。 */
let faceNames = null;
async function loadFaces() {
  try {
    const d = await fetchJSON('/api/chat/faces');
    faceNames = new Set(d.names || []);
  } catch (e) {
    faceNames = new Set();               // 后端不支持时维持纯文本
  }
}
const FACE_TOKEN_RE = /\[[^\[\]\n]{1,20}\]/g;
function faceText(escaped) {
  if (!faceNames || !faceNames.size) return escaped;
  return escaped.replace(FACE_TOKEN_RE, tok => {
    const name = tok.slice(1, -1);
    if (!faceNames.has(name)) return tok;
    return `<img class="wx-face" alt="${tok}" title="${tok}"` +
           ` src="/api/chat/face?name=${encodeURIComponent(name)}">`;
  });
}

/** 复制用的纯文本：正文优先（保留 [表情名] 字面量）；媒体/特殊类型回退可读占位 */
function messageCopyText(m) {
  if (m.text) return m.text;
  const fallback = {
    image: '[图片]', file: '[文件]', voice: '[语音]', card: '[名片]',
    video: '[视频]', sticker: '[动画表情]', location: '[位置]', call: '[通话]',
    record: '[聊天记录]', channels: '[视频号卡片]',
  }[m.kind];
  return fallback || (m.link && m.link.url) || `[${m.type ?? '消息'}]`;
}

/** 合并转发（recordinfo）逐条展开的 HTML。子消息时间：服务端形态是
 *  "YYYY-MM-DD HH:MM" 字符串（item.time），本地形态是 epoch（item.ts）。 */
function recordHtml(rec) {
  const items = (rec && rec.items) || [];
  const rows = items.map(it => {
    const t = it.time || (it.ts ? fmtTs(it.ts) : '');
    return `<div class="m-record-item"><span class="rs">${esc(it.sender || '')}</span>` +
           `<span class="rt-text">${faceText(esc(it.text || ''))}</span>` +
           (t ? `<span class="rt">${esc(t)}</span>` : '') + `</div>`;
  }).join('');
  return `<div class="m-record">` +
    `<div class="m-record-title">${esc((rec && rec.title) || '聊天记录')}</div>` +
    `<div class="m-record-items">${rows || '<div class="m-record-item"><span class="rt">子消息详情未包含在数据中</span></div>'}</div>` +
    `<div class="m-record-count">${(rec && rec.count) || items.length} 条消息</div></div>`;
}

function bubble(m) {
  if (m.type === 10000 || m.type === 10002) {
    return `<div class="m-row sys" data-key="${esc(msgKey(m))}" data-ts="${m.ts || 0}"><div class="m-bubble">${faceText(esc(m.text))}</div><button class="m-copy">复制</button></div>`;
  }
  const who = m.is_me ? 'me' : '';
  // 本人头像用服务端解析的 owner wxid：账号目录名可能带设备后缀，
  // 直接用 ownerUsername() 剥不干净（微信 4.x 后缀是十六进制段）
  const avaUser = m.is_me ? (ownerWxid || ownerUsername())
                          : (m.sender_wxid || currentChat.username);
  const ava = `<div class="cell-ava">${avaHtml(avaUser, (m.sender_name || '?').slice(0, 2).toUpperCase())}</div>`;
  const name = (!m.is_me && currentChat && currentChat.is_group && m.sender_name)
    ? `<div class="m-name">${esc(m.sender_name)}</div>` : '';
  let inner = '';
  if (m.render) {
    inner = SX.renderNodes(m.render);
  } else if (m.kind === 'quote' && m.quote) {
    inner = `<div class="m-quote"><div class="m-quote-n">${esc(m.quote.displayname)}</div>` +
            `<div class="m-quote-t">${faceText(esc(m.quote.content))}</div></div>` + faceText(esc(m.text));
  } else if (m.kind === 'channels' && m.channels) {
    // 视频号卡片：还原客户端可见信息（作者/描述/封面），播放不强求
    const ch = m.channels;
    const dur = ch.duration
      ? `<span class="m-finder-dur">${Math.floor(ch.duration / 60)}:${String(ch.duration % 60).padStart(2, '0')}</span>`
      : '';
    const cover = ch.cover
      ? `<div class="m-finder-cover"><img loading="lazy" src="${esc(ch.cover)}" alt="" data-finder="1"><span class="m-finder-play">▶</span>${dur}</div>`
      : '';
    const desc = ch.desc ? `<div class="m-finder-desc">${esc(ch.desc)}</div>` : '';
    const ava = ch.avatar
      ? `<img class="m-finder-ava" loading="lazy" src="${esc(ch.avatar)}" alt="" data-finder="1">` : '';
    const author = ch.nickname
      ? `<div class="m-finder-author">${ava}视频号 · ${esc(ch.nickname)}${ch.mediaCount > 1 ? ` · ${ch.mediaCount} 个视频` : ''}</div>` : '';
    inner = `<div class="m-finder">${cover}<div class="m-finder-body">${desc}${author}</div></div>`;
  } else if (m.record) {
    // 合并转发（appmsg type=19 recordinfo）：与导出 HTML 同口径逐条展开。
    // 此前只显示单行 [聊天记录] 预览（后端还产了 record 字段没人用）。
    inner = recordHtml(m.record);
  } else if (m.kind === 'link' && m.link) {
    // 与 common.js/renderer.js 同一白名单：http(s)/相对/锚点原样；裸域名补 https://；
    // 其余（javascript:、data: 等）丢弃，避免拼接出的伪协议被当可点击链接执行。
    const raw = (m.link.url || '').trim();
    const full = /^(https?:|\/|\.\/|\.\.\/|#)/i.test(raw)
      ? raw
      : (/^[\w.-]+\.[a-z]{2,}(?::\d+)?(?:[/?#]|$)/i.test(raw) ? 'https://' + raw : '');
    let host = '';
    try { host = new URL(full).hostname; } catch (e) {}
    inner = full
      ? `<div class="m-link"><a href="${esc(full)}" target="_blank" rel="noreferrer">${esc(m.link.title)}</a>` +
        (host ? `<div class="m-link-host">${esc(host)}</div>` : '') + `</div>`
      : `<div class="m-link">${esc(m.link.title)}</div>`;
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
    // 失败重试由捕获级 error 委托接管，重试 URL 放 data-retry（不带 r 参数，
    // imgFallback 会自行拼缓存穿透参数）；点击查看大图由 click 委托处理
    inner = `<img class="m-img" loading="lazy" src="${src}" alt="图片" data-retry="${esc(src)}">`;
  } else if (m.kind === 'sticker') {
    if (m.sticker && m.sticker.url) {
      // 动画表情：本地 Emoticon 缓存是微信私有封装解不开，走 CDN 直链
      // （/media/sticker 内置腾讯域名白名单；加载失败回退 [动画表情] 文案）
      const ssrc = `/api/chat/media/sticker?account=${encodeURIComponent(account)}` +
                   `&md5=${encodeURIComponent(m.md5 || '')}` +
                   `&url=${encodeURIComponent(m.sticker.url)}`;
      inner = `<img class="m-img m-sticker" loading="lazy" src="${ssrc}"` +
              ` alt="动画表情" data-sticker="1">`;
    } else {
      inner = '[动画表情]';
    }
  } else {
    inner = faceText(esc(m.text));
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
  const chat = currentChat.username;      // 快速切换会话时丢弃过期响应
  const list = el('c-timeline-list');
  list.innerHTML = '<div class="tl-empty">正在生成月份索引…</div>';
  timelineDayCache.clear();
  try {
    const data = await fetchJSON(`/api/chat/timeline?account=${encodeURIComponent(account)}&chat=${encodeURIComponent(chat)}`);
    if (gone() || !currentChat || currentChat.username !== chat) return;
    timelineMonths = data.months || [];
    renderTimelineMonths();
  } catch (e) {
    if (gone() || !currentChat || currentChat.username !== chat) return;
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
    // 悬停只展开已缓存月份；未缓存月份需点击展开——否则扫一遍时间轴
    // 会对每个月各打出一次聚合查询（实测 3 秒 30+ 条请求）
    section.addEventListener('mouseenter', () => {
      if (timelineDayCache.has(section.dataset.month)) expandTimelineMonth(section);
    }, { once: false });
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
    if (gone() || !section.isConnected) return;
    const days = data.days || [];
    timelineDayCache.set(month, days);
    renderTimelineDays(inner, days);
  } catch (e) {
    if (gone() || !section.isConnected) return;
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
  } catch (e) {
    if (!gone()) {
      el('c-msgs').innerHTML = `<div class="c-empty">跳转失败：${esc(e.message)}<br>` +
        '<button class="btn" id="c-jump-retry" type="button">重试</button></div>';
      el('c-jump-retry')?.addEventListener('click', () => jumpToTime(day));
      toast('跳转失败：' + (e.message || e), true);
    }
    return;
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
  await copyWithFlash(el('c-copy-selected'), picked.map(messageCopyText).join('\n'), '已复制所选');
}

/** SX.copyText + 按钮闪字反馈；失败走全局 toast（不再 window.alert） */
async function copyWithFlash(btn, text, doneText) {
  try {
    await copyText(text);
    flashButton(btn, doneText || '已复制');
  } catch (e) {
    toast('复制失败：' + (e.message || e), true);
  }
}

function flashButton(btn, text) {
  if (!btn) return;
  // 1 秒内重复闪字（如多选连点多条）：复位到基础文案再闪，避免「已复制」被当成基础文案永久残留
  if (btn._flashTimer) {
    clearTimeout(btn._flashTimer);
    btn.textContent = btn._flashBase ?? btn.textContent;
  }
  btn._flashBase = btn._flashBase ?? btn.textContent;
  btn.textContent = text;
  btn.classList.add('copied');
  btn._flashTimer = setTimeout(() => {
    btn._flashTimer = null;
    btn.textContent = btn._flashBase;
    delete btn._flashBase;
    btn.classList.remove('copied');
  }, 1000);
}

function updateSubtitle() {
  if (!currentChat) return;
  const type = currentChat.is_group ? '群聊' : '私聊';
  let more = '';
  if (hasMore && hasNewer) more = ' · 两侧都有未载入消息';
  else if (hasMore) more = ' · 向上滚动加载更早';
  else if (hasNewer) more = ' · 点击底部按钮加载之后的消息';
  el('c-sub').textContent = `${type} · 已载入 ${loadedMessages.length} 条${more}`;
}

/* ── 会话统计弹层：Esc 关闭 ───────────────────────────────────── */
let statsModalPrevFocus = null;
let statsModalKeyHandler = null;

async function openStatsModal() {
  if (!currentChat) return;
  const modal = el('c-stats-modal');
  const body = el('c-stats-body');
  statsModalPrevFocus = document.activeElement;
  modal.classList.remove('hidden');
  el('c-stats-close').focus();
  if (statsModalKeyHandler) document.removeEventListener('keydown', statsModalKeyHandler);
  statsModalKeyHandler = (ev) => {
    if (ev.key === 'Escape') { ev.preventDefault(); closeStatsModal(); }
  };
  document.addEventListener('keydown', statsModalKeyHandler);
  body.innerHTML = '<div class="c-loading">正在统计这个会话…</div>';
  try {
    const d = await fetchJSON(`/api/chat/stats?account=${encodeURIComponent(account)}&chat=${encodeURIComponent(currentChat.username)}`);
    if (gone() || modal.classList.contains('hidden')) return;
    body.innerHTML = renderStats(d);
  } catch (e) {
    if (!gone()) body.innerHTML = `<div class="c-empty">统计失败：${esc(e.message)}</div>`;
  }
}

function closeStatsModal() {
  el('c-stats-modal').classList.add('hidden');
  if (statsModalKeyHandler) {
    document.removeEventListener('keydown', statsModalKeyHandler);
    statsModalKeyHandler = null;
  }
  if (statsModalPrevFocus && statsModalPrevFocus.isConnected) statsModalPrevFocus.focus();
  statsModalPrevFocus = null;
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
  if (statsModalKeyHandler) {
    document.removeEventListener('keydown', statsModalKeyHandler);
    statsModalKeyHandler = null;
  }
}
export { init };
