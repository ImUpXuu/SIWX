/* 聊天统计页 —— 账号来自全局账号卡（SX.getAccount），本页不再自选 */
import { createDropdown, createDatePicker } from '/widgets.js?v=2026100601';

const { esc, fmtTs, fetchJSON, getAccount, go, toast } = window.SX;

function el(id) { return document.getElementById(id); }
function nf(n) { return (Number(n) || 0).toLocaleString('zh-CN'); }

let account = null;
let lastData = null;
let busy = false;
let pendingLoad = 0;             // busy 期间用户又改了筛选：0 无 / 1 普通刷新 / 2 强刷
let rangeDrop = null;
let startPick = null;
let endPick = null;

/* 页面存活哨兵：切页后仍在飞的异步回调就此止步 */
function gone() { return !document.getElementById('st-body'); }

/* ── SVG 小工具 ─────────────────────────────────────────── */
function svg(w, h, inner, cls) {
  return `<svg class="st-chart ${cls || ''}" viewBox="0 0 ${w} ${h}"
    preserveAspectRatio="xMidYMid meet" role="img">${inner}</svg>`;
}

/* 动画工具：用 CSS 变量把目标值交给样式表，由 CSS transition 补间 */
const animQueue = [];

/** 让元素在下一帧从 from 过渡到 to（写 CSS 变量，样式表负责 transition） */
function animVar(node, prop, from, to, delay) {
  if (!node) return;
  node.style.setProperty(prop, from);
  animQueue.push(() => {
    if (!node.isConnected) return;
    node.style.transitionDelay = (delay || 0) + 'ms';
    node.style.setProperty(prop, to);
  });
}

/** 统一冲刷动画队列：两帧后统一触发 */
function flushAnim() {
  requestAnimationFrame(() => requestAnimationFrame(() => {
    animQueue.forEach(fn => { try { fn(); } catch (e) { /* 忽略单点失败 */ } });
    animQueue.length = 0;
  }));
}

/* ── 环形图（类型分布）────────────────────────────────────── */
function donut(groups) {
  // 色板只有 8 档：超过 8 类时合并为「其他」，避免扇区与图例撞色
  if (groups.length > 8) {
    const head = groups.slice(0, 7);
    const rest = groups.slice(7);
    groups = head.concat([{ label: `其他（${rest.length} 类）`, count: rest.reduce((s, g) => s + g.count, 0) }]);
  }
  const total = groups.reduce((s, g) => s + g.count, 0) || 1;
  const size = 190, cx = size / 2, cy = size / 2;
  const r = 68, sw = 26;
  const C = 2 * Math.PI * r;
  let offset = 0;

  const arcs = groups.map((g, i) => {
    const frac = g.count / total;
    const len = Math.max(0, frac * C - 1.5);
    const seg = `<circle class="st-arc st-shade-${i % 8}" cx="${cx}" cy="${cy}" r="${r}" fill="none"
      stroke-width="${sw}"
      stroke-dasharray="${len} ${C - len}"
      stroke-dashoffset="${-offset}"
      data-i="${i}"
      transform="rotate(-90 ${cx} ${cy})"
      style="--dash:${len}"><title>${esc(g.label)}：${nf(g.count)} 条</title></circle>`;
    offset += frac * C;
    return seg;
  }).join('');

  const hole = `<circle cx="${cx}" cy="${cy}" r="${r - sw / 2 - 1}" fill="var(--card)"></circle>`;
  const txt = `<text class="st-donut-val" x="${cx}" y="${cy - 4}" text-anchor="middle"
      style="font-size:22px;font-weight:700;fill:var(--text)">${nf(total)}</text>
    <text x="${cx}" y="${cy + 15}" text-anchor="middle"
      style="font-size:11px">条消息</text>`;

  return `<div class="st-donut-wrap">
    <div class="st-donut">${svg(size, size, arcs + hole + txt)}</div>
    <div class="st-legend">${groups.map((g, i) => `
      <div class="st-leg" style="--i:${i}">
        <span class="sw st-shade-${i % 8}"></span>
        <span class="nm">${esc(g.label)}</span>
        <span class="ct">${nf(g.count)}</span>
        <span class="pc">${(g.count / total * 100).toFixed(1)}%</span>
      </div>`).join('')}</div>
  </div>`;
}

