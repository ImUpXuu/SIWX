"""IPv4 web controls with no network allowlist or authentication, by user choice."""
from __future__ import annotations

from http.server import BaseHTTPRequestHandler, HTTPServer
import json
import io
from pathlib import Path
import threading
import zipfile
from urllib.parse import urlsplit, parse_qs


def interface_hosts():
    """Suggested local URLs for the console, not an HTTP access allowlist."""
    hosts = {"127.0.0.1", "localhost"}
    try:
        import psutil
        import socket
        for addresses in psutil.net_if_addrs().values():
            for address in addresses:
                if address.family == socket.AF_INET:
                    hosts.add(address.address)
    except (ImportError, OSError):
        pass
    return hosts

from .documents import FORMATS, render

STATIC = Path(__file__).with_name("live_ui")
ASSETS = {"/": ("index.html", "text/html; charset=utf-8"),
          "/app.js": ("app.js", "text/javascript; charset=utf-8"),
          **{"/" + name: (name, "text/css; charset=utf-8") for name in
             ("app.css", "style.css", "chat.css", "export.css", "shared.css", "settings.css", "logs.css", "sns.css")},
          **{"/" + name: (name, "text/javascript; charset=utf-8") for name in ("sns.js", "sns-render.js", "widgets.js", "vue.js")}}


class LocalServer(HTTPServer):
    allow_reuse_address = False
    request_queue_size = 128

    def __init__(self, engine, port=0, host="0.0.0.0"):
        if host not in ("127.0.0.1", "0.0.0.0"):
            raise ValueError("监听地址只能是127.0.0.1或0.0.0.0。")
        self.engine = engine
        self.hosts = interface_hosts() if host == "0.0.0.0" else {"127.0.0.1", "localhost"}
        super().__init__((host, port), Handler)

    @property
    def origin(self):
        return "http://127.0.0.1:" + str(self.server_port)

    @property
    def url(self):
        return self.origin + "/"

    @property
    def urls(self):
        return ["http://" + host + ":" + str(self.server_port) + "/"
                for host in sorted(self.hosts) if host != "localhost"]

    def server_close(self):
        try:
            self.engine.close()
        finally:
            super().server_close()


