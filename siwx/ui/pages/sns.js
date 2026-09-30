const { esc, fetchJSON, fmtTs } = window.SX;
const el = id => document.getElementById(id);
let account = '', before = null, busy = false;

function mediaUrl(m) {
  const q = new URLSearchParams({ account, url: m.url || '', key: m.key || '', token: m.token || '' });
  return `/api/sns/media?${q}`;
}

/* ── 灯箱（支持图片与视频）────────────────────────────── */
function openLightbox(src, isVideo) {
  const box = el('sns-lightbox');
  const img = el('sns-lightbox-img');
  const vid = el('sns-lightbox-video');
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

/* ── 媒体块 ───────────────────────────────────────────── */
function renderMedia(medias, cls) {
  const items = (medias || []).filter(m => m.url).map(m => {
    const src = esc(mediaUrl(m));
    if (m.type === 6) return `<video controls preload="metadata" src="${src}"></video>`;
    const lp = m.live_photo;
    const liveAttr = lp && lp.url
      ? ` data-live="${esc(mediaUrl(lp))}" title="实况照片：点击播放"` : '';
    const badge = liveAttr ? '<span class="live-badge">实况</span>' : '';
    return `<span class="sns-media-cell"><img class="sns-lb" loading="lazy" src="${src}"`
      + ` alt="朋友圈图片"${liveAttr} onerror="this.style.display='none'">${badge}</span>`;
  }).join('');
  return items ? `<div class="${cls || 'sns-media'}">${items}</div>` : '';
}

/* ── 评论 ─────────────────────────────────────────────── */
function renderComments(comments, limit) {
  let list = comments || [];
  if (limit && list.length > limit) list = list.slice(-limit);
  return list.map(c => {
    const emo = (c.emojis || []).map(e =>
      `<img class="cm-emoji" loading="lazy" src="${esc(emojiUrl(e))}" alt="表情" onerror="this.remove()">`
    ).join('');
    const imgs = (c.images || []).filter(i => i.url).map(i =>
      `<img class="cm-img sns-lb" loading="lazy" src="${esc(mediaUrl(i))}" alt="评论图片" onerror="this.remove()">`
    ).join('');
    const body = esc(c.content || '');
    const reply = c.ref_username ? `<span class="dim">回复 ${esc(c.ref_username)}</span> ` : '';
    const txt = body ? esc(body) : (emo || imgs ? '' : '<span class="dim">赞了这条动态</span>');
    return `<div class="sns-comment"><b>${esc(c.nickname || c.username)}</b>：${reply}${txt}${emo}${imgs}</div>`;
  }).join('');
}

function emojiUrl(e) {
  const q = new URLSearchParams({ account, emoji: JSON.stringify({ url: e.url || '', encrypt_url: e.encrypt_url || '', aes_key: e.aes_key || '' }) });
  return `/api/sns/emoji?${q}`;
}

/* ── 卡片（链接 / 视频号 / 直播 / 音乐 / 笔记）─────────── */
// esc() 走 textContent/innerHTML，不转义引号；放进属性值时必须再转一次
const attr = s => esc(s).replace(/"/g, '&quot;');
function fmtDur(sec) {
  const n = Math.floor(Number(sec) || 0);
  if (n <= 0) return '';
  return `${Math.floor(n / 60)}:${String(n % 60).padStart(2, '0')}`;
}
function proxyUrl(url) {
  return `/api/sns/media?${new URLSearchParams({ account, url: url || '' })}`;
}
function cardShell(card, inner, cls) {
  const url = (card.url || '').trim();
  const tag = cls ? `sns-card ${cls}` : 'sns-card';
  if (!url) return `<div class="${tag}">${inner}</div>`;
  return `<a class="${tag}" href="${attr(url)}" target="_blank" rel="noreferrer">${inner}</a>`;
}
function renderCard(card) {
  if (!card || !card.kind) return '';
  if (card.kind === 'music') {
    const m = card.music || {};
    const d = fmtDur((m.duration_ms || 0) / 1000);
    return cardShell(card,
      `<div class="sns-card-body"><div class="sns-card-tag">🎵 音乐</div>`
      + `<div class="sns-card-title">${esc(m.album || card.title || '音乐')}</div>`
      + `<div class="sns-card-sub">${esc(m.singer || card.description || '')}${d ? ' · ' + d : ''}</div></div>`,
      'sns-card--music');
  }
  if (card.kind === 'finder') {
    const f = card.finder || {};
    const d = fmtDur(card.duration);
    const cover = (card.cover || '').trim();
    const vid = ((f.video_url || '').trim()
      || ((f.media && f.media[0] && f.media[0].url) || '')).trim();
    // 有视频地址时点封面直接播放（复用灯箱的视频模式）
    const img = cover
      ? `<img class="sns-card-cover sns-lb" loading="lazy" src="${esc(proxyUrl(cover))}"`
        + ` alt="视频号封面"${vid ? ` data-live="${attr(proxyUrl(vid))}"` : ''}`
        + ` onerror="this.remove()">` : '';
    return `<div class="sns-card sns-card--finder">${img}<div class="sns-card-body">`
      + `<div class="sns-card-tag">📹 视频号</div>`
      + `<div class="sns-card-title">${esc(f.nickname || '')}</div>`
      + `<div class="sns-card-sub">${[esc(f.media_count ? f.media_count + ' 个' : ''), d].filter(Boolean).join(' · ')}</div>`
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
  // link
  return cardShell(card,
    `<div class="sns-card-body"><div class="sns-card-tag">🔗 链接</div>`
    + `<div class="sns-card-title">${esc(card.title || card.url || '')}</div>`
    + `<div class="sns-card-sub">${esc(card.source || '')}</div>`
    + `<div class="sns-card-desc">${esc(card.description || '')}</div></div>`,
    'sns-card--link');
}

/* ── 单条动态 ─────────────────────────────────────────── */
function renderPost(p, compact = true) {
  const text = esc(p.content_desc || '');
  const card = renderCard(p.card);
  const loc = p.location
    ? `<div class="sns-loc">📍 ${esc(p.location.name || '')} ${esc(p.location.address || '')}</div>` : '';
  const comments = renderComments(p.comments, compact ? 20 : 0);
  const likes = (p.likes || []).length;
  const textHtml = text
    ? `<div class="sns-text">${text}</div>`
    : (card || (p.medias || []).length ? '' : '<div class="sns-text"><span class="dim">（无文字）</span></div>');
  return `<article class="sns-post" data-tid="${esc(String(p.tid))}">
    <div class="sns-head"><span class="sns-who">${esc(p.user_name || p.username)}</span><span class="sns-time">${fmtTs(p.ts)}</span></div>
    ${textHtml}
    ${card}
    ${renderMedia(p.medias)}
    ${loc}
    <div class="sns-actions"><span>赞 ${likes}</span><span>评论 ${(p.comments || []).length}</span>
      ${compact ? '<a href="#" class="sns-detail">查看详情</a>' : ''}</div>
    ${comments ? `<div class="sns-comments">${comments}</div>` : ''}
  </article>`;
}

/* ── 详情弹层 ─────────────────────────────────────────── */
async function openDetail(tid) {
  const box = el('sns-modal');
  el('sns-modal-title').textContent = '动态详情';
  el('sns-modal-body').innerHTML = '<div class="st-loading">加载中…</div>';
  box.hidden = false;
  try {
    const d = await fetchJSON(`/api/sns/detail?account=${encodeURIComponent(account)}&tid=${encodeURIComponent(tid)}`);
    el('sns-modal-body').innerHTML = renderPost(d.post, false);
  } catch (e) {
    el('sns-modal-body').innerHTML = `<div class="st-err">加载失败：${esc(e.message)}</div>`;
  }
}
function closeDetail() { el('sns-modal').hidden = true; el('sns-modal-body').innerHTML = ''; }
function setNotice(text) { const n = el('sns-notice'); n.textContent = text; n.hidden = !text; }
async function loadAccounts() {
  const d = await fetchJSON('/api/sns/accounts');
  const sel = el('sns-account');
  sel.innerHTML = (d.accounts || []).map(a => `<option value="${esc(a.wxid)}">${esc(a.wxid)} · ${a.count || 0} 条</option>`).join('');
  if (!d.accounts?.length) throw new Error('没有找到已解密的朋友圈数据库');
  account = sel.value;
  const a = d.accounts.find(x => x.wxid === account);
  await loadFriends();
  setNotice(`本地朋友圈图片只包含微信已经下载过的资源；未下载的图片会尝试从 CDN 获取。数据库共 ${a.count} 条动态。`);
}

/** 发布者下拉：直接取自 /api/sns/friends（服务端纯 SQL 聚合 + 昵称解析） */
async function loadFriends() {
  const sel = el('sns-user');
  const keep = sel.value;
  sel.innerHTML = '<option value="">全部发布者</option>';
  try {
    const d = await fetchJSON(`/api/sns/friends?account=${encodeURIComponent(account)}&limit=200`);
    sel.insertAdjacentHTML('beforeend', (d.friends || []).map(f => {
      const name = f.display || f.username;
      const label = name === f.username ? name : `${name}（${f.username.slice(0, 12)}）`;
      return `<option value="${esc(f.username)}">${esc(label)} · ${f.count} 条</option>`;
    }).join(''));
    if (keep) sel.value = keep;
  } catch (e) { /* 好友列表失败不阻断主流程，仍可手动用关键词搜索 */ }
}
async function load(reset = false) {
  if (busy || !account) return; busy = true;
  if (reset) { before = null; el('sns-list').innerHTML = ''; }
  const qs = new URLSearchParams({ account, limit: '20' });
  if (before) qs.set('before_tid', before);
  const kw = el('sns-keyword').value.trim(); if (kw) qs.set('keyword', kw);
  const user = el('sns-user').value.trim(); if (user) qs.set('username', user);
  try {
    const d = await fetchJSON(`/api/sns/timeline?${qs}`);
    el('sns-list').insertAdjacentHTML('beforeend',
      (d.timeline || []).map(p => renderPost(p)).join('') || '<div class="st-empty">没有匹配的动态</div>');
    before = d.next_before_tid || null; el('sns-more').hidden = !d.has_more;
    el('sns-meta').textContent = `${d.timeline?.length || 0} 条 · 游标分页`;
  } catch (e) { setNotice(e.message); }
  finally { busy = false; }
}
let pollTimer = null;

async function doExport() {
  const msg = el('sns-export-msg');
  const btn = el('sns-export');
  const withMedia = el('sns-exp-media').checked;
  if (withMedia && !window.confirm('下载媒体会逐张访问 CDN，可能耗时较久，确定继续？')) return;
  btn.disabled = true;
  msg.textContent = withMedia ? '导出中（含媒体）…' : '导出中…';
  try {
    const r = await fetch('/api/sns/export', {
      method: 'POST', headers: { 'content-type': 'application/json' },
      body: JSON.stringify({
        account, format: el('sns-fmt').value, media: withMedia,
        concurrency: Number(el('sns-exp-conc').value) || 5,
        keyword: el('sns-keyword').value.trim() || undefined,
        username: el('sns-user').value.trim() || undefined,
      }),
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
  const msg = el('sns-export-msg');
  const btn = el('sns-export');
  if (pollTimer) clearInterval(pollTimer);
  pollTimer = setInterval(async () => {
    let job;
    try { job = await (await fetch('/api/job')).json(); }
    catch (e) { return; }                        // 单次轮询失败忽略
    const logs = job.logs || [];
    const last = logs.length ? (logs[logs.length - 1].length >= 4
      ? logs[logs.length - 1][3] : logs[logs.length - 1][1]) : '';
    if (job.running) {
      msg.textContent = last ? `导出中… ${last}` : '导出中…';
      return;
    }
    clearInterval(pollTimer); pollTimer = null;
    btn.disabled = false;
    const rep = job.report || {};
    if (job.ok && rep.kind === 'sns_export') {
      const m = rep.media || {};
      const extra = m.total ? `，媒体 ${m.ok}/${m.total}（失败 ${m.fail}）` : '';
      msg.innerHTML = `✓ 已导出 ${rep.count} 条${extra} · `
        + `<a href="/api/sns/export/download?path=${encodeURIComponent(rep.file)}">下载</a> · `
        + `<a href="#" onclick="SX.openPath('${esc(rep.export_dir)}');return false">打开目录</a>`;
    } else {
      msg.textContent = '✗ ' + (job.error || last || '导出失败');
    }
  }, 800);
}

export async function init() {
  try { await loadAccounts(); await load(true); } catch (e) { setNotice(e.message); el('sns-list').innerHTML = ''; }
  el('sns-account').addEventListener('change', async () => { account = el('sns-account').value; await load(true); });
  el('sns-refresh').addEventListener('click', () => load(true));
  el('sns-more').addEventListener('click', () => load(false));
  el('sns-export').addEventListener('click', doExport);

  // 事件委托：详情 / 灯箱 / 关闭（列表和弹层共用）
  document.addEventListener('click', onDocClick);
  el('sns-modal-close').addEventListener('click', closeDetail);
  el('sns-modal-mask').addEventListener('click', closeDetail);
  el('sns-lightbox').addEventListener('click', closeLightbox);
  document.addEventListener('keydown', onKey);

  // 关键词输入防抖；发布者是下拉框，用 change
  let timer;
  el('sns-keyword').addEventListener('input', () => {
    clearTimeout(timer); timer = setTimeout(() => load(true), 350);
  });
  el('sns-user').addEventListener('change', () => load(true));
}

function onDocClick(ev) {
  const a = ev.target.closest('.sns-detail');
  if (a) {
    ev.preventDefault();
    const post = a.closest('.sns-post');
    if (post && post.dataset.tid) openDetail(post.dataset.tid);
    return;
  }
  const img = ev.target.closest('img.sns-lb');
  if (img && img.src) {
    ev.preventDefault();
    // 实况照片：点图播放动态部分
    const live = img.dataset.live;
    if (live) openLightbox(live, true);
    else openLightbox(img.src, false);
  }
}

function onKey(ev) {
  if (ev.key !== 'Escape') return;
  if (!el('sns-lightbox').hidden) { closeLightbox(); return; }
  if (!el('sns-modal').hidden) closeDetail();
}

export function destroy() {
  document.removeEventListener('click', onDocClick);
  document.removeEventListener('keydown', onKey);
  if (pollTimer) { clearInterval(pollTimer); pollTimer = null; }
  closeDetail();
  closeLightbox();
}