/* ── 月度趋势柱状图 ───────────────────────────────────────── */
function monthBars(items) {
  if (!items.length) return '<div class="st-empty">该范围内没有数据</div>';
  const W = 900, H = 260;
  const padL = 52, padR = 14, padT = 18, padB = 46;
  const iw = W - padL - padR, ih = H - padT - padB;
  const max = Math.max(...items.map(d => d.count), 1);

  let grid = '', yl = '';
  for (let i = 0; i <= 4; i++) {
    const v = max * i / 4;
    const y = padT + ih - (v / max) * ih;
    grid += `<line class="grid" x1="${padL}" y1="${y}" x2="${W - padR}" y2="${y}"></line>`;
    yl += `<text x="${padL - 8}" y="${y + 4}" text-anchor="end"
      style="font-size:10.5px">${nf(Math.round(v))}</text>`;
  }

  const bw = Math.max(3, Math.min(38, iw / items.length * 0.62));
  const step = iw / items.length;
  const bars = items.map((d, i) => {
    const h = Math.max(1, (d.count / max) * ih);
    const x = padL + step * i + (step - bw) / 2;
    const isTop = d.count === max;
    return `<rect class="bar-grow ${isTop ? 'bar' : 'bar-dim'}"
      x="${x.toFixed(1)}" y="${(padT + ih - h).toFixed(1)}" width="${bw.toFixed(1)}"
      height="${h.toFixed(1)}"
      rx="2" style="--grow:${(h / ih).toFixed(4)};--i:${i}">
      <title>${esc(d.month)}：${nf(d.count)} 条</title></rect>`;
  }).join('');

  const every = Math.ceil(items.length / Math.floor(iw / 52)) || 1;
  const xl = items.map((d, i) => {
    if (i % every) return '';
    const x = padL + step * i + step / 2;
    return `<text x="${x.toFixed(1)}" y="${H - padB + 17}" text-anchor="middle"
      style="font-size:10.5px">${esc(d.month.slice(2))}</text>`;
  }).join('');

  return svg(W, H,
    grid + yl +
    `<line class="axis" x1="${padL}" y1="${padT + ih}" x2="${W - padR}" y2="${padT + ih}"></line>`
    + bars + xl);
}

/* ── 24 小时活跃度柱状图 ──────────────────────────────────── */
function hourBars(hours) {
  const W = 900, H = 230;
  const padL = 50, padR = 14, padT = 18, padB = 40;
  const iw = W - padL - padR, ih = H - padT - padB;
  const max = Math.max(...hours, 1);
  const peak = hours.indexOf(max);

  let grid = '', yl = '';
  for (let i = 0; i <= 4; i++) {
    const v = max * i / 4;
    const y = padT + ih - (v / max) * ih;
    grid += `<line class="grid" x1="${padL}" y1="${y}" x2="${W - padR}" y2="${y}"></line>`;
    yl += `<text x="${padL - 8}" y="${y + 4}" text-anchor="end"
      style="font-size:10.5px">${nf(Math.round(v))}</text>`;
  }

  const step = iw / 24;
  const bw = Math.max(4, step * 0.66);
  const bars = hours.map((v, i) => {
    const h = Math.max(1, (v / max) * ih);
    const x = padL + step * i + (step - bw) / 2;
    const cls = i === peak ? 'bar' : 'bar-dim';
    return `<rect class="bar-grow ${cls}" x="${x.toFixed(1)}" y="${(padT + ih - h).toFixed(1)}"
      width="${bw.toFixed(1)}" height="${h.toFixed(1)}" rx="2"
      style="--grow:${(h / ih).toFixed(4)};--i:${i}">
      <title>${String(i).padStart(2, '0')}:00 — ${nf(v)} 条</title></rect>`;
  }).join('');

  const xl = hours.map((_, i) => (i % 3 ? '' :
    `<text x="${(padL + step * i + step / 2).toFixed(1)}" y="${H - padB + 17}"
      text-anchor="middle" style="font-size:10.5px">${String(i).padStart(2, '0')}</text>`
  )).join('');

  const px = padL + step * peak + step / 2;
  const peakH = (hours[peak] / max) * ih;
  const mark = `<text x="${px.toFixed(1)}" y="${(padT + ih - peakH - 7).toFixed(1)}"
    text-anchor="middle" class="lbl" style="font-size:11px">峰值 ${String(peak).padStart(2, '0')}:00</text>`;

  return svg(W, H,
    grid + yl +
    `<line class="axis" x1="${padL}" y1="${padT + ih}" x2="${W - padR}" y2="${padT + ih}"></line>`
    + bars + xl + mark);
}