class Handler(BaseHTTPRequestHandler):
    # Single request thread keeps extraction, SQLite and exports serialized.
    server_version = "SIWX-Lite"

    def setup(self):
        super().setup()
        self.connection.settimeout(20)

    def log_message(self, *_):
        pass  # No account names, keywords, tokens, message bodies or paths in logs.

    def _send(self, status, data, mime="application/json; charset=utf-8", attachment=None):
        self.send_response(status)
        self.send_header("Content-Type", mime)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Content-Security-Policy", "default-src 'none'; script-src 'self'; "
                         "style-src 'self'; img-src 'self' data:; media-src 'self'; connect-src 'self'; base-uri 'none'; "
                         "frame-ancestors 'none'; form-action 'none'")
        if attachment:
            self.send_header("Content-Disposition", 'attachment; filename="' + attachment + '"')
        self.end_headers()
        try:
            self.wfile.write(data)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def _json(self, status, value):
        self._send(status, json.dumps(value, ensure_ascii=False).encode())

    def do_GET(self):
        path = self.path.split("?", 1)[0]
        if path.startswith("/api/sns/") or path == "/api/chat/avatar":
            self._sns_get(path)
            return
        if path in ASSETS:
            filename, mime = ASSETS[path]
            self._send(200, (STATIC / filename).read_bytes(), mime)
        elif self.path == "/api/accounts":
            self._json(200, self.server.engine.account_list())
        elif self.path == "/api/state":
            engine = self.server.engine
            self._json(200, {"opened": engine.source is not None, "account": engine.account_index})
        elif self.path == "/api/status":
            engine = self.server.engine
            self._json(200, {"version": "1.4.0", "opened": engine.source is not None,
                             "account": engine.account_index, "accounts": engine.account_list(),
                             "warnings": len(engine.source.load_errors) if engine.source else 0})
        elif self.path == "/api/logs":
            self._json(200, {"logs": list(self.server.engine.events)})
        elif self.path == "/api/chats":
            try:
                self._json(200, self.server.engine.chats())
            except ValueError as exc:
                self._json(400, {"error": str(exc)})
        else:
            self._json(404, {"error": "无此功能。"})

    def _sns_get(self, path):
        try:
            query = {key: values[0] for key, values in parse_qs(urlsplit(self.path).query, max_num_fields=20).items()}
            engine = self.server.engine
            sns = engine.moments
            if path == "/api/sns/accounts":
                accounts = []
                if sns:
                    try:
                        accounts.append({"wxid": "current", "count": sns.count()})
                    except ValueError:
                        pass
                self._json(200, {"accounts": accounts})
                return
            if sns is None:
                raise ValueError("请先通过运行向导选择账号并提取。")
            if path == "/api/sns/timeline":
                self._json(200, sns.timeline(query))
            elif path == "/api/sns/friends":
                rows = sns.friends()
                self._json(200, {"friends": rows, "total": len(rows)})
            elif path == "/api/sns/detail":
                self._json(200, {"post": sns.detail(query.get("tid"))})
            elif path == "/api/sns/media":
                data, mime = sns.media(query.get("url", ""))
                self._send(200, data, mime)
            elif path == "/api/chat/avatar":
                pair = sns.avatar(query.get("username", ""))
                if pair:
                    self._send(200, *pair)
                else:
                    self._send(200, b'<svg xmlns="http://www.w3.org/2000/svg" width="48" height="48"><rect width="48" height="48" rx="8" fill="#e7f8ef"/><circle cx="24" cy="17" r="8" fill="#07c160"/><path d="M8 45a16 16 0 0 1 32 0" fill="#07c160"/></svg>', "image/svg+xml")
            else:
                self._json(404, {"error": "无此朋友圈功能。"})
        except ValueError as exc:
            self._json(400, {"error": str(exc)})
        except Exception:
            self._json(500, {"error": "朋友圈读取未完成，请确认已提取朋友圈数据库后重试。"})

    def do_POST(self):
        try:
            if self.headers.get("Content-Type", "").split(";", 1)[0] != "application/json":
                raise ValueError("请提交JSON请求。")
            if self.headers.get("Transfer-Encoding"):
                raise ValueError("不接受分块请求。")
            length = int(self.headers.get("Content-Length", "0"))
            if not 0 < length <= 65536:
                raise ValueError("请求大小不正确。")
            body = json.loads(self.rfile.read(length))
            if not isinstance(body, dict):
                raise ValueError("请求必须是JSON对象。")
            engine = self.server.engine
            if self.path == "/api/logs/clear":
                engine.events.clear()
                self._json(200, {"cleared": True})
            elif self.path == "/api/accounts/add":
                self._json(200, engine.add_account_directory(body.get("path")))
            elif self.path == "/api/sns/export":
                if engine.moments is None:
                    raise ValueError("请先提取账号数据。")
                engine.event("info", "开始导出朋友圈。")
                data, mime, name = engine.moments.export(body)
                engine.event("info", "朋友圈导出完成。")
                self._send(200, data, mime, name)
            elif self.path == "/api/open":
                self._json(200, engine.open(body.get("account"), body.get("consent")))
            elif self.path == "/api/export" and "chats" in body:
                chats = body["chats"]
                if not isinstance(chats, list) or not 1 <= len(chats) <= 100 or any(not isinstance(c, str) for c in chats) or len(set(chats)) != len(chats):
                    raise ValueError("请选择1至100个不同的会话。")
                fmt = body.get("format", "html")
                if fmt not in FORMATS:
                    raise ValueError("不支持的导出格式。")
                output, count, size = io.BytesIO(), 0, 0
                with zipfile.ZipFile(output, "w", zipfile.ZIP_DEFLATED) as archive:
                    for index, chat in enumerate(chats, 1):
                        document = engine.document(chat, body.get("query", ""), body.get("since", ""), body.get("until", ""), body.get("kind", ""))
                        count += len(document["messages"])
                        if count > 100_000:
                            raise ValueError("所选消息合计超过十万条，请缩小日期或会话范围。")
                        data = render(document, fmt).encode()
                        size += len(data)
                        if size > 64 * 1024 * 1024:
                            raise ValueError("导出内容超过64 MiB，请分批导出。")
                        archive.writestr("conversation-" + str(index).zfill(3) + "." + fmt, data)
                self._send(200, output.getvalue(), "application/zip", "chat-exports.zip")
            elif self.path in ("/api/messages", "/api/export"):
                document = engine.document(body.get("chat"), body.get("query", ""),
                                           body.get("since", ""), body.get("until", ""), body.get("kind", ""))
                if self.path == "/api/export":
                    if "ids" in body:
                        ids = body["ids"]
                        if not isinstance(ids, list) or not ids or any(type(i) is not int or i < 0 or i >= len(document["messages"]) for i in ids) or len(set(ids)) != len(ids):
                            raise ValueError("所选消息已变化，请重新选择。")
                        selected = set(ids)
                        document["messages"] = [row for row in document["messages"] if row["id"] in selected]
                    fmt = body.get("format", "html")
                    if fmt not in FORMATS:
                        raise ValueError("不支持的导出格式。")
                    data = render(document, fmt).encode()
                    if body.get("pack") == "zip":
                        output = io.BytesIO()
                        with zipfile.ZipFile(output, "w", zipfile.ZIP_DEFLATED) as archive:
                            archive.writestr("conversation-001." + fmt, data)
                        self._send(200, output.getvalue(), "application/zip", "chat-exports.zip")
                    else:
                        self._send(200, data, "application/octet-stream", "chat-export." + fmt)
                else:
                    offset = body.get("offset", 0)
                    if type(offset) is not int or not 0 <= offset <= 100_000:
                        raise ValueError("页码不正确。")
                    rows = document["messages"]
                    months = {}
                    for index, row in enumerate(rows):
                        month = row["createTime"][:7]
                        if month not in months:
                            months[month] = index
                    self._json(200, {"messages": rows[offset:offset + 200], "total": len(rows), "offset": offset,
                                     "months": [{"month": month, "offset": index} for month, index in months.items()]})
            elif self.path == "/api/close":
                engine.close()
                self._json(200, {"closed": True})
            elif self.path == "/api/stop":
                engine.close()
                self._json(200, {"stopped": True})
                threading.Thread(target=self.server.shutdown, daemon=True).start()
            else:
                self._json(404, {"error": "无此功能。"})
        except ValueError as exc:
            self._json(400, {"error": str(exc)})
        except Exception:
            # Upstream errors may contain local account paths; do not return/log them.
            self._json(500, {"error": "读取未完成。请确认本人微信4.x已登录、数据目录正确且可读；保留杀软检测信息。"})
