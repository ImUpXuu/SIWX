/* 朋友圈页（Vue 3 全局构建版，无构建链）。
 *
 * 架构：
 *   - 模板就是 pages/sns.html —— init() 用「容器 innerHTML 即模板」挂载 Vue，
 *     保持既有的 hash 路由 + 模块化加载契约（init(view) / destroy()）不变；
 *   - 纯函数区保持导出（relTime/monthCells/sortFriends/…），便于脚本化验证；
 *   - 响应式状态集中在 store，子组件（sns-post / sns-ava）共享；
 *   - 好友列表到达后昵称/头像自动刷新（Vue 响应式，不再需要 rerenderList 补丁）。
 */
const { esc, fetchJSON, copyText } = window.SX;
const pad = n => String(n).padStart(2, '0');
const nowSec = () => Math.floor(Date.now() / 1000);

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

const QUICK_RANGES = [
  { key: 'all', label: '全部' },
  { key: 'today', label: '今天' },
  { key: '7', label: '最近 7 天' },
  { key: '30', label: '最近 30 天' },
  { key: '90', label: '最近 90 天' },
  { key: '365', label: '最近一年' },
  { key: 'thisyear', label: '今年' },
];

/* ══ 响应式共享状态 ═══════════════════════════════════════════ */

const store = Vue.reactive({
  account: '',
  accounts: [],
  friends: [],
  friendsLoaded: false,
  friendsLoading: false,

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

  lightbox: { visible: false, src: '', video: false },
  modal: { visible: false, post: null, error: '' },
  meAvaOk: false,
  activeMenuTid: null,

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
  const q = new URLSearchParams({ account: store.account, url: m.url || '', key: m.key || '', token: m.token || '' });
  return `/api/sns/media?${q}`;
}
function proxyUrl(url) {
  return `/api/sns/media?${new URLSearchParams({ account: store.account, url: url || '' })}`;
}
function emojiUrl(e) {
  const q = new URLSearchParams({
    account: store.account,
    emoji: JSON.stringify({ url: e.url || '', encrypt_url: e.encrypt_url || '', aes_key: e.aes_key || '' }),
  });
  return `/api/sns/emoji?${q}`;
}
function fmtDur(sec) {
  const n = Math.floor(Number(sec) || 0);
  if (n <= 0) return '';
  return `${Math.floor(n / 60)}:${pad(n % 60)}`;
}

function toast(text) {
  store.notice = text || '';
  clearTimeout(toast._t);
  toast._t = setTimeout(() => { store.notice = ''; }, 2600);
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
  template: `
    <span class="ava"><span>{{ initial }}</span>
      <img v-if="hasAvatar" :src="src" alt="" loading="lazy" @error="$event.target.remove()">
    </span>`,
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
    menuOpen() { return store.activeMenuTid === this.tid; },
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
    openMedia(ev, m) {
      ev.preventDefault();
      const lp = m.live_photo;
      if (lp && lp.url) {
        store.lightbox = { visible: true, src: mediaUrl(lp), video: true };
      } else {
        store.lightbox = { visible: true, src: mediaUrl(m), video: false };
      }
    },
    openCoverVideo(url) {
      if (url) store.lightbox = { visible: true, src: url, video: true };
    },
    openDetail() {
      store.activeMenuTid = null;
      store.modal = { visible: true, post: null, error: '' };
      fetchJSON(`/api/sns/detail?account=${encodeURIComponent(store.account)}&tid=${encodeURIComponent(this.tid)}`)
        .then(d => { store.modal.post = d.post; })
        .catch(e => { store.modal.error = e.message; });
    },
    copyText_() {
      store.activeMenuTid = null;
      copyText(this.post.content_desc || '')
        .then(ok => toast(ok ? '已复制文字' : '复制失败'));
    },
    copyId() {
      store.activeMenuTid = null;
      copyText(this.tid).then(ok => toast(ok ? '已复制动态 ID' : '复制失败'));
    },
    toggleMenu(ev) {
      ev.stopPropagation();
      store.activeMenuTid = this.menuOpen ? null : this.tid;
    },
  },
  template: `
    <article class="sns-post">
      <sns-ava :username="uname" :display="display"></sns-ava>
      <div class="sns-main">
        <div class="sns-name">{{ display }}</div>
        <div v-if="post.content_desc" class="sns-text">{{ post.content_desc }}</div>

        <template v-if="card">
          <a v-if="card.kind === 'music'" class="sns-card sns-card--music"
             :href="card.url" target="_blank" rel="noreferrer">
            <div class="sns-card-body"><div class="sns-card-tag">🎵 音乐</div>
              <div class="sns-card-title">{{ (card.music && card.music.album) || card.title || '音乐' }}</div>
              <div class="sns-card-sub">{{ ((card.music && card.music.singer) || card.description || '') + (card.music && card.music.duration_ms ? ' · ' + fmtDur(card.music.duration_ms / 1000) : '') }}</div></div>
          </a>
          <div v-else-if="card.kind === 'finder'" class="sns-card sns-card--finder">
            <img v-if="coverUrl" class="sns-card-cover" loading="lazy" :src="coverUrl"
                 alt="视频号封面"
                 :title="finderVideoUrl ? '点击播放' : ''"
                 @click="openCoverVideo(finderVideoUrl)"
                 @error="$event.target.remove()">
            <div class="sns-card-body">
              <div class="sns-card-tag">📹 视频号</div>
              <div class="sns-card-title">{{ card.finder && card.finder.nickname }}</div>
              <div class="sns-card-sub">{{ [(card.finder && card.finder.media_count ? card.finder.media_count + ' 个作品' : ''), fmtDur(card.duration)].filter(Boolean).join(' · ') }}</div>
              <div class="sns-card-desc">{{ card.description || '' }}</div></div>
          </div>
          <div v-else-if="card.kind === 'live'" class="sns-card sns-card--live">
            <img v-if="coverUrl" class="sns-card-cover" loading="lazy" :src="coverUrl"
                 alt="直播封面" @error="$event.target.remove()">
            <div class="sns-card-body">
              <div class="sns-card-tag">📺 视频号直播</div>
              <div class="sns-card-title">{{ card.live && card.live.nickname }}</div>
              <div class="sns-card-desc">{{ card.live && card.live.desc }}</div></div>
          </div>
          <div v-else-if="card.kind === 'note'" class="sns-card sns-card--note">
            <div class="sns-card-body">
              <div class="sns-card-tag">📝 笔记</div>
              <div class="sns-card-desc">{{ (card.note && card.note.text) || card.title || '' }}</div></div>
          </div>
          <a v-else class="sns-card sns-card--link"
             :href="card.url" target="_blank" rel="noreferrer">
            <div class="sns-card-body"><div class="sns-card-tag">🔗 链接</div>
              <div class="sns-card-title">{{ card.title || card.url || '' }}</div>
              <div class="sns-card-sub">{{ card.source || '' }}</div>
              <div class="sns-card-desc">{{ card.description || '' }}</div></div>
          </a>
        </template>

        <div v-if="items.length" class="sns-media" :class="gridCls">
          <span v-for="(m, i) in items" :key="i" class="sns-media-cell">
            <video v-if="m.type === 6" class="sns-vid" controls preload="metadata"
                   :src="mediaUrl(m)" @error="mediaFail($event, true)"></video>
            <template v-else>
              <img class="sns-lb" loading="lazy" :src="mediaUrl(m)" alt="朋友圈图片"
                   :title="m.live_photo && m.live_photo.url ? '实况照片：点击播放' : ''"
                   @click="openMedia($event, m)"
                   @error="mediaFail($event, false)">
              <span v-if="m.live_photo && m.live_photo.url" class="live-badge">实况</span>
            </template>
          </span>
        </div>

        <div v-if="post.location" class="sns-loc">📍 {{ post.location.name || '' }}{{ post.location.address ? ' · ' + post.location.address : '' }}</div>

        <div class="sns-foot">
          <span class="sns-time" :title="fullTime(post.ts)">{{ relTime(post.ts) }}</span>
          <span v-if="compact" class="sns-detail-link" @click="openDetail">详情</span>
          <span v-else class="sns-time">· 赞 {{ likes.length }} · 评论 {{ comments.length }}</span>
          <span class="sns-ops">
            <button class="sns-ops-btn" title="更多操作" @click="toggleMenu">···</button>
            <span class="sns-ops-menu" :hidden="!menuOpen">
              <button @click="openDetail">查看详情</button>
              <button @click="copyText_">复制文字</button>
              <button @click="copyId">复制动态 ID</button>
            </span>
          </span>
        </div>

        <div v-if="likes.length || comments.length" class="sns-inter">
          <div v-if="likes.length" class="sns-likes"><span class="ic">❤</span><template
            v-for="(x, i) in likes" :key="i"><span v-if="i" class="sep">，</span>{{ displayOf(x.username, x.nickname) }}</template></div>
          <div v-if="comments.length" class="sns-comments" :class="{ 'has-like': likes.length }">
            <div v-for="(c, i) in shownComments" :key="i" class="sns-comment">
              <b>{{ displayOf(c.username, c.nickname) }}</b>：<span v-if="c.ref_username" class="dim">回复 {{ displayOf(c.ref_username, '') }} </span>{{ c.content || '' }}<template
                v-for="(e, j) in (c.emojis || [])" :key="'e' + j"><img class="cm-emoji" loading="lazy" :src="emojiUrl(e)" alt="表情" @error="$event.target.remove()"></template><template
                v-for="(im, j) in (c.images || []).filter(x => x.url)" :key="'i' + j"><img class="cm-img" loading="lazy" :src="mediaUrl(im)" alt="评论图片" @error="$event.target.remove()"></template>
            </div>
            <div v-if="hiddenComments" class="sns-more-comments">还有 {{ hiddenComments }} 条评论，点「详情」查看</div>
          </div>
        </div>
      </div>
    </article>`,
};