/* ── 星期分布（横向条）────────────────────────────────────── */
function weekdayBars(days) {
  const names = ['周一', '周二', '周三', '周四', '周五', '周六', '周日'];
  const max = Math.max(...days, 1);
  return `<div class="st-week-rows">${
    days.map((v, i) => `
    <div class="st-mini">
      <span class="st-week-name">${names[i]}</span>
      <span class="track"><span class="fill" data-w="${(v / max * 100).toFixed(1)}"
        style="--i:${i}"></span></span>
      <span class="st-week-num">${nf(v)}</span>
    </div>`).join('')
  }</div>`;
}

/* ── 排行表 ───────────────────────────────────────────────── */
function rankTable(rows, maxLabel) {
  if (!rows.length) return '<div class="st-empty">暂无数据</div>';
  const max = Math.max(...rows.map(r => r.count), 1);
  return `<table class="st-table">
    <thead><tr><th></th><th>${esc(maxLabel)}</th><th class="col-share">占比</th>
    <th class="col-num">消息数</th></tr></thead>
    <tbody>${rows.map((r, i) => `
      <tr style="--i:${i}">
        <td class="idx">${i + 1}</td>
        <td class="nm">${esc(r.name)}${
          r.wxid && r.wxid !== r.name
            ? `<span class="st-wxid mono">${esc(r.wxid)}</span>` : ''}</td>
        <td><span class="st-mini"><span class="track">
          <span class="fill" data-w="${(r.count / max * 100).toFixed(1)}"></span>
        </span></span></td>
        <td class="num">${nf(r.count)}</td>
      </tr>`).join('')}</tbody>
  </table>`;
}

/* ── 渲染整页 ─────────────────────────────────────────────── */
function render(d) {
  const span = d.span || {};
  const durDays = span.max && span.min ? Math.round((span.max - span.min) / 86400) : 0;
  const cards = [
    ['消息总数', nf(d.total), d.shards ? `来自 ${d.shards} 个分片` : ''],
    ['会话数', nf(d.chat_count), '有消息的会话'],
    ['时间跨度', durDays ? `${durDays} 天` : '—',
      span.min ? `${fmtTs(span.min).slice(0, 10)} 起` : ''],
    ['日均消息', nf(d.per_day), '按跨度估算'],
    ['最活跃时段', `${String(d.peak_hour).padStart(2, '0')}:00`,
      '当天消息最多的小时'],
    ['最活跃星期', ['周一', '周二', '周三', '周四', '周五', '周六', '周日'][d.peak_weekday] || '—', ''],
  ];

  const topSenders = (d.top_senders || []).slice(0, 12);

  const body = el('st-body');
  if (!body) return;
  body.innerHTML = `
    <div class="st-cards">${cards.map((c, i) => `
      <div class="st-card" style="--i:${i}">
        <div class="k">${esc(c[0])}</div>
        <div class="v">${esc(c[1])}</div>
        ${c[2] ? `<div class="s">${esc(c[2])}</div>` : ''}
      </div>`).join('')}</div>

    <div class="st-grid">
      <div class="st-panel" style="--i:0">
        <div class="st-panel-head"><h2>消息类型分布</h2>
          <span class="hint">${(d.type_groups || []).length} 类</span></div>
        ${(d.type_groups || []).length ? donut(d.type_groups)
          : '<div class="st-empty">暂无数据</div>'}
      </div>

      <div class="st-panel" style="--i:1">
        <div class="st-panel-head"><h2>星期活跃度</h2>
          <span class="hint">累计消息量</span></div>
        ${weekdayBars(d.by_weekday || [0, 0, 0, 0, 0, 0, 0])}
        <div class="st-tip">周末通常消息更集中；工作日分布更平缓。</div>
      </div>

      <div class="st-panel st-full" style="--i:2">
        <div class="st-panel-head"><h2>月度消息趋势</h2>
          <span class="hint">绿柱 = 峰值月份</span></div>
        ${monthBars(d.by_month || [])}
      </div>

      <div class="st-panel st-full" style="--i:3">
        <div class="st-panel-head"><h2>24 小时活跃度</h2>
          <span class="hint">按消息发送时间统计</span></div>
        ${hourBars(d.by_hour || new Array(24).fill(0))}
      </div>

      <div class="st-panel st-full" style="--i:4">
        <div class="st-panel-head"><h2>私聊发送者排行</h2>
          <span class="hint">Top ${topSenders.length} · 仅私聊</span></div>
        ${rankTable(topSenders, '联系人')}
        <div class="st-tip">${esc(d.top_senders_note || '仅统计私聊')}；
          按会话聚合，昵称取自联系人备注/微信昵称。</div>
      </div>
    </div>`;

  startAnimations();

  const meta = el('st-meta');
  if (meta) {
    meta.textContent = (account ? `${account} · ` : '') +
      (d.generated_at ? `统计于 ${SX.timeStr(d.generated_at * 1000)}` : '');
  }
}

