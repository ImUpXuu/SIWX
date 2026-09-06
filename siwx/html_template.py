"""HTML 导出模板 —— 数据驱动交互式查看器（参考 CipherTalk 风格）。

把消息数据嵌入 window.CHAT_DATA，JS 端渲染：
统计面板 / 类型筛选 / 日期范围 / 搜索 / 日期跳转 / 明暗主题 / 灯箱。
支持所有消息类型的微信风格渲染。
"""
import json
import re
from datetime import datetime


def _fmt_time(ts):
    return datetime.fromtimestamp(ts or 0).strftime("%Y-%m-%d %H:%M:%S")


def _fmt_date(ts):
    return datetime.fromtimestamp(ts or 0).strftime("%Y年%m月%d日")


def build_chat_data(session, msgs, avatar_map):
    """把内部消息格式转为 CHAT_DATA 嵌入格式。"""
    members_map = {}
    members = []
    for m in msgs:
        un = m["senderUsername"]
        if un and un not in members_map:
            name = m["senderDisplayName"] or un
            members_map[un] = name
            members.append({"id": un, "name": name,
                            "avatar": avatar_map.get(un, "")})

    messages = []
    for m in msgs:
        entry = {
            "timestamp": m["createTime"],
            "sender": m["senderUsername"],
            "senderName": m["senderDisplayName"],
            "type": m["localType"],
            "content": m["content"],
            "rawContent": m["rawContent"][:8000],
            "isSend": m["isSend"],
        }
        if m.get("mediaFile"):
            entry["mediaPath"] = m["mediaFile"]
        if m.get("quote"):
            entry["quote"] = {"sender": m["quote"]["displayname"],
                              "content": m["quote"]["content"]}
        if m.get("link"):
            entry["link"] = {"title": m["link"]["title"],
                             "url": m["link"]["url"] or ""}
        messages.append(entry)

    is_group = session.get("isGroup", False)
    return {
        "meta": {
            "sessionId": session["wxid"],
            "sessionName": session["displayName"],
            "isGroup": is_group,
            "exportTime": int(datetime.now().timestamp() * 1000),
            "messageCount": len(messages),
            "dateRange": {"start": session["firstTimestamp"],
                          "end": session["lastTimestamp"]},
            "ownerId": session.get("ownerId", ""),
        },
        "members": members,
        "avatarFiles": avatar_map,
        "messages": messages,
    }


def render_html(chat_data: dict) -> str:
    """生成自包含 HTML。data = CHAT_DATA dict。"""
    data_json = json.dumps(chat_data, ensure_ascii=False)
    title = chat_data["meta"]["sessionName"]
    count = chat_data["meta"]["messageCount"]

    return _HTML_HEAD + f"\n<script>window.CHAT_DATA = {data_json};</script>\n" + \
        f"\n<script>\nconst MSG_COUNT = {count};\nconst CHAT_TITLE = {json.dumps(title, ensure_ascii=False)};\n" + \
        _HTML_RENDERER + "\n</script>\n</body>\n</html>"


