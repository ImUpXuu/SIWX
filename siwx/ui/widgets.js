/* stories-in-wx 自绘控件（零依赖）—— 下拉选择 + 日期选择 + 右键菜单
 *
 * 背景：原生 <select> / <input type="date"> 与控制台的黑白描边设计不搭，
 * 弹出的系统面板也无法跟随主题。这里用纯 DOM 自绘替代，样式集中在
 * app.css 的「自绘组件」段，颜色全部走 CSS 变量，深浅主题自动适配。
 *
 * 页面用法（ESM）：
 *   import { createDropdown, createDatePicker, openMenu } from '/widgets.js?v=xxx';
 *   const dd = createDropdown({ options, value, onChange, label });
 *   mount.replaceChildren(dd.el);   dd.value = 'x';  dd.options = [...];
 */

/* ── 小工具 ─────────────────────────────────────────────── */
function h(tag, cls, text) {
  const n = document.createElement(tag);
  if (cls) n.className = cls;
  if (text != null) n.textContent = text;
  return n;
}

const PAD = (n) => String(n).padStart(2, '0');

/** 'YYYY-MM-DD' → 当天零点的 Date；解析失败返回 null */
function parseDay(s) {
  if (!s) return null;
  const m = /^(\d{4})-(\d{1,2})-(\d{1,2})$/.exec(String(s));
  if (!m) return null;
  const d = new Date(Number(m[1]), Number(m[2]) - 1, Number(m[3]));
  return Number.isNaN(d.getTime()) ? null : d;
}

function fmtDay(s) {
  const d = parseDay(s);
  return d ? `${d.getFullYear()}-${PAD(d.getMonth() + 1)}-${PAD(d.getDate())}` : '';
}

const CAL_SVG = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round"><rect x="3.5" y="5" width="17" height="16" rx="2"/><path d="M8 3v4M16 3v4M3.5 10h17"/></svg>';

/* ── 下拉选择 ─────────────────────────────────────────────
 * 替代原生 <select>：触发按钮 + 列表弹层，键盘 ↑↓/Enter/Esc 可用。 */
export function createDropdown({ options = [], value = null, onChange = null,
                                 label = '', placeholder = '请选择' } = {}) {
  let opts = (options || []).slice();
  let val = value;
  let open = false;
  let activeIdx = -1;

  const root = h('div', 'sxw-dd');
  if (label) { root.setAttribute('role', 'group'); root.setAttribute('aria-label', label); }
  const btn = h('button', 'sxw-dd-btn');
  btn.type = 'button';
  btn.setAttribute('aria-haspopup', 'listbox');
  btn.setAttribute('aria-expanded', 'false');
  const txt = h('span', 'sxw-dd-txt');
  const caret = h('span', 'sxw-dd-caret', '▾');
  btn.append(txt, caret);
  const pop = h('div', 'sxw-pop sxw-dd-pop');
  pop.setAttribute('role', 'listbox');
  root.append(btn, pop);

  function labelOf(v) {
    const o = opts.find(o => String(o.value) === String(v));
    return o ? o.label : (v == null || v === '' ? '' : String(v));
  }
  function renderBtn() {
    const empty = val == null || val === '';
    txt.textContent = empty ? placeholder : labelOf(val);
    txt.classList.toggle('is-empty', empty);
  }
  function renderPop() {
    pop.replaceChildren();
    opts.forEach((o, i) => {
      const on = String(o.value) === String(val);
      const item = h('div', 'sxw-opt' + (on ? ' on' : '') + (i === activeIdx ? ' focus' : ''),
                     o.label);
      item.setAttribute('role', 'option');
      item.setAttribute('aria-selected', String(on));
      item.addEventListener('click', () => { setValue(o.value, true); close(); btn.focus(); });
      pop.appendChild(item);
    });
  }
  function onDocDown(ev) { if (!root.contains(ev.target)) close(); }
  function onDocKey(ev) {
    if (ev.key === 'Escape') { close(); ev.preventDefault(); return; }
    if (ev.key === 'ArrowDown' || ev.key === 'ArrowUp') {
      ev.preventDefault();
      if (!opts.length) return;
      activeIdx = ev.key === 'ArrowDown'
        ? Math.min(opts.length - 1, activeIdx + 1)
        : Math.max(0, activeIdx - 1);
      renderPop();
      const t = pop.children[activeIdx];
      if (t) t.scrollIntoView({ block: 'nearest' });
    } else if (ev.key === 'Enter') {
      ev.preventDefault();
      if (activeIdx >= 0 && activeIdx < opts.length) { setValue(opts[activeIdx].value, true); close(); }
    }
  }
  function openPop() {
    if (open) return;
    open = true;
    activeIdx = Math.max(0, opts.findIndex(o => String(o.value) === String(val)));
    root.classList.add('open');
    btn.setAttribute('aria-expanded', 'true');
    renderPop();
    document.addEventListener('pointerdown', onDocDown, true);
    document.addEventListener('keydown', onDocKey, true);
  }
  function close() {
    if (!open) return;
    open = false;
    root.classList.remove('open');
    btn.setAttribute('aria-expanded', 'false');
    document.removeEventListener('pointerdown', onDocDown, true);
    document.removeEventListener('keydown', onDocKey, true);
  }
  btn.addEventListener('click', () => (open ? close() : openPop()));

  /* onChange 从实例上读取（api.onChange），页面可以在构造之后再赋值/换绑 */
  function setValue(v, fire) {
    val = v;
    renderBtn();
    if (fire && api.onChange) api.onChange(val);
  }

  const api = {
    onChange: onChange || null,
    el: root,
    get value() { return val; },
    set value(v) { setValue(v, false); },
    get options() { return opts.slice(); },
    /** 替换选项；当前值不在新选项里时回落到第一项 */
    set options(list) {
      opts = (list || []).slice();
      if (!opts.some(o => String(o.value) === String(val))) {
        val = opts.length ? opts[0].value : null;
      }
      renderBtn();
      if (open) renderPop();
    },
    focus() { btn.focus(); },
    destroy() { close(); },
  };
  renderBtn();
  return api;
}