/** 把 data-w / --grow 之类的目标值交给 CSS 过渡，避免首帧被"瞬移" */
function startAnimations() {
  const body = el('st-body');
  if (!body) return;

  body.querySelectorAll('.st-mini .fill[data-w]').forEach((n, i) => {
    animVar(n, 'width', '0%', n.dataset.w + '%', i * 45);
  });
  body.querySelectorAll('.bar-grow').forEach((n, i) => {
    const g = n.style.getPropertyValue('--grow') || '1';
    animVar(n, '--sy', '0', g, i * 18);
  });
  body.querySelectorAll('.st-arc').forEach((n, i) => {
    animVar(n, '--arc', '0', '1', i * 90);
  });
  body.querySelectorAll('.st-card .v, .st-donut-val').forEach((n, i) => {
    const raw = n.textContent || '';
    if (raw.includes(':')) return;         // 「21:00」这类时间不是数值，滚动会毁成 2,100
    const target = Number(raw.replace(/[^\d.]/g, ''));
    if (!target) return;
    // 保留单位后缀（如「365 天」的「 天」、百分号）：此前直接置 0 再只写数字，单位被抹掉
    const unit = (raw.match(/[^\d.,]+/) || [''])[0];
    n.textContent = '0' + unit;
    animVar(n, 'opacity', '1', '1', 0);
    countUp(n, target, i * 60, raw.includes('.'), unit);
  });

  flushAnim();
}

/** 数字滚动动画（带千分位；unit 为需要保留的单位后缀） */
function countUp(node, target, delay, keepDecimal, unit) {
  const dur = 700;
  const suffix = unit || '';
  const fmt = (v) => (keepDecimal
    ? v.toLocaleString('zh-CN', { minimumFractionDigits: 1, maximumFractionDigits: 1 })
    : Math.round(v).toLocaleString('zh-CN')) + suffix;
  setTimeout(() => {
    if (!node.isConnected) return;
    let t0 = null;
    const step = (ts) => {
      if (!node.isConnected) return;
      if (t0 === null) t0 = ts;
      const p = Math.min(1, (ts - t0) / dur);
      const e = 1 - Math.pow(1 - p, 3);
      node.textContent = fmt(target * e);
      if (p < 1) requestAnimationFrame(step);
      else node.textContent = fmt(target);
    };
    requestAnimationFrame(step);
  }, delay);
}

/* ── 数据加载 ─────────────────────────────────────────────── */
function dateStr(d) {
  return `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, '0')}-${String(d.getDate()).padStart(2, '0')}`;
}

function rangeStart(months) {
  if (!months) return null;
  const d = new Date();
  d.setMonth(d.getMonth() - months);
  return dateStr(d);
}

function currentRange() {
  const v = rangeDrop ? rangeDrop.value : 'all';
  if (v === 'custom') {
    let start = startPick && startPick.value ? startPick.value : null;
    let end = endPick && endPick.value ? endPick.value : null;
    if (start && end && start > end) {
      // 起止倒置：交换并回写日期框，用户看到的就是实际生效的范围
      const t = start; start = end; end = t;
      if (startPick && endPick) { startPick.value = start; endPick.value = end; }
      toast('起止日期写反了，已自动交换');
    }
    return { start, end };
  }
  if (v === 'all') return { start: null, end: null };
  return { start: rangeStart(Number(v)), end: null };
}

function setEmpty(show, html) {
  const box = el('st-empty');
  if (!box) return;
  if (html != null) box.innerHTML = html;
  box.classList.toggle('hidden', !show);
}

