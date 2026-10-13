
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
  var loaded = 0, BATCH = 50, isLoading = false;
  var activeTypes = new Set();
  var searchKw = '', fStart = 0, fEnd = 0;
  var rendererSyncTypeTags = null;   // renderTypeFilters 注入：按 activeTypes 重算高亮
  var searchTimer = null;

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
  function fmtDate(ts) { var d = new Date(ts*1000); return d.getFullYear()+'年'+(d.getMonth()+1)+'月'+d.getDate()+'日'; }
  function fmtTime(ts) { var d = new Date(ts*1000); var p = function(n){return String(n).padStart(2,'0')}; return p(d.getHours())+':'+p(d.getMinutes()); }
  function fmtFull(ts) { return fmtDate(ts)+' '+fmtTime(ts); }
  function fmtSize(n) {
    if (!isFinite(n) || n <= 0) return '';
    var units = ['B', 'KB', 'MB', 'GB'], i = 0;
    while (n >= 1024 && i < units.length - 1) { n /= 1024; i++; }
    return (i === 0 ? n : n.toFixed(1)) + ' ' + units[i];
  }
  function avatarColor(id) { var h = 0; for (var i = 0; i < id.length; i++) h = (h*31 + id.charCodeAt(i)) & 0x7fffffff; return 'c' + (h % 8); }
  function avaFile(un) { return avatarFiles[un] || ''; }

  function xmlVal(raw, tag) {
    var m = (raw||'').match(new RegExp('<'+tag+'>(.*?)</'+tag+'>', 's'));
    if (!m) return '';
    return m[1].replace(/<!\[CDATA\[([\s\S]*?)\]\]>/g, '$1').trim();
  }

  function xmlAttr(raw, tag, attr) {
    var m = (raw||'').match(new RegExp('<'+tag+'\\b[^>]*?\\s'+attr+'="([^"]*)"', 's'));
    return m ? m[1] : '';
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
  function fmtText(s) {
    var h = esc(s);
    h = h.replace(/\[[^\[\]\n]{1,20}\]/g, function(tok) {
      var u = FACES[tok];
      return u ? '<img class="wx-face" alt="'+tok+'" src="'+u+'">' : tok;
    });
    return h;
  }

  function renderMsg(msg, prev) {
    var html = '';
    if (!prev || fmtDate(msg.timestamp) !== fmtDate(prev.timestamp))
      html += '<div class="date-divider"><span>'+fmtDate(msg.timestamp)+'</span></div>';
    var t = msg.type;
    if (t === 10000 || t === 10002 || t === 266287972401) {
      // 10002 的 content 常是 <sysmsg type="revokemsg"> XML——取 replacemsg
      // 的可读文案（含撤回后重新编辑的内容）；取不到再剥标签兜底
      var sc = msg.content || '';
      if (t === 10002 && sc.indexOf('<') === 0)
        sc = xmlVal(sc, 'replacemsg') || sc.replace(/<[^>]+>/g, '');
      html += '<div class="system-msg"><span>'+fmtText(sc)+'</span></div>';
      return html;
    }
    var mem = members[msg.sender];
    var name = mem ? mem.name : (msg.senderName || msg.sender);
    var avaRel = avaFile(msg.sender);
    var colorCls = avatarColor(msg.sender);
    html += '<div class="msg-row'+(msg.isSend?' sent':'')+'">';
    html += '<div class="msg-avatar '+colorCls+'">';
    // 头像加载失败 → 回退首字母：不用内联 onerror（esc() 的 &#39; 会被 HTML
    // 解码回 ' 再进入 JS 字面量，名字含单引号时整段语法错误），走捕获级委托
    if (avaRel) html += '<img src="'+esc(avaRel)+'" data-ava-fallback="'+esc(name.charAt(0))+'"/>';
    else html += esc(name.charAt(0));
    html += '</div>';
    html += '<div class="msg-bubble">';
    if (isGroup && !msg.isSend) html += '<div class="msg-sender">'+esc(name)+'</div>';
    html += '<div class="bubble-body">';
    if (msg.quote) html += '<div class="msg-quote"><div class="qn">'+esc(msg.quote.sender || msg.quote.displayname || '')+
        (msg.quote.ts ? '<span class="qt">'+fmtDate(msg.quote.ts)+'</span>' : '')+'</div>'+fmtText(msg.quote.content)+'</div>';
    html += renderContent(msg);
    html += '<div class="msg-time">'+fmtTime(msg.timestamp)+'</div>';
    html += '</div></div></div>';
    return html;
  }

  function renderContent(msg) {
    var content = msg.content || '';
    var t = msg.type;
    var raw = msg.rawContent || '';

    // 媒体路径（导出时解密落盘）
    if (msg.mediaPath) {
      var p = esc(msg.mediaPath);
      if (t === 3) return '<img class="msg-image" src="'+p+'" loading="lazy" onclick="window.__lb(this.src)" onerror="window.__imgErr(this)">';
      if (t === 43) return '<video class="msg-video" controls preload="metadata" src="'+p+'"></video>';
      if (t === 47) return '<img class="msg-emoji" src="'+p+'" loading="lazy" onclick="window.__lb(this.src)" onerror="window.__stickerErr(this)">';
      if (t === 34) {
        var vt2 = voiceTrans(raw);
        return '<div class="msg-voice"><audio controls src="'+p+'"></audio></div>' +
               (vt2 ? '<div class="msg-vtrans">'+esc(vt2)+'</div>' : '');
      }
      // 审计 D6：未知类型不再兜底渲染成 <img>（"错挂成图"的放大器），回退纯文本
      console.warn('[render] unknown type', t, 'ts=' + msg.timestamp, 'path=' + p);
      return fmtText(content) || '<em style="opacity:.5">无内容</em>';
    }

    if (t === 3) return '<div class="msg-image broken">📷 图片未下载</div>';
    if (t === 43) return '<div class="msg-image broken">🎬 视频未下载</div>';
    // 47 未下载：rawContent 带官方 CDN 地址时在线回退（离线时降级占位）
    if (t === 47) {
      var cdn = xmlAttr(raw, 'emoji', 'cdnurl');
      if (cdn) return '<img class="msg-emoji" src="'+esc(cdn)+'" loading="lazy" onclick="window.__lb(this.src)" onerror="window.__stickerErr(this)">';
      return '<div class="msg-image broken">😊 表情</div>';
    }
    if (t === 34) {
      var vt = voiceTrans(raw);
      if (vt) return '<div class="msg-image broken">🎤 语音</div><div class="msg-vtrans">'+esc(vt)+'</div>';
      return '<div class="msg-image broken">🎤 语音</div>';
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
    // 名片：rawContent <nickname>/<province>/<city>/<desc>/<sign>
    if (t === 42) {
      var cnick = xmlVal(raw, 'nickname') || content.replace(/^\[名片\]\s*/, '') || '个人名片';
      var csub = [];
      var cprov = (xmlVal(raw, 'province') + ' ' + xmlVal(raw, 'city')).trim();
      if (cprov) csub.push(cprov);
      var csign = xmlVal(raw, 'desc') || xmlVal(raw, 'sign');
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
      h += '<div class="msg-record-item"><span class="rs">'+esc(it.sender || '')+'</span>'+fmtText(it.text || '')+
           (t ? '<span class="rt">'+esc(t)+'</span>' : '')+'</div>';
    });
    if (!items.length) h += '<div class="msg-record-item"><span class="rt">子消息详情未包含在导出数据中</span></div>';
    h += '</div><div class="msg-record-count">'+(rec.count || items.length)+' 条消息</div></div>';
    return h;
  }

  function renderAll() {
    container.innerHTML = '';
    loaded = 0;
    if (!filtered.length) {
      // 搜索/筛选 0 结果时给出空态，而不是一片空白
      container.innerHTML = '<div class="no-result">没有匹配的消息</div>';
      document.getElementById('loadingIndicator').style.display = 'none';
      return;
    }
    loadMore();
  }

  function loadMore() {
    if (isLoading || loaded >= filtered.length) {
      document.getElementById('loadingIndicator').style.display = 'none';
      return;
    }
    isLoading = true;
    document.getElementById('loadingIndicator').style.display = 'block';
    requestAnimationFrame(function() {
      var end = Math.min(loaded + BATCH, filtered.length);
      var html = '';
      for (var i = loaded; i < end; i++) {
        var prev = i > 0 ? filtered[i-1] : null;
        html += renderMsg(filtered[i], prev);
      }
      container.insertAdjacentHTML('beforeend', html);
      loaded = end;
      isLoading = false;
      if (loaded >= filtered.length)
        document.getElementById('loadingIndicator').style.display = 'none';
    });
  }

  chatBody.addEventListener('scroll', function() {
    if (chatBody.scrollTop + chatBody.clientHeight >= chatBody.scrollHeight - 300) loadMore();
  });

  window.__lb = function(src) {
    document.getElementById('lightbox').classList.add('active');
    document.getElementById('lightboxImg').src = src;
  };
  // 灯箱 Esc 关闭（此前只能点击/触摸关闭）
  document.addEventListener('keydown', function(ev) {
    if (ev.key === 'Escape') document.getElementById('lightbox').classList.remove('active');
  });
  window.__imgErr = function(img) {
    // 审计 §4.6：onerror 静默处理，破图无从排查；src 在替换前取
    console.warn('[img-err]', img.getAttribute('src'), 'ts=' + (img.getAttribute('data-ts') || ''));
    // img 是替换元素，textContent 永远不渲染——必须换成真正的占位元素
    var ph = document.createElement('div');
    ph.className = 'msg-image broken';
    ph.textContent = '📷 图片';
    img.replaceWith(ph);
  };
  // 47 表情 CDN 回退失败（离线/URL 失效）→ 占位
  window.__stickerErr = function(img) {
    console.warn('[sticker-err]', img.getAttribute('src'));
    var ph = document.createElement('div');
    ph.className = 'msg-image broken';
    ph.textContent = '😊 表情';
    img.replaceWith(ph);
  };
  // 头像加载失败（error 不冒泡，捕获接管）：隐藏图片、回退首字母
  document.addEventListener('error', function(ev) {
    var img = ev.target;
    if (!img || img.tagName !== 'IMG' || img.dataset.avaFallback === undefined) return;
    img.parentElement.textContent = img.dataset.avaFallback;
  }, true);
  var lightbox = document.getElementById('lightbox');
  lightbox.addEventListener('click', function() {
    this.classList.remove('active');
  });
  // 审计 F5：触屏适配——导出页原先只有 click，触屏设备无法关闭灯箱；
  // 轻触关闭，长按图片保留系统级保存/分享菜单（-webkit-touch-callout）。
  lightbox.addEventListener('touchend', function(e) {
    this.classList.remove('active');
  }, {passive: true});

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
  }

  // 统计面板
  function renderStats() {
    var types = {};
    messages.forEach(function(m) {
      var k = m.type;
      types[k] = (types[k] || 0) + 1;
    });
    var row = document.getElementById('statsRow');
    var names = {1:'文本',3:'图片',34:'语音',43:'视频',47:'表情',49:'链接',57:'引用',10000:'系统',
                 42:'名片',48:'位置',50:'通话',10002:'撤回'};
    var html = '';
    html += '<div class="stat-card"><div class="stat-num">'+messages.length+'</div><div class="stat-label">总消息</div></div>';
    var membersCount = Object.keys(members).length;
    html += '<div class="stat-card"><div class="stat-num">'+membersCount+'</div><div class="stat-label">成员</div></div>';
    var days = new Set(messages.map(function(m){ return fmtDate(m.timestamp); }));
    html += '<div class="stat-card"><div class="stat-num">'+days.size+'</div><div class="stat-label">天数</div></div>';
    Object.keys(types).sort(function(a,b){return types[b]-types[a];}).forEach(function(k) {
      var n = names[k] || ('类型'+k);
      html += '<div class="stat-card"><div class="stat-num">'+types[k]+'</div><div class="stat-label">'+n+'</div></div>';
    });
    row.innerHTML = html;
  }

  // 类型筛选
  function renderTypeFilters() {
    var types = {};
    messages.forEach(function(m) {
      if (m.type !== 10000 && m.type !== 10002)
        types[m.type] = (types[m.type] || 0) + 1;
    });
    var box = document.getElementById('typeFilters');
    var names = {1:'文本',3:'图片',34:'语音',43:'视频',47:'表情',49:'链接',57:'引用',
                 42:'名片',48:'位置',50:'通话',10002:'撤回'};
    var html = '<button type="button" class="type-tag active" data-type="all">全部</button>';
    Object.keys(types).sort().forEach(function(k) {
      html += '<button type="button" class="type-tag" data-type="'+k+'">'+(names[k]||'类型'+k)+' <span class="tag-count">'+types[k]+'</span></button>';
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

  // 事件绑定
  document.getElementById('statsToggle').addEventListener('click', function() {
    document.getElementById('statsPanel').classList.toggle('active');
  });
  document.getElementById('filterToggle').addEventListener('click', function() {
    document.getElementById('filterPanel').classList.toggle('active');
  });
  document.getElementById('searchToggle').addEventListener('click', function() {
    var bar = document.getElementById('searchBar');
    bar.classList.toggle('active');
    if (bar.classList.contains('active')) bar.querySelector('input').focus();
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

  // 初始化
  document.getElementById('chatTitle').textContent = data.meta.sessionName;
  document.title = data.meta.sessionName + ' · 聊天记录';   // 浏览器历史里能区分会话
  var range = data.meta.dateRange;
  document.getElementById('chatMeta').textContent =
    data.meta.messageCount + ' 条消息 · ' + fmtDate(range.start) + ' - ' + fmtDate(range.end);
  document.getElementById('headerAva').textContent = data.meta.sessionName.charAt(0);
  renderStats();
  renderTypeFilters();
  renderAll();
})();
