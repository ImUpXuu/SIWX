
(function() {
  var data = window.CHAT_DATA;
  var messages = data.messages;
  var isGroup = data.meta.isGroup;
  var ownerId = data.meta.ownerId;
  var members = {};
  data.members.forEach(function(m) { members[m.id] = m; });
  var avatarFiles = data.avatarFiles || {};

  var chatBody = document.getElementById('chatBody');
  var container = document.getElementById('msgContainer');
  var filtered = messages;
  // 渲染引擎：窗口式随机渲染。渲染 [renderStart, renderEnd) 区间，滚动到
  // 边缘双向扩窗、超出上限双向裁剪——默认打开定位到最新一屏（微信习惯），
  // 日期跳转/搜索直达任意下标，9 万条会话不再"永远停在第一屏"。
  var BATCH = 50, MAX_ROWS = 1200;
  var renderStart = 0, renderEnd = 0, isLoading = false;
  var activeTypes = new Set();
  var searchKw = '', fStart = 0, fEnd = 0;
  var searchPos = -1;                // 搜索跳转游标（在 filtered 里的下标）
  var rendererSyncTypeTags = null;   // renderTypeFilters 注入：按 activeTypes 重算高亮
  var searchTimer = null;
  var gallery = [];                  // 灯箱图源：filtered 里可看的图片/表情
  var lbPos = 0;

  function esc(s) {
    // 手写转义而非 div.innerHTML：innerHTML 不转义引号，名字含 ' " 时
    // 会破坏 onerror 等属性的字面量注入边界
    return String(s == null ? '' : s)
      .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
      .replace(/"/g, '&quot;').replace(/'/g, '&#39;');
  }
  function safeUrl(u) {
    // esc 只防属性越界，不防 scheme：javascript: 是合法 href，点击即执行。
    // 链接卡 url 来自对端 type-49 appmsg XML → 必须过白名单（与 ui/common.js 同规则）
    var s = String(u == null ? '' : u).trim();
    if (!s) return '';
    if (/^(https?:|\/|\.\/|\.\.\/|#)/i.test(s)) return s;
    return '';
  }
  function safeMedia(u) {
    // 媒体路径是导出目录内的相对路径（media/xxx.jpg），safeUrl 的白名单要求
    // 显式前缀，裸相对路径会被判空——先用 safeUrl 判一次，未命中时再放行
    // "无 scheme、不以 // 开头"的纯相对路径；javascript:/data:/file: 一律拒绝
    var s = safeUrl(u);
    if (s) return s;
    var raw = String(u == null ? '' : u).trim();
    if (!raw || /^[a-z][a-z0-9+.-]*:/i.test(raw) || raw.slice(0, 2) === '//') return '';
    return raw;
  }
  function fmtDate(ts) { var d = new Date(ts*1000); return d.getFullYear()+'年'+(d.getMonth()+1)+'月'+d.getDate()+'日'; }
  function fmtTime(ts) { var d = new Date(ts*1000); var p = function(n){return String(n).padStart(2,'0')}; return p(d.getHours())+':'+p(d.getMinutes()); }
  function fmtFull(ts) { return fmtDate(ts)+' '+fmtTime(ts); }
  function fmtISO(ts) { var d = new Date(ts*1000); var p = function(n){return String(n).padStart(2,'0')};
    return d.getFullYear()+'-'+p(d.getMonth()+1)+'-'+p(d.getDate()); }
  function fmtSize(n) {
    if (!isFinite(n) || n <= 0) return '';
    var units = ['B', 'KB', 'MB', 'GB'], i = 0;
    while (n >= 1024 && i < units.length - 1) { n /= 1024; i++; }
    return (i === 0 ? n : n.toFixed(1)) + ' ' + units[i];
  }
  function avatarColor(id) { var h = 0; for (var i = 0; i < id.length; i++) h = (h*31 + id.charCodeAt(i)) & 0x7fffffff; return 'c' + (h % 8); }
  function avaFile(un) { return avatarFiles[un] || ''; }
  function isSysType(t) { return t === 10000 || t === 10002 || t === 266287972401; }

  // XML 实体解码：raw XML 里 & 存成 &amp;，不还原的话表情 cdnurl 带
  // 字面 &amp; 去请求 → 服务器 400 → 表情全部掉占位（导出页与控制台
  // 的差异：控制台走真 XML 解析器自动解码，这里必须手动补）
  function xmlDec(s) {
    return s.replace(/&(?:amp|lt|gt|quot|apos|#x?[0-9a-fA-F]+);/g, function(e) {
      var name = e.slice(1, -1);
      if (name === 'amp') return '&';
      if (name === 'lt') return '<';
      if (name === 'gt') return '>';
      if (name === 'quot') return '"';
      if (name === 'apos') return "'";
      var cp = name.charAt(0) === 'x' || name.charAt(0) === 'X'
        ? parseInt(name.slice(1), 16) : parseInt(name.slice(1), 10);
      return cp > 0 && cp <= 0x10ffff ? String.fromCodePoint(cp) : e;
    });
  }

  function xmlVal(raw, tag) {
    var m = (raw||'').match(new RegExp('<'+tag+'>(.*?)</'+tag+'>', 's'));
    if (!m) return '';
    return xmlDec(m[1].replace(/<!\[CDATA\[([\s\S]*?)\]\]>/g, '$1').trim());
  }

  // 旧版微信 XML 属性可能写成 attr = "value"（等号两侧带空格），一并兼容
  function xmlAttr(raw, tag, attr) {
    var m = (raw||'').match(new RegExp('<'+tag+'\\b[^>]*?\\s'+attr+'\\s*=\\s*"([^"]*)"', 's'));
    return m ? xmlDec(m[1]) : '';
  }

  // 经纬度 → slippy map 瓦片键（与导出端 wx_maps.tile_key 同算法，
  // WX_MAPS 键值匹配才能命中缩略图）
  function tileKey(lat, lng) {
    var z = 15, n = Math.pow(2, z);
    lat = Math.max(-85.05112878, Math.min(85.05112878, parseFloat(lat)));
    lng = Math.max(-180, Math.min(180, parseFloat(lng)));
    if (!isFinite(lat) || !isFinite(lng)) return '';
    var x = Math.max(0, Math.min(n-1, Math.floor((lng+180)/360*n)));
    var lr = lat*Math.PI/180;
    var y = Math.max(0, Math.min(n-1, Math.floor((1 - Math.log(Math.tan(lr)+1/Math.cos(lr))/Math.PI)/2*n)));
    return z+'/'+x+'/'+y;
  }

  // 语音转文字：旧版是 <voicetrans> 子元素（CDATA），新版是 voicemsg 属性
  function voiceTrans(raw) {
    return xmlVal(raw, 'voicetrans') || xmlAttr(raw, 'voicemsg', 'voicetrans');
  }

  // 微信小黄脸：window.WX_FACES 由导出端注入（仅含本会话用到的名称，
  // base64 dataURI），文本/引用/系统消息里的 [表情名] 替换为内联图片。
  // 命中不了的 token 原样保留——历史名称或非表情的 [xxx] 不受影响。
  var FACES = window.WX_FACES || {};
  // 位置缩略图：window.WX_MAPS 由导出端注入（瓦片键 z/x/y → dataURI，
  // 仅含本会话位置消息命中的瓦片），命中不了回退文字卡 + 跳转链接。
  var MAPS = window.WX_MAPS || {};
  // 媒体导出开关：window.MEDIA_EXPORT 由导出端注入（want_media）。
  // null = 未知（JSON 转换等路径），占位原因用通用措辞。
  var mediaEnabled = window.MEDIA_EXPORT ? window.MEDIA_EXPORT.enabled !== false : null;

  // 媒体占位的原因文案：视频是管线暂不支持（永远占位），其余类型取决于
  // 是否启用媒体导出——"未启用导出"与"解密失败/源缺失"必须分开说清楚
  function phReason(t) {
    if (t === 43) return '暂不支持导出视频';
    if (t === 47) return mediaEnabled === false ? '本次导出未包含媒体' : '表情源不可用';
    if (mediaEnabled === false) return '本次导出未包含媒体文件';
    if (t === 3) return '源文件缺失或解密失败';
    if (t === 34) return '语音文件缺失';
    return '未包含在导出中';
  }
  var PH_META = {3: ['🖼️', '图片'], 43: ['🎬', '视频'],
                 47: ['😊', '表情'], 34: ['🎤', '语音']};
  function phCard(t) {
    var meta = PH_META[t] || ['❔', '媒体'];
    return '<div class="ph-card"><div class="ph-icon">' + meta[0] + '</div>' +
           '<div class="ph-body"><div class="ph-label">' + meta[1] + '</div>' +
           '<div class="ph-reason">' + esc(phReason(t)) + '</div></div></div>';
  }
  // fmtText 先 esc 再替换表情；搜索高亮必须插在两者之间——放在表情替换
  // 之后会把 kw 匹配进 dataURI/alt 属性（base64 随机撞词），破坏标签结构
  function markKw(h) {
    if (!searchKw) return h;
    var kw = searchKw.replace(/[.*+?^${}()|[\]\\]/g, '\\$&');
    try { return h.replace(new RegExp('(' + kw + ')', 'gi'), '<mark>$1</mark>'); }
    catch (e) { return h; }
  }
  function fmtText(s) {
    var h = markKw(esc(s));
    h = h.replace(/\[[^\[\]\n]{1,20}\]/g, function(tok) {
      var u = FACES[tok];
      return u ? '<img class="wx-face" alt="'+tok+'" src="'+u+'">' : tok;
    });
    return h;
  }

  // ── 消息分组：同一发送者、同一天、间隔 ≤5 分钟的连续消息合并展示 ──
  // 只依赖消息数据本身（而非 DOM），窗口裁剪/随机跳转不破坏分组状态
  function isContRow(i) {
    if (i <= 0) return false;
    var m = filtered[i], p = filtered[i-1];
    if (isSysType(m.type) || isSysType(p.type)) return false;
    if (m.sender !== p.sender || !!m.isSend !== !!p.isSend) return false;
    if (fmtDate(m.timestamp) !== fmtDate(p.timestamp)) return false;
    return m.timestamp >= p.timestamp && m.timestamp - p.timestamp <= 300;
  }

  // 单条消息 → .msg-row（含前置日期分隔条）。i 为 filtered 下标，
  // 分组状态（cont：续上行；contNext：下行续本行）是下标的纯函数
  function rowHTML(i) {
    var msg = filtered[i];
    var prev = i > 0 ? filtered[i-1] : null;
    var next = i < filtered.length - 1 ? filtered[i+1] : null;
    var html = '';
    // 窗口首条必带日期条：随机跳转后仍有"我在哪天"的上下文
    if (i === 0 || i === renderStart ||
        (prev && fmtDate(msg.timestamp) !== fmtDate(prev.timestamp)))
      html += '<div class="date-divider" data-date="'+fmtISO(msg.timestamp)+
              '"><span title="点击按日期跳转">'+fmtDate(msg.timestamp)+'</span></div>';
    var t = msg.type;
    if (isSysType(t)) {
      // 10002 的 content 常是 <sysmsg type="revokemsg"> XML——取 replacemsg
      // 的可读文案（含撤回后重新编辑的内容）；取不到再剥标签兜底
      var sc = msg.content || '';
      if (t === 10002 && sc.indexOf('<') === 0)
        sc = xmlVal(sc, 'replacemsg') || sc.replace(/<[^>]+>/g, '');
      // data-idx：窗口裁剪与搜索跳转都按它定位（系统消息也占一个下标）
      html += '<div class="system-msg" data-idx="'+i+'"><span>'+fmtText(sc)+'</span></div>';
      return html;
    }
    var mem = members[msg.sender];
    var name = mem ? mem.name : (msg.senderName || msg.sender);
    var avaRel = avaFile(msg.sender);
    var colorCls = avatarColor(msg.sender);
    var cont = isContRow(i);
    var contNext = next !== null && !isSysType(next.type) && isContRow(i+1);
    // data-idx = 在 filtered 里的下标，搜索跳转定位用
    html += '<div class="msg-row'+(cont?' cont':'')+(msg.isSend?' sent':'')+'" data-idx="'+i+'">';
    // 头像加载失败 → 回退首字母：不用内联 onerror（esc() 的 &#39; 会被 HTML
    // 解码回 ' 再进入 JS 字面量，名字含单引号时整段语法错误），走捕获级委托
    html += '<div class="msg-avatar '+(cont?'ghost':colorCls)+'">';
    if (!cont) {
      if (avaRel) html += '<img src="'+esc(avaRel)+'" data-ava-fallback="'+esc(name.charAt(0))+'"/>';
      else html += esc(name.charAt(0));
    }
    html += '</div>';
    html += '<div class="msg-bubble">';
    if (isGroup && !msg.isSend && !cont)
      html += '<div class="msg-sender">'+esc(name)+'</div>';
    html += '<div class="bubble-body" title="'+fmtFull(msg.timestamp)+'">';
    if (msg.quote) html += '<div class="msg-quote"><div class="qn">'+esc(msg.quote.sender || msg.quote.displayname || '')+
        (msg.quote.ts ? '<span class="qt">'+fmtDate(msg.quote.ts)+'</span>' : '')+'</div>'+fmtText(msg.quote.content)+'</div>';
    html += renderContent(msg, i);
    if (!contNext) html += '<div class="msg-time">'+fmtTime(msg.timestamp)+'</div>';
    html += '</div></div></div>';
    return html;
  }

  function renderContent(msg, i) {
    var content = msg.content || '';
    var t = msg.type;
    var raw = msg.rawContent || '';

    // 媒体路径（导出时解密落盘）
    if (msg.mediaPath) {
      var p = esc(msg.mediaPath);
      if (t === 3) return '<img class="msg-image" src="'+p+'" loading="lazy" onclick="window.__lbAt('+i+')" onerror="window.__imgErr(this)">';
      if (t === 43) return '<video class="msg-video" controls preload="metadata" src="'+p+'"></video>';
      if (t === 47) return '<img class="msg-emoji" src="'+p+'" loading="lazy" onclick="window.__lbAt('+i+')" onerror="window.__stickerErr(this)">';
      if (t === 34) {
        var vt2 = voiceTrans(raw);
        return '<div class="msg-voice"><audio controls src="'+p+'"></audio></div>' +
               (vt2 ? '<div class="msg-vtrans">'+esc(vt2)+'</div>' : '');
      }
      // 审计 D6：未知类型不再兜底渲染成 <img>（"错挂成图"的放大器），回退纯文本
      console.warn('[render] unknown type', t, 'ts=' + msg.timestamp, 'path=' + p);
      return fmtText(content) || '<em style="opacity:.5">无内容</em>';
    }

    if (t === 3) return phCard(3);
    if (t === 43) return phCard(43);
    // 47 未下载：rawContent 带官方 CDN 地址时在线回退（离线时降级占位）
    if (t === 47) {
      var cdn = xmlAttr(raw, 'emoji', 'cdnurl');
      if (cdn) return '<img class="msg-emoji" src="'+esc(cdn)+'" loading="lazy" onclick="window.__lbAt('+i+')" onerror="window.__stickerErr(this)">';
      return phCard(47);
    }
    if (t === 34) {
      var vt = voiceTrans(raw);
      if (vt) return phCard(34) + '<div class="msg-vtrans">'+esc(vt)+'</div>';
      return phCard(34);
    }
    // 通话（rawContent: <voipmsg>…<calltype>1语音/2视频</calltype><duration>秒</duration>…）
    if (t === 50) {
      var vKind = xmlVal(raw, 'calltype') === '2' ? '视频通话' : '语音通话';
      var vSub = (content || '').replace(/^\[通话\]\s*/, '');
      if (!vSub) {
        var vDur = parseInt(xmlVal(raw, 'duration'), 10);
        if (!isNaN(vDur) && vDur > 0)
          vSub = '通话时长 ' + Math.floor(vDur / 60) + '分' + (vDur % 60) + '秒';
        else
          vSub = '通话';
      }
      return '<div class="wx-card"><div class="wx-card-left">📞</div><div class="wx-card-right"><div class="wx-card-title">'+esc(vKind)+'</div><div class="wx-card-sub">'+esc(vSub)+'</div></div></div>';
    }
    // 位置：rawContent <location poiname="" label="" x=纬度 y=经度 …/>，卡片点击跳腾讯地图；
    // 导出端下载到地图瓦片时（WX_MAPS 命中）缩略图替换 📍 图标
    if (t === 48) {
      var lat = xmlAttr(raw, 'location', 'x'), lng = xmlAttr(raw, 'location', 'y');
      var poi = xmlAttr(raw, 'location', 'poiname') || xmlAttr(raw, 'location', 'label') ||
                (content || '').replace(/^\[位置\]\s*/, '');
      var addr = xmlAttr(raw, 'location', 'label');
      var mapUrl = (lat && lng) ? MAPS[tileKey(lat, lng)] || '' : '';
      var lCard = '<div class="wx-card'+(mapUrl?' wx-card-mapleft':'')+'"><div class="wx-card-left">'+
                  (mapUrl ? '<img class="wx-map" src="'+mapUrl+'">' : '📍')+
                  '</div><div class="wx-card-right"><div class="wx-card-title">'+esc(poi || '位置')+'</div><div class="wx-card-sub">'+esc(addr && addr !== poi ? addr : '点击查看地图')+'</div></div></div>';
      // 跳转用 URI API marker（旧 poi 接口已废弃，HTTP 501"暂不支持此API"）；
      // 坐标即腾讯/GCJ-02，与微信位置一致。整段 marker 值整体编码（含 : ; ,）
      var lHref = '';
      var vLat = parseFloat(lat), vLng = parseFloat(lng);
      if (lat && lng && isFinite(vLat) && isFinite(vLng)) {
        var mv = 'coord:'+vLat+','+vLng+';title:'+(poi || addr || '位置')+';addr:'+(addr || '');
        lHref = 'https://apis.map.qq.com/uri/v1/marker?marker='+encodeURIComponent(mv)+'&referer=SIWX';
      } else if (poi) {
        lHref = 'https://apis.map.qq.com/uri/v1/search?keyword='+encodeURIComponent(poi)+'&referer=SIWX';
      }
      if (lHref) return '<a href="'+esc(lHref)+'" target="_blank" rel="noopener" style="text-decoration:none;color:inherit">'+lCard+'</a>';
      return lCard;
    }

    // 转账/红包：rawContent 含 <wcpayinfo> 才认——[红包] 同时是小黄脸名，
    // 纯靠 content 前缀会把以 [红包] 表情开头的普通文本错挂成红包卡
    if (/^\[转账\]\s+/.test(content) && raw.indexOf('<wcpayinfo') >= 0)
      return '<div class="wx-card wx-transfer"><div class="wx-card-left">💰</div><div class="wx-card-right"><div class="wx-card-title">'+esc(content.replace(/^\[转账\]\s+/,''))+'</div><div class="wx-card-sub">微信转账</div></div></div>';
    if (/^\[红包\]/.test(content) && raw.indexOf('<wcpayinfo') >= 0)
      return '<div class="wx-card wx-transfer"><div class="wx-card-left">🧧</div><div class="wx-card-right"><div class="wx-card-title">'+esc(xmlVal(raw,'sendertitle')||content)+'</div><div class="wx-card-sub">微信红包</div></div></div>';
    // 位置
    if (/^\[位置\]/.test(content))
      return '<div class="wx-card"><div class="wx-card-left">📍</div><div class="wx-card-right"><div class="wx-card-title">'+esc(content.replace(/^\[位置\]\s*/,''))+'</div><div class="wx-card-sub">位置共享</div></div></div>';
    // 链接 / 小程序 / 文件 / 合并转发
    if (t === 49) {
      // D5 导出侧：合并转发（recordinfo）逐条展开，不再只显示 "[链接] 标题"
      if (msg.record) return renderRecord(msg.record);
      // 视频号卡片（type=51 finderFeed）：与聊天页同口径，不再落链接卡兜底
      // （占位 title"当前版本不支持展示该内容"就是从这里漏出去的）
      if (msg.channels) return renderFinder(msg.channels);
      // 引用消息（外层 49、内层 57）：正文走纯文本，引用块由 .msg-quote 呈现，
      // 不再落入链接卡片分支（此前被错挂图片时整卡被顶掉）
      if (msg.quote) return fmtText(content) || '<em style="opacity:.5">引用</em>';
      // 优先复用预解析的 link 字段（单一数据源），rawContent 仅作旧数据兜底
      var lk = msg.link || {};
      var title = lk.title || xmlVal(raw, 'title') || content;
      var url = lk.url || xmlVal(raw, 'url');
      var des = lk.desc || xmlVal(raw, 'des');
      // 文件消息（appmsg type=6）：补大小/格式
      var flen = parseInt(xmlVal(raw, 'totallen'), 10);
      var fext = xmlVal(raw, 'fileext');
      var finfo = '';
      if (isFinite(flen) && flen > 0) finfo = (fext ? fext.toUpperCase() + ' · ' : '') + fmtSize(flen);
      else if (fext) finfo = fext.toUpperCase();
      var html = '<div class="wx-card"><div class="wx-card-left">'+(finfo ? '📄' : '🔗')+'</div><div class="wx-card-right">';
      html += '<div class="wx-card-title">'+esc(title)+'</div>';
      var sub2 = (des ? esc(des.substring(0,80)) : '') + (finfo ? (des ? ' · ' : '') + esc(finfo) : '');
      if (sub2) html += '<div class="wx-card-sub">'+sub2+'</div>';
      if (url) {
        html += '</div></div>';
        var href = safeUrl(url);
        // 非白名单 scheme（javascript:/data: 等）退化为纯卡片，不给可点击链接
        if (href) return '<a href="'+esc(href)+'" target="_blank" rel="noopener" style="text-decoration:none;color:inherit">'+html+'</a>';
        return html;
      }
      return html + '</div></div>';
    }
    // 名片：旧版是 <nickname>/<province>/<city>/<desc>/<sign> 子元素；
    // 新版（实测 2026-09）是自闭合属性式 <msg nickname="…" sign="…" …/>，
    // 只找子元素会把名片打成光杆"[名片]"——两种形态都试。
    if (t === 42) {
      var cnick = xmlVal(raw, 'nickname') || xmlAttr(raw, 'msg', 'nickname')
                  || content.replace(/^\[名片\]\s*/, '') || '个人名片';
      var csub = [];
      var cprov = ((xmlVal(raw, 'province') || xmlAttr(raw, 'msg', 'province')) + ' '
                   + (xmlVal(raw, 'city') || xmlAttr(raw, 'msg', 'city'))).trim();
      if (cprov) csub.push(cprov);
      var csign = xmlVal(raw, 'desc') || xmlVal(raw, 'sign')
                  || xmlAttr(raw, 'msg', 'desc') || xmlAttr(raw, 'msg', 'sign');
      if (csign) csub.push(csign.length > 40 ? csign.substring(0, 40) + '…' : csign);
      return '<div class="wx-card"><div class="wx-card-left">👤</div><div class="wx-card-right"><div class="wx-card-title">'+esc(cnick)+'</div><div class="wx-card-sub">'+esc(csub.join(' · ') || '个人名片')+'</div></div></div>';
    }

    if (!content) return '<em style="opacity:.5">无内容</em>';
    return fmtText(content);
  }

  // 合并转发（recordinfo）逐条展开：标题 + 逐条 sender/text/时间 + 条数脚注。
  // 子消息时间：服务器端形态是 "YYYY-MM-DD HH:MM" 字符串（item.time），
  // 本地形态是 epoch（item.ts），此前时间整体丢失。
  function recordItemTime(it) {
    if (it.time) return it.time;
    if (it.ts) return fmtDate(it.ts) + ' ' + fmtTime(it.ts);
    return '';
  }
  function renderRecord(rec) {
    var items = rec.items || [];
    var h = '<div class="msg-record"><div class="msg-record-title">'+esc(rec.title || '聊天记录')+'</div>';
    h += '<div class="msg-record-items">';
    items.forEach(function(it) {
      var t = recordItemTime(it);
      var rt = t ? '<span class="rt">'+esc(t)+'</span>' : '';
      var rs = '<span class="rs">'+esc(it.sender || '')+'</span>';
      if (it.kind === 'image' && it.mediaFile) {
        // issue #31：子项图片已随导出解密，直接用相对路径引用
        h += '<div class="msg-record-item">'+rs+
             '<img class="msg-record-img" src="'+esc(it.mediaFile)+'" alt="图片" loading="lazy">'+rt+'</div>';
      } else if (it.kind === 'record' && it.record) {
        h += '<div class="msg-record-item msg-record-nested">'+rs+renderRecord(it.record)+rt+'</div>';
      } else {
        h += '<div class="msg-record-item">'+rs+fmtText(it.title || it.text || '')+rt+'</div>';
      }
    });
    if (!items.length) h += '<div class="msg-record-item"><span class="rt">子消息详情未包含在导出数据中</span></div>';
    h += '</div><div class="msg-record-count">'+(rec.count || items.length)+' 条消息</div></div>';
    return h;
  }

  // 视频号卡片（type=51）：还原客户端可见信息（描述/作者/封面/时长）。
  // cover/avatar 是腾讯 CDN 公网直链，离线打开可能已过期——裂图即隐藏，
  // 文字信息（描述+作者）始终可用。播放不强求（scheme 无公开格式）。
  function renderFinder(ch) {
    var dur = ch.duration ? '<span class="finder-dur">'+
      Math.floor(ch.duration/60)+':'+String(ch.duration%60).padStart(2,'0')+'</span>' : '';
    var cover = ch.cover ? '<div class="finder-cover">'+
      '<img loading="lazy" src="'+esc(ch.cover)+'" alt="" '+
      'onerror="this.parentNode.style.display=\'none\'"><span class="finder-play">▶</span>'+dur+'</div>' : '';
    var ava = ch.avatar ? '<img class="finder-ava" loading="lazy" src="'+esc(ch.avatar)+
      '" alt="" onerror="this.style.display=\'none\'">' : '';
    var author = ch.nickname ? '<div class="finder-author">'+ava+'视频号 · '+
      esc(ch.nickname)+(ch.mediaCount > 1 ? ' · '+ch.mediaCount+' 个视频' : '')+'</div>' : '';
    var desc = ch.desc ? '<div class="finder-desc">'+esc(ch.desc)+'</div>' : '';
    return '<div class="finder-card">'+cover+
      '<div class="finder-body">'+desc+author+'</div></div>';
  }

  // ── 窗口式渲染引擎 ──
  function showLoading() { document.getElementById('loadingIndicator').style.display = 'block'; }
  function hideLoading() { document.getElementById('loadingIndicator').style.display = 'none'; }

  function renderWindow(start, end) {
    renderStart = start; renderEnd = end;
    var html = '';
    for (var i = start; i < end; i++) html += rowHTML(i);
    container.innerHTML = html;
    hideLoading();
  }
  function scrollBottom() { chatBody.scrollTop = chatBody.scrollHeight; }

  function appendBatch(done) {
    if (isLoading || renderEnd >= filtered.length) {
      hideLoading();
      if (done) done();
      return;
    }
    isLoading = true;
    showLoading();
    // setTimeout 而非 requestAnimationFrame：后台标签页里 rAF 会被冻结，
    // 批次渲染就再也不触发；宏任务在任意可见性下都会执行
    setTimeout(function() {
      var end2 = Math.min(renderEnd + BATCH, filtered.length);
      var html = '';
      for (var i = renderEnd; i < end2; i++) html += rowHTML(i);
      container.insertAdjacentHTML('beforeend', html);
      renderEnd = end2;
      isLoading = false;
      trimTop();
      hideLoading();
      if (done) done();
    }, 0);
  }

  function prependBatch(done) {
    if (isLoading || renderStart <= 0) {
      if (done) done();
      return;
    }
    isLoading = true;
    setTimeout(function() {
      var start2 = Math.max(0, renderStart - BATCH);
      var html = '';
      for (var i = start2; i < renderStart; i++) html += rowHTML(i);
      var oldFirst = container.firstElementChild;
      var before = chatBody.scrollTop, hBefore = chatBody.scrollHeight;
      // afterbegin 保序插入（逐个 insertBefore(firstChild) 会整段倒序）
      container.insertAdjacentHTML('afterbegin', html);
      // 旧窗口首条的日期条若与新块末行同日，就是窗口切分产生的重复，去掉
      if (oldFirst && oldFirst.classList.contains('date-divider') &&
          oldFirst.dataset.date === fmtISO(filtered[renderStart - 1].timestamp))
        oldFirst.remove();
      renderStart = start2;
      // 上方插入后补偿滚动量，视觉位置不跳
      chatBody.scrollTop = before + (chatBody.scrollHeight - hBefore);
      isLoading = false;
      trimBottom();
      if (done) done();
    }, 0);
  }

  // 窗口总量超上限时从对侧裁剪：分组状态是下标的纯函数，裁剪零副作用
  function trimTop() {
    while (renderEnd - renderStart > MAX_ROWS && container.firstElementChild) {
      while (container.firstElementChild) {
        var el = container.firstElementChild;
        var counted = el.dataset.idx != null;
        el.remove();
        if (counted) { renderStart++; break; }
      }
    }
  }
  function trimBottom() {
    while (renderEnd - renderStart > MAX_ROWS && container.lastElementChild) {
      var el = container.lastElementChild;
      var counted = el.dataset.idx != null;
      el.remove();
      if (counted) renderEnd--;
    }
  }

  function renderAll() {
    searchPos = -1;
    buildGallery();
    if (!filtered.length) {
      // 搜索/筛选 0 结果时给出空态，而不是一片空白
      container.innerHTML = '<div class="no-result">没有匹配的消息</div>';
      hideLoading();
      return;
    }
    // 无筛选 → 打开定位最新一屏；有筛选/搜索 → 从第一条结果开始
    if (activeTypes.size > 0 || fStart || fEnd || searchKw) {
      stickBottom = false;
      renderWindow(0, Math.min(BATCH, filtered.length));
      chatBody.scrollTop = 0;
    } else {
      stickBottom = true;
      renderWindow(Math.max(0, filtered.length - BATCH), filtered.length);
      scrollBottom();
    }
  }

  chatBody.addEventListener('scroll', function() {
    if (chatBody.scrollTop < 500 && renderStart > 0) prependBatch();
    if (chatBody.scrollTop + chatBody.clientHeight >= chatBody.scrollHeight - 600) appendBatch();
    // 用户贴底时保持贴底：上方懒加载图片撑高会把视图推离底部
    stickBottom = chatBody.scrollTop + chatBody.clientHeight >= chatBody.scrollHeight - 40;
  });
  // 图片加载完成撑高文档时，若用户本贴底则重新贴底（捕获级委托）
  var stickBottom = true;
  document.addEventListener('load', function(e) {
    if (!stickBottom || !e.target || e.target.tagName !== 'IMG') return;
    if (!e.target.closest || !e.target.closest('#chatBody')) return;
    scrollBottom();
  }, true);

  // ── 灯箱：图库 = 当前筛选下所有可看的图片/表情，支持左右切换/计数/下载 ──
  function buildGallery() {
    gallery.length = 0;
    for (var i = 0; i < filtered.length; i++) {
      var m = filtered[i], src = null, kind = '';
      // 图源一律过白名单：路径来自导出数据（可能被改动）与对端 CDN 属性，
      // 不放行 javascript:/data: 之类 scheme；判空即跳过，不进图库
      if (m.type === 3 && m.mediaPath) { src = safeMedia(m.mediaPath); kind = 'image'; }
      else if (m.type === 47) {
        src = safeMedia(m.mediaPath) ||
          safeMedia(xmlAttr(m.rawContent || '', 'emoji', 'cdnurl'));
        if (src) kind = 'sticker';
      }
      if (!src) continue;
      var mem = members[m.sender];
      var nm = mem ? mem.name : (m.senderName || m.sender);
      gallery.push({idx: i, src: src,
                    label: (nm ? nm + ' · ' : '') + fmtFull(m.timestamp),
                    name: 'siwx_' + kind + '_' + m.timestamp});
    }
  }
  window.__lbAt = function(i) {
    for (var g = 0; g < gallery.length; g++)
      if (gallery[g].idx === i) { showLb(g); return; }
  };
  function showLb(pos) {
    if (!gallery.length) return;
    lbPos = (pos + gallery.length) % gallery.length;
    var it = gallery[lbPos];
    // 二次白名单：图库已过滤过，这里再兜一次，确保 src/href 不会是 javascript:
    var src = safeMedia(it.src);
    var img = document.getElementById('lightboxImg');
    var dl = document.getElementById('lbDownload');
    if (src) {
      img.src = src;
      dl.href = src;                       // 下载链接同走白名单
      dl.setAttribute('download', it.name);
      dl.style.display = '';
    } else {
      // 源不可用：不给可点的 href（无 href 的 <a> 不触发导航），下载入口隐藏
      img.removeAttribute('src');
      dl.removeAttribute('href');
      dl.removeAttribute('download');
      dl.style.display = 'none';
    }
    document.getElementById('lbCounter').textContent = (lbPos + 1) + ' / ' + gallery.length;
    document.getElementById('lbCaption').textContent = it.label;
    var one = gallery.length <= 1;
    document.getElementById('lbPrev').style.display = one ? 'none' : '';
    document.getElementById('lbNext').style.display = one ? 'none' : '';
    img.classList.remove('lb-zoom');
    document.getElementById('lightbox').classList.add('active');
  }
  function hideLb() {
    var lb = document.getElementById('lightbox');
    lb.classList.remove('active');
    document.getElementById('lightboxImg').src = '';
  }
  document.getElementById('lbPrev').addEventListener('click', function() { showLb(lbPos - 1); });
  document.getElementById('lbNext').addEventListener('click', function() { showLb(lbPos + 1); });
  document.getElementById('lbClose').addEventListener('click', hideLb);
  var lightbox = document.getElementById('lightbox');
  // 点背景或图片本体关闭（按钮不冒泡关闭——download/切换不能被误触）
  lightbox.addEventListener('click', function(e) {
    if (e.target === this || e.target.id === 'lightboxImg') hideLb();
  });
  // 审计 F5：触屏适配——轻触关闭，长按图片保留系统级保存/分享菜单
  lightbox.addEventListener('touchend', function(e) {
    if (e.target === this || e.target.id === 'lightboxImg') hideLb();
  }, {passive: true});
  document.getElementById('lightboxImg').addEventListener('dblclick', function() {
    this.classList.toggle('lb-zoom');
  });
  // 灯箱 Esc 关闭 + 左右方向键切换
  document.addEventListener('keydown', function(ev) {
    if (!lightbox.classList.contains('active')) return;
    if (ev.key === 'Escape') hideLb();
    else if (ev.key === 'ArrowLeft') showLb(lbPos - 1);
    else if (ev.key === 'ArrowRight') showLb(lbPos + 1);
  });

  window.__imgErr = function(img) {
    // 审计 §4.6：onerror 静默处理，破图无从排查；src 在替换前取
    console.warn('[img-err]', img.getAttribute('src'), 'ts=' + (img.getAttribute('data-ts') || ''));
    // img 是替换元素，textContent 永远不渲染——必须换成真正的占位元素
    replaceWithPh(img, 3);
  };
  // 47 表情 CDN 回退失败（离线/URL 失效）→ 占位
  window.__stickerErr = function(img) {
    console.warn('[sticker-err]', img.getAttribute('src'));
    replaceWithPh(img, 47);
  };
  function replaceWithPh(img, type) {
    var tmp = document.createElement('div');
    tmp.innerHTML = phCard(type);
    img.replaceWith(tmp.firstChild);
  }
  // 头像加载失败（error 不冒泡，捕获接管）：隐藏图片、回退首字母
  document.addEventListener('error', function(ev) {
    var img = ev.target;
    if (!img || img.tagName !== 'IMG' || img.dataset.avaFallback === undefined) return;
    img.parentElement.textContent = img.dataset.avaFallback;
  }, true);

  function applyFilter() {
    filtered = messages.filter(function(m) {
      var t = m.type;
      if (activeTypes.size > 0 && !activeTypes.has(t) && t !== 10000 && t !== 10002) return false;
      if (fStart && m.timestamp < fStart) return false;
      if (fEnd && m.timestamp > fEnd) return false;
      if (searchKw) {
        var kw = searchKw.toLowerCase();
        var hit = (m.content||'').toLowerCase().indexOf(kw) >= 0 ||
                  (m.senderName||'').toLowerCase().indexOf(kw) >= 0;
        // 链接卡标题与合并转发子消息文本不在 content 里（后者只有前 3 条预览）
        if (!hit && m.link)
          hit = ((m.link.title||'') + ' ' + (m.link.url||'')).toLowerCase().indexOf(kw) >= 0;
        if (!hit && m.record && m.record.items) {
          var blob = ' ' + (m.record.title||'');
          for (var ri = 0; ri < m.record.items.length; ri++)
            blob += ' ' + (m.record.items[ri].sender||'') + ' ' + (m.record.items[ri].text||'');
          hit = blob.toLowerCase().indexOf(kw) >= 0;
        }
        if (!hit) return false;
      }
      return true;
    });
    renderAll();
    document.getElementById('searchCount').textContent =
      searchKw ? filtered.length + ' 条匹配' : '';
    syncChips();
  }

  // ── 生效中的筛选条件胶囊：面板关闭后条件仍可见，可逐个移除 ──
  var TYPE_NAMES = {1:'文本',3:'图片',34:'语音',43:'视频',47:'表情',49:'链接',57:'引用',
                    42:'名片',48:'位置',50:'通话',10002:'撤回'};
  function syncChips() {
    var box = document.getElementById('filterChips');
    var chips = [];
    if (activeTypes.size)
      activeTypes.forEach(function(t) {
        chips.push({label: '类型: ' + (TYPE_NAMES[t] || t), fn: function() {
          activeTypes.delete(t);
          if (rendererSyncTypeTags) rendererSyncTypeTags();
          applyFilter();
        }});
      });
    if (fStart || fEnd)
      chips.push({label: '日期: ' + (fStart ? fmtDate(fStart) : '…') + ' ~ ' +
                  (fEnd ? fmtDate(fEnd) : '…'), fn: function() {
        document.getElementById('fStart').value = '';
        document.getElementById('fEnd').value = '';
        fStart = fEnd = 0;
        applyFilter();
      }});
    if (searchKw)
      chips.push({label: '搜索: ' + searchKw, fn: function() {
        document.getElementById('searchInput').value = '';
        searchKw = '';
        applyFilter();
      }});
    if (!chips.length) { box.className = 'filter-chips'; box.innerHTML = ''; return; }
    var html = '';
    chips.forEach(function(c, i) {
      html += '<span class="chip"><span class="chip-label">' + esc(c.label) + '</span>' +
              '<button type="button" class="chip-x" data-chip="' + i + '" title="移除该条件">✕</button></span>';
    });
    box.innerHTML = html;
    box.className = 'filter-chips active';
    box.querySelectorAll('.chip-x').forEach(function(btn) {
      btn.addEventListener('click', function() {
        chips[Number(btn.dataset.chip)].fn();
      });
    });
  }

  // ── 媒体缺失汇总条：导出端没拿到的媒体，页面上必须明确说清"缺了什么、
  // 为什么"。点类型 chip 直接筛选到该类型；收起后本次浏览不再打扰 ──
  function renderMediaBanner() {
    var counts = {};
    messages.forEach(function(m) {
      if (!m.mediaPath && PH_META[m.type])
        counts[m.type] = (counts[m.type] || 0) + 1;
    });
    var box = document.getElementById('mediaBanner');
    var types = Object.keys(counts).map(Number);
    if (!types.length) { box.style.display = 'none'; return; }
    var html = '<span class="mb-title">部分媒体未包含在此导出中：</span>';
    types.sort(function(a, b) { return counts[b] - counts[a]; }).forEach(function(t) {
      html += '<button type="button" class="mb-chip" data-type="' + t + '">' +
              PH_META[t][1] + ' ' + counts[t] + ' 条' +
              '<span class="mb-reason">(' + esc(phReason(t)) + ')</span></button>';
    });
    html += '<button type="button" class="mb-close" title="收起">✕</button>';
    box.innerHTML = html;
    box.style.display = 'flex';
    box.querySelectorAll('.mb-chip').forEach(function(chip) {
      chip.addEventListener('click', function() {
        activeTypes.clear();
        activeTypes.add(Number(chip.dataset.type));
        if (rendererSyncTypeTags) rendererSyncTypeTags();
        applyFilter();
        chatBody.scrollTop = 0;
      });
    });
    box.querySelector('.mb-close').addEventListener('click', function() {
      box.style.display = 'none';
    });
  }

  // 统计面板（模态框）：类型卡可点击 → 直接按该类型筛选
  function renderStats() {
    var types = {};
    messages.forEach(function(m) {
      var k = m.type;
      types[k] = (types[k] || 0) + 1;
    });
    var row = document.getElementById('statsRow');
    var html = '';
    html += '<div class="stat-card"><div class="stat-num">'+messages.length+'</div><div class="stat-label">总消息</div></div>';
    var membersCount = Object.keys(members).length;
    html += '<div class="stat-card"><div class="stat-num">'+membersCount+'</div><div class="stat-label">成员</div></div>';
    var days = new Set(messages.map(function(m){ return fmtDate(m.timestamp); }));
    html += '<div class="stat-card"><div class="stat-num">'+days.size+'</div><div class="stat-label">天数</div></div>';
    Object.keys(types).sort(function(a,b){return types[b]-types[a];}).forEach(function(k) {
      var n = TYPE_NAMES[k] || ('类型'+k);
      html += '<div class="stat-card clickable" data-type="'+k+'" title="点击筛选该类型">'+
              '<div class="stat-num">'+types[k]+'</div><div class="stat-label">'+n+'</div></div>';
    });
    row.innerHTML = html;
    row.querySelectorAll('.stat-card.clickable').forEach(function(card) {
      card.addEventListener('click', function() {
        activeTypes.clear();
        activeTypes.add(Number(card.dataset.type));
        if (rendererSyncTypeTags) rendererSyncTypeTags();
        applyFilter();
        document.getElementById('statsPanel').close();
        chatBody.scrollTop = 0;
      });
    });
  }

  // 类型筛选
  function renderTypeFilters() {
    var types = {};
    messages.forEach(function(m) {
      if (m.type !== 10000 && m.type !== 10002)
        types[m.type] = (types[m.type] || 0) + 1;
    });
    var box = document.getElementById('typeFilters');
    var html = '<button type="button" class="type-tag active" data-type="all">全部</button>';
    Object.keys(types).sort().forEach(function(k) {
      html += '<button type="button" class="type-tag" data-type="'+k+'">'+(TYPE_NAMES[k]||'类型'+k)+' <span class="tag-count">'+types[k]+'</span></button>';
    });
    box.innerHTML = html;
    // 高亮一律由 activeTypes 重算：「全部」与具体类型互斥，不会同时亮着
    function syncTypeTags() {
      box.querySelectorAll('.type-tag').forEach(function(t) {
        t.classList.toggle('active', t.dataset.type === 'all'
          ? activeTypes.size === 0 : activeTypes.has(Number(t.dataset.type)));
      });
    }
    box.querySelectorAll('.type-tag').forEach(function(tag) {
      tag.addEventListener('click', function() {
        // activeTypes 存数值，与 m.type（localType 整数）同型；此前存 dataset 字符串
        // 导致 has(number) 恒假 —— 勾选任一类型后除系统/撤回外全部消息被隐藏。
        var type = this.dataset.type === 'all' ? 'all' : Number(this.dataset.type);
        if (type === 'all') { activeTypes.clear(); }
        else { activeTypes.has(type) ? activeTypes.delete(type) : activeTypes.add(type); }
        syncTypeTags();
        applyFilter();
      });
    });
    rendererSyncTypeTags = syncTypeTags;
  }

  // 事件绑定 —— 统计/筛选是 <dialog> 模态框：不再下推消息流，
  // Esc 由 dialog 原生处理，点 backdrop 空白处同样关闭
  document.getElementById('statsToggle').addEventListener('click', function() {
    document.getElementById('statsPanel').showModal();
  });
  document.getElementById('filterToggle').addEventListener('click', function() {
    document.getElementById('filterPanel').showModal();
  });
  ['statsPanel', 'filterPanel'].forEach(function(id) {
    var dlg = document.getElementById(id);
    dlg.addEventListener('click', function(e) {
      if (e.target === dlg) dlg.close();   // 点在 backdrop 上（非内部元素）
    });
  });
  document.querySelectorAll('.modal-close').forEach(function(btn) {
    btn.addEventListener('click', function() {
      document.getElementById(btn.dataset.close).close();
    });
  });

  // 主题：跟随控制台的偏好键（siwx-theme），无记录时跟随系统；
  // data-theme 挂在 <html> 上，让 color-scheme 覆盖视口滚动条
  var ICON_MOON = '<svg width="17" height="17" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round"><path d="M21 12.8A9 9 0 1 1 11.2 3a7 7 0 0 0 9.8 9.8z"/></svg>';
  var ICON_SUN = '<svg width="17" height="17" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="12" r="4"/><path d="M12 2v2M12 20v2M4.9 4.9l1.4 1.4M17.7 17.7l1.4 1.4M2 12h2M20 12h2M6.3 17.7l-1.4 1.4M19.1 4.9l-1.4 1.4"/></svg>';
  var themeBtn = document.getElementById('themeToggle');
  function syncThemeIcon() {
    themeBtn.innerHTML = document.documentElement.dataset.theme === 'dark' ? ICON_SUN : ICON_MOON;
  }
  try {
    var themeSaved = localStorage.getItem('siwx-theme');
    if (themeSaved === 'dark' || (!themeSaved && matchMedia('(prefers-color-scheme: dark)').matches))
      document.documentElement.dataset.theme = 'dark';
  } catch (e) {}
  syncThemeIcon();
  themeBtn.addEventListener('click', function() {
    var el = document.documentElement;
    el.dataset.theme = el.dataset.theme === 'dark' ? '' : 'dark';
    try { localStorage.setItem('siwx-theme', el.dataset.theme); } catch (e) {}
    syncThemeIcon();
  });
  document.getElementById('searchInput').addEventListener('input', function() {
    var v = this.value;
    clearTimeout(searchTimer);
    // 每敲一个字符就全量重渲染大导出文件会卡顿，300ms 防抖
    searchTimer = setTimeout(function() {
      searchKw = v;
      applyFilter();
    }, 300);
  });
  // 搜索跳转：命中结果在 filtered 里本来就连续，prev/next 就是游标 +1/-1；
  // 目标不在当前窗口时直接重建窗口（渲染是随机访问的）
  function ensureRendered(idx, cb) {
    if (idx >= renderStart && idx < renderEnd) { cb(); return; }
    stickBottom = false;
    var start = Math.max(0, Math.min(idx - 10, Math.max(0, filtered.length - BATCH)));
    renderWindow(start, Math.min(start + BATCH, filtered.length));
    cb();
  }
  function jumpSearch(dir) {
    if (!searchKw || !filtered.length) return;
    searchPos = (searchPos + dir + filtered.length) % filtered.length;
    ensureRendered(searchPos, function() {
      var el = container.querySelector('[data-idx="' + searchPos + '"]');
      if (!el) return;
      el.scrollIntoView({block: 'center'});
      if (el.classList.contains('msg-row')) {
        el.classList.add('flash');
        setTimeout(function() { el.classList.remove('flash'); }, 1400);
      }
    });
  }
  document.getElementById('searchPrev').addEventListener('click', function() { jumpSearch(-1); });
  document.getElementById('searchNext').addEventListener('click', function() { jumpSearch(1); });
  document.getElementById('searchInput').addEventListener('keydown', function(e) {
    if (e.key === 'Enter') jumpSearch(e.shiftKey ? -1 : 1);
  });
  // P1-5：搜索栏默认 display:none，页头放大镜是唯一入口（button 的 Enter
  // 天然触发 click）；再点一次收起，收起时与 searchClose 同口径清空条件，
  // 避免留下看不见的过滤。
  document.getElementById('searchToggle').addEventListener('click', function() {
    var bar = document.getElementById('searchBar');
    var open = bar.classList.toggle('active');
    var input = document.getElementById('searchInput');
    if (open) {
      input.focus();
      input.select();
    } else if (searchKw) {
      input.value = '';
      searchKw = '';
      applyFilter();
    }
  });
  document.getElementById('searchClose').addEventListener('click', function() {
    // 关闭搜索栏必须连条件一起清——隐藏着的过滤条件比没有条件更难排查
    var bar = document.getElementById('searchBar');
    bar.classList.remove('active');
    if (searchKw) {
      document.getElementById('searchInput').value = '';
      searchKw = '';
      applyFilter();
    }
  });
  document.getElementById('fApply').addEventListener('click', function() {
    var s = document.getElementById('fStart').value;
    var e = document.getElementById('fEnd').value;
    fStart = s ? new Date(s + 'T00:00:00').getTime() / 1000 : 0;
    fEnd = e ? new Date(e + 'T23:59:59').getTime() / 1000 : 0;
    // 起止倒置时交换并回写输入框，用户看到的就是实际生效的范围
    if (fStart && fEnd && fStart > fEnd) {
      var tmp = fStart; fStart = fEnd; fEnd = tmp;
      document.getElementById('fStart').value = e;
      document.getElementById('fEnd').value = s;
    }
    applyFilter();
  });
  document.getElementById('fReset').addEventListener('click', function() {
    document.getElementById('fStart').value = '';
    document.getElementById('fEnd').value = '';
    fStart = fEnd = 0;
    activeTypes.clear();
    if (rendererSyncTypeTags) rendererSyncTypeTags();
    applyFilter();
  });

  // ── 悬浮导航：回到最新 / 跳到最早 / 按日期跳转 ──
  function goLatest() {
    stickBottom = true;
    renderWindow(Math.max(0, filtered.length - BATCH), filtered.length);
    scrollBottom();
  }
  function goEarliest() {
    stickBottom = false;
    renderWindow(0, Math.min(BATCH, filtered.length));
    chatBody.scrollTop = 0;
  }
  function firstIndexOnOrAfter(ts) {
    var lo = 0, hi = filtered.length;
    while (lo < hi) {
      var mid = (lo + hi) >> 1;
      if (filtered[mid].timestamp < ts) lo = mid + 1; else hi = mid;
    }
    return lo;
  }
  function jumpToDate(str) {
    if (!str || !filtered.length) return;
    var ts = new Date(str + 'T00:00:00').getTime() / 1000;
    var i = firstIndexOnOrAfter(ts);
    if (i >= filtered.length) { goLatest(); return; }   // 晚于全部消息 → 最新
    stickBottom = false;
    renderWindow(Math.max(0, i), Math.min(i + BATCH, filtered.length));
    chatBody.scrollTop = 0;
  }
  document.getElementById('fabLatest').addEventListener('click', goLatest);
  document.getElementById('fabTop').addEventListener('click', goEarliest);
  var datePop = document.getElementById('datePop');
  document.getElementById('fabDate').addEventListener('click', function() {
    datePop.classList.toggle('active');
  });
  document.getElementById('jumpGo').addEventListener('click', function() {
    jumpToDate(document.getElementById('jumpDate').value);
    datePop.classList.remove('active');
  });
  document.getElementById('jumpDate').addEventListener('keydown', function(e) {
    if (e.key === 'Enter') {
      jumpToDate(this.value);
      datePop.classList.remove('active');
    }
  });
  // 点吸顶日期条 → 带着该日期打开跳转弹层
  container.addEventListener('click', function(e) {
    var d = e.target.closest ? e.target.closest('.date-divider') : null;
    if (d && d.dataset.date) {
      document.getElementById('jumpDate').value = d.dataset.date;
      datePop.classList.add('active');
    }
  });

  // 初始化
  document.getElementById('chatTitle').textContent = data.meta.sessionName;
  document.title = data.meta.sessionName + ' · 聊天记录';   // 浏览器历史里能区分会话
  var range = data.meta.dateRange;
  document.getElementById('chatMeta').textContent =
    data.meta.messageCount + ' 条消息 · ' + fmtDate(range.start) + ' - ' + fmtDate(range.end);
  // 页头头像用真实头像（私聊优先会话对象，其次导出者），拿不到再回退首字母
  var headerAva = document.getElementById('headerAva');
  var ownAva = avaFile(data.meta.sessionId) || avaFile(ownerId);
  if (ownAva) headerAva.innerHTML = '<img src="' + esc(ownAva) + '" alt="">';
  else headerAva.textContent = data.meta.sessionName.charAt(0);
  var jumpInput = document.getElementById('jumpDate');
  jumpInput.min = fmtISO(range.start);
  jumpInput.max = fmtISO(range.end);
  renderStats();
  renderTypeFilters();
  renderMediaBanner();
  renderAll();
})();