_HTML_HEAD = """<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>聊天记录</title>
<style>
:root {
  --bg:#eef1f6; --chat-bg:#f7f8fa; --header-bg:#fff; --text:#1a2027;
  --sub:#6b7785; --tm:#9aa6b2; --border:#e8ebf0; --recv:#fff; --send:#e7f0ff;
  --accent:#2563eb; --shadow:rgba(20,30,50,.07); --system-bg:rgba(20,30,50,.05);
  --panel:#fff; --tag-bg:#f2f4f7; --link:#2563eb; --media-bg:#eef1f5;
}
[data-theme="dark"] {
  --bg:#0d1117; --chat-bg:#0d1117; --header-bg:#161b22; --text:#e6edf3;
  --sub:#8b98a5; --tm:#6e7b87; --border:#2a313a; --recv:#1c232b; --send:#1e3a5f;
  --accent:#60a5fa; --shadow:rgba(0,0,0,.4); --system-bg:rgba(255,255,255,.06);
  --panel:#161b22; --tag-bg:#222c36; --link:#60a5fa; --media-bg:#222c36;
}
*{margin:0;padding:0;box-sizing:border-box}
body{font-family:-apple-system,"Segoe UI","Microsoft YaHei",sans-serif;
     background:var(--bg);color:var(--text);line-height:1.45;
     -webkit-font-smoothing:antialiased}
.app{max-width:880px;margin:0 auto;height:100vh;display:flex;
     flex-direction:column;background:var(--chat-bg);box-shadow:0 0 60px var(--shadow)}
.chat-header{background:var(--header-bg);padding:12px 18px;display:flex;
             align-items:center;justify-content:space-between;flex-shrink:0;
             border-bottom:1px solid var(--border)}
.header-left{display:flex;align-items:center;gap:12px;min-width:0}
.header-avatar{width:40px;height:40px;border-radius:12px;background:var(--accent);
               color:#fff;display:flex;align-items:center;justify-content:center;
               font-size:18px;font-weight:600;flex-shrink:0;overflow:hidden}
.header-info h1{font-size:16px;font-weight:600;white-space:nowrap;overflow:hidden;
                text-overflow:ellipsis}
.header-meta{font-size:12px;color:var(--sub)}
.header-actions{display:flex;gap:2px}
.icon-btn{background:none;border:none;color:var(--sub);font-size:17px;cursor:pointer;
          padding:8px;border-radius:10px;transition:background .18s}
.icon-btn:hover{background:rgba(20,30,50,.06)}
.stats-panel{background:var(--panel);border-bottom:1px solid var(--border);
             display:none;padding:12px 16px;flex-shrink:0}
.stats-panel.active{display:block}
.stats-row{display:flex;flex-wrap:wrap;gap:8px}
.stat-card{background:var(--tag-bg);border-radius:10px;padding:10px 14px;
           min-width:90px;flex:1;text-align:center}
.stat-num{font-size:22px;font-weight:700;color:var(--accent)}
.stat-label{font-size:11px;color:var(--sub);margin-top:2px}
.filter-panel{background:var(--panel);border-bottom:1px solid var(--border);
              display:none;padding:12px 16px;flex-shrink:0}
.filter-panel.active{display:block}
.filter-label{font-size:12px;font-weight:600;color:var(--sub);margin-bottom:8px}
.filter-tags{display:flex;flex-wrap:wrap;gap:6px;margin-bottom:10px}
.type-tag{display:inline-flex;align-items:center;gap:4px;padding:5px 12px;
          border-radius:16px;border:1.5px solid var(--border);background:var(--tag-bg);
          color:var(--text);font-size:13px;cursor:pointer;user-select:none}
.type-tag.active{background:var(--accent);color:#fff;border-color:var(--accent)}
.filter-divider{height:1px;background:var(--border);margin:10px 0}
.date-row{display:flex;align-items:center;gap:8px;flex-wrap:wrap}
.date-row input[type=date]{padding:6px 10px;border:1.5px solid var(--border);
                           border-radius:8px;background:var(--tag-bg);color:var(--text)}
.filter-btn{padding:6px 16px;border:none;border-radius:8px;background:var(--accent);
            color:#fff;font-size:13px;cursor:pointer}
.filter-btn.secondary{background:var(--tag-bg);color:var(--text);
                      border:1.5px solid var(--border)}
.search-bar{display:none;padding:0 18px 12px;align-items:center;gap:8px;
            border-bottom:1px solid var(--border)}
.search-bar.active{display:flex}
.search-bar input{flex:1;padding:9px 14px;border:1px solid var(--border);
                  border-radius:10px;background:var(--tag-bg);color:var(--text);
                  font-size:14px;outline:none}
.chat-body{flex:1;overflow-y:auto;padding:8px 0}
.chat-body::-webkit-scrollbar{width:6px}
.chat-body::-webkit-scrollbar-thumb{background:rgba(0,0,0,.2);border-radius:3px}
.date-divider{text-align:center;padding:12px 0 8px}
.date-divider span{background:var(--system-bg);color:var(--sub);padding:4px 12px;
                   border-radius:8px;font-size:12px;font-weight:500}
.system-msg{text-align:center;padding:4px 60px;margin:2px 0}
.system-msg span{background:var(--system-bg);color:var(--sub);padding:4px 12px;
                 border-radius:8px;font-size:12px;display:inline-block;
                 max-width:100%;word-break:break-word}
.msg-row{display:flex;padding:4px 14px;align-items:flex-end;gap:8px}
.msg-row.sent{flex-direction:row-reverse}
.msg-avatar{width:36px;height:36px;border-radius:11px;flex-shrink:0;overflow:hidden;
            background:#c7ccd4;display:flex;align-items:center;justify-content:center;
            font-size:14px;font-weight:600;color:#fff;margin-top:2px}
.msg-avatar img{width:100%;height:100%;object-fit:cover}
.msg-avatar.c0{background:#2563eb}.msg-avatar.c1{background:#0891b2}
.msg-avatar.c2{background:#7c3aed}.msg-avatar.c3{background:#0ea5e9}
.msg-avatar.c4{background:#059669}.msg-avatar.c5{background:#db2777}
.msg-avatar.c6{background:#ea580c}.msg-avatar.c7{background:#dc2626}
.msg-bubble{max-width:66%;min-width:60px}
.msg-sender{font-size:12px;color:var(--sub);font-weight:500;margin-bottom:3px;padding:0 6px}
.bubble-body{background:var(--recv);padding:8px 12px 6px;border-radius:14px;
             border:1px solid var(--border);position:relative;
             box-shadow:0 1px 2px var(--shadow);word-break:break-word;
             white-space:pre-wrap;font-size:14px}
.msg-row:not(.sent) .bubble-body{border-top-left-radius:5px}
.msg-row.sent .bubble-body{background:var(--send);border-color:transparent;
                           border-top-right-radius:5px}
.msg-time{font-size:11px;color:var(--tm);text-align:right;margin-top:2px}
.msg-quote{background:var(--media-bg);border-left:3px solid var(--accent);
           border-radius:8px;padding:5px 10px;margin-bottom:6px;font-size:12px;
           color:var(--sub);white-space:pre-wrap;word-break:break-word}
.msg-quote .qn{color:var(--accent);font-weight:600}
.msg-image{cursor:pointer;border-radius:6px;max-width:300px;max-height:300px;
           display:block;object-fit:contain;background:var(--media-bg)}
.msg-image.broken{width:200px;height:60px;display:flex;align-items:center;
                  justify-content:center;background:var(--media-bg);color:var(--sub);
                  font-size:12px;border-radius:6px}
.msg-emoji{max-width:120px;max-height:120px;display:block;cursor:pointer}
.msg-video{max-width:320px;max-height:240px;border-radius:6px;background:#000}
.msg-voice audio{height:32px;max-width:240px}
.wx-card{max-width:260px;padding:11px 13px;border-radius:12px;background:var(--panel);
         border:1px solid var(--border);display:flex;gap:10px}
.wx-card-left{width:38px;height:38px;border-radius:9px;flex-shrink:0;display:flex;
              align-items:center;justify-content:center;font-size:18px;
              background:var(--tag-bg)}
.wx-card-title{font-size:14px;display:-webkit-box;-webkit-line-clamp:2;
               -webkit-box-orient:vertical;overflow:hidden;line-height:1.35}
.wx-card-sub{font-size:11px;color:var(--sub);margin-top:3px}
.wx-card a{color:var(--accent);text-decoration:none}
.wx-transfer{background:#fa9e3b;border-color:transparent;color:#fff}
.wx-transfer .wx-card-title{color:#fff;font-weight:600}
.wx-transfer .wx-card-sub{color:rgba(255,255,255,.8)}
.wx-transfer .wx-card-left{background:rgba(255,255,255,.2);color:#fff}
.chat-footer{background:var(--bg);text-align:center;padding:10px;font-size:12px;
             color:var(--sub);border-top:1px solid var(--border);flex-shrink:0}
.lightbox{display:none;position:fixed;inset:0;background:rgba(0,0,0,.9);z-index:1000;
          align-items:center;justify-content:center;cursor:zoom-out}
.lightbox.active{display:flex}
.lightbox img{max-width:95vw;max-height:95vh;object-fit:contain;border-radius:4px}
.loading{text-align:center;padding:20px;color:var(--sub);font-size:13px}
@media(max-width:600px){.msg-bubble{max-width:80%}.msg-image{max-width:220px}}
</style>
</head>
<body>
<div class="app">
  <header class="chat-header">
    <div class="header-left">
      <div class="header-avatar" id="headerAva">💬</div>
      <div class="header-info"><h1 id="chatTitle"></h1>
        <span class="header-meta" id="chatMeta"></span></div>
    </div>
    <div class="header-actions">
      <button class="icon-btn" id="statsToggle" title="统计">📊</button>
      <button class="icon-btn" id="filterToggle" title="筛选">🔽</button>
      <button class="icon-btn" id="themeToggle" title="主题">🌓</button>
      <button class="icon-btn" id="searchToggle" title="搜索">🔍</button>
    </div>
  </header>
  <div class="stats-panel" id="statsPanel"><div class="stats-row" id="statsRow"></div></div>
  <div class="filter-panel" id="filterPanel">
    <div class="filter-label">消息类型</div>
    <div class="filter-tags" id="typeFilters"></div>
    <div class="filter-divider"></div>
    <div class="filter-label">时间范围</div>
    <div class="date-row">
      <input type="date" id="fStart"><span style="color:var(--sub)">至</span>
      <input type="date" id="fEnd">
      <button class="filter-btn" id="fApply">确定</button>
      <button class="filter-btn secondary" id="fReset">重置</button>
    </div>
  </div>
  <div class="search-bar" id="searchBar">
    <input type="text" id="searchInput" placeholder="搜索消息内容或发送者…">
    <span id="searchCount" style="color:var(--sub);font-size:12px;white-space:nowrap"></span>
  </div>
  <div class="chat-body" id="chatBody"><div id="msgContainer"></div>
    <div class="loading" id="loadingIndicator">加载中…</div></div>
  <footer class="chat-footer">由 stories-in-wx 导出 · 仅限个人数据备份研究</footer>
</div>
<div class="lightbox" id="lightbox"><img id="lightboxImg"></div>
"""