/* ══ 页面组件 ═════════════════════════════════════════════════ */

let _app = null;
let _pollTimer = null;
let _docClick = null;
let _keyDown = null;
let _kwTimer = null;

const SnsPage = {
  components: { 'sns-post': SnsPost, 'sns-ava': SnsAva },
  data() { return store; },
  computed: {
    meName() {
      const clean = store.account.replace(/_[0-9a-f]{4}$/i, '');
      const me = store.friends.find(f => f.username === store.account || f.username === clean);
      return (me && me.display) || clean || '我';
    },
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
    // 账号是「挂载后」才拉到的：这里重载本人头像，否则首屏那次必然失败且不再重试
    account() { this.preloadMeAvatar(); },
  },
  methods: {
    avaUrl,
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
      const f = t => {
        const d = new Date(t * 1000);
        return `${d.getFullYear()}/${d.getMonth() + 1}/${d.getDate()}`;
      };
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
      try {
        const d = await fetchJSON(`/api/sns/friends?account=${encodeURIComponent(store.account)}&limit=1000`);
        store.friends = d.friends || [];
        store.friendsLoaded = true;
      } catch (e) {
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
      if (store.loading || !store.account) return;
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
        const rows = d.timeline || [];
        store.timeline = reset ? rows : store.timeline.concat(rows);
        store.before = d.next_before_tid || null;
        store.hasMore = !!d.has_more;
      } catch (e) {
        if (reset) store.emptyText = e.message;
        else toast(e.message);
      } finally {
        store.loading = false;
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
    onAccountChange() {
      store.before = null;
      store.friendsLoaded = false;
      store.friends = [];
      store.meAvaOk = false;
      this.applyUser('');
      this.ensureFriends(true);
    },

    /* ── 弹层 / 灯箱 ── */
    closeModal() { store.modal.visible = false; },
    closePops() { store.pops.range = store.pops.user = store.pops.export = false; },

    /* ── 导出 ── */
    async doExport() {
      if (store.exportMedia && !window.confirm('下载媒体会逐张访问 CDN，可能耗时较久，确定继续？')) return;
      store.exportState = 'running';
      store.exportProgress = '';
      store.exportError = '';
      store.exportResult = null;
      try {
        const body = {
          account: store.account, format: store.exportFmt, media: store.exportMedia,
          concurrency: Number(store.exportConc) || 5,
          keyword: store.keyword || undefined,
          username: store.username || undefined,
          start: store.start || undefined,
          end: store.end || undefined,
        };
        const r = await fetch('/api/sns/export', {
          method: 'POST', headers: { 'content-type': 'application/json' }, body: JSON.stringify(body),
        });
        if (r.status === 409) throw new Error('已有任务在运行，请稍后再试');
        if (!r.ok) {
          const j = await r.json().catch(() => ({}));
          throw new Error(j.error || '启动失败');
        }
        this.pollExport();
      } catch (e) {
        store.exportState = 'error';
        store.exportError = e.message;
      }
    },
    pollExport() {
      if (_pollTimer) clearInterval(_pollTimer);
      _pollTimer = setInterval(async () => {
        let job;
        try { job = await (await fetch('/api/job')).json(); }
        catch (e) { return; }
        const logs = job.logs || [];
        const last = logs.length ? (logs[logs.length - 1].length >= 4
          ? logs[logs.length - 1][3] : logs[logs.length - 1][1]) : '';
        if (job.running) { store.exportProgress = last || ''; return; }
        clearInterval(_pollTimer); _pollTimer = null;
        const rep = job.report || {};
        if (job.ok && rep.kind === 'sns_export') {
          store.exportState = 'ok';
          store.exportResult = rep;
        } else {
          store.exportState = 'error';
          store.exportError = job.error || last || '导出失败';
        }
      }, 800);
    },
    openExportDir() {
      const dir = store.exportResult && store.exportResult.export_dir;
      if (dir) window.SX.openPath(dir);
    },
  },
  mounted() {
    // 本人头像：加载成功才显示（避免破图闪一下）
    this.preloadMeAvatar();
  },
};

/* ══ 生命周期 ═════════════════════════════════════════════════ */

export async function init(view) {
  // 全局事件：点外面关弹层 / Esc 关弹层与灯箱 / 菜单互斥
  _docClick = ev => {
    // [data-pop] 是三个弹层触发器（时间范围 / 发布者 / 导出）——
    // 漏掉任何一个，点它会先开、再被这里立刻关掉，表现为「点了没反应」
    if (ev.target.closest('.sns-pop') || ev.target.closest('[data-pop]')) return;
    store.pops.range = store.pops.user = store.pops.export = false;
    if (!ev.target.closest('.sns-ops')) store.activeMenuTid = null;
  };
  _keyDown = ev => {
    if (ev.key !== 'Escape') return;
    if (store.pops.range || store.pops.user || store.pops.export) {
      store.pops.range = store.pops.user = store.pops.export = false;
      return;
    }
    if (store.lightbox.visible) { store.lightbox.visible = false; return; }
    if (store.modal.visible) store.modal.visible = false;
  };
  document.addEventListener('click', _docClick);
  document.addEventListener('keydown', _keyDown);

  _app = Vue.createApp(SnsPage);
  const vm = _app.mount(view);

  // 首屏数据
  try {
    const d = await fetchJSON('/api/sns/accounts');
    store.accounts = d.accounts || [];
    if (!store.accounts.length) throw new Error('没有找到已解密的朋友圈数据库');
    store.account = store.accounts[0].wxid;
    const a = store.accounts[0];
    store.notice = `本地朋友圈图片只包含微信已经下载过的资源；未下载的会尝试从 CDN 获取。数据库共 ${a.count || 0} 条动态。`;
  } catch (e) {
    store.notice = e.message;
    store.emptyText = e.message;
    return;
  }
  store.timeline = [];
  store.before = null;
  await Promise.all([vm.load(true), vm.ensureFriends(true)]);
}

export function destroy() {
  if (_pollTimer) { clearInterval(_pollTimer); _pollTimer = null; }
  if (_docClick) { document.removeEventListener('click', _docClick); _docClick = null; }
  if (_keyDown) { document.removeEventListener('keydown', _keyDown); _keyDown = null; }
  clearTimeout(_kwTimer);
  store.lightbox.visible = false;
  store.modal.visible = false;
  store.activeMenuTid = null;
  if (_app) { try { _app.unmount(); } catch (e) { /* 忽略 */ } _app = null; }
}
