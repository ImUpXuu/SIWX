"""Read the opened user's in-memory SNS database and its recorded media only."""
from __future__ import annotations
import copy
from collections import OrderedDict
import hashlib
import html
import io
import ipaddress
import json
import secrets
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
import socket
import sqlite3
import ssl
import urllib.request
from urllib.parse import urlsplit
import zipfile
import zlib
from .vendor import sns_parser as parser, sns_cdn as cdn

MEDIA_LIMIT = 32 * 1024 * 1024

def _check_url(url):
    parsed = urlsplit(url)
    if parsed.scheme not in ('http', 'https') or parsed.username or parsed.password or parsed.port not in (None, 80, 443) or not cdn.is_wechat_cdn(url):
        raise ValueError('Not a recorded WeChat media destination')
    answers = socket.getaddrinfo(parsed.hostname, parsed.port or (443 if parsed.scheme == 'https' else 80), type=socket.SOCK_STREAM)
    if not answers or any(not ipaddress.ip_address(row[4][0]).is_global for row in answers):
        raise ValueError('Non-public media destination')

class _Redirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        _check_url(newurl)
        return super().redirect_request(req, fp, code, msg, headers, newurl)

def _fetch(url, timeout=15.0, ctx=None):
    _check_url(url)
    opener = urllib.request.build_opener(_Redirect(), urllib.request.HTTPSHandler(context=ssl.create_default_context()))
    with opener.open(urllib.request.Request(url, headers=cdn.DEFAULT_HEADERS), timeout=timeout) as response:
        raw = response.read(MEDIA_LIMIT + 1)
        headers = {k.lower(): v for k, v in response.headers.items()}
    if len(raw) > MEDIA_LIMIT:
        raise ValueError('Media exceeds 32 MiB')
    encoding = headers.get('content-encoding', '').lower()
    if encoding in ('gzip', 'deflate'):
        decoder = zlib.decompressobj(31 if encoding == 'gzip' else 15)
        raw = decoder.decompress(raw, MEDIA_LIMIT + 1)
        if len(raw) > MEDIA_LIMIT or decoder.unconsumed_tail:
            raise ValueError('Decoded media exceeds 32 MiB')
    return raw, headers

# Retain the original media reconstruction/ISAAC/AES logic, with bounded transport.
cdn.fetch = _fetch

def _text(value):
    return value.decode('utf-8', 'replace') if isinstance(value, bytes) else str(value or '')

