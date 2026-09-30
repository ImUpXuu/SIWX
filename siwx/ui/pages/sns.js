const { esc, fetchJSON, copyText } = window.SX;
const el = id => document.getElementById(id);
const pad = n => String(n).padStart(2, '0');
// esc() 走 textContent/innerHTML，不转义引号；放进属性值时必须再转一次
const attr = s => esc(s).replace(/"/g, '&quot;');
const nowSec = () => Math.floor(Date.now() / 1000);

let account = '', before = null, busy = false, inited = false;
let friends = [];                       // /api/sns/friends 全量
const friendMap = new Map();            // username -> friend
let usersLoaded = false, usersLoading = false;
let pollTimer = null;

/** 当前生效的筛选条件（时间范围 + 发布者 + 关键词 + 排序）。 */
const filter = { keyword: '', username: '', start: null, end: null, rangeKey: 'all' };
/** 发布者面板：搜索词与排序方式。 */
let userQuery = '', userSort = 'count-desc';
/** 日历面板的草稿选择与当前显示月份。 */
let draft = { start: null, end: null };
let cal = { y: 0, m: 0, jump: null };

/* ══ 纯函数区（不碰 DOM，便于脚本化验证）══════════════════════ */

/** 微信式相对时间：刚刚 / N分钟前 / N小时前 / 昨天 HH:mm / M月D日 / YYYY年M月D日 */
export function relTime(ts, now) {
  if (!ts) return '';
  now = now || nowSec();
  const diff = now - ts;
  if (diff < 0) return fullTime(ts);
  if (diff < 60) return '刚刚';
  if (diff < 3600) return `${Math.floor(diff / 60)}分钟前`;
  const d = new Date(ts * 1000), n = new Date(now * 1000);
  if (d.toDateString() === n.toDateString()) return `${Math.floor(diff / 3600)}小时前`;
  const y = new Date(n.getTime());
  y.setDate(y.getDate() - 1);
  if (d.toDateString() === y.toDateString()) return `昨天 ${pad(d.getHours())}:${pad(d.getMinutes())}`;
  if (d.getFullYear() === n.getFullYear()) return `${d.getMonth() + 1}月${d.getDate()}日`;
  return `${d.getFullYear()}年${d.getMonth() + 1}月${d.getDate()}日`;
}

/** 完整时间（详情弹层 / title 提示用） */
export function fullTime(ts) {
  if (!ts) return '';
  const d = new Date(ts * 1000);
  return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())} `
    + `${pad(d.getHours())}:${pad(d.getMinutes())}`;
}

/** 日历网格（周日起始，固定 6 行 42 格）；ts 为**本地零点**的 unix 秒 */
export function monthCells(year, month) {
  const first = new Date(year, month, 1);
  const start = new Date(year, month, 1 - first.getDay());
  const out = [];
  for (let i = 0; i < 42; i++) {
    const d = new Date(start.getFullYear(), start.getMonth(), start.getDate() + i);
    out.push({
      y: d.getFullYear(), m: d.getMonth(), d: d.getDate(),
      ts: Math.floor(d.getTime() / 1000),
      out: d.getMonth() !== month,
    });
  }
  return out;
}

/** 快捷范围 → {start, end, label}；无效 key 返回 null */
export function rangeFromKey(key, now) {
  now = now || nowSec();
  const d = new Date(now * 1000);
  const midnight = Math.floor(new Date(d.getFullYear(), d.getMonth(), d.getDate()).getTime() / 1000);
  const day = 86400;
  if (key === 'all') return { start: null, end: null, label: '全部时间' };
  if (key === 'today') return { start: midnight, end: now, label: '今天' };
  if (key === 'thisyear') {
    return { start: Math.floor(new Date(d.getFullYear(), 0, 1).getTime() / 1000), end: now, label: '今年' };
  }
  const n = Number(key);
  if (!Number.isFinite(n) || n <= 0) return null;
  return { start: midnight - (n - 1) * day, end: now, label: `最近 ${n} 天` };
}

/** 拼音排序器（浏览器 ICU 自带拼音 collation；失败回退默认比较） */
let _collator = null;
export function pinyinCompare(a, b) {
  if (!_collator) {
    try {
      _collator = new Intl.Collator('zh-Hans-CN', { collation: 'pinyin', numeric: true, sensitivity: 'base' });
    } catch (e) {
      _collator = new Intl.Collator('zh-Hans-CN');
    }
  }
  return _collator.compare(a, b);
}

/** 发布者排序：发送量 / 名称拼音 / 最近发布 / 最早发布。同级用拼音兜底，保证稳定 */
export function sortFriends(list, mode) {
  const name = f => f.display || f.username || '';
  const byName = (a, b) => pinyinCompare(name(a), name(b))
    || String(a.username).localeCompare(String(b.username));
  const cmp = {
    'count-desc': (a, b) => (b.count - a.count) || byName(a, b),
    'count-asc': (a, b) => (a.count - b.count) || byName(a, b),
    'name-asc': byName,
    'name-desc': (a, b) => -byName(a, b),
    'recent-desc': (a, b) => ((b.last_ts || 0) - (a.last_ts || 0)) || byName(a, b),
    'oldest-asc': (a, b) => ((a.first_ts || 0) - (b.first_ts || 0)) || byName(a, b),
  }[mode] || ((a, b) => (b.count - a.count) || byName(a, b));
  return list.slice().sort(cmp);
}

/** 发布者搜索：昵称 / 备注 / 微信号（大小写不敏感） */
export function matchFriend(f, q) {
  const s = String(q || '').trim().toLowerCase();
  if (!s) return true;
  return `${f.display || ''} ${f.username || ''}`.toLowerCase().includes(s);
}

/** 九宫格列数：1 张大图，2 张 2 列，3 张 3 列，4 张 2×2，其余 3 列 */
export function mediaCols(n) {
  if (n <= 1) return 'sns-media--1';
  if (n === 2) return 'sns-media--2';
  if (n === 3) return 'sns-media--3';
  if (n === 4) return 'sns-media--4';
  return 'sns-media--n';
}

/** 微信/腾讯 CDN 域名后缀：朋友圈 XML 里的媒体一定在这些域上。
 *  tc.qq.com 是视频号封面（wxapp.tc.qq.com），wechat.com 是 snsvideo.c2c.wechat.com —— 少一个就一片破图。 */
const CDN_HOSTS = ['qpic.cn', 'qlogo.cn', 'video.qq.com', 'tc.qq.com',
  'weixin.qq.com', 'weixin.com', 'wechat.com', 'gtimg.com'];

/** 是否微信 CDN 媒体地址（外链卡片不是媒体，请求只会 400） */
export function isCdnMedia(url) {
  try {
    const h = new URL(String(url), location.origin).hostname.toLowerCase();
    return CDN_HOSTS.some(s => h === s || h.endsWith('.' + s));
  } catch (e) {
    return false;
  }
}

/** 导出/失败提示里的原因文案（与后端 sns_cdn.fetch_media 的 reason 对齐） */
const REASON_TEXT = {
  'http-404': 'CDN 已无此图',
  'http-403': 'CDN 拒绝访问（token 可能过期）',
  'http-400': 'CDN 拒绝请求（token 可能过期）',
  'http-500': 'CDN 服务端错误',
  'undecodable': '解密后格式异常',
  'not-cdn': '外链不是媒体',
  'network': '网络失败',
  'write-error': '写文件失败',
  'empty-url': '缺少 URL',
  'unknown': '未知原因',
};

export function reasonText(reason) {
  return REASON_TEXT[reason] || reason || '未知原因';
}

const MEDIA_FAIL_HINT = '微信 CDN 上已经没有这个资源（原图可能被清理或已过期）。'
  + '详细原因见「运行日志」里的 [sns-media] 记录。';

/* ══ 工具 ══════════════════════════════════════════════════ */

function nameOf(username) {
  const f = friendMap.get(username);
  return (f && f.display) || username || '';
}

/** 昵称优先，其次回退（评论里 XML 自带 nickname） */
function displayOf(username, fallback) {
  const f = friendMap.get(username);
  return (f && f.display) || fallback || username || '';
}

function avaUrl(username) {
  return `/api/chat/avatar?account=${encodeURIComponent(account)}&username=${encodeURIComponent(username)}`;
}

/** 头像内部结构（首字母色块 + 有头像时才请求图片） */
function avaInner(username, display) {
  const f = friendMap.get(username);
  const initial = esc(String(display || username || '?').trim().slice(0, 1).toUpperCase() || '?');
  const img = (f && f.has_avatar)
    ? `<img loading="lazy" alt="" src="${attr(avaUrl(username))}" onerror="this.remove()">` : '';
  return `<span>${initial}</span>${img}`;
}

function avaHtml(username, display, cls = 'ava') {
  return `<span class="${cls}">${avaInner(username, display)}</span>`;
}

function mediaUrl(m) {
  const q = new URLSearchParams({ account, url: m.url || '', key: m.key || '', token: m.token || '' });
  return `/api/sns/media?${q}`;
}

function setNotice(text) { const n = el('sns-notice'); n.textContent = text || ''; n.hidden = !text; }

function toast(text) {
  setNotice(text);
  clearTimeout(toast._t);
  toast._t = setTimeout(() => setNotice(''), 2600);
}

/* ══ 浮层管理 ══════════════════════════════════════════════ */

const POPS = ['sns-range-panel', 'sns-user-panel', 'sns-export-panel'];

function closePops(except) {
  POPS.forEach(id => { if (id !== except) el(id).hidden = true; });
}

function togglePop(id) {
  const p = el(id);
  const show = p.hidden;
  closePops(show ? id : null);
  p.hidden = !show;
  if (show && id === 'sns-user-panel') ensureFriends();
  if (show && id === 'sns-export-panel') updateExportScope();
}

/* ══ 时间范围面板 ══════════════════════════════════════════ */

function openRangePanel() {
  const p = el('sns-range-panel');
  p.hidden = false;
  // 面板贴着筛选栏左缘（容器是 position:relative）
  const anchor = el('sns-range-btn');
  p.style.left = `${Math.max(0, anchor ? anchor.offsetLeft : 0)}px`;
  // 用当前生效的筛选回填草稿（打开面板**不改变**筛选）
  draft = { start: filter.start, end: filter.end };
  const d = new Date((filter.start || nowSec()) * 1000);
  cal = { y: d.getFullYear(), m: d.getMonth(), jump: null };
  markQuick(filter.rangeKey);
  renderCal();
}

function markQuick(key) {
  [...el('sns-quick').children].forEach(b => b.classList.toggle('on', b.dataset.range === key));
}

/** 选中快捷范围（applyNow=true 时立即生效，否则只填进日历草稿） */
function selectRange(key, applyNow) {
  if (key === 'custom') { markQuick('custom'); return; }
  const r = rangeFromKey(key);
  if (!r) return;
  markQuick(key);
  draft = { start: r.start, end: r.end };
  const d = new Date((r.start || nowSec()) * 1000);
  cal = { y: d.getFullYear(), m: d.getMonth(), jump: null };
  if (applyNow) applyRange(r.start, r.end, key, r.label);
  else renderCal();
}

function applyRange(start, end, key, label) {
  filter.start = start;
  filter.end = end;
  filter.rangeKey = key || 'custom';
  filter.rangeLabel = label || describeRange(start, end);
  el('sns-range-label').textContent = filter.rangeLabel;
  el('sns-range-btn').classList.toggle('on', !!(start || end));
  closePops();
  load(true);
  updateExportScope();
}

function describeRange(start, end) {
  const f = t => {
    const d = new Date(t * 1000);
    return `${d.getFullYear()}/${d.getMonth() + 1}/${d.getDate()}`;
  };
  if (start && end) return start === end ? f(start) : `${f(start)} - ${f(end)}`;
  if (start) return `${f(start)} 起`;
  if (end) return `至 ${f(end)}`;
  return '全部时间';
}

function renderCal() {
  if (cal.jump) return renderCalJump();
  el('cal-title').textContent = `${cal.y}年${cal.m + 1}月 ▾`;
  el('cal-grid').classList.remove('jump');
  const cells = monthCells(cal.y, cal.m);
  const todayTs = Math.floor(new Date(new Date().getFullYear(), new Date().getMonth(),
    new Date().getDate()).getTime() / 1000);
  const lo = Math.min(draft.start || Infinity, draft.end || Infinity);
  const hi = Math.max(draft.start || -Infinity, draft.end || -Infinity);
  el('cal-grid').innerHTML = cells.map((c, i) => {
    const cls = ['sns-cal-day'];
    if (c.out) cls.push('out');
    if (c.ts === todayTs) cls.push('today');
    if (c.ts === draft.start || c.ts === draft.end) cls.push('edge');
    else if (c.ts > lo && c.ts < hi) cls.push('in-range');
    return `<button class="${cls.join(' ')}" data-i="${i}" data-ts="${c.ts}">${c.d}</button>`;
  }).join('');
  el('cal-hint').textContent = draft.start
    ? (draft.end ? describeRange(draft.start, draft.end) : `${describeRange(draft.start, null)} → 选择结束日期`)
    : '点击日期选择开始';
}

/** 年 / 月快速跳转（点标题进入） */
function renderCalJump() {
  const nowY = new Date().getFullYear();
  const grid = el('cal-grid');
  grid.classList.add('jump');
  if (cal.jump === 'year') {
    el('cal-title').textContent = '选择年份';
    const years = [];
    for (let y = nowY; y >= nowY - 11; y--) years.push(y);
    grid.innerHTML = years.map(y =>
      `<button class="sns-cal-jump-btn ${y === cal.y ? 'edge' : ''}" data-year="${y}">${y}年</button>`).join('');
  } else {
    el('cal-title').textContent = '选择月份';
    grid.innerHTML = Array.from({ length: 12 }, (_, i) =>
      `<button class="sns-cal-jump-btn ${i === cal.m ? 'edge' : ''}" data-month="${i}">${i + 1}月</button>`).join('');
  }
  el('cal-hint').textContent = '选择月份后再点日期微调';
}

function onCalClick(ev) {
  if (ev.target.closest('#cal-title')) {          // 标题：年 → 月 → 日 循环
    cal.jump = cal.jump === 'year' ? 'month' : (cal.jump === 'month' ? null : 'year');
    renderCal();
    return;
  }
  const btn = ev.target.closest('button');
  if (!btn) return;
  if (btn.dataset.year) { cal.y = Number(btn.dataset.year); cal.jump = 'month'; renderCal(); return; }
  if (btn.dataset.month) { cal.m = Number(btn.dataset.month); cal.jump = null; renderCal(); return; }
  if (btn.dataset.ts) {
    const ts = Number(btn.dataset.ts);
    if (!draft.start || draft.end) draft = { start: ts, end: null };
    else if (ts >= draft.start) draft.end = ts;
    else draft = { start: ts, end: null };
    markQuick(draft.end ? 'custom' : '');
    renderCal();
  }
}

/* ══ 发布者面板 ════════════════════════════════════════════ */

async function ensureFriends(force) {
  if (usersLoaded && !force) return;
  if (usersLoading) return;
  usersLoading = true;
  el('sns-user-hint').textContent = '加载中…';
  try {
    const d = await fetchJSON(`/api/sns/friends?account=${encodeURIComponent(account)}&limit=1000`);
    friends = d.friends || [];
    friendMap.clear();
    friends.forEach(f => friendMap.set(f.username, f));
    usersLoaded = true;
  } catch (e) {
    el('sns-user-hint').textContent = '发布者加载失败：' + e.message;
  } finally {
    usersLoading = false;
  }
  renderFriends();
}

function renderFriends() {
  const list = sortFriends(friends.filter(f => matchFriend(f, userQuery)), userSort);
  const total = friends.length;
  el('sns-user-hint').textContent = total
    ? `共 ${total} 位发布者${userQuery ? ` · 匹配 ${list.length}` : ''}`
    : '';
  if (!total) { el('sns-user-list').innerHTML = '<div class="sns-user-empty">暂无发布者</div>'; return; }
  const rows = [`<button class="sns-user-row ${filter.username ? '' : 'on'}" data-user="">
      <span class="ava"><span>全</span></span>
      <span class="who"><span class="nm">全部发布者</span><span class="un">不筛选发送者</span></span>
      <span class="cnt">${total}</span></button>`];
  rows.push(...list.map(f => `<button class="sns-user-row ${f.username === filter.username ? 'on' : ''}"
      data-user="${attr(f.username)}">
      ${avaHtml(f.username, f.display, 'ava')}
      <span class="who"><span class="nm">${esc(f.display || f.username)}</span>
        <span class="un">${esc(f.username)}</span></span>
      <span class="cnt">${f.count}</span></button>`));
  el('sns-user-list').innerHTML = rows.join('');
}

function applyUser(username) {
  filter.username = username || '';
  const f = username ? friendMap.get(username) : null;
  el('sns-user-label').textContent = f ? (f.display || f.username) : '全部发布者';
  el('sns-user-btn').classList.toggle('on', !!username);
  const chip = el('sns-user-chip-ava');
  chip.innerHTML = username ? avaInner(username, (f && f.display) || username) : '';
  chip.hidden = !username;
  closePops();
  renderFriends();
  load(true);
  updateExportScope();
}

/* ══ 列表渲染（微信式）══════════════════════════════════════ */

function renderMedia(medias) {
  // 只渲染微信 CDN 上的媒体：外链卡片（b23.tv 等）不是图片/视频，
  // 放进来只会拿到 400 并渲染成一个破图。
  const items = (medias || []).filter(m => m.url && isCdnMedia(m.url));
  if (!items.length) return '';
  const cells = items.map(m => {
    const src = esc(mediaUrl(m));
    if (m.type === 6) {
      return `<span class="sns-media-cell"><video class="sns-vid" controls preload="metadata" src="${src}"></video></span>`;
    }
    const lp = m.live_photo;
    const liveAttr = lp && lp.url ? ` data-live="${attr(mediaUrl(lp))}" title="实况照片：点击播放"` : '';
    const badge = liveAttr ? '<span class="live-badge">实况</span>' : '';
    return `<span class="sns-media-cell"><img class="sns-lb" loading="lazy" src="${src}"`
      + ` alt="朋友圈图片"${liveAttr}>${badge}</span>`;
  }).join('');
  return `<div class="sns-media ${mediaCols(items.length)}">${cells}</div>`;
}

/** 媒体加载失败：**必须可见**。
 *
 * 以前是 `onerror` 直接 visibility:hidden —— 用户只看到一块空白，
 * 既不知道是哪张图的锅，也拿不到任何线索（这正是「拉不下来 + 没日志」的观感来源）。
 * 现在换成占位条，并在 title 里写清去哪看原因。
 */
function onMediaError(ev) {
  const el = ev.target;
  if (!el || el.nodeType !== 1) return;
  const isVideo = el.tagName === 'VIDEO';
  if (!isVideo && !(el.classList && el.classList.contains('sns-lb'))) return;
  const cell = el.closest && el.closest('.sns-media-cell');
  el.remove();
  if (cell && !cell.querySelector('.sns-media-bad')) {
    cell.insertAdjacentHTML('beforeend',
      `<span class="sns-media-bad" title="${attr(MEDIA_FAIL_HINT)}">`
      + `${isVideo ? '视频' : '图片'}加载失败</span>`);
  }
}

function renderComments(comments, limit) {
  let list = comments || [];
  const total = list.length;
  let hidden = 0;
  if (limit && total > limit) { list = list.slice(-limit); hidden = total - limit; }
  const html = list.map(c => {
    const emo = (c.emojis || []).map(e =>
      `<img class="cm-emoji" loading="lazy" src="${esc(emojiUrl(e))}" alt="表情" onerror="this.remove()">`
    ).join('');
    const imgs = (c.images || []).filter(i => i.url).map(i =>
      `<img class="cm-img sns-lb" loading="lazy" src="${esc(mediaUrl(i))}" alt="评论图片" onerror="this.remove()">`
    ).join('');
    const body = c.content || '';
    const reply = c.ref_username
      ? `<span class="dim">回复 ${esc(displayOf(c.ref_username, ''))}</span> ` : '';
    const txt = body ? esc(body) : (emo || imgs ? '' : '<span class="dim">赞了这条动态</span>');
    return `<div class="sns-comment"><b>${esc(displayOf(c.username, c.nickname))}</b>：${reply}${txt}${emo}${imgs}</div>`;
  }).join('');
  const more = hidden ? `<div class="sns-more-comments">还有 ${hidden} 条评论，点「详情」查看</div>` : '';
  return html + more;
}

function emojiUrl(e) {
  const q = new URLSearchParams({ account, emoji: JSON.stringify({ url: e.url || '', encrypt_url: e.encrypt_url || '', aes_key: e.aes_key || '' }) });
  return `/api/sns/emoji?${q}`;
}

function renderInter(p, compact) {
  const likes = p.likes || [], comments = p.comments || [];
  if (!likes.length && !comments.length) return '';
  let html = '';
  if (likes.length) {
    const names = likes.map(x => esc(displayOf(x.username, x.nickname)))
      .join('<span class="sep">，</span>');
    html += `<div class="sns-likes"><span class="ic">❤</span>${names}</div>`;
  }
  if (comments.length) {
    html += `<div class="sns-comments ${likes.length ? 'has-like' : ''}">`
      + renderComments(comments, compact ? 20 : 0) + '</div>';
  }
  return `<div class="sns-inter">${html}</div>`;
}

function renderCard(card) {
  if (!card || !card.kind) return '';
  const linkOpen = (url, inner, cls) => {
    const u = String(url || '').trim();
    return u ? `<a class="${cls}" href="${attr(u)}" target="_blank" rel="noreferrer">${inner}</a>`
      : `<div class="${cls}">${inner}</div>`;
  };
  if (card.kind === 'music') {
    const m = card.music || {};
    const d = fmtDur(m.duration_ms ? m.duration_ms / 1000 : 0);
    return linkOpen(card.url,
      `<div class="sns-card-body"><div class="sns-card-tag">🎵 音乐</div>`
      + `<div class="sns-card-title">${esc(m.album || card.title || '音乐')}</div>`
      + `<div class="sns-card-sub">${esc(m.singer || card.description || '')}${d ? ' · ' + d : ''}</div></div>`,
      'sns-card sns-card--music');
  }
  if (card.kind === 'finder') {
    const f = card.finder || {};
    const d = fmtDur(card.duration);
    const cover = (card.cover || '').trim();
    const vid = ((f.video_url || '').trim() || (((f.media || [])[0] || {}).url || '')).trim();
    const img = cover
      ? `<img class="sns-card-cover sns-lb" loading="lazy" src="${esc(proxyUrl(cover))}"`
        + ` alt="视频号封面"${vid ? ` data-live="${attr(proxyUrl(vid))}" title="点击播放"` : ''}`
        + ` onerror="this.remove()">` : '';
    return `<div class="sns-card sns-card--finder">${img}<div class="sns-card-body">`
      + `<div class="sns-card-tag">📹 视频号</div>`
      + `<div class="sns-card-title">${esc(f.nickname || '')}</div>`
      + `<div class="sns-card-sub">${[esc(f.media_count ? f.media_count + ' 个作品' : ''), d].filter(Boolean).join(' · ')}</div>`
      + `<div class="sns-card-desc">${esc(card.description || '')}</div></div></div>`;
  }
  if (card.kind === 'live') {
    const lv = card.live || {};
    const cover = (card.cover || '').trim();
    const img = cover
      ? `<img class="sns-card-cover" loading="lazy" src="${esc(proxyUrl(cover))}" alt="直播封面" onerror="this.remove()">` : '';
    return `<div class="sns-card sns-card--live">${img}<div class="sns-card-body">`
      + `<div class="sns-card-tag">📺 视频号直播</div>`
      + `<div class="sns-card-title">${esc(lv.nickname || '')}</div>`
      + `<div class="sns-card-desc">${esc(lv.desc || '')}</div></div></div>`;
  }
  if (card.kind === 'note') {
    const nt = card.note || {};
    return `<div class="sns-card sns-card--note"><div class="sns-card-body">`
      + `<div class="sns-card-tag">📝 笔记</div>`
      + `<div class="sns-card-desc">${esc(nt.text || card.title || '')}</div></div></div>`;
  }
  return linkOpen(card.url,
    `<div class="sns-card-body"><div class="sns-card-tag">🔗 链接</div>`
    + `<div class="sns-card-title">${esc(card.title || card.url || '')}</div>`
    + `<div class="sns-card-sub">${esc(card.source || '')}</div>`
    + `<div class="sns-card-desc">${esc(card.description || '')}</div></div>`,
    'sns-card sns-card--link');
}

function proxyUrl(url) {
  return `/api/sns/media?${new URLSearchParams({ account, url: url || '' })}`;
}

function fmtDur(sec) {
  const n = Math.floor(Number(sec) || 0);
  if (n <= 0) return '';
  return `${Math.floor(n / 60)}:${pad(n % 60)}`;
}

function postText(p) {
  const text = esc(p.content_desc || '');
  const card = renderCard(p.card);
  const media = renderMedia(p.medias);
  const textHtml = text
    ? `<div class="sns-text">${text}</div>`
    : (card || (p.medias || []).length ? '' : '');
  return { textHtml, card, media };
}

function renderPost(p, compact = true) {
  const uname = p.user_name || p.username || '';
  const display = nameOf(uname);
  const { textHtml, card, media } = postText(p);
  const loc = p.location
    ? `<div class="sns-loc">📍 ${esc(p.location.name || '')}${p.location.address ? ' · ' + esc(p.location.address) : ''}</div>` : '';
  const inter = renderInter(p, compact);
  const likes = (p.likes || []).length, comments = (p.comments || []).length;
  return `<article class="sns-post" data-tid="${attr(String(p.tid))}" data-user="${attr(uname)}">
    ${avaHtml(uname, display)}
    <div class="sns-main">
      <div class="sns-name">${esc(display)}</div>
      ${textHtml}${card}${media}${loc}
      <div class="sns-foot">
        <span class="sns-time" title="${attr(fullTime(p.ts))}">${esc(relTime(p.ts))}</span>
        ${compact ? '<span class="sns-detail-link" data-act="detail">详情</span>'
                  : `<span class="sns-time">· 赞 ${likes} · 评论 ${comments}</span>`}
        <span class="sns-ops">
          <button class="sns-ops-btn" data-act="ops" title="更多操作">···</button>
          <div class="sns-ops-menu" hidden>
            <button data-act="detail">查看详情</button>
            <button data-act="copy-text">复制文字</button>
            <button data-act="copy-id">复制动态 ID</button>
          </div>
        </span>
      </div>
      ${inter}
    </div>
  </article>`;
}

/* ══ 详情 / 灯箱 ═══════════════════════════════════════════ */

function openLightbox(src, isVideo) {
  const box = el('sns-lightbox'), img = el('sns-lightbox-img'), vid = el('sns-lightbox-video');
  if (isVideo) {
    img.hidden = true; img.src = '';
    vid.hidden = false; vid.src = src;
    vid.play().catch(() => {});
  } else {
    vid.hidden = true; vid.pause(); vid.src = '';
    img.hidden = false; img.src = src;
  }
  box.hidden = false;
}

function closeLightbox() {
  el('sns-lightbox').hidden = true;
  el('sns-lightbox-img').src = '';
  const vid = el('sns-lightbox-video');
  vid.pause(); vid.src = ''; vid.hidden = true;
}

async function openDetail(tid) {
  const box = el('sns-modal');
  el('sns-modal-body').innerHTML = '<div class="sns-loading">加载中…</div>';
  box.hidden = false;
  closePops();
  try {
    const d = await fetchJSON(`/api/sns/detail?account=${encodeURIComponent(account)}&tid=${encodeURIComponent(tid)}`);
    el('sns-modal-body').innerHTML = renderPost(d.post, false);
  } catch (e) {
    el('sns-modal-body').innerHTML = `<div class="sns-empty">加载失败：${esc(e.message)}</div>`;
  }
}

function closeDetail() { el('sns-modal').hidden = true; el('sns-modal-body').innerHTML = ''; }

/* ══ 账号 / 列表加载 ═══════════════════════════════════════ */

async function loadAccounts() {
  const d = await fetchJSON('/api/sns/accounts');
  const sel = el('sns-account');
  sel.innerHTML = (d.accounts || []).map(a =>
    `<option value="${attr(a.wxid)}">${esc(a.wxid)} · ${a.count || 0} 条</option>`).join('');
  if (!d.accounts || !d.accounts.length) throw new Error('没有找到已解密的朋友圈数据库');
  account = sel.value;
  const a = d.accounts.find(x => x.wxid === account) || {};
  setNotice(`本地朋友圈图片只包含微信已经下载过的资源；未下载的会尝试从 CDN 获取。`
    + `数据库共 ${a.count || 0} 条动态。`);
  renderMe();
  load(true);
  ensureFriends(true).then(() => { renderMe(); rerenderList(); });
}

/** 顶部封面上的“我”：昵称与头像（会被调用多次，必须幂等） */
function renderMe() {
  const clean = account.replace(/_[0-9a-f]{4}$/i, '');
  const me = friends.find(f => f.username === account || f.username === clean);
  const name = (me && me.display) || clean;
  const nameEl = el('sns-me-name');
  if (nameEl) nameEl.textContent = name;
  const initialEl = el('sns-me-initial');
  if (initialEl) initialEl.textContent = String(name).trim().slice(0, 1) || '我';

  // 头像用绝对定位叠在首字母上，**不替换 innerHTML**：
  // 首次加载后 #sns-me-initial 仍要在（renderMe 会被调用两次：
  // 账号加载后 + 好友信息到达后，第二次再找它就找不到了）。
  const box = el('sns-me-ava');
  if (!box || box.querySelector('img')) return;
  const img = new Image();
  img.alt = '';
  img.onerror = () => img.remove();
  img.onload = () => { if (box && !box.querySelector('img')) box.appendChild(img); };
  img.src = avaUrl(account);
}

/** 好友信息到达后，把已经渲染出来的列表补上昵称/头像（不重新请求） */
function rerenderList() {
  if (!el('sns-list').querySelector('.sns-post')) return;
  el('sns-list').querySelectorAll('.sns-post').forEach(post => {
    const uname = post.dataset.user;
    const nameEl = post.querySelector('.sns-name');
    if (nameEl) nameEl.textContent = nameOf(uname);
    const ava = post.querySelector('.ava');
    if (ava && !ava.querySelector('img')) {
      const f = friendMap.get(uname);
      if (f && f.has_avatar) {
        const img = document.createElement('img');
        img.loading = 'lazy';
        img.onerror = () => img.remove();
        img.src = avaUrl(uname);
        ava.appendChild(img);
      }
    }
  });
  renderFriends();
}

async function load(reset = false) {
  if (busy || !account) return;
  busy = true;
  if (reset) { before = null; el('sns-list').innerHTML = '<div class="sns-loading">加载中…</div>'; }
  const qs = new URLSearchParams({ account, limit: '20' });
  if (before) qs.set('before_tid', before);
  if (filter.keyword) qs.set('keyword', filter.keyword);
  if (filter.username) qs.set('username', filter.username);
  if (filter.start) qs.set('start', String(filter.start));
  if (filter.end) qs.set('end', String(filter.end));
  try {
    const d = await fetchJSON(`/api/sns/timeline?${qs}`);
    const html = (d.timeline || []).map(p => renderPost(p)).join('');
    if (reset) el('sns-list').innerHTML = html || '<div class="sns-empty">没有符合条件的动态</div>';
    else el('sns-list').insertAdjacentHTML('beforeend', html);
    before = d.next_before_tid || null;
    el('sns-more').hidden = !d.has_more;
    const n = (d.timeline || []).length;
    el('sns-meta').textContent = `${n} 条${filter.rangeLabel && filter.rangeKey !== 'all' ? ' · ' + filter.rangeLabel : ''}`
      + `${filter.username ? ' · ' + (nameOf(filter.username) || filter.username) : ''}`;
  } catch (e) {
    if (reset) el('sns-list').innerHTML = `<div class="sns-empty">${esc(e.message)}</div>`;
    else toast(e.message);
  } finally { busy = false; }
}

/* ══ 导出 ═══════════════════════════════════════════════════ */

function updateExportScope() {
  const parts = [];
  parts.push(`时间：${filter.rangeKey === 'all' ? '全部' : (filter.rangeLabel || describeRange(filter.start, filter.end))}`);
  parts.push(`发布者：${filter.username ? (nameOf(filter.username) || filter.username) : '全部'}`);
  if (filter.keyword) parts.push(`关键词：${filter.keyword}`);
  el('sns-export-scope').textContent = '导出范围 —— ' + parts.join(' · ');
}

async function doExport() {
  const msg = el('sns-export-msg'), btn = el('sns-export');
  const withMedia = el('sns-exp-media').checked;
  if (withMedia && !window.confirm('下载媒体会逐张访问 CDN，可能耗时较久，确定继续？')) return;
  btn.disabled = true;
  msg.textContent = withMedia ? '导出中（含媒体）…' : '导出中…';
  try {
    const body = {
      account, format: el('sns-fmt').value, media: withMedia,
      concurrency: Number(el('sns-exp-conc').value) || 5,
      keyword: filter.keyword || undefined,
      username: filter.username || undefined,
      start: filter.start || undefined,
      end: filter.end || undefined,
    };
    const r = await fetch('/api/sns/export', {
      method: 'POST', headers: { 'content-type': 'application/json' }, body: JSON.stringify(body),
    });
    if (r.status === 409) throw new Error('已有任务在运行，请稍后再试');
    if (!r.ok) {
      const j = await r.json().catch(() => ({}));
      throw new Error(j.error || '启动失败');
    }
    pollExport();
  } catch (e) {
    msg.textContent = '✗ ' + e.message;
    btn.disabled = false;
  }
}

function pollExport() {
  const msg = el('sns-export-msg'), btn = el('sns-export');
  if (pollTimer) clearInterval(pollTimer);
  pollTimer = setInterval(async () => {
    let job;
    try { job = await (await fetch('/api/job')).json(); }
    catch (e) { return; }
    const logs = job.logs || [];
    const last = logs.length ? (logs[logs.length - 1].length >= 4
      ? logs[logs.length - 1][3] : logs[logs.length - 1][1]) : '';
    if (job.running) { msg.textContent = last ? `导出中… ${last}` : '导出中…'; return; }
    clearInterval(pollTimer); pollTimer = null;
    btn.disabled = false;
    const rep = job.report || {};
    if (job.ok && rep.kind === 'sns_export') {
      const m = rep.media || {};
      // 失败原因必须写出来：只显示「失败 N」等于没说
      const why = (m.fail && m.reasons)
        ? '：' + Object.keys(m.reasons).map(k => `${reasonText(k)} ×${m.reasons[k]}`).join('、')
        : '';
      const extra = m.total ? `，媒体 ${m.ok}/${m.total}（失败 ${m.fail}${why}）` : '';
      msg.innerHTML = `✓ 已导出 ${rep.count} 条${extra} · `
        + `<a href="/api/sns/export/download?path=${encodeURIComponent(rep.file)}">下载</a> · `
        + `<a href="#" onclick="SX.openPath('${esc(rep.export_dir)}');return false">打开目录</a>`;
    } else {
      msg.textContent = '✗ ' + (job.error || last || '导出失败');
    }
  }, 800);
}

/* ══ 事件 ═══════════════════════════════════════════════════ */

function onDocClick(ev) {
  // 浮层外点击关闭
  const inPop = ev.target.closest('.sns-pop');
  const onTrigger = ev.target.closest('#sns-range-btn, #sns-user-btn, #sns-export-toggle');
  if (!inPop && !onTrigger) closePops();

  if (ev.target.closest('[data-close]')) {
    const id = ev.target.closest('[data-close]').dataset.close;
    el(id).hidden = true;
    return;
  }
  if (ev.target.closest('#sns-range-btn')) { toggleRangePanel(); return; }
  if (ev.target.closest('#sns-user-btn')) { togglePop('sns-user-panel'); renderFriends(); return; }
  if (ev.target.closest('#sns-export-toggle')) { togglePop('sns-export-panel'); return; }

  const quick = ev.target.closest('#sns-quick button');
  if (quick) { selectRange(quick.dataset.range, true); return; }
  // 注意：日历/确定/清除都在面板内，必须排在「面板整体 return」之前
  if (ev.target.closest('#cal-ok')) {
    if (draft.start) {
      const end = draft.end || draft.start;
      applyRange(draft.start, end, 'custom', describeRange(draft.start, end));
      markQuick('custom');
    }
    return;
  }
  if (ev.target.closest('#cal-clear')) {
    draft = { start: null, end: null };
    markQuick('all');
    applyRange(null, null, 'all', '全部时间');
    return;
  }
  if (ev.target.closest('#sns-range-panel')) {
    if (ev.target.closest('#cal-grid') || ev.target.closest('#cal-title')) onCalClick(ev);
    return;
  }

  const urow = ev.target.closest('.sns-user-row');
  if (urow) { applyUser(urow.dataset.user || ''); return; }
  if (ev.target.closest('#sns-user-clear')) { applyUser(''); return; }

  // 动态上的操作
  const actBtn = ev.target.closest('[data-act]');
  if (actBtn) {
    const act = actBtn.dataset.act;
    const post = actBtn.closest('.sns-post');
    if (act === 'ops') {
      ev.preventDefault();
      const menu = actBtn.parentElement.querySelector('.sns-ops-menu');
      const wasHidden = menu.hidden;
      document.querySelectorAll('.sns-ops-menu').forEach(m => { m.hidden = true; });
      menu.hidden = !wasHidden;
      return;
    }
    document.querySelectorAll('.sns-ops-menu').forEach(m => { m.hidden = true; });
    if (!post) return;
    const tid = post.dataset.tid;
    if (act === 'detail') { openDetail(tid); return; }
    if (act === 'copy-text') {
      const text = post.querySelector('.sns-text');
      copyText(text ? text.textContent : '').then(ok => toast(ok ? '已复制文字' : '复制失败'));
      return;
    }
    if (act === 'copy-id') { copyText(String(tid)).then(ok => toast(ok ? '已复制动态 ID' : '复制失败')); return; }
  }

  const img = ev.target.closest('img.sns-lb');
  if (img && img.src) {
    ev.preventDefault();
    const live = img.dataset.live;
    if (live) openLightbox(live, true);
    else openLightbox(img.src, false);
  }
}

function toggleRangePanel() {
  const p = el('sns-range-panel');
  const show = p.hidden;
  closePops(show ? 'sns-range-panel' : null);
  if (show) openRangePanel();
  else p.hidden = true;
}

function onKey(ev) {
  if (ev.key !== 'Escape') return;
  if (POPS.some(id => !el(id).hidden)) { closePops(); return; }
  if (!el('sns-lightbox').hidden) { closeLightbox(); return; }
  if (!el('sns-modal').hidden) closeDetail();
}

function onKeywordInput() {
  const v = el('sns-keyword').value.trim();
  el('sns-keyword-clear').hidden = !v;
  clearTimeout(onKeywordInput._t);
  onKeywordInput._t = setTimeout(() => {
    filter.keyword = v;
    load(true);
    updateExportScope();
  }, 350);
}

/* ══ 生命周期 ═══════════════════════════════════════════════ */

export async function init() {
  if (inited) return;
  inited = true;
  try { await loadAccounts(); }
  catch (e) { setNotice(e.message); el('sns-list').innerHTML = ''; }

  el('sns-account').addEventListener('change', async () => {
    account = el('sns-account').value;
    before = null;
    usersLoaded = false;
    friends = [];
    friendMap.clear();
    renderMe();
    applyUser('');                 // 内部会 load(true) + 刷新导出范围
    ensureFriends(true).then(() => { renderMe(); rerenderList(); });
  });
  el('sns-refresh').addEventListener('click', () => { usersLoaded = false; ensureFriends(true); load(true); });
  el('sns-more').addEventListener('click', () => load(false));
  el('sns-export').addEventListener('click', doExport);
  el('sns-keyword').addEventListener('input', onKeywordInput);
  el('sns-keyword-clear').addEventListener('click', () => {
    el('sns-keyword').value = '';
    el('sns-keyword-clear').hidden = true;
    filter.keyword = '';
    load(true);
  });
  el('sns-user-search').addEventListener('input', () => {
    userQuery = el('sns-user-search').value;
    renderFriends();
  });
  el('sns-user-sort').addEventListener('change', () => {
    userSort = el('sns-user-sort').value;
    renderFriends();
  });
  el('sns-modal-close').addEventListener('click', closeDetail);
  el('sns-modal-mask').addEventListener('click', closeDetail);
  el('sns-lightbox').addEventListener('click', closeLightbox);

  document.addEventListener('click', onDocClick);
  document.addEventListener('keydown', onKey);
  // 资源加载错误不冒泡，但能在捕获阶段拿到
  document.addEventListener('error', onMediaError, true);
}

export function destroy() {
  inited = false;
  document.removeEventListener('click', onDocClick);
  document.removeEventListener('keydown', onKey);
  document.removeEventListener('error', onMediaError, true);
  if (pollTimer) { clearInterval(pollTimer); pollTimer = null; }
  closeDetail();
  closeLightbox();
  closePops();
  clearTimeout(onKeywordInput._t);
}