# JS 渲染器 —— 处理全部消息类型 + 交互功能
_HTML_RENDERER = r"""
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

  function esc(s) { var d = document.createElement('div'); d.textContent = s == null ? '' : String(s); return d.innerHTML; }
  function fmtDate(ts) { var d = new Date(ts*1000); return d.getFullYear()+'年'+(d.getMonth()+1)+'月'+d.getDate()+'日'; }
  function fmtTime(ts) { var d = new Date(ts*1000); var p = function(n){return String(n).padStart(2,'0')}; return p(d.getHours())+':'+p(d.getMinutes()); }
  function fmtFull(ts) { return fmtDate(ts)+' '+fmtTime(ts); }
  function avatarColor(id) { var h = 0; for (var i = 0; i < id.length; i++) h = (h*31 + id.charCodeAt(i)) & 0x7fffffff; return 'c' + (h % 8); }
  function avaFile(un) { return avatarFiles[un] || ''; }

  function xmlVal(raw, tag) {
    var m = (raw||'').match(new RegExp('<'+tag+'>(.*?)</'+tag+'>', 's'));
    if (!m) return '';
    return m[1].replace(/<!\[CDATA\[([\s\S]*?)\]\]>/g, '$1').trim();
  }

  function renderMsg(msg, prev) {
    var html = '';
    if (!prev || fmtDate(msg.timestamp) !== fmtDate(prev.timestamp))
      html += '<div class="date-divider"><span>'+fmtDate(msg.timestamp)+'</span></div>';
    var t = msg.type;
    if (t === 10000 || t === 10002 || t === 266287972401) {
      html += '<div class="system-msg"><span>'+esc(msg.content||'')+'</span></div>';
      return html;
    }
    var mem = members[msg.sender];
    var name = mem ? mem.name : (msg.senderName || msg.sender);
    var avaRel = avaFile(msg.sender);
    var colorCls = avatarColor(msg.sender);
    html += '<div class="msg-row'+(msg.isSend?' sent':'')+'">';
    html += '<div class="msg-avatar '+colorCls+'">';
    if (avaRel) html += '<img src="'+esc(avaRel)+'" onerror="this.style.display=\'none\';this.parentElement.textContent=\''+esc(name.charAt(0))+'\'"/>';
    else html += esc(name.charAt(0));
    html += '</div>';
    html += '<div class="msg-bubble">';
    if (isGroup && !msg.isSend) html += '<div class="msg-sender">'+esc(name)+'</div>';
    html += '<div class="bubble-body">';
    if (msg.quote) html += '<div class="msg-quote"><div class="qn">'+esc(msg.quote.sender)+'</div>'+esc(msg.quote.content)+'</div>';
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
      if (t === 47) return '<img class="msg-emoji" src="'+p+'" loading="lazy" onclick="window.__lb(this.src)">';
      if (t === 34) return '<div class="msg-voice"><audio controls src="'+p+'"></audio></div>';
      return '<img class="msg-image" src="'+p+'" loading="lazy">';
    }

    if (t === 3) return '<div class="msg-image broken">📷 图片未下载</div>';
    if (t === 43) return '<div class="msg-image broken">🎬 视频未下载</div>';
    if (t === 47) return '<div class="msg-image broken">😊 表情</div>';
    if (t === 34) return '<div class="msg-image broken">🎤 语音</div>';
    if (t === 50) return '<div class="msg-image broken">📞 通话</div>';

    // 转账
    if (/^\[转账\]\s+/.test(content))
      return '<div class="wx-card wx-transfer"><div class="wx-card-left">💰</div><div class="wx-card-right"><div class="wx-card-title">'+esc(content.replace(/^\[转账\]\s+/,''))+'</div><div class="wx-card-sub">微信转账</div></div></div>';
    // 红包
    if (/^\[红包\]/.test(content))
      return '<div class="wx-card wx-transfer"><div class="wx-card-left">🧧</div><div class="wx-card-right"><div class="wx-card-title">'+esc(xmlVal(raw,'sendertitle')||content)+'</div><div class="wx-card-sub">微信红包</div></div></div>';
    // 位置
    if (/^\[位置\]/.test(content))
      return '<div class="wx-card"><div class="wx-card-left">📍</div><div class="wx-card-right"><div class="wx-card-title">'+esc(content.replace(/^\[位置\]\s*/,''))+'</div><div class="wx-card-sub">位置共享</div></div></div>';
    // 链接 / 小程序 / 文件
    if (t === 49) {
      var title = xmlVal(raw, 'title') || content;
      var url = xmlVal(raw, 'url');
      var des = xmlVal(raw, 'des');
      var html = '<div class="wx-card"><div class="wx-card-left">🔗</div><div class="wx-card-right">';
      html += '<div class="wx-card-title">'+esc(title)+'</div>';
      if (des) html += '<div class="wx-card-sub">'+esc(des.substring(0,80))+'</div>';
      if (url) { html += '</div></div>'; return '<a href="'+esc(url)+'" target="_blank" rel="noopener" style="text-decoration:none;color:inherit">'+html+'</a>'; }
      return html + '</div></div>';
    }
    // 名片
    if (t === 42) return '<div class="wx-card"><div class="wx-card-left">👤</div><div class="wx-card-right"><div class="wx-card-title">'+esc(content)+'</div><div class="wx-card-sub">个人名片</div></div></div>';

    if (!content) return '<em style="opacity:.5">无内容</em>';
    return esc(content);
  }

  function renderAll() {
    container.innerHTML = '';
    loaded = 0;
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
  window.__imgErr = function(img) {
    img.classList.add('broken');
    img.removeAttribute('src');
    img.style.width = '200px'; img.style.height = '60px';
    img.textContent = '📷 图片';
  };
  document.getElementById('lightbox').addEventListener('click', function() {
    this.classList.remove('active');
  });

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
    var names = {1:'文本',3:'图片',34:'语音',43:'视频',47:'表情',49:'链接',57:'引用',10000:'系统'};
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
    var names = {1:'文本',3:'图片',34:'语音',43:'视频',47:'表情',49:'链接',57:'引用'};
    var html = '<span class="type-tag active" data-type="all">全部</span>';
    Object.keys(types).sort().forEach(function(k) {
      html += '<span class="type-tag" data-type="'+k+'">'+(names[k]||'类型'+k)+' <span class="tag-count">'+types[k]+'</span></span>';
    });
    box.innerHTML = html;
    box.querySelectorAll('.type-tag').forEach(function(tag) {
      tag.addEventListener('click', function() {
        var type = this.dataset.type;
        if (type === 'all') { activeTypes.clear(); }
        else { activeTypes.has(type) ? activeTypes.delete(type) : activeTypes.add(type); }
        this.classList.toggle('active');
        applyFilter();
      });
    });
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
  document.getElementById('themeToggle').addEventListener('click', function() {
    var b = document.body;
    b.dataset.theme = b.dataset.theme === 'dark' ? '' : 'dark';
  });
  document.getElementById('searchInput').addEventListener('input', function() {
    searchKw = this.value;
    applyFilter();
  });
  document.getElementById('fApply').addEventListener('click', function() {
    var s = document.getElementById('fStart').value;
    var e = document.getElementById('fEnd').value;
    fStart = s ? new Date(s + 'T00:00:00').getTime() / 1000 : 0;
    fEnd = e ? new Date(e + 'T23:59:59').getTime() / 1000 : 0;
    applyFilter();
  });
  document.getElementById('fReset').addEventListener('click', function() {
    document.getElementById('fStart').value = '';
    document.getElementById('fEnd').value = '';
    fStart = fEnd = 0;
    activeTypes.clear();
    document.querySelectorAll('.type-tag').forEach(function(t) { t.classList.add('active'); });
    applyFilter();
  });

  // 初始化
  document.getElementById('chatTitle').textContent = data.meta.sessionName;
  var range = data.meta.dateRange;
  document.getElementById('chatMeta').textContent =
    data.meta.messageCount + ' 条消息 · ' + fmtDate(range.start) + ' - ' + fmtDate(range.end);
  document.getElementById('headerAva').textContent = data.meta.sessionName.charAt(0);
  renderStats();
  renderTypeFilters();
  renderAll();
})();
"""