class Moments:
    def __init__(self, source):
        self.source = source
        self.salt = secrets.token_bytes(24)
        self.resources = {}
        self.cache = OrderedDict()
        self.cached_bytes = 0
        self.lock = threading.RLock()

    @property
    def conn(self):
        value = getattr(self.source, '_conns', {}).get('sns')
        if value is None:
            raise ValueError('当前账号没有可读取的朋友圈数据库，请保持微信登录后重新提取。')
        return value

    def count(self):
        try:
            return self.conn.execute('SELECT count(*) FROM SnsTimeLine').fetchone()[0]
        except sqlite3.DatabaseError as exc:
            raise ValueError('朋友圈数据库结构无法读取，请重新提取。') from exc

    def _name(self, username):
        contact = (getattr(self.source, '_contacts', None) or {}).get(username)
        return getattr(contact, 'display_name', '') or username

    def avatar(self, username):
        conn = getattr(self.source, '_conns', {}).get('head_image')
        if conn:
            try:
                row = conn.execute('SELECT image_buffer FROM head_image WHERE username=?', (username,)).fetchone()
                if row and isinstance(row[0], bytes) and len(row[0]) <= MEDIA_LIMIT:
                    ext, mime = cdn.detect_mime(row[0])
                    if ext and mime.startswith('image/'):
                        return row[0], mime
            except sqlite3.DatabaseError:
                pass
        return None

    def friends(self):
        rows = self.conn.execute('SELECT user_name,count(*),min(tid),max(tid) FROM SnsTimeLine GROUP BY user_name ORDER BY count(*) DESC LIMIT 2000')
        return [{'username': _text(user), 'display': self._name(_text(user)), 'count': count,
                 'first_ts': parser.sns_id_to_seconds(first), 'last_ts': parser.sns_id_to_seconds(last),
                 'has_avatar': self.avatar(_text(user)) is not None} for user, count, first, last in rows]

    def _register(self, value):
        """Replace recorded CDN URLs with opaque same-origin references; keep keys server-side."""
        if isinstance(value, list):
            return [self._register(item) for item in value]
        if not isinstance(value, dict):
            return value
        result = {}
        if value.get('encrypt_url') and not value.get('url'):
            value = {**value, 'url': value['encrypt_url']}
        for key, item in value.items():
            if key in ('key', 'token', 'aes_key', 'enc_key', 'thumb_token', 'encrypt_url', 'emoji_aes_key', 'md5'):
                continue
            if key in ('url', 'cover', 'avatar', 'video_url', 'thumb_url') and isinstance(item, str):
                if cdn.is_wechat_cdn(item):
                    spec = {'url': item, 'key': value.get('key'), 'token': value.get('token')}
                    if value.get('encrypt_url') or value.get('aes_key'):
                        spec = {'emoji': {k: value.get(k, '') for k in ('url', 'encrypt_url', 'aes_key')}}
                    token = hashlib.sha256(self.salt + json.dumps(spec, sort_keys=True).encode()).hexdigest()
                    if len(self.resources) >= 20000 and token not in self.resources:
                        raise ValueError('当前朋友圈媒体数量过多，请缩小日期范围后重新打开账号。')
                    self.resources[token] = spec
                    result[key] = 'https://qpic.cn/_siwx_media/' + token
                elif urlsplit(item).scheme in ('http', 'https'):
                    result[key] = item if key == 'url' else ''
                else:
                    result[key] = ''
            else:
                result[key] = self._register(item)
        return result

    def _parse(self, row):
        tid, user, xml = row
        if not isinstance(xml, (str, bytes)):
            return None
        if len(xml) > 4 * 1024 * 1024 or b'<!DOCTYPE' in (xml.upper() if isinstance(xml, bytes) else xml.upper().encode()) or b'<!ENTITY' in (xml.upper() if isinstance(xml, bytes) else xml.upper().encode()):
            return None
        feed = parser.parse_timeline(xml)
        if feed is None:
            return None
        feed.update(tid=str(tid), user_name=_text(user), ts=parser.sns_id_to_seconds(tid), ts_ms=parser.sns_id_to_ms(tid))
        feed['card'] = parser.public_card(feed.get('card'))
        return feed

    def _rows(self, options, export=False):
        keyword = options.get('keyword', '') or ''
        if not isinstance(keyword, str) or len(keyword) > 500:
            raise ValueError('朋友圈搜索内容过长。')
        user = options.get('username', '') or ''
        if not isinstance(user, str) or len(user) > 300:
            raise ValueError('发布者参数不正确。')
        start, end = parser.parse_ts_arg(options.get('start')), parser.parse_ts_arg(options.get('end'))
        if start is not None and end is not None and start > end:
            raise ValueError('开始日期不能晚于结束日期。')
        sql, args = 'SELECT tid,user_name,content FROM SnsTimeLine WHERE 1=1', []
        if user:
            sql += ' AND user_name=?'; args.append(user)
        before = options.get('before_tid')
        if before:
            try: cursor = int(before)
            except (TypeError, ValueError) as exc: raise ValueError('朋友圈分页位置不正确。') from exc
            if not -(1 << 63) <= cursor < 1 << 63: raise ValueError('朋友圈分页位置不正确。')
            sql += ' AND tid<?'; args.append(cursor)
        sql += ' ORDER BY tid DESC LIMIT ?'; args.append(100001 if export else 5001)
        for row in self.conn.execute(sql, args):
            feed = self._parse(row)
            yield row[0], feed, bool(feed and (start is None or feed['ts'] >= start) and (end is None or feed['ts'] <= end) and keyword.casefold() in parser.search_text(feed).casefold())

    def timeline(self, options):
        try: limit = max(1, min(100, int(options.get('limit', 20))))
        except (TypeError, ValueError) as exc: raise ValueError('朋友圈分页数量不正确。') from exc
        rows, last, scanned, more = [], None, 0, False
        for tid, feed, match in self._rows(options):
            if scanned >= 5000 or len(rows) >= limit:
                more = True; break
            last = tid; scanned += 1
            if match:
                rows.append(self._register(feed))
        return {'timeline': rows, 'next_before_tid': str(last) if more and last is not None else None,
                'has_more': more, 'truncated': scanned >= 5000 and more}

    def detail(self, tid):
        try: value = int(tid)
        except (ValueError, TypeError) as exc: raise ValueError('动态标识不正确。') from exc
        if not -(1 << 63) <= value < 1 << 63: raise ValueError('动态标识不正确。')
        row = self.conn.execute('SELECT tid,user_name,content FROM SnsTimeLine WHERE tid=?', (value,)).fetchone()
        feed = self._parse(row) if row else None
        if not feed: raise ValueError('动态不存在或内容无法读取。')
        return self._register(feed)

    def media(self, reference):
        token = str(reference).rsplit('/', 1)[-1]
        spec = self.resources.get(token)
        if spec is None: raise ValueError('媒体不属于当前已打开的朋友圈记录。')
        with self.lock:
            if token in self.cache:
                self.cache.move_to_end(token); return self.cache[token]
        result = cdn.fetch_emoji(spec['emoji'], cache_dir=None, timeout=10) if 'emoji' in spec else cdn.fetch_media(**spec, cache_dir=None, timeout=10, hosts=2)
        if not result.get('ok'):
            raise ValueError('朋友圈媒体读取失败，资源可能已过期，请在微信查看后重新提取。')
        data = cdn.strip_wechat_tail(result['data'])
        if len(data) > MEDIA_LIMIT: raise ValueError('单个媒体超过32 MiB。')
        pair = (data, result.get('mime') or 'application/octet-stream')
        with self.lock:
            if token in self.cache: return self.cache[token]
            while self.cache and self.cached_bytes + len(data) > 64 * 1024 * 1024:
                _, old = self.cache.popitem(last=False); self.cached_bytes -= len(old[0])
            self.cache[token] = pair; self.cached_bytes += len(data)
        return pair

    def export(self, options):
        fmt = options.get('format', 'json')
        if fmt not in ('json', 'markdown', 'html', 'txt'): raise ValueError('不支持的朋友圈导出格式。')
        feeds = []
        for scanned, (_, feed, match) in enumerate(self._rows(options, export=True), 1):
            if scanned > 100000: raise ValueError('朋友圈记录超过十万条，请缩小发布者或日期范围。')
            if match: feeds.append(self._register(feed))
            if len(feeds) > 10000: raise ValueError('朋友圈导出超过一万条，请缩小日期范围。')
        use_media = options.get('media') is True
        media, missing = {}, 0
        if use_media:
            import re
            tokens = sorted(set(re.findall(r'https://qpic\.cn/_siwx_media/([0-9a-f]{64})', json.dumps(feeds, ensure_ascii=False))))
            if len(tokens) > 1000: raise ValueError('媒体超过1000个，请分批导出。')
            try: concurrency = max(1, min(16, int(options.get('concurrency', 5))))
            except (ValueError, TypeError) as exc: raise ValueError('媒体并发数不正确。') from exc
            total_size = 0
            with ThreadPoolExecutor(max_workers=concurrency) as pool:
                pending = {pool.submit(self.media, token): token for token in tokens}
                try:
                    for future in as_completed(pending, timeout=120):
                        token = pending[future]
                        try:
                            data, mime = future.result()
                        except (ValueError, OSError):
                            missing += 1; continue
                        ext = mime.split('/')[-1].split(';')[0].replace('jpeg', 'jpg')
                        if ext not in ('jpg','png','gif','webp','mp4','heic','avif'): ext = 'bin'
                        total_size += len(data)
                        if total_size > 128 * 1024 * 1024:
                            for f in pending: f.cancel()
                            raise ValueError('导出媒体合计超过128 MiB，请分批导出。')
                        media[token] = ('media/' + str(len(media) + 1).zfill(4) + '.' + ext, data)
                except TimeoutError:
                    missing += sum(not f.done() or f.cancelled() for f in pending)
                    for f in pending: f.cancel()
            text = json.dumps(feeds, ensure_ascii=False)
            for token, (name, _) in media.items(): text = text.replace('https://qpic.cn/_siwx_media/' + token, name)
            feeds = json.loads(text)
        if fmt == 'json': content = json.dumps(feeds, ensure_ascii=False, indent=2)
        elif fmt in ('txt','markdown'):
            content = '\n\n'.join(self._name(f['user_name'])+' · '+str(f['create_time'])+'\n'+f['content_desc']+'\n'+json.dumps({'medias':f['medias'],'likes':f['likes'],'comments':f['comments'],'card':f['card']},ensure_ascii=False) for f in feeds)
            if fmt == 'markdown':
                from .documents import _md
                content = _md(content)
        else:
            parts = ['<!doctype html><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>朋友圈</title>']
            for feed in feeds:
                parts.append('<article><h2>'+html.escape(self._name(feed['user_name']))+'</h2><p>'+html.escape(feed['content_desc'])+'</p><pre>'+html.escape(json.dumps({'time':feed['create_time'],'card':feed['card'],'likes':feed['likes'],'comments':feed['comments']},ensure_ascii=False,indent=2))+'</pre>')
                if use_media:
                    for item in feed['medias']:
                        url = item.get('url','')
                        if url.startswith('media/'):
                            tag = 'video controls' if url.endswith('.mp4') else 'img'
                            parts.append('<'+tag+' src="'+html.escape(url,quote=True)+'" alt="朋友圈媒体">'+('</video>' if tag.startswith('video') else ''))
                parts.append('</article>')
            content = '\n'.join(parts)
        data = content.encode()
        if len(data) > 64 * 1024 * 1024: raise ValueError('朋友圈文本超过64 MiB，请分批导出。')
        ext = 'md' if fmt == 'markdown' else fmt
        if use_media:
            output=io.BytesIO()
            with zipfile.ZipFile(output,'w',zipfile.ZIP_DEFLATED) as archive:
                archive.writestr('moments.'+ext,data)
                for name, body in media.values(): archive.writestr(name,body)
                archive.writestr('export-report.json',json.dumps({'count':len(feeds),'downloaded_media':len(media),'unavailable_media':missing},ensure_ascii=False))
            return output.getvalue(), 'application/zip', 'moments.zip'
        return data, 'application/octet-stream', 'moments.'+ext

    def close(self):
        self.resources.clear(); self.cache.clear(); self.cached_bytes=0