/* ── 日期选择 ─────────────────────────────────────────────
 * 替代原生 <input type="date">：按钮 + 自绘月历弹层。
 * 值与原生一致使用 'YYYY-MM-DD'，空值用 null 表示。 */
export function createDatePicker({ value = null, onChange = null,
                                   placeholder = '选择日期', label = '',
                                   clearText = '清除', todayText = '今天' } = {}) {
  let val = value || null;              // 'YYYY-MM-DD' | null
  let view = null;                      // 弹层当前展示的 {y, m}
  let open = false;

  const root = h('div', 'sxw-date');
  if (label) root.setAttribute('aria-label', label);
  const btn = h('button', 'sxw-date-btn');
  btn.type = 'button';
  btn.setAttribute('aria-haspopup', 'dialog');
  btn.setAttribute('aria-expanded', 'false');
  const ico = h('span', 'sxw-date-ico');
  ico.innerHTML = CAL_SVG;
  const txt = h('span', 'sxw-date-txt');
  btn.append(ico, txt);

  const prevBtn = h('button', 'sxw-cal-nav', '‹'); prevBtn.type = 'button'; prevBtn.title = '上个月';
  const nextBtn = h('button', 'sxw-cal-nav', '›'); nextBtn.type = 'button'; nextBtn.title = '下个月';
  const title = h('span', 'sxw-cal-title');
  const head = h('div', 'sxw-cal-head');
  head.append(prevBtn, title, nextBtn);

  const week = h('div', 'sxw-cal-week');
  for (const c of ['日', '一', '二', '三', '四', '五', '六']) week.appendChild(h('span', '', c));

  const grid = h('div', 'sxw-cal-grid');

  const clearBtn = h('button', 'sxw-cal-link', clearText); clearBtn.type = 'button';
  const todayBtn = h('button', 'sxw-cal-link', todayText); todayBtn.type = 'button';
  const foot = h('div', 'sxw-cal-foot');
  foot.append(clearBtn, todayBtn);

  const pop = h('div', 'sxw-pop sxw-cal');
  pop.setAttribute('role', 'dialog');
  pop.append(head, week, grid, foot);
  root.append(btn, pop);

  function renderBtn() {
    const empty = !val;
    txt.textContent = empty ? placeholder : fmtDay(val);
    txt.classList.toggle('is-empty', empty);
  }
  function renderGrid() {
    if (!view) {
      const base = parseDay(val) || new Date();
      view = { y: base.getFullYear(), m: base.getMonth() };
    }
    title.textContent = `${view.y} 年 ${view.m + 1} 月`;
    const first = new Date(view.y, view.m, 1);
    const start = new Date(view.y, view.m, 1 - first.getDay());
    const today = new Date();
    grid.replaceChildren();
    for (let i = 0; i < 42; i++) {
      const d = new Date(start.getFullYear(), start.getMonth(), start.getDate() + i);
      const out = d.getMonth() !== view.m;
      const iso = `${d.getFullYear()}-${PAD(d.getMonth() + 1)}-${PAD(d.getDate())}`;
      const cell = h('button', 'sxw-cal-day'
        + (out ? ' out' : '')
        + (iso === val ? ' sel' : '')
        + (d.getFullYear() === today.getFullYear() && d.getMonth() === today.getMonth()
           && d.getDate() === today.getDate() ? ' today' : ''),
        String(d.getDate()));
      cell.type = 'button';
      cell.title = iso;
      cell.addEventListener('click', () => { setValue(iso, true); close(); });
      grid.appendChild(cell);
    }
  }
  function onDocDown(ev) { if (!root.contains(ev.target)) close(); }
  function onDocKey(ev) { if (ev.key === 'Escape') { close(); ev.preventDefault(); } }
  function openPop() {
    if (open) return;
    open = true;
    view = null;                          // 每次打开回到已选值（或今天）所在月份
    root.classList.add('open');
    btn.setAttribute('aria-expanded', 'true');
    renderGrid();
    document.addEventListener('pointerdown', onDocDown, true);
    document.addEventListener('keydown', onDocKey, true);
  }
  function close() {
    if (!open) return;
    open = false;
    root.classList.remove('open');
    btn.setAttribute('aria-expanded', 'false');
    document.removeEventListener('pointerdown', onDocDown, true);
    document.removeEventListener('keydown', onDocKey, true);
  }
  btn.addEventListener('click', () => (open ? close() : openPop()));
  prevBtn.addEventListener('click', () => { view = view ? shift(view, -1) : null; renderGrid(); });
  nextBtn.addEventListener('click', () => { view = view ? shift(view, 1) : null; renderGrid(); });
  todayBtn.addEventListener('click', () => {
    const t = new Date();
    setValue(`${t.getFullYear()}-${PAD(t.getMonth() + 1)}-${PAD(t.getDate())}`, true);
    close();
  });
  clearBtn.addEventListener('click', () => { setValue(null, true); close(); });

  function shift(v, delta) {
    return { y: v.y + Math.floor((v.m + delta) / 12), m: (v.m + delta + 12) % 12 };
  }
  /* onChange 从实例上读取（api.onChange），页面可以在构造之后再赋值/换绑 */
  function setValue(v, fire) {
    val = v || null;
    renderBtn();
    if (fire && api.onChange) api.onChange(val);
  }

  const api = {
    onChange: onChange || null,
    el: root,
    get value() { return val; },
    set value(v) { setValue(v, false); if (open) renderGrid(); },
    focus() { btn.focus(); },
    destroy() { close(); },
  };
  renderBtn();
  return api;
}

