/* 聊天统计页 —— 所有图表用内联 SVG 手绘，零外部依赖，沿用黑白描边风格 */
const { esc, fmtTs, fetchJSON } = window.SX;

function el(id) { return document.getElementById(id); }
function nf(n) { return (Number(n) || 0).toLocaleString('zh-CN'); }

let account = null;
let lastData = null;
let busy = false;

/* ── SVG 小工具 ─────────────────────────────────────────── */
function svg(w, h, inner, cls) {
  return `<svg class="st-chart ${cls || ''}" viewBox="0 0 ${w} ${h}"
    preserveAspectRatio="xMidYMid meet" role="img">${inner}</svg>`;
}

/** 灰阶调色板：主色用实心黑，其余用不同密度的描边/灰阶区分 */
const SHADES = ['#111111', '#3d3d3d', '#6b6b6b', '#949494', '#b5b5b5',
                '#cfcfcf', '#e2e2e2', '#f0f0f0'];

/* 动画工具：用 CSS 变量把目标值交给样式表，由 CSS transition 补间。
   直接写死属性会让动画被"瞬移"掉 —— 浏览器不会对首次渲染的 SVG
   属性做过渡，必须先给出起点（0），下一帧再改成目标值。        */
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

/** 统一冲刷动画队列：两帧后统一触发，避免每个元素各自 requestAnimationFrame */
function flushAnim() {
  requestAnimationFrame(() => requestAnimationFrame(() => {
    animQueue.forEach(fn => { try { fn(); } catch (e) { /* 忽略单点失败 */ } });
    animQueue.length = 0;
  }));
}

/* ── 环形图（类型分布）────────────────────────────────────── */
function donut(groups) {
  const total = groups.reduce((s, g) => s + g.count, 0) || 1;
  const size = 190, cx = size / 2, cy = size / 2;
  const r = 68, sw = 26;
  const C = 2 * Math.PI * r;
  let offset = 0;

  const arcs = groups.map((g, i) => {
    // 每段留 1.5px 视觉间隙，避免同色相邻糊成一片
    const frac = g.count / total;
    const len = Math.max(0, frac * C - 1.5);
    const seg = `<circle class="st-arc" cx="${cx}" cy="${cy}" r="${r}" fill="none"
      stroke="${SHADES[i % SHADES.length]}" stroke-width="${sw}"
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
      style="font-size:22px;font-weight:700">${nf(total)}</text>
    <text x="${cx}" y="${cy + 15}" text-anchor="middle"
      style="font-size:11px">条消息</text>`;

  return `<div class="st-donut-wrap">
    <div class="st-donut">${svg(size, size, arcs + hole + txt)}</div>
    <div class="st-legend">${groups.map((g, i) => `
      <div class="st-leg" style="--i:${i}">
        <span class="sw" style="background:${SHADES[i % SHADES.length]}"></span>
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

  // Y 轴 4 条网格线
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
    const h = (d.count / max) * ih;
    const x = padL + step * i + (step - bw) / 2;
    const isTop = d.count === max;
    // 每根柱子都带 <title>，鼠标悬停可看具体月份与条数。
    // 高度用 CSS 变量 + scaleY，这样能走 GPU 合成，比动画 height 顺滑。
    return `<rect class="bar-grow ${isTop ? 'bar' : 'bar-dim'}"
      x="${x.toFixed(1)}" y="${padT.toFixed(1)}" width="${bw.toFixed(1)}"
      height="${Math.max(1, ih).toFixed(1)}"
      rx="2" style="--grow:${(h / Math.max(1, ih)).toFixed(4)};--i:${i}">
      <title>${esc(d.month)}：${nf(d.count)} 条</title></rect>`;
  }).join('');

  // X 轴标签：按可用宽度抽稀，避免重叠
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
    const h = (v / max) * ih;
    const x = padL + step * i + (step - bw) / 2;
    const cls = i === peak ? 'bar' : 'bar-dim';
    return `<rect class="bar-grow ${cls}" x="${x.toFixed(1)}" y="${padT.toFixed(1)}"
      width="${bw.toFixed(1)}" height="${Math.max(1, ih).toFixed(1)}" rx="2"
      style="--grow:${(h / Math.max(1, ih)).toFixed(4)};--i:${i}">
      <title>${String(i).padStart(2, '0')}:00 — ${nf(v)} 条</title></rect>`;
  }).join('');

  const xl = hours.map((_, i) => (i % 3 ? '' :
    `<text x="${(padL + step * i + step / 2).toFixed(1)}" y="${H - padB + 17}"
      text-anchor="middle" style="font-size:10.5px">${String(i).padStart(2, '0')}</text>`
  )).join('');

  // 峰值标注
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
  return `<div style="display:flex;flex-direction:column;gap:9px">${
    days.map((v, i) => `
    <div class="st-mini">
      <span style="width:38px;flex:none;font-size:12.5px">${names[i]}</span>
      <span class="track"><span class="fill" data-w="${(v / max * 100).toFixed(1)}"
        style="--i:${i}"></span></span>
      <span style="width:66px;text-align:right;font-size:12.5px;font-weight:700;
        font-variant-numeric:tabular-nums">${nf(v)}</span>
    </div>`).join('')
  }</div>`;
}

