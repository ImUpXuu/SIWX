/* 聊天查看 —— 重写版：table 布局 + 绝对定位，会话行绝不吃字 */
const { esc, fetchJSON, fmtTs } = window.SX;

let account = null;
let sessions = [];
let currentChat = null;
let earliest = 0;
let hasMore = false;
let loading = false;

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
    sel.addEventListener('change', () => { account = sel.value; loadSessions(); });
    await loadSessions();
  } catch (e) {
    el('c-sessions').innerHTML = `<div class="c-empty">${esc(e.message)}</div>`;
  }

  el('c-search').addEventListener('input', () => renderSessions(el('c-search').value));
  el('c-back').addEventListener('click', () => el('chat-wrap').classList.remove('open'));
  // 刷新 = 增量同步 → 刷新列表
  el('c-refresh').addEventListener('click', async () => {
    const btn = el('c-refresh');
    btn.disabled = true;
    el('c-sessions').innerHTML = '<div class="c-loading">⏳ 增量同步中…</div>';
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
    } catch (e) {
      el('c-sessions').innerHTML = `<div class="c-empty">刷新失败: ${esc(e.message)}</div>`;
    } finally {
      btn.disabled = false;
    }
  });
  // 跳转导出页并预选当前会话
  el('c-export').addEventListener('click', () => {
    if (!currentChat) { window.alert('先在左侧打开一个会话'); return; }
    sessionStorage.setItem('siwx-export-preset', JSON.stringify({
      account, chat: currentChat.username, display: currentChat.display,
    }));
    location.hash = '#/export';
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

async function loadSessions() {
  el('c-sessions').innerHTML = '<div class="c-loading">加载会话…</div>';
  const { sessions: ss } = await fetchJSON(
    `/api/chat/sessions?account=${encodeURIComponent(account)}`);
  sessions = ss;
  renderSessions('');
}

function renderSessions(kw) {
  const kwL = (kw || '').toLowerCase();
  const list = sessions.filter(s =>
    !kwL || s.display.toLowerCase().includes(kwL) || s.username.toLowerCase().includes(kwL));
  el('c-sessions').innerHTML = list.length ? list.map(s => {
    const initials = (s.display || s.username || '?').slice(0, 2).toUpperCase();
    const isGrp = s.is_group;
    return `
    <div class="sess ${currentChat && currentChat.username === s.username ? 'sel' : ''}" data-u="${esc(s.username)}">
      <div class="ava">${esc(initials)}</div>
      <span class="name">${esc(s.display)}</span>
      <span class="prev">${esc(s.preview || '点击查看详情')}</span>
      <span class="tm">${fmtTs(s.last_time)}</span>
      <span class="tag">${isGrp ? '群聊' : ''}</span>
    </div>`;
  }).join('')
    : '<div class="c-empty">没有匹配的会话</div>';
  el('c-sessions').querySelectorAll('.sess').forEach(n => {
    n.addEventListener('click', () => {
      const s = sessions.find(x => x.username === n.dataset.u);
      openChat(s);
    });
  });
}

async function openChat(s) {
  currentChat = s;
  earliest = 0;
  hasMore = false;
  el('chat-wrap').classList.add('open');
  el('c-title').textContent = s.display;
  el('c-sub').textContent = s.is_group ? '群聊' : '';
  el('c-msgs').innerHTML = '<div class="c-loading">加载消息…</div>';
  await loadMessages(true);
}

async function loadMessages(fresh) {
  const s = currentChat;
  if (!s) return false;
  const url = `/api/chat/messages?account=${encodeURIComponent(account)}` +
    `&chat=${encodeURIComponent(s.username)}` +
    (fresh || !earliest ? '' : `&before=${earliest}`);
  const data = await fetchJSON(url);
  const box = el('c-msgs');
  const html = data.messages.map((m, i) => {
    // 与上一条间隔 >5 分钟时居中显示时间
    const prev = i > 0 ? data.messages[i - 1] : null;
    const showTime = fresh || i > 0 || !earliest
      ? (!prev || m.ts - prev.ts > 300)
      : false;
    return (showTime ? `<div class="m-time-chip">${fmtTs(m.ts)}</div>` : '') + bubble(m);
  }).join('');
  if (fresh) {
    box.innerHTML = html || '<div class="c-empty">这个会话没有消息</div>';
    box.scrollTop = box.scrollHeight;
  } else {
    box.insertAdjacentHTML('afterbegin', html);
  }
  earliest = data.messages.length ? data.messages[0].ts : earliest;
  hasMore = data.has_more;
  return true;
}

function avaHtml(username, letters) {
  const src = `/api/chat/avatar?account=${encodeURIComponent(account)}` +
              `&username=${encodeURIComponent(username)}`;
  return `<div class="m-ava"><span>${esc(letters)}</span>` +
         `<img loading="lazy" src="${src}" onerror="this.remove()"></div>`;
}

function bubble(m) {
  if (m.type === 10000 || m.type === 10002) {
    return `<div class="msg-row sys"><div class="m-bubble">${esc(m.text)}</div></div>`;
  }
  const who = m.is_me ? 'me' : '';
  const avaUser = m.is_me ? account : (m.sender_wxid || currentChat.username);
  const ava = avaHtml(avaUser, (m.sender_name || '?').slice(0, 2).toUpperCase());
  const name = (!m.is_me && currentChat && currentChat.is_group && m.sender_name)
    ? `<div class="m-name">${esc(m.sender_name)}</div>` : '';
  let inner = '';
  if (m.kind === 'quote' && m.quote) {
    inner = `<div class="m-quote"><div class="m-quote-n">${esc(m.quote.displayname)}</div>` +
            `<div class="m-quote-t">${esc(m.quote.content)}</div></div>` + esc(m.text);
  } else if (m.kind === 'link' && m.link) {
    const url = m.link.url || '';
    const full = url.startsWith('http') ? url : 'https://' + url;
    let host = '';
    try { host = new URL(full).hostname; } catch (e) {}
    inner = `<div class="m-link"><a href="${esc(full)}" target="_blank" rel="noreferrer">${esc(m.link.title)}</a>` +
            (host ? `<div class="m-link-host">${esc(host)}</div>` : '') + `</div>`;
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
  return `
    <div class="msg-row ${who}">
      ${ava}
      <div class="m-col">
        ${name}
        <div class="m-bubble">${quoteHtml}${inner}</div>
        <div class="m-time">${fmtTs(m.ts)}</div>
      </div>
    </div>`;
}
    quoteHtml = `<div class="m-quote"><div class="m-quote-n">${esc(m.quote.displayname)}</div>` +
                `<div class="m-quote-t">${esc(m.quote.content)}</div></div>`;
  }
  return `
    <div class="m-row ${who}">
      ${ava}
      <div class="cell-body">
        ${name}
        <div class="m-bubble">${quoteHtml}${inner}</div>
        <div class="m-time">${fmtTs(m.ts)}</div>
      </div>
    </div>`;
}

export function destroy() { /* 无常驻定时器 */ }
export { init };