/* ── 轻量弹出菜单（右键 / 长按触发）──────────────────────────
 * openMenu(items, x, y) → 关闭函数。
 * items: [{ label, onClick?, disabled?, note? }]，note 仅作说明文字。 */
export function openMenu(items, x, y) {
  closeMenu();
  const menu = h('div', 'sxw-menu');
  menu.setAttribute('role', 'menu');
  for (const it of items || []) {
    if (it.note) { menu.appendChild(h('div', 'sxw-menu-note', it.note)); continue; }
    const b = h('button', '', it.label);
    b.type = 'button';
    b.setAttribute('role', 'menuitem');
    if (it.disabled) b.disabled = true;
    b.addEventListener('click', () => { closeMenu(); it.onClick && it.onClick(); });
    menu.appendChild(b);
  }
  document.body.appendChild(menu);
  // 先挂载量尺寸，再防止贴出视口
  const r = menu.getBoundingClientRect();
  const vw = document.documentElement.clientWidth;
  const vh = document.documentElement.clientHeight;
  menu.style.left = `${Math.max(8, Math.min(x, vw - r.width - 8))}px`;
  menu.style.top = `${Math.max(8, Math.min(y, vh - r.height - 8))}px`;
  const onDocDown = (ev) => { if (!menu.contains(ev.target)) closeMenu(); };
  const onDocKey = (ev) => { if (ev.key === 'Escape') { closeMenu(); ev.preventDefault(); } };
  const onScroll = () => closeMenu();
  setTimeout(() => {                    // 等触发事件冒泡完再监听，避免立即自关
    document.addEventListener('pointerdown', onDocDown, true);
    document.addEventListener('keydown', onDocKey, true);
    window.addEventListener('scroll', onScroll, true);
    window.addEventListener('resize', onScroll);
  }, 0);
  window.__sxwMenuClose = () => {
    menu.remove();
    document.removeEventListener('pointerdown', onDocDown, true);
    document.removeEventListener('keydown', onDocKey, true);
    window.removeEventListener('scroll', onScroll, true);
    window.removeEventListener('resize', onScroll);
    window.__sxwMenuClose = null;
  };
  return closeMenu;
}

export function closeMenu() {
  if (typeof window.__sxwMenuClose === 'function') window.__sxwMenuClose();
}