/* ── 排行表 ───────────────────────────────────────────────── */
function rankTable(rows, maxLabel) {
  if (!rows.length) return '<div class="st-empty">暂无数据</div>';
  const max = Math.max(...rows.map(r => r.count), 1);
  return `<table class="st-table">
    <thead><tr><th></th><th>${esc(maxLabel)}</th><th style="width:180px">占比</th>
    <th style="text-align:right">消息数</th></tr></thead>
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

  // 后端已按私聊过滤并给出昵称；这里只取前 12 位
  const topSenders = (d.top_senders || []).slice(0, 12);

  const body = el('st-body');
  if (!body) return;                       // 页面已切走
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
          <span class="hint">实心柱 = 峰值月份</span></div>
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
    meta.textContent = d.generated_at
      ? `统计于 ${SX.timeStr(d.generated_at * 1000)}` : '';
  }
}

/** 把 data-w / --grow 之类的目标值交给 CSS 过渡，避免首帧被"瞬移" */
function startAnimations() {
  const body = el('st-body');
  if (!body) return;

  // 横向条：宽度从 0 长到目标
  body.querySelectorAll('.st-mini .fill[data-w]').forEach((n, i) => {
    animVar(n, 'width', '0%', n.dataset.w + '%', i * 45);
  });

  // 纵向柱：scaleY 从 0 长到 1
  body.querySelectorAll('.bar-grow').forEach((n, i) => {
    const g = n.style.getPropertyValue('--grow') || '1';
    animVar(n, '--sy', '0', g, i * 18);
  });

  // 环形图：整圈先转出来，再逐段展开
  body.querySelectorAll('.st-arc').forEach((n, i) => {
    animVar(n, '--arc', '0', '1', i * 90);
  });

  // 数字滚动：从 0 数到目标
  body.querySelectorAll('.st-card .v, .st-donut-val').forEach((n, i) => {
    const raw = n.textContent || '';
    const target = Number(raw.replace(/[^\d.]/g, ''));
    if (!target) return;
    n.textContent = '0';
    animVar(n, 'opacity', '1', '1', 0);
    countUp(n, target, i * 60, raw.includes('.'));
  });

  flushAnim();
}

/** 数字滚动动画（带千分位） */
function countUp(node, target, delay, keepDecimal) {
  const dur = 700;
  const fmt = (v) => keepDecimal
    ? v.toLocaleString('zh-CN', { minimumFractionDigits: 1, maximumFractionDigits: 1 })
    : Math.round(v).toLocaleString('zh-CN');
  setTimeout(() => {
    if (!node.isConnected) return;
    let t0 = null;
    const step = (ts) => {
      if (!node.isConnected) return;
      if (t0 === null) t0 = ts;
      const p = Math.min(1, (ts - t0) / dur);
      // easeOutCubic：先快后慢，收尾更自然
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

/** 读取当前工具条上的时间范围。
 *  返回 {start, end}，两者都是 "YYYY-MM-DD" 或 null。
 *  后端会用日级聚合缓存过滤整页统计，不只是裁剪月度图。 */
function currentRange() {
  const sel = el('st-range');
  const v = sel ? sel.value : 'all';
  if (v === 'custom') {
    const s = el('st-start'), e = el('st-end');
    let start = s && s.value ? s.value : null;
    let end = e && e.value ? e.value : null;
    if (start && end && start > end) { const t = start; start = end; end = t; }
    return { start, end };
  }
  if (v === 'all') return { start: null, end: null };
  return { start: rangeStart(Number(v)), end: null };
}

/** 显示/隐藏正文下方的占位提示。占位符在 HTML 里独立于 #st-body，
 *  因为 render() 会整体覆写 st-body.innerHTML。 */
function setEmpty(show, html) {
  const box = el('st-empty');
  if (!box) return;                       // 页面已切走，安全退出
  if (html != null) box.innerHTML = html;
  box.classList.toggle('hidden', !show);
}

async function load(force) {
  if (!account || busy) return;
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
  }

  const { start, end } = currentRange();
  const qs = new URLSearchParams({ account });
  if (start) qs.set('start', start);
  if (end) qs.set('end', end);
  if (force) qs.set('refresh', '1');

  try {
    const d = await fetchJSON(`/api/stats/overview?${qs}`);
    lastData = d;
    setEmpty(false);
    render(d);
  } catch (e) {
    // 失败时保留上次成功的数据，避免整页被清空
    if (lastData) {
      const m = el('st-meta');
      if (m) m.textContent = '';
      setEmpty(true, `<div class="st-err">统计失败：${esc(e.message)}（下方为上次结果）</div>`);
    } else {
      if (body) body.innerHTML = '';
      setEmpty(true, `<div class="st-err">统计失败：${esc(e.message)}</div>`);
    }
  } finally {
    busy = false;
    if (rf) rf.disabled = false;
  }
}

/** 自定义范围的两个日期框，随下拉框切换显隐 */
function syncCustomVisible() {
  const sel = el('st-range');
  const box = el('st-custom');
  if (!sel || !box) return false;
  const on = sel.value === 'custom';
  box.classList.toggle('hidden', !on);
  if (on) {
    const s = el('st-start'), e = el('st-end');
    const today = new Date();
    if (e && !e.value) e.value = dateStr(today);
    if (s && !s.value) {
      const d = new Date(today.getTime());
      d.setMonth(d.getMonth() - 1);
      s.value = dateStr(d);
    }
  }
  return on;
}

async function init() {
  const rf = el('st-refresh');
  if (rf) rf.disabled = true;
  const body = el('st-body');
  try {
    const { accounts } = await fetchJSON('/api/stats/accounts');
    if (!accounts.length) {
      if (body) body.innerHTML = '';
      setEmpty(true, '<div class="st-err">还没有解密产物。'
        + '请先在「引导设置」完成密钥提取与解密，再回来看统计。</div>');
      return;
    }
    const sel = el('st-account');
    if (!sel) return;                     // 页面已切走
    sel.innerHTML = accounts.map(a =>
      `<option value="${esc(a.wxid)}">${esc(a.wxid)}（${a.shards} 分片）</option>`).join('');
    account = sel.value;

    sel.addEventListener('change', () => {
      account = sel.value;
      lastData = null;
      load(false);
    });

    const rsel = el('st-range');
    if (!rsel) return;
    rsel.addEventListener('change', () => {
      syncCustomVisible();
      // 切到自定义时会自动填好「最近 1 个月」的起止日期，因此可以立即刷新，
      // 用户再改两个日期框时也会自动刷新。
      load(false);
    });

    ['st-start', 'st-end'].forEach(id => {
      const n = el(id);
      if (n) n.addEventListener('change', () => load(false));
    });

    if (rf) rf.addEventListener('click', () => load(true));

    syncCustomVisible();
    await load(false);
  } catch (e) {
    if (body) body.innerHTML = '';
    setEmpty(true, `<div class="st-err">加载失败：${esc(e.message)}</div>`);
  } finally {
    if (rf) rf.disabled = false;
  }
}

export { init };
