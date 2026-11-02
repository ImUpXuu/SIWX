(function(){
let generation=0;
/* 朋友圈页（Vue 3 全局构建版，无构建链）。
 *
 * 架构：
 *   - 模板就是 pages/sns.html —— init() 用「容器 innerHTML 即模板」挂载 Vue，
 *     保持既有的 hash 路由 + 模块化加载契约（init(view) / destroy()）不变；
 *   - 纯函数区保持导出（relTime/monthCells/sortFriends/…），便于脚本化验证；
 *   - 响应式状态集中在 store，子组件（sns-post / sns-ava）共享；
 *   - 好友列表到达后昵称/头像自动刷新（Vue 响应式，不再需要 rerenderList 补丁）；
 *   - 下拉/菜单用 widgets.js 的自绘控件（sx-dd 包装组件桥接 Vue 状态，
 *     「···」操作菜单走 openMenu），不再混用原生 select 与手写菜单。
 */
const {createDropdown,openMenu}=window.SnsWidgets;

const { fetchJSON, copyText, fmtTs, fmtDate } = window.SX;
const pad = n => String(n).padStart(2, '0');
const nowSec = () => Math.floor(Date.now() / 1000);

/* ══ 纯函数区（不碰 DOM，便于脚本化验证）══════════════════════ */

/** 微信式相对时间：刚刚 / N分钟前 / N小时前 / 昨天 HH:mm / M月D日 / YYYY年M月D日 */
function relTime(ts, now) {
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

/** 完整时间（详情弹层 / title 提示用）：与全站 fmtTs 同一份 YYYY-MM-DD HH:MM */
function fullTime(ts) {
  return fmtTs(ts);
}

/** 日历网格（周日起始，固定 6 行 42 格）；ts 为**本地零点**的 unix 秒 */
function monthCells(year, month) {
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
function rangeFromKey(key, now) {
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
function pinyinCompare(a, b) {
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
function sortFriends(list, mode) {
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
function matchFriend(f, q) {
  const s = String(q || '').trim().toLowerCase();
  if (!s) return true;
  return `${f.display || ''} ${f.username || ''}`.toLowerCase().includes(s);
}

/** 九宫格列数：1 张大图，2 张 2 列，3 张 3 列，4 张 2×2，其余 3 列 */
function mediaCols(n) {
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
function isCdnMedia(url) {
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

function reasonText(reason) {
  return REASON_TEXT[reason] || reason || '未知原因';
}

const MEDIA_FAIL_HINT = '微信 CDN 上已经没有这个资源（原图可能被清理或已过期）。'
  + '详细原因见「运行日志」里的 [sns-media] 记录。';

const QUICK_RANGES = [
  { key: 'all', label: '全部' },
  { key: 'today', label: '今天' },
  { key: '7', label: '最近 7 天' },
  { key: '30', label: '最近 30 天' },
  { key: '90', label: '最近 90 天' },
  { key: '365', label: '最近一年' },
  { key: 'thisyear', label: '今年' },
];

/* 发布者排序 / 导出格式下拉选项（与原 select 的 option 一一对应） */
const SORT_OPTIONS = [
  { value: 'count-desc', label: '发送量 多 → 少' },
  { value: 'count-asc', label: '发送量 少 → 多' },
  { value: 'name-asc', label: '名称拼音 A → Z' },
  { value: 'name-desc', label: '名称拼音 Z → A' },
  { value: 'recent-desc', label: '最近发布' },
  { value: 'oldest-asc', label: '最早发布' },
];
const FMT_OPTIONS = [
  { value: 'json', label: 'JSON' },
  { value: 'markdown', label: 'Markdown' },
  { value: 'html', label: 'HTML' },
  { value: 'txt', label: 'TXT' },
];

/* ══ 响应式共享状态 ═══════════════════════════════════════════ */

const store = Vue.reactive({
  account: '',
  accounts: [],                // /api/sns/accounts：用于校验全局账号有无朋友圈库
  friends: [],
  friendsLoaded: false,
  friendsLoading: false,
  friendsError: '',

  timeline: [],
  before: null,
  hasMore: false,
  loading: false,
  emptyText: '没有符合条件的动态',
  notice: '',

  kwInput: '',
  keyword: '',
  username: '',
  start: null,
  end: null,
  rangeKey: 'all',
  rangeLabel: '全部时间',

  pops: { range: false, user: false, export: false },
  draft: { start: null, end: null },
  cal: { y: 0, m: 0, jump: null },
  userQuery: '',
  userSort: 'count-desc',

  lightbox: { visible: false, src: '', video: false, items: [], index: 0 },
  modal: { visible: false, post: null, error: '' },
  meAvaOk: false,

  exportFmt: 'json',
  exportMedia: false,
  exportConc: 5,
  exportState: 'idle',     // idle | running | ok | error
  exportProgress: '',
  exportResult: null,
  exportError: '',
});

const friendMap = Vue.computed(() => {
  const m = new Map();
  for (const f of store.friends) m.set(f.username, f);
  return m;
});

function nameOf(username) {
  const f = friendMap.value.get(username);
  return (f && f.display) || username || '';
}
/** 昵称优先，其次回退（评论里 XML 自带 nickname） */
function displayOf(username, fallback) {
  const f = friendMap.value.get(username);
  return (f && f.display) || fallback || username || '';
}
function avaUrl(username) {
  return `/api/chat/avatar?account=${encodeURIComponent(store.account)}&username=${encodeURIComponent(username)}`;
}
function mediaUrl(m) {
  const q = new URLSearchParams({ url: m.url || '' });
  return `/api/sns/media?${q}`;
}
function proxyUrl(url) {
  return `/api/sns/media?${new URLSearchParams({ account: store.account, url: url || '' })}`;
}
function emojiUrl(e) { return `/api/sns/media?${new URLSearchParams({ url: e.url || '' })}`; }

function fmtDur(sec) {
  const n = Math.floor(Number(sec) || 0);
  if (n <= 0) return '';
  return `${Math.floor(n / 60)}:${pad(n % 60)}`;
}

/* 轻反馈走全局 toast（壳层统一体系）；store.notice 只承载常驻提示
   （如"本地朋友圈图片只包含已下载资源"这类说明性文案） */
function toast(text, err) {
  window.SX.toast(text, err);
}

/* ── 灯箱翻页（模块级：卡片组件与页面级键盘导航共用）────────── */
function lbSync() {
  const lb = store.lightbox;
  const cur = lb.items[lb.index];
  if (cur) { lb.src = cur.src; lb.video = cur.video; }
}
function lbStep(delta) {
  const lb = store.lightbox;
  const next = lb.index + delta;
  if (next < 0 || next >= lb.items.length) return;
  lb.index = next;
  lbSync();
}

/** 媒体加载失败：**必须可见**（以前是静默隐藏，用户只看到空白） */
function onMediaErrorEl(el, isVideo) {
  const cell = el.closest && el.closest('.sns-media-cell');
  el.remove();
  if (cell && !cell.querySelector('.sns-media-bad')) {
    const bad = document.createElement('span');
    bad.className = 'sns-media-bad';
    bad.title = MEDIA_FAIL_HINT;
    bad.textContent = (isVideo ? '视频' : '图片') + '加载失败';
    cell.appendChild(bad);
  }
}

/* ══ 头像组件（首字母色块 + 有头像时才请求图片）══════════════ */

const SnsAva = {
  name: 'sns-ava',
  props: { username: { type: String, default: '' }, display: { type: String, default: '' } },
  computed: {
    f() { return friendMap.value.get(this.username); },
    initial() {
      return String(this.display || this.username || '?').trim().slice(0, 1).toUpperCase() || '?';
    },
    hasAvatar() { return !!(this.f && this.f.has_avatar); },
    src() { return avaUrl(this.username); },
  },
  render: (()=>{const _Vue = Vue

return function render(_ctx, _cache) {
  with (_ctx) {
    const { toDisplayString: _toDisplayString, createElementVNode: _createElementVNode, openBlock: _openBlock, createElementBlock: _createElementBlock, createCommentVNode: _createCommentVNode } = _Vue

    return (_openBlock(), _createElementBlock("span", { class: "ava" }, [_createElementVNode("span", null, _toDisplayString(initial), 1), hasAvatar
      ? (_openBlock(), _createElementBlock("img", {
          key: 0,
          src: src,
          alt: "",
          loading: "lazy",
          onError: $event => ($event.target.remove())
        }, null, 40, ["src", "onError"]))
      : _createCommentVNode("", true)]))
  }
}})(),
};

/* ══ 自绘下拉的 Vue 桥（widgets.createDropdown）══════════════
 * 页内不再用原生 select（弹出面板无法跟随主题）。组件只渲染一个挂载点，
 * 命令式控件在 mounted 里创建；值经 v-model 双向同步，options 变化整体
 * 下发。销毁时交还 widgets（收起弹层、摘除文档级监听）。 */
const SxDd = {
  name: 'sx-dd',
  props: {
    options: { type: Array, default: () => [] },
    modelValue: { default: null },
    label: { type: String, default: '' },
    placeholder: { type: String, default: '' },
  },
  emits: ['update:modelValue'],
  watch: {
    modelValue(v) {
      if (this._dd && String(this._dd.value) !== String(v ?? null)) this._dd.value = v;
    },
    options(list) { if (this._dd) this._dd.options = list; },
  },
  mounted() {
    this._dd = createDropdown({
      options: this.options,
      value: this.modelValue,
      label: this.label,
      placeholder: this.placeholder || '请选择',
      onChange: v => this.$emit('update:modelValue', v),
    });
    this.$refs.mount.appendChild(this._dd.el);
  },
  beforeUnmount() {
    if (this._dd) { this._dd.destroy(); this._dd = null; }
  },
  render: (()=>{const _Vue = Vue

return function render(_ctx, _cache) {
  with (_ctx) {
    const { openBlock: _openBlock, createElementBlock: _createElementBlock } = _Vue

    return (_openBlock(), _createElementBlock("span", {
      class: "sx-dd-mount",
      ref: "mount"
    }, null, 512))
  }
}})(),
};

/* ══ 动态卡片组件（正文/媒体/卡片/互动/操作菜单）══════════════ */

const SnsPost = {
  name: 'sns-post',
  components: { 'sns-ava': SnsAva },
  props: {
    post: { type: Object, required: true },
    compact: { type: Boolean, default: true },
  },
  computed: {
    uname() { return this.post.user_name || this.post.username || ''; },
    display() { return nameOf(this.uname); },
    tid() { return String(this.post.tid); },
    items() { return (this.post.medias || []).filter(m => m.url && isCdnMedia(m.url)); },
    gridCls() { return mediaCols(this.items.length); },
    likes() { return this.post.likes || []; },
    comments() { return this.post.comments || []; },
    shownComments() { return this.compact ? this.comments.slice(-20) : this.comments; },
    hiddenComments() { return this.compact ? Math.max(0, this.comments.length - 20) : 0; },
    card() { return this.post.card || null; },
    coverUrl() {
      const raw = ((this.card || {}).cover || '').trim();
      return raw ? proxyUrl(raw) : '';
    },
    finderVideoUrl() {
      const c = this.card || {};
      if (c.kind !== 'finder') return '';
      const f = c.finder || {};
      const vid = ((f.video_url || '').trim() || (((f.media || [])[0] || {}).url || '')).trim();
      return vid ? proxyUrl(vid) : '';
    },
  },
  methods: {
    relTime, fullTime, mediaUrl, emojiUrl, displayOf, fmtDur,
    mediaFail(ev, isVideo) { onMediaErrorEl(ev.target, isVideo); },
    /** 灯箱以整条动态的媒体列表为画廊：点开任意一张即可 ←/→ 翻页 */
    openMedia(ev, m) {
      ev.preventDefault();
      const list = this.items.map(x => ({
        // 实况照片点开播放的是视频流，翻页时保持同样的取流规则
        src: (x.live_photo && x.live_photo.url) ? mediaUrl(x.live_photo) : mediaUrl(x),
        video: !!(x.type === 6 || (x.live_photo && x.live_photo.url)),
      }));
      store.lightbox = {
        visible: true, items: list,
        index: Math.max(0, this.items.indexOf(m)),
        src: '', video: false,
      };
      lbSync();
    },
    /** 视频号封面点击：只看大图。不做播放——实测 CDN 拉回的视频流是加密
     *  mp4（finderFeed 不带媒体密钥），且落库链接数小时后签名过期（400），
     *  "点击播放"是兑现不了的承诺（2026-10-06 排查）。 */
    openCoverImage(url) {
      if (url) store.lightbox = { visible: true, src: url, video: false, items: [], index: 0 };
    },
    openDetail() {
      const tid = this.tid;
      store.modal = { visible: true, post: null, error: '', tid };
      fetchJSON(`/api/sns/detail?account=${encodeURIComponent(store.account)}&tid=${encodeURIComponent(tid)}`)
        .then(d => {
          // 连点「详情」/切换目标时只应用当前打开的那一条
          if (store.modal.visible && store.modal.tid === tid) store.modal.post = d.post;
        })
        .catch(e => {
          if (store.modal.visible && store.modal.tid === tid) store.modal.error = e.message;
        });
    },
    copyText_() {
      copyText(this.post.content_desc || '')
        .then(ok => toast(ok ? '已复制文字' : '复制失败'));
    },
    copyId() {
      copyText(this.tid).then(ok => toast(ok ? '已复制动态 ID' : '复制失败'));
    },
    /** 「···」操作菜单：走 widgets.openMenu（点外部/Esc/滚动自动收起，
     *  与聊天页消息右键菜单同一套交互）；锚点右对齐贴住按钮。 */
    openOpsMenu(ev) {
      const r = ev.currentTarget.getBoundingClientRect();
      openMenu([
        { label: '查看详情', onClick: () => this.openDetail() },
        { label: '复制文字', onClick: () => this.copyText_() },
        { label: '复制动态 ID', onClick: () => this.copyId() },
      ], r.right, r.bottom + 4, { align: 'right' });
    },
  },
  render: (()=>{const _Vue = Vue

return function render(_ctx, _cache) {
  with (_ctx) {
    const { resolveComponent: _resolveComponent, createVNode: _createVNode, toDisplayString: _toDisplayString, createElementVNode: _createElementVNode, openBlock: _openBlock, createElementBlock: _createElementBlock, createCommentVNode: _createCommentVNode, Fragment: _Fragment, renderList: _renderList, normalizeClass: _normalizeClass, createTextVNode: _createTextVNode } = _Vue

    const _component_sns_ava = _resolveComponent("sns-ava")

    return (_openBlock(), _createElementBlock("article", { class: "sns-post" }, [_createVNode(_component_sns_ava, {
      username: uname,
      display: display
    }, null, 8, ["username", "display"]), _createElementVNode("div", { class: "sns-main" }, [
      _createElementVNode("div", { class: "sns-name" }, _toDisplayString(display), 1),
      (post.content_desc)
        ? (_openBlock(), _createElementBlock("div", {
            key: 0,
            class: "sns-text"
          }, _toDisplayString(post.content_desc), 1))
        : _createCommentVNode("", true),
      card
        ? (_openBlock(), _createElementBlock(_Fragment, { key: 1 }, [(card.kind === 'music')
            ? (_openBlock(), _createElementBlock("a", {
                key: 0,
                class: "sns-card sns-card--music",
                href: card.url,
                target: "_blank",
                rel: "noreferrer"
              }, [_createElementVNode("div", { class: "sns-card-body" }, [_createElementVNode("div", { class: "sns-card-tag" }, "音乐"), _createElementVNode("div", { class: "sns-card-title" }, _toDisplayString((card.music && card.music.album) || card.title || '音乐'), 1), _createElementVNode("div", { class: "sns-card-sub" }, _toDisplayString(((card.music && card.music.singer) || card.description || '') + (card.music && card.music.duration_ms ? ' · ' + fmtDur(card.music.duration_ms / 1000) : '')), 1)])], 8, ["href"]))
            : (card.kind === 'finder')
              ? (_openBlock(), _createElementBlock("div", {
                  key: 1,
                  class: "sns-card sns-card--finder"
                }, [coverUrl
                  ? (_openBlock(), _createElementBlock("img", {
                      key: 0,
                      class: "sns-card-cover",
                      loading: "lazy",
                      src: coverUrl,
                      alt: "视频号封面",
                      title: "点击查看封面大图",
                      onClick: $event => (openCoverImage(coverUrl)),
                      onError: $event => ($event.target.remove())
                    }, null, 40, ["src", "onClick", "onError"]))
                  : _createCommentVNode("", true), _createElementVNode("div", { class: "sns-card-body" }, [
                  _createElementVNode("div", { class: "sns-card-tag" }, "视频号"),
                  _createElementVNode("div", { class: "sns-card-title" }, _toDisplayString(card.finder && card.finder.nickname), 1),
                  _createElementVNode("div", { class: "sns-card-sub" }, _toDisplayString([(card.finder && card.finder.media_count ? card.finder.media_count + ' 个作品' : ''), fmtDur(card.duration)].filter(Boolean).join(' · ')), 1),
                  _createElementVNode("div", { class: "sns-card-desc" }, _toDisplayString(card.description || ''), 1)
                ])]))
              : (card.kind === 'live')
                ? (_openBlock(), _createElementBlock("div", {
                    key: 2,
                    class: "sns-card sns-card--live"
                  }, [coverUrl
                    ? (_openBlock(), _createElementBlock("img", {
                        key: 0,
                        class: "sns-card-cover",
                        loading: "lazy",
                        src: coverUrl,
                        alt: "直播封面",
                        onError: $event => ($event.target.remove())
                      }, null, 40, ["src", "onError"]))
                    : _createCommentVNode("", true), _createElementVNode("div", { class: "sns-card-body" }, [_createElementVNode("div", { class: "sns-card-tag" }, "视频号直播"), _createElementVNode("div", { class: "sns-card-title" }, _toDisplayString(card.live && card.live.nickname), 1), _createElementVNode("div", { class: "sns-card-desc" }, _toDisplayString(card.live && card.live.desc), 1)])]))
                : (card.kind === 'note')
                  ? (_openBlock(), _createElementBlock("div", {
                      key: 3,
                      class: "sns-card sns-card--note"
                    }, [_createElementVNode("div", { class: "sns-card-body" }, [_createElementVNode("div", { class: "sns-card-tag" }, "笔记"), _createElementVNode("div", { class: "sns-card-desc" }, _toDisplayString((card.note && card.note.text) || card.title || ''), 1)])]))
                  : (_openBlock(), _createElementBlock("a", {
                      key: 4,
                      class: "sns-card sns-card--link",
                      href: card.url,
                      target: "_blank",
                      rel: "noreferrer"
                    }, [_createElementVNode("div", { class: "sns-card-body" }, [
                      _createElementVNode("div", { class: "sns-card-tag" }, "链接"),
                      _createElementVNode("div", { class: "sns-card-title" }, _toDisplayString(card.title || card.url || ''), 1),
                      _createElementVNode("div", { class: "sns-card-sub" }, _toDisplayString(card.source || ''), 1),
                      _createElementVNode("div", { class: "sns-card-desc" }, _toDisplayString(card.description || ''), 1)
                    ])], 8, ["href"]))], 64))
        : _createCommentVNode("", true),
      (items.length)
        ? (_openBlock(), _createElementBlock("div", {
            key: 2,
            class: _normalizeClass(["sns-media", gridCls])
          }, [(_openBlock(true), _createElementBlock(_Fragment, null, _renderList(items, (m, i) => {
            return (_openBlock(), _createElementBlock("span", {
              key: i,
              class: "sns-media-cell"
            }, [(m.type === 6)
              ? (_openBlock(), _createElementBlock("video", {
                  key: 0,
                  class: "sns-vid",
                  controls: "",
                  preload: "metadata",
                  src: mediaUrl(m),
                  onError: $event => (mediaFail($event, true))
                }, null, 40, ["src", "onError"]))
              : (_openBlock(), _createElementBlock(_Fragment, { key: 1 }, [_createElementVNode("img", {
                  class: "sns-lb",
                  loading: "lazy",
                  src: mediaUrl(m),
                  alt: "朋友圈图片",
                  title: m.live_photo && m.live_photo.url ? '实况照片：点击播放' : '',
                  onClick: $event => (openMedia($event, m)),
                  onError: $event => (mediaFail($event, false))
                }, null, 40, ["src", "title", "onClick", "onError"]), (m.live_photo && m.live_photo.url)
                  ? (_openBlock(), _createElementBlock("span", {
                      key: 0,
                      class: "live-badge"
                    }, "实况"))
                  : _createCommentVNode("", true)], 64))]))
          }), 128))], 2))
        : _createCommentVNode("", true),
      (post.location)
        ? (_openBlock(), _createElementBlock("div", {
            key: 3,
            class: "sns-loc"
          }, "📍 " + _toDisplayString(post.location.name || '') + _toDisplayString(post.location.address ? ' · ' + post.location.address : ''), 1))
        : _createCommentVNode("", true),
      _createElementVNode("div", { class: "sns-foot" }, [_createElementVNode("span", {
        class: "sns-time",
        title: fullTime(post.ts)
      }, _toDisplayString(relTime(post.ts)), 9, ["title"]), compact
        ? (_openBlock(), _createElementBlock("span", {
            key: 0,
            class: "sns-detail-link",
            onClick: openDetail
          }, "详情", 8, ["onClick"]))
        : (_openBlock(), _createElementBlock("span", {
            key: 1,
            class: "sns-time"
          }, "· 赞 " + _toDisplayString(likes.length) + " · 评论 " + _toDisplayString(comments.length), 1)), _createElementVNode("span", { class: "sns-ops" }, [_createElementVNode("button", {
        class: "sns-ops-btn",
        title: "更多操作",
        onClick: openOpsMenu
      }, "···", 8, ["onClick"])])]),
      (likes.length || comments.length)
        ? (_openBlock(), _createElementBlock("div", {
            key: 4,
            class: "sns-inter"
          }, [(likes.length)
            ? (_openBlock(), _createElementBlock("div", {
                key: 0,
                class: "sns-likes"
              }, [_createElementVNode("span", { class: "ic" }, "❤"), (_openBlock(true), _createElementBlock(_Fragment, null, _renderList(likes, (x, i) => {
                return (_openBlock(), _createElementBlock(_Fragment, { key: i }, [i
                  ? (_openBlock(), _createElementBlock("span", {
                      key: 0,
                      class: "sep"
                    }, "，"))
                  : _createCommentVNode("", true), _createTextVNode(_toDisplayString(displayOf(x.username, x.nickname)), 1)], 64))
              }), 128))]))
            : _createCommentVNode("", true), (comments.length)
            ? (_openBlock(), _createElementBlock("div", {
                key: 1,
                class: _normalizeClass(["sns-comments", { 'has-like': likes.length }])
              }, [(_openBlock(true), _createElementBlock(_Fragment, null, _renderList(shownComments, (c, i) => {
                return (_openBlock(), _createElementBlock("div", {
                  key: i,
                  class: "sns-comment"
                }, [
                  _createElementVNode("b", null, _toDisplayString(displayOf(c.username, c.nickname)), 1),
                  _createTextVNode("："),
                  (c.ref_username)
                    ? (_openBlock(), _createElementBlock("span", {
                        key: 0,
                        class: "dim"
                      }, "回复 " + _toDisplayString(displayOf(c.ref_username, '')), 1))
                    : _createCommentVNode("", true),
                  _createTextVNode(_toDisplayString(c.content || ''), 1),
                  (_openBlock(true), _createElementBlock(_Fragment, null, _renderList((c.emojis || []), (e, j) => {
                    return (_openBlock(), _createElementBlock("img", {
                      key: 'e' + j,
                      class: "cm-emoji",
                      loading: "lazy",
                      src: emojiUrl(e),
                      alt: "表情",
                      onError: $event => ($event.target.remove())
                    }, null, 40, ["src", "onError"]))
                  }), 128)),
                  (_openBlock(true), _createElementBlock(_Fragment, null, _renderList((c.images || []).filter(x => x.url), (im, j) => {
                    return (_openBlock(), _createElementBlock("img", {
                      key: 'i' + j,
                      class: "cm-img",
                      loading: "lazy",
                      src: mediaUrl(im),
                      alt: "评论图片",
                      onError: $event => ($event.target.remove())
                    }, null, 40, ["src", "onError"]))
                  }), 128))
                ]))
              }), 128)), hiddenComments
                ? (_openBlock(), _createElementBlock("div", {
                    key: 0,
                    class: "sns-more-comments"
                  }, "还有 " + _toDisplayString(hiddenComments) + " 条评论，点「详情」查看", 1))
                : _createCommentVNode("", true)], 2))
            : _createCommentVNode("", true)]))
        : _createCommentVNode("", true)
    ])]))
  }
}})(),
};

/* ══ 页面组件 ═════════════════════════════════════════════════ */

let _app = null;
let _pollTimer = null;
let _pollFailStreak = 0;      // 导出轮询连续失败计数（后端死亡时退出 running，不再永久"导出中…"）
let _docClick = null;
let _keyDown = null;
let _kwTimer = null;
let _io = null;

const SnsPage = {
  render: (()=>{const _Vue = Vue

return function render(_ctx, _cache) {
  with (_ctx) {
    const { createElementVNode: _createElementVNode, toDisplayString: _toDisplayString, openBlock: _openBlock, createElementBlock: _createElementBlock, createCommentVNode: _createCommentVNode, vModelText: _vModelText, withDirectives: _withDirectives, vShow: _vShow, normalizeClass: _normalizeClass, resolveComponent: _resolveComponent, createBlock: _createBlock, renderList: _renderList, Fragment: _Fragment, createVNode: _createVNode, createTextVNode: _createTextVNode, vModelCheckbox: _vModelCheckbox, withModifiers: _withModifiers } = _Vue

    const _component_sns_ava = _resolveComponent("sns-ava")
    const _component_sx_dd = _resolveComponent("sx-dd")
    const _component_sns_post = _resolveComponent("sns-post")

    return (_openBlock(), _createElementBlock("div", { class: "sns-page" }, [
      _createElementVNode("header", { class: "sns-hero" }, [_createElementVNode("div", { class: "sns-cover" }), _createElementVNode("div", { class: "sns-hero-row" }, [_createElementVNode("div", { class: "sns-hero-right" }, [_createElementVNode("span", { class: "sns-hero-name" }, _toDisplayString(meName), 1), _createElementVNode("div", { class: "sns-hero-ava" }, [_createElementVNode("span", null, _toDisplayString(meInitial), 1), meAvaOk
        ? (_openBlock(), _createElementBlock("img", {
            key: 0,
            src: avaUrl(account),
            alt: "",
            onError: $event => (meAvaOk = false)
          }, null, 40, ["src", "onError"]))
        : _createCommentVNode("", true)])])])]),
      _createElementVNode("div", { class: "sns-bar" }, [
        _createElementVNode("div", { class: "sns-search" }, [_createElementVNode("span", {
          class: "sns-search-icon",
          "aria-hidden": "true"
        }, [(_openBlock(), _createElementBlock("svg", {
          viewBox: "0 0 24 24",
          fill: "none",
          stroke: "currentColor",
          "stroke-width": "1.8",
          "stroke-linecap": "round"
        }, [_createElementVNode("circle", {
          cx: "11",
          cy: "11",
          r: "7"
        }), _createElementVNode("path", { d: "M20 20l-3.5-3.5" })]))]), _withDirectives(_createElementVNode("input", {
          "onUpdate:modelValue": $event => ((kwInput) = $event),
          placeholder: "搜索朋友圈内容、标题、歌手、视频号…"
        }, null, 8, ["onUpdate:modelValue"]), [[
          _vModelText,
          kwInput,
          void 0,
          { trim: true }
        ]]), _withDirectives(_createElementVNode("button", {
          class: "sns-search-clear",
          title: "清除",
          onClick: clearKeyword
        }, "✕", 8, ["onClick"]), [[_vShow, kwInput]])]),
        _createElementVNode("div", { class: "sns-chips" }, [
          _createElementVNode("button", {
            class: _normalizeClass(["sns-chip", { on: !!rangeOn }]),
            "data-pop": "range",
            "aria-haspopup": "true",
            onClick: $event => (togglePop('range'))
          }, [_createElementVNode("span", {
            class: "sns-chip-icon",
            "aria-hidden": "true"
          }, [(_openBlock(), _createElementBlock("svg", {
            viewBox: "0 0 24 24",
            fill: "none",
            stroke: "currentColor",
            "stroke-width": "1.7",
            "stroke-linecap": "round"
          }, [_createElementVNode("rect", {
            x: "3.5",
            y: "5",
            width: "17",
            height: "15.5",
            rx: "2"
          }), _createElementVNode("path", { d: "M3.5 10h17M8 3v4M16 3v4" })]))]), _createElementVNode("span", null, _toDisplayString(rangeLabel), 1)], 10, ["onClick"]),
          _createElementVNode("button", {
            class: _normalizeClass(["sns-chip sns-chip-user", { on: !!username }]),
            "data-pop": "user",
            "aria-haspopup": "true",
            onClick: $event => (togglePop('user'))
          }, [_withDirectives(_createElementVNode("span", { class: "sns-chip-ava" }, [username
            ? (_openBlock(), _createBlock(_component_sns_ava, {
                key: 0,
                username: username,
                display: userLabel
              }, null, 8, ["username", "display"]))
            : _createCommentVNode("", true)], 512), [[_vShow, username]]), _createElementVNode("span", null, _toDisplayString(userLabel), 1), _createElementVNode("span", { class: "sns-chip-caret" }, "▾")], 10, ["onClick"]),
          _createElementVNode("span", { class: "sns-bar-gap" }),
          _createElementVNode("button", {
            class: "sns-icon-btn",
            title: "刷新",
            "aria-label": "刷新",
            onClick: refresh
          }, [(_openBlock(), _createElementBlock("svg", {
            viewBox: "0 0 24 24",
            fill: "none",
            stroke: "currentColor",
            "stroke-width": "1.8",
            "stroke-linecap": "round",
            "stroke-linejoin": "round"
          }, [_createElementVNode("path", { d: "M21 12a9 9 0 1 1-2.6-6.4" }), _createElementVNode("path", { d: "M21 3v6h-6" })]))], 8, ["onClick"]),
          _createElementVNode("button", {
            class: "sns-icon-btn sns-export-toggle",
            "data-pop": "export",
            title: "导出朋友圈",
            "aria-haspopup": "true",
            onClick: $event => (togglePop('export'))
          }, [(_openBlock(), _createElementBlock("svg", {
            viewBox: "0 0 24 24",
            fill: "none",
            stroke: "currentColor",
            "stroke-width": "1.7",
            "stroke-linecap": "round",
            "stroke-linejoin": "round"
          }, [_createElementVNode("path", { d: "M21 8l-9-5-9 5v8l9 5 9-5z" }), _createElementVNode("path", { d: "M3 8l9 5 9-5M12 13v8" })])), _createElementVNode("span", null, "导出")], 8, ["onClick"]),
          _createElementVNode("span", { class: "sns-meta" }, _toDisplayString(metaText), 1)
        ]),
        _withDirectives(_createElementVNode("div", { class: "sns-pop sns-pop-range" }, [
          _createElementVNode("div", { class: "sns-pop-head" }, [_createElementVNode("b", null, "选择时间范围"), _createElementVNode("button", {
            class: "sns-pop-close",
            title: "关闭",
            onClick: $event => (pops.range = false)
          }, "✕", 8, ["onClick"])]),
          _createElementVNode("div", { class: "sns-quick" }, [(_openBlock(true), _createElementBlock(_Fragment, null, _renderList(quickRanges, (q) => {
            return (_openBlock(), _createElementBlock("button", {
              key: q.key,
              class: _normalizeClass({ on: rangeKey === q.key }),
              onClick: $event => (selectRange(q.key))
            }, _toDisplayString(q.label), 11, ["onClick"]))
          }), 128))]),
          _createElementVNode("div", { class: "sns-cal" }, [_createElementVNode("div", { class: "sns-cal-head" }, [_createElementVNode("button", {
            class: "sns-cal-nav",
            title: "上个月",
            onClick: $event => (shiftMonth(-1))
          }, "‹", 8, ["onClick"]), _createElementVNode("button", {
            class: "sns-cal-title",
            onClick: cycleJump
          }, _toDisplayString(calTitle), 9, ["onClick"]), _createElementVNode("button", {
            class: "sns-cal-nav",
            title: "下个月",
            onClick: $event => (shiftMonth(1))
          }, "›", 8, ["onClick"])]), _createElementVNode("div", { class: "sns-cal-week" }, [
            _createElementVNode("span", null, "日"),
            _createElementVNode("span", null, "一"),
            _createElementVNode("span", null, "二"),
            _createElementVNode("span", null, "三"),
            _createElementVNode("span", null, "四"),
            _createElementVNode("span", null, "五"),
            _createElementVNode("span", null, "六")
          ]), _createElementVNode("div", { class: _normalizeClass(["sns-cal-grid", { jump: !!cal.jump }]) }, [(cal.jump === 'year')
            ? (_openBlock(true), _createElementBlock(_Fragment, { key: 0 }, _renderList(jumpYears, (y) => {
                return (_openBlock(), _createElementBlock("button", {
                  key: y,
                  class: _normalizeClass(["sns-cal-jump-btn", { edge: y === cal.y }]),
                  onClick: $event => {cal.y = y; cal.jump = 'month'}
                }, _toDisplayString(y) + "年", 11, ["onClick"]))
              }), 128))
            : (cal.jump === 'month')
              ? (_openBlock(true), _createElementBlock(_Fragment, { key: 1 }, _renderList(12, (i) => {
                  return (_openBlock(), _createElementBlock("button", {
                    key: i,
                    class: _normalizeClass(["sns-cal-jump-btn", { edge: i - 1 === cal.m }]),
                    onClick: $event => {cal.m = i - 1; cal.jump = null}
                  }, _toDisplayString(i) + "月", 11, ["onClick"]))
                }), 128))
              : (_openBlock(true), _createElementBlock(_Fragment, { key: 2 }, _renderList(calCells, (c, i) => {
                  return (_openBlock(), _createElementBlock("button", {
                    key: i,
                    class: _normalizeClass(calCellClass(c)),
                    onClick: $event => (pickDay(c))
                  }, _toDisplayString(c.d), 11, ["onClick"]))
                }), 128))], 2)]),
          _createElementVNode("div", { class: "sns-pop-foot" }, [_createElementVNode("span", { class: "sns-cal-hint" }, _toDisplayString(calHint), 1), _createElementVNode("button", {
            class: "sns-btn-ghost",
            onClick: clearRange
          }, "清除", 8, ["onClick"]), _createElementVNode("button", {
            class: "sns-btn-primary",
            disabled: !draft.start,
            title: "先选择日期",
            onClick: confirmRange
          }, "确定", 8, ["disabled", "onClick"])])
        ], 512), [[_vShow, pops.range]]),
        _withDirectives(_createElementVNode("div", { class: "sns-pop sns-pop-user" }, [
          _createElementVNode("div", { class: "sns-pop-head" }, [_createElementVNode("b", null, "选择发布者"), _createElementVNode("button", {
            class: "sns-pop-close",
            title: "关闭",
            onClick: $event => (pops.user = false)
          }, "✕", 8, ["onClick"])]),
          _createElementVNode("div", { class: "sns-user-tools" }, [_createElementVNode("div", { class: "sns-search sns-search-sm" }, [_createElementVNode("span", {
            class: "sns-search-icon",
            "aria-hidden": "true"
          }, [(_openBlock(), _createElementBlock("svg", {
            viewBox: "0 0 24 24",
            fill: "none",
            stroke: "currentColor",
            "stroke-width": "1.8",
            "stroke-linecap": "round"
          }, [_createElementVNode("circle", {
            cx: "11",
            cy: "11",
            r: "7"
          }), _createElementVNode("path", { d: "M20 20l-3.5-3.5" })]))]), _withDirectives(_createElementVNode("input", {
            "onUpdate:modelValue": $event => ((userQuery) = $event),
            placeholder: "搜索昵称 / 微信号"
          }, null, 8, ["onUpdate:modelValue"]), [[_vModelText, userQuery]])]), _createVNode(_component_sx_dd, {
            modelValue: userSort,
            "onUpdate:modelValue": $event => ((userSort) = $event),
            options: sortOptions,
            label: "排序方式"
          }, null, 8, ["modelValue", "onUpdate:modelValue", "options"])]),
          _createElementVNode("div", { class: "sns-user-list" }, [_createElementVNode("button", {
            class: _normalizeClass(["sns-user-row", { on: !username }]),
            onClick: $event => (applyUser(''))
          }, [_createElementVNode("span", { class: "ava" }, [_createElementVNode("span", null, "全")]), _createElementVNode("span", { class: "who" }, [_createElementVNode("span", { class: "nm" }, "全部发布者"), _createElementVNode("span", { class: "un" }, "不筛选发送者")]), _createElementVNode("span", { class: "cnt" }, _toDisplayString(friends.length), 1)], 10, ["onClick"]), (_openBlock(true), _createElementBlock(_Fragment, null, _renderList(sortedFriends, (f) => {
            return (_openBlock(), _createElementBlock("button", {
              key: f.username,
              class: _normalizeClass(["sns-user-row", { on: f.username === username }]),
              onClick: $event => (applyUser(f.username))
            }, [_createVNode(_component_sns_ava, {
              username: f.username,
              display: f.display
            }, null, 8, ["username", "display"]), _createElementVNode("span", { class: "who" }, [_createElementVNode("span", { class: "nm" }, _toDisplayString(f.display || f.username), 1), _createElementVNode("span", { class: "un" }, _toDisplayString(f.username), 1)]), _createElementVNode("span", { class: "cnt" }, _toDisplayString(f.count), 1)], 10, ["onClick"]))
          }), 128)), (friendsError && !friends.length)
            ? (_openBlock(), _createElementBlock("div", {
                key: 0,
                class: "sns-user-empty"
              }, [_createTextVNode(" 发布者加载失败：" + _toDisplayString(friendsError), 1), _createElementVNode("br"), _createElementVNode("button", {
                class: "sns-btn-ghost",
                onClick: $event => (ensureFriends(true))
              }, "重试", 8, ["onClick"])]))
            : (!friends.length && friendsLoaded)
              ? (_openBlock(), _createElementBlock("div", {
                  key: 1,
                  class: "sns-user-empty"
                }, "暂无发布者"))
              : _createCommentVNode("", true)]),
          _createElementVNode("div", { class: "sns-pop-foot" }, [_createElementVNode("span", { class: "sns-cal-hint" }, _toDisplayString(userHint), 1), _createElementVNode("button", {
            class: "sns-btn-ghost",
            onClick: $event => (applyUser(''))
          }, "清除筛选", 8, ["onClick"])])
        ], 512), [[_vShow, pops.user]]),
        _withDirectives(_createElementVNode("div", { class: "sns-pop sns-pop-export" }, [
          _createElementVNode("div", { class: "sns-pop-head" }, [_createElementVNode("b", null, "导出朋友圈"), _createElementVNode("button", {
            class: "sns-pop-close",
            title: "关闭",
            onClick: $event => (pops.export = false)
          }, "✕", 8, ["onClick"])]),
          _createElementVNode("div", { class: "sns-export" }, [
            _createVNode(_component_sx_dd, {
              modelValue: exportFmt,
              "onUpdate:modelValue": $event => ((exportFmt) = $event),
              options: fmtOptions,
              label: "导出格式"
            }, null, 8, ["modelValue", "onUpdate:modelValue", "options"]),
            _createElementVNode("label", { class: "f-chk" }, [_withDirectives(_createElementVNode("input", {
              type: "checkbox",
              "onUpdate:modelValue": $event => ((exportMedia) = $event)
            }, null, 8, ["onUpdate:modelValue"]), [[_vModelCheckbox, exportMedia]]), _createTextVNode(" 同时下载媒体（较慢）")]),
            _createElementVNode("label", { class: "sns-conc" }, [_createTextVNode("并发 "), _withDirectives(_createElementVNode("input", {
              type: "number",
              "onUpdate:modelValue": $event => ((exportConc) = $event),
              min: "1",
              max: "16",
              title: "媒体下载并发数（1-16，默认 5）"
            }, null, 8, ["onUpdate:modelValue"]), [[
              _vModelText,
              exportConc,
              void 0,
              { number: true }
            ]])]),
            _createElementVNode("button", {
              class: "sns-btn-primary",
              disabled: exportState === 'running',
              onClick: doExport
            }, "导出", 8, ["disabled", "onClick"])
          ]),
          _createElementVNode("div", { class: "sns-export-tip" }, _toDisplayString(exportScopeText), 1),
          (exportState === 'running')
            ? (_openBlock(), _createElementBlock("span", {
                key: 0,
                class: "sns-export-msg"
              }, "导出中… " + _toDisplayString(exportProgress), 1))
            : (exportState === 'ok' && exportResult && exportResult.empty)
              ? (_openBlock(), _createElementBlock("span", {
                  key: 1,
                  class: "sns-export-msg"
                }, " 没有符合条件的动态（不生成文件） "))
              : (exportState === 'ok' && exportResult)
                ? (_openBlock(), _createElementBlock("span", {
                    key: 2,
                    class: "sns-export-msg"
                  }, "导出完成，请在浏览器下载中查看。媒体缺失情况见ZIP中的export-report.json。"))
                : (exportState === 'error')
                  ? (_openBlock(), _createElementBlock("span", {
                      key: 3,
                      class: "sns-export-msg"
                    }, "✗ " + _toDisplayString(exportError), 1))
                  : _createCommentVNode("", true)
        ], 512), [[_vShow, pops.export]])
      ]),
      _withDirectives(_createElementVNode("div", { class: "sns-notice" }, _toDisplayString(notice), 513), [[_vShow, notice]]),
      _createElementVNode("div", { class: "sns-list" }, [(loading && !timeline.length)
        ? (_openBlock(), _createElementBlock("div", {
            key: 0,
            class: "sns-loading"
          }, "加载中…"))
        : (!timeline.length)
          ? (_openBlock(), _createElementBlock("div", {
              key: 1,
              class: "sns-empty"
            }, _toDisplayString(emptyText), 1))
          : (_openBlock(true), _createElementBlock(_Fragment, { key: 2 }, _renderList(timeline, (p) => {
              return (_openBlock(), _createBlock(_component_sns_post, {
                key: String(p.tid),
                post: p
              }, null, 8, ["post"]))
            }), 128))]),
      _withDirectives(_createElementVNode("button", {
        class: "sns-more",
        onClick: loadMore
      }, "加载更多", 8, ["onClick"]), [[_vShow, hasMore && !loading]]),
      _withDirectives(_createElementVNode("button", {
        class: "sns-more",
        disabled: ""
      }, "加载中…", 512), [[_vShow, hasMore && loading]]),
      _withDirectives(_createElementVNode("div", { class: "sns-modal" }, [_createElementVNode("div", {
        class: "sns-modal-mask",
        onClick: closeModal
      }, null, 8, ["onClick"]), _createElementVNode("div", {
        class: "sns-modal-box",
        role: "dialog",
        "aria-modal": "true",
        "aria-label": "动态详情"
      }, [_createElementVNode("div", { class: "sns-modal-head" }, [_createElementVNode("span", null, "动态详情"), _createElementVNode("button", {
        class: "sns-icon-btn",
        title: "关闭",
        onClick: closeModal
      }, "✕", 8, ["onClick"])]), _createElementVNode("div", { class: "sns-modal-body" }, [(modal.error)
        ? (_openBlock(), _createElementBlock("div", {
            key: 0,
            class: "sns-empty"
          }, "加载失败：" + _toDisplayString(modal.error), 1))
        : (modal.post)
          ? (_openBlock(), _createBlock(_component_sns_post, {
              key: 1,
              post: modal.post,
              compact: false
            }, null, 8, ["post", "compact"]))
          : (_openBlock(), _createElementBlock("div", {
              key: 2,
              class: "sns-loading"
            }, "加载中…"))])])], 512), [[_vShow, modal.visible]]),
      _withDirectives(_createElementVNode("div", {
        class: "sns-lightbox",
        onClick: $event => (lightbox.visible = false)
      }, [(lightbox.video)
        ? (_openBlock(), _createElementBlock("video", {
            key: 0,
            src: lightbox.src,
            controls: "",
            autoplay: "",
            loop: "",
            muted: "",
            playsinline: "",
            onClick: _withModifiers(() => {}, ["stop"])
          }, null, 8, ["src", "onClick"]))
        : (_openBlock(), _createElementBlock("img", {
            key: 1,
            src: lightbox.src,
            alt: "朋友圈图片 大图预览",
            onClick: _withModifiers(() => {}, ["stop"])
          }, null, 8, ["src", "onClick"])), (lightbox.items.length > 1)
        ? (_openBlock(), _createElementBlock(_Fragment, { key: 2 }, [_createElementVNode("button", {
            class: "sns-lb-nav sns-lb-prev",
            title: "上一张（←）",
            "aria-label": "上一张",
            onClick: _withModifiers($event => (lbStep(-1)), ["stop"])
          }, "‹", 8, ["onClick"]), _createElementVNode("button", {
            class: "sns-lb-nav sns-lb-next",
            title: "下一张（→）",
            "aria-label": "下一张",
            onClick: _withModifiers($event => (lbStep(1)), ["stop"])
          }, "›", 8, ["onClick"]), _createElementVNode("span", {
            class: "sns-lb-count",
            onClick: _withModifiers(() => {}, ["stop"])
          }, _toDisplayString(lightbox.index + 1) + " / " + _toDisplayString(lightbox.items.length), 9, ["onClick"])], 64))
        : _createCommentVNode("", true)], 8, ["onClick"]), [[_vShow, lightbox.visible]])
    ]))
  }
}})(),
  components: { 'sns-post': SnsPost, 'sns-ava': SnsAva, 'sx-dd': SxDd },
  data() { return store; },
  computed: {
    meName() { return window.SX.getAccountName(); },
    meInitial() { return String(this.meName).trim().slice(0, 1) || '我'; },
    userLabel() {
      if (!store.username) return '全部发布者';
      const f = friendMap.value.get(store.username);
      return (f && f.display) || store.username;
    },
    rangeOn() { return !!(store.start || store.end); },
    metaText() {
      const n = store.timeline.length;
      let s = `${n} 条`;
      if (store.rangeKey !== 'all') s += ' · ' + store.rangeLabel;
      if (store.username) s += ' · ' + this.userLabel;
      return s;
    },
    quickRanges() { return QUICK_RANGES; },
    sortOptions() { return SORT_OPTIONS; },
    fmtOptions() { return FMT_OPTIONS; },
    calTitle() {
      if (store.cal.jump === 'year') return '选择年份';
      if (store.cal.jump === 'month') return '选择月份';
      return `${store.cal.y}年${store.cal.m + 1}月 ▾`;
    },
    calCells() { return monthCells(store.cal.y, store.cal.m); },
    jumpYears() {
      const nowY = new Date().getFullYear();
      const years = [];
      for (let y = nowY; y >= nowY - 11; y--) years.push(y);
      return years;
    },
    todayTs() {
      const d = new Date();
      return Math.floor(new Date(d.getFullYear(), d.getMonth(), d.getDate()).getTime() / 1000);
    },
    calHint() {
      if (store.cal.jump) return '选择月份后再点日期微调';
      if (!store.draft.start) return '点击日期选择开始';
      return store.draft.end
        ? this.describeRange(store.draft.start, store.draft.end)
        : `${this.describeRange(store.draft.start, null)} → 选择结束日期`;
    },
    sortedFriends() {
      return sortFriends(store.friends.filter(f => matchFriend(f, store.userQuery)), store.userSort);
    },
    userHint() {
      if (!store.friends.length) return store.friendsLoaded ? '' : '加载中…';
      return `共 ${store.friends.length} 位发布者${store.userQuery ? ` · 匹配 ${this.sortedFriends.length}` : ''}`;
    },
    exportScopeText() {
      const parts = [];
      parts.push(`时间：${store.rangeKey === 'all' ? '全部' : store.rangeLabel}`);
      parts.push(`发布者：${store.username ? this.userLabel : '全部'}`);
      if (store.keyword) parts.push(`关键词：${store.keyword}`);
      return '导出范围 —— ' + parts.join(' · ');
    },
    exportExtra() {
      const m = (store.exportResult && store.exportResult.media) || {};
      if (!m.total) return '';
      const why = (m.fail && m.reasons)
        ? '：' + Object.keys(m.reasons).map(k => `${reasonText(k)} ×${m.reasons[k]}`).join('、')
        : '';
      return `，媒体 ${m.ok}/${m.total}（失败 ${m.fail}${why}）`;
    },
  },
  watch: {
    kwInput(v) {
      clearTimeout(_kwTimer);
      _kwTimer = setTimeout(() => {
        store.keyword = v;
        this.load(true);
      }, 350);
    },
  },
  methods: {
    avaUrl,
    lbStep,
    preloadMeAvatar() {
      store.meAvaOk = false;
      if (!store.account) return;
      const img = new Image();
      img.onload = () => { store.meAvaOk = true; };
      img.src = avaUrl(store.account);
    },
    togglePop(id) {
      const show = !store.pops[id];
      store.pops.range = store.pops.user = store.pops.export = false;
      store.pops[id] = show;
      if (show && id === 'user') this.ensureFriends();
      if (show && id === 'range') this.openRange();
    },
    describeRange(start, end) {
      // 日期走 SX.fmtDate（YYYY-MM-DD）：原页内自拼不补零的 YYYY/M/D，
      // 与导出页范围横幅等处相邻出现观感不齐（审计 §4.8）
      const f = t => fmtDate(t);
      if (start && end) return start === end ? f(start) : `${f(start)} - ${f(end)}`;
      if (start) return `${f(start)} 起`;
      if (end) return `至 ${f(end)}`;
      return '全部时间';
    },

    /* ── 时间范围 ── */
    openRange() {
      store.draft = { start: store.start, end: store.end };
      const d = new Date((store.start || nowSec()) * 1000);
      store.cal = { y: d.getFullYear(), m: d.getMonth(), jump: null };
    },
    selectRange(key) {
      const r = rangeFromKey(key);
      if (!r) return;
      store.draft = { start: r.start, end: r.end };
      const d = new Date((r.start || nowSec()) * 1000);
      store.cal = { y: d.getFullYear(), m: d.getMonth(), jump: null };
      this.applyRange(r.start, r.end, key, r.label);
    },
    applyRange(start, end, key, label) {
      store.start = start;
      store.end = end;
      store.rangeKey = key || 'custom';
      store.rangeLabel = label || this.describeRange(start, end);
      store.pops.range = false;
      this.load(true);
    },
    shiftMonth(delta) {
      const d = new Date(store.cal.y, store.cal.m + delta, 1);
      store.cal.y = d.getFullYear();
      store.cal.m = d.getMonth();
      store.cal.jump = null;
    },
    cycleJump() {
      store.cal.jump = store.cal.jump === 'year' ? 'month' : (store.cal.jump === 'month' ? null : 'year');
    },
    calCellClass(c) {
      const cls = [];
      if (c.out) cls.push('out');
      if (c.ts === this.todayTs) cls.push('today');
      if (c.ts === store.draft.start || c.ts === store.draft.end) cls.push('edge');
      else {
        const lo = Math.min(store.draft.start || Infinity, store.draft.end || Infinity);
        const hi = Math.max(store.draft.start || -Infinity, store.draft.end || -Infinity);
        if (c.ts > lo && c.ts < hi) cls.push('in-range');
      }
      return cls;
    },
    pickDay(c) {
      const ts = c.ts;
      if (!store.draft.start || store.draft.end) store.draft = { start: ts, end: null };
      else if (ts >= store.draft.start) store.draft.end = ts;
      else store.draft = { start: ts, end: null };
    },
    clearRange() { this.applyRange(null, null, 'all', '全部时间'); },
    confirmRange() {
      if (store.draft.start) {
        const end = store.draft.end || store.draft.start;
        this.applyRange(store.draft.start, end, 'custom', this.describeRange(store.draft.start, end));
      }
    },

    /* ── 发布者 ── */
    async ensureFriends(force) {
      if (store.friendsLoaded && !force) return;
      if (store.friendsLoading) return;
      store.friendsLoading = true;
      store.friendsError = '';
      try {
        const d = await fetchJSON(`/api/sns/friends?account=${encodeURIComponent(store.account)}&limit=1000`);
        store.friends = d.friends || [];
        store.friendsLoaded = true;
      } catch (e) {
        store.friendsError = e.message;      // 面板内常驻错误态，toast 一闪就没了
        toast('发布者加载失败：' + e.message);
      } finally {
        store.friendsLoading = false;
      }
    },
    applyUser(username) {
      store.username = username || '';
      store.pops.user = false;
      this.load(true);
    },

    /* ── 列表加载 ── */
    async load(reset) {
      const activeGeneration=generation;
      if (!store.account) return;
      if (store.loading) {
        // 加载中用户又改了筛选：记下来，本轮结束后补跑最后一次，不再静默吞掉
        store._pendingLoad = store._pendingLoad || reset;
        return;
      }
      store.loading = true;
      if (reset) {
        store.before = null;
        store.timeline = [];
        store.emptyText = '没有符合条件的动态';
      }
      const qs = new URLSearchParams({ account: store.account, limit: '20' });
      if (store.before) qs.set('before_tid', store.before);
      if (store.keyword) qs.set('keyword', store.keyword);
      if (store.username) qs.set('username', store.username);
      if (store.start) qs.set('start', String(store.start));
      if (store.end) qs.set('end', String(store.end));
      try {
        const d = await fetchJSON(`/api/sns/timeline?${qs}`);
        if(activeGeneration!==generation)return;
        const rows = d.timeline || [];
        store.timeline = reset ? rows : store.timeline.concat(rows);
        store.before = d.next_before_tid || null;
        store.hasMore = !!d.has_more;
      } catch (e) {
        if (reset) store.emptyText = e.message;
        else toast(e.message);
      } finally {
        if(activeGeneration!==generation)return;
        store.loading = false;
        if (store._pendingLoad) {
          const rerun = store._pendingLoad;
          store._pendingLoad = false;
          this.load(rerun);
        }
      }
    },
    loadMore() { this.load(false); },
    refresh() {
      store.friendsLoaded = false;
      this.ensureFriends(true);
      this.load(true);
    },
    clearKeyword() {
      store.kwInput = '';
      store.keyword = '';
      this.load(true);
    },

    /* ── 弹层 / 灯箱 ── */
    closeModal() { store.modal.visible = false; },
    closePops() { store.pops.range = store.pops.user = store.pops.export = false; },

    /* ── 导出 ── */
    async doExport() {
      if (store.exportState === 'running') return;
      if (store.exportMedia && !window.confirm('同时下载当前筛选朋友圈的媒体？部分过期资源可能无法下载。')) return;
      store.exportState = 'running'; store.exportError = ''; store.exportResult = null;
      try {
        const body = {format:store.exportFmt, media:store.exportMedia, concurrency:Math.min(16,Math.max(1,Number(store.exportConc)||5)),keyword:store.keyword,
          username:store.username, start:store.start, end:store.end};
        const response = await fetch('/api/sns/export', {method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body),credentials:'omit'});
        if (!response.ok) { const data = await response.json(); throw new Error(data.error || '导出未完成'); }
        const filename = store.exportMedia ? 'moments.zip' : 'moments.' + (store.exportFmt === 'markdown' ? 'md' : store.exportFmt);
        const url = URL.createObjectURL(await response.blob());
        const a = document.createElement('a'); a.href=url; a.download=filename; document.body.append(a); a.click(); a.remove();
        setTimeout(()=>URL.revokeObjectURL(url),60000);
        store.exportState='ok'; store.exportResult={downloaded:true};
        window.SX.recordEvent('朋友圈导出完成。');
      } catch(error) {store.exportState='error'; store.exportError=error.message;}
    },

  },
  mounted() {
    // 本人头像：加载成功才显示（避免破图闪一下）
    this.preloadMeAvatar();
  },
};

/* ══ 生命周期 ═════════════════════════════════════════════════ */

async function init(view) {
  const activeGeneration=++generation;
  store.timeline=[]; store.friends=[]; store.friendsLoaded=false; store.friendsLoading=false; store.account=''; store.notice=''; store.hasMore=false; store.before=null; store.loading=false; store.exportState='idle'; store.exportResult=null; store.modal={visible:false,post:null,error:''}; store.lightbox={visible:false,src:'',video:false,items:[],index:0};
  // 全局事件：点外面关弹层 / Esc 关弹层与灯箱（「···」菜单由 widgets.openMenu
  // 自带点外/Esc 收起，不再走 store 互斥）
  _docClick = ev => {
    // [data-pop] 是三个弹层触发器（时间范围 / 发布者 / 导出）——
    // 漏掉任何一个，点它会先开、再被这里立刻关掉，表现为「点了没反应」
    if (ev.target.closest('.sns-pop') || ev.target.closest('[data-pop]')) return;
    store.pops.range = store.pops.user = store.pops.export = false;
  };
  _keyDown = ev => {
    if (ev.key === 'Escape') {
      if (store.pops.range || store.pops.user || store.pops.export) {
        store.pops.range = store.pops.user = store.pops.export = false;
        return;
      }
      if (store.lightbox.visible) { store.lightbox.visible = false; return; }
      if (store.modal.visible) store.modal.visible = false;
      return;
    }
    // 灯箱打开时 ←/→ 翻页
    if (store.lightbox.visible && store.lightbox.items.length > 1) {
      if (ev.key === 'ArrowLeft') { lbStep(-1); ev.preventDefault(); }
      else if (ev.key === 'ArrowRight') { lbStep(1); ev.preventDefault(); }
    }
  };
  document.addEventListener('click', _docClick);
  document.addEventListener('keydown', _keyDown);

  _app = Vue.createApp(SnsPage);
  const vm2 = _app.mount(view);
  vm = vm2;

  // 账号来自壳层全局账号卡；朋友圈库可用性仍由 /api/sns/accounts 校验
  // （wxid 可能带设备后缀，精确匹配失败时按"剥后缀"再对一次）
  const global = window.SX.getAccount();
  if (!global) {
    store.emptyText = '还没有解密产物 —— 请先在「引导设置」完成解密';
    return;
  }
  store.account = global;

  try {
    const d = await fetchJSON('/api/sns/accounts');
    store.accounts = d.accounts || [];
    if (!store.accounts.length) throw new Error('没有找到已解密的朋友圈数据库');
    const clean = String(global).replace(/_[0-9a-f]{4}$/i, '');
    const matched = store.accounts.find(a => a.wxid === global || a.wxid === clean);
    if (matched) {
      store.notice = `共 ${matched.count || 0} 条动态；媒体按记录从微信 CDN 读取。`;
    } else {
      // 全局账号没有朋友圈库：退回第一条可用库并说明，避免整页空白
      store.emptyText = "当前账号没有可读取的朋友圈数据库，请重新提取。"; return;
      store.notice = `当前微信号没有朋友圈数据库，已显示「${store.accounts[0].wxid}」的内容（共 ${store.accounts[0].count || 0} 条动态）。`;
    }
  } catch (e) {
    store.notice = e.message;
    store.emptyText = e.message;
    return;
  }
  if(activeGeneration!==generation)return;
  vm.preloadMeAvatar();
  store.timeline = [];
  store.before = null;
  await Promise.all([vm.load(true), vm.ensureFriends(true)]);

  // 无限滚动：列表尾部进入视口前 240px 就预取下一页；按钮保留为兜底入口
  const moreBtn = view.querySelector('.sns-more');
  if (moreBtn && 'IntersectionObserver' in window) {
    _io = new IntersectionObserver(entries => {
      if (entries.some(e => e.isIntersecting) && store.hasMore && !store.loading) {
        vm.loadMore();
      }
    }, { rootMargin: '240px' });
    _io.observe(moreBtn);
  }
}

let vm = null;

function destroy() {
  generation++;
  if (_pollTimer) { clearInterval(_pollTimer); _pollTimer = null; }
  if (_io) { _io.disconnect(); _io = null; }
  if (_docClick) { document.removeEventListener('click', _docClick); _docClick = null; }
  if (_keyDown) { document.removeEventListener('keydown', _keyDown); _keyDown = null; }
  clearTimeout(_kwTimer);
  store.lightbox={visible:false,src:'',video:false,items:[],index:0};
  store.modal={visible:false,post:null,error:''};
  if (_app) { try { _app.unmount(); } catch (e) { /* 忽略 */ } _app = null; }
  vm = null;
}

window.SnsUI={init,destroy};
})();
