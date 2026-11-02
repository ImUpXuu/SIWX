"""Foreground IPv4 Web controls, deliberately unauthenticated by user choice.

Host/Origin checks prevent some browser cross-origin requests, not network
clients. Every client able to reach the port can read data and use controls.
"""
from __future__ import annotations

from http.server import BaseHTTPRequestHandler, HTTPServer
import json
from pathlib import Path
import threading
from urllib.parse import urlsplit


def interface_hosts():
    """Only numeric IPs assigned to this computer, plus loopback names."""
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
          "/style.css": ("style.css", "text/css; charset=utf-8")}


class LocalServer(HTTPServer):
    allow_reuse_address = False

    def __init__(self, engine, port=0, host="127.0.0.1"):
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
                         "style-src 'self'; connect-src 'self'; base-uri 'none'; "
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

    def _guard(self):
        request_host = self.headers.get("Host", "")
        try:
            parsed = urlsplit("http://" + request_host)
            valid = (parsed.hostname in self.server.hosts and parsed.port == self.server.server_port
                     and not parsed.username and not parsed.password and not parsed.path
                     and not parsed.query and not parsed.fragment)
        except ValueError:
            valid = False
        if not valid:
            self._json(403, {"error": "请使用程序列出的本机或网络IP网址。"})
            return False
        origin = self.headers.get("Origin")
        if origin is not None and origin != "http://" + request_host:
            self._json(403, {"error": "跨来源请求已拒绝。"})
            return False
        if self.headers.get("Sec-Fetch-Site", "none") not in ("same-origin", "none"):
            self._json(403, {"error": "跨站请求已拒绝。"})
            return False
        # No token, password, cookie or other authentication by explicit request.
        # These browser-origin guards are not access control for network clients.
        return True

    def do_GET(self):
        if not self._guard():
            return
        if self.path in ASSETS:
            filename, mime = ASSETS[self.path]
            self._send(200, (STATIC / filename).read_bytes(), mime)
        elif self.path == "/api/accounts":
            self._json(200, self.server.engine.account_list())
        elif self.path == "/api/chats":
            try:
                self._json(200, self.server.engine.chats())
            except ValueError as exc:
                self._json(400, {"error": str(exc)})
        else:
            self._json(404, {"error": "无此功能。"})

    def do_POST(self):
        if not self._guard():
            return
        try:
            if self.headers.get("Content-Type", "").split(";", 1)[0] != "application/json":
                raise ValueError("请提交JSON请求。")
            if self.headers.get("Transfer-Encoding"):
                raise ValueError("不接受分块请求。")
            length = int(self.headers.get("Content-Length", "0"))
            if not 0 < length <= 4096:
                raise ValueError("请求大小不正确。")
            body = json.loads(self.rfile.read(length))
            if not isinstance(body, dict):
                raise ValueError("请求必须是JSON对象。")
            engine = self.server.engine
            if self.path == "/api/open":
                self._json(200, engine.open(body.get("account"), body.get("consent")))
            elif self.path in ("/api/messages", "/api/export"):
                document = engine.document(body.get("chat"), body.get("query", ""),
                                           body.get("since", ""), body.get("until", ""))
                if self.path == "/api/export":
                    fmt = body.get("format", "html")
                    if fmt not in FORMATS:
                        raise ValueError("不支持的导出格式。")
                    self._send(200, render(document, fmt).encode(),
                               "application/octet-stream", "chat-export." + fmt)
                else:
                    offset = body.get("offset", 0)
                    if type(offset) is not int or not 0 <= offset <= 100_000:
                        raise ValueError("页码不正确。")
                    rows = document["messages"]
                    self._json(200, {"messages": rows[offset:offset + 200], "total": len(rows), "offset": offset})
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