async function load(force) {
  if (!account) return;
  if (busy) {                     // 加载中改筛选：记下来，本轮结束后补跑
    pendingLoad = Math.max(pendingLoad, force ? 2 : 1);
    return;
  }
  busy = true;
  const rf = el('st-refresh');
  if (rf) rf.disabled = true;
  const body = el('st-body');
  if (force || !lastData) {
    setEmpty(false);
    if (body) {
      body.innerHTML = `<div class="st-loading">${
        force ? '正在重新统计，请稍候…' : '正在统计…'}</div>`;
    }
  } else {
    // 已有数据时后台更新：给一个可见的进行中标记，正文保持可读
    const m = el('st-meta');
    if (m) m.textContent = '正在更新…';
  }

  const { start, end } = currentRange();
  const qs = new URLSearchParams({ account });
  if (start) qs.set('start', start);
  if (end) qs.set('end', end);
  if (force) qs.set('refresh', '1');

  try {
    const d = await fetchJSON(`/api/stats/overview?${qs}`);
    if (gone()) return;
    lastData = d;
    setEmpty(false);
    render(d);
  } catch (e) {
    if (gone()) return;
    // 失败时保留上次成功的数据，避免整页被清空
    if (lastData) {
      const m = el('st-meta');
      if (m) m.textContent = '';
      setEmpty(true, `<div class="st-err">统计失败：${esc(e.message)}（下方为上次结果）</div>`);
    } else {
      if (body) body.innerHTML = '';
      setEmpty(true, `<div class="st-err">统计失败：${esc(e.message)}</div>
        <div class="st-retry"><button class="btn" id="st-retry" type="button">重试</button></div>`);
      el('st-retry')?.addEventListener('click', () => load(true));
    }
  } finally {
    busy = false;
    if (rf) rf.disabled = false;
    if (pendingLoad) {
      const f = pendingLoad > 1;
      pendingLoad = 0;
      load(f);
    }
  }
}

/** 自定义范围：范围下拉切到「自定义日期」时显示两个自绘日期框 */
function syncCustomVisible() {
  const on = rangeDrop ? rangeDrop.value === 'custom' : false;
  const box = el('st-custom');
  if (!box) return false;
  box.classList.toggle('hidden', !on);
  if (on) {
    const today = new Date();
    if (endPick && !endPick.value) endPick.value = dateStr(today);
    if (startPick && !startPick.value) {
      const d = new Date(today.getTime());
      d.setMonth(d.getMonth() - 1);
      startPick.value = dateStr(d);
    }
  }
  return on;
}

async function init() {
  account = getAccount();
  if (!account) {
    setEmpty(true, '还没有解密产物。<br><br><button class="btn btn-primary" id="st-go-guide" type="button">去添加账号</button>');
    el('st-go-guide')?.addEventListener('click', () => window.__sxOnboarding?.open('add'));
    return;
  }

  rangeDrop = createDropdown({
    options: [
      { value: 'all', label: '全部时间' },
      { value: '12', label: '最近 12 个月' },
      { value: '6', label: '最近 6 个月' },
      { value: '3', label: '最近 3 个月' },
      { value: '1', label: '最近 1 个月' },
      { value: 'custom', label: '自定义日期' },
    ],
    value: 'all',
    label: '时间范围',
  });
  el('st-range').appendChild(rangeDrop.el);
  startPick = createDatePicker({ placeholder: '开始日期', label: '开始日期' });
  el('st-start').appendChild(startPick.el);
  endPick = createDatePicker({ placeholder: '结束日期', label: '结束日期' });
  el('st-end').appendChild(endPick.el);

  const rf = el('st-refresh');
  if (rf) rf.disabled = true;
  const body = el('st-body');
  try {
    rangeDrop.onChange = () => {
      syncCustomVisible();
      // 切到自定义时会自动填好「最近 1 个月」的起止日期，因此可以立即刷新
      load(false);
    };
    startPick.onChange = () => load(false);
    endPick.onChange = () => load(false);

    if (rf) rf.addEventListener('click', () => load(true));

    syncCustomVisible();
    await load(false);
  } catch (e) {
    if (gone()) return;
    if (body) body.innerHTML = '';
    setEmpty(true, `<div class="st-err">加载失败：${esc(e.message)}</div>`);
  } finally {
    if (rf) rf.disabled = false;
  }
}

export { init };
