import hashlib
import http.client
import json
import zipfile
from pathlib import Path
import sqlite3
import tempfile
import threading
import unittest
from types import SimpleNamespace
from unittest.mock import patch
import contextlib
import io

from siwx_safe.live import Engine, discover_accounts, own_wechat_pids
from siwx_safe.local_web import LocalServer
from siwx_safe.live_app import main
from siwx_safe.vendor.wexport.crypto.aes import AesCbc
from siwx_safe.vendor.wexport.model import Account, Conversation, Message
from siwx_safe.vendor.wexport.sources.win4 import Win4Source


class FakeSource:
    def __init__(self):
        self.load_errors, self._keys = [], {"synthetic": b"test"}
        self.closed = False

    def conversations(self):
        return [Conversation("user_example", "合成会话")]

    def iter_messages(self, *args):
        yield Message(1, timestamp=1700000000, sender_name="测试发送者", type=1,
                      raw='<script>test</script> & 合成记录')

    def close(self):
        self.closed = True


class EngineTests(unittest.TestCase):
    def test_explicit_consent_and_account_required(self):
        called = []
        engine = Engine([Account("example", "win4")], lambda a: called.append(a))
        for index, consent in ((0, False), (0, "true"), (-1, True), (True, True), (2, True)):
            with self.assertRaises(ValueError):
                engine.open(index, consent)
        self.assertEqual([], called)

    def test_only_same_user_wechat_processes(self):
        api = SimpleNamespace(Process=lambda: SimpleNamespace(username=lambda: "PC\\user_example"),
            process_iter=lambda fields: [SimpleNamespace(info={"pid": i, "name": name, "username": user})
                for i, name, user in ((1, "Weixin.exe", "PC\\user_example"),
                                     (2, "Weixin.exe", "PC\\other_example"),
                                     (3, "browser.exe", "PC\\user_example"),
                                     (4, "WeChat.exe", None))])
        self.assertEqual([1], own_wechat_pids(api))
        api.Process = lambda: SimpleNamespace(username=lambda: "NT AUTHORITY\\SYSTEM")
        with self.assertRaises(ValueError):
            own_wechat_pids(api)

    def test_engine_search_date_validation_and_close(self):
        source = FakeSource()
        engine = Engine([Account("example", "win4")], lambda _: source)
        engine.open(0, True)
        self.assertEqual(1, len(engine.document("user_example", "合成")["messages"]))
        self.assertEqual([], engine.document("user_example", "不匹配")["messages"])
        with self.assertRaises(ValueError):
            engine.document("../outside")
        with self.assertRaises(ValueError):
            engine.document("user_example", since="2026-11-03", until="2026-11-02")
        engine.close()
        self.assertTrue(source.closed)
        self.assertEqual({}, source._keys)
        self.assertIsNone(engine.source)

    def test_encrypted_fixture_to_document_without_source_changes(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            db = root / "db_storage/message/message_0.db"
            db.parent.mkdir(parents=True)
            chat = "user_example"
            table = "Msg_" + hashlib.md5(chat.encode()).hexdigest()
            conn = sqlite3.connect(db)
            conn.execute("PRAGMA page_size=4096")
            conn.executescript(f'''CREATE TABLE Name2Id(id INTEGER PRIMARY KEY,user_name TEXT);
                INSERT INTO Name2Id VALUES(1,'{chat}');
                CREATE TABLE {table}(local_id INTEGER PRIMARY KEY,server_id INTEGER,local_type INTEGER,
                    create_time INTEGER,message_content TEXT,real_sender_id INTEGER,sort_seq INTEGER);
                INSERT INTO {table} VALUES(1,1,1,1700000000,'合成端到端',1,1);''')
            conn.close()
            raw = bytearray(db.read_bytes())
            raw[20] = 80
            db.write_bytes(raw)
            conn = sqlite3.connect(db)
            conn.execute("VACUUM")
            conn.close()
            raw, key, salt, iv = db.read_bytes(), bytes(range(32)), bytes(range(16)), bytes(16)
            cipher, encrypted = AesCbc(key), bytearray()
            try:
                for offset in range(0, len(raw), 4096):
                    page = raw[offset:offset + 4096]
                    encrypted.extend((salt if offset == 0 else b"")
                        + cipher.encrypt(iv, page[16 if offset == 0 else 0:4096 - 80])
                        + iv + bytes(64))
            finally:
                cipher.close()
            db.write_bytes(encrypted)
            before = hashlib.sha256(db.read_bytes()).hexdigest()
            account = discover_accounts(root)[0]
            account.wxid = chat  # Synthetic self-chat, resolved without a contact database.
            def opener(a):
                source = Win4Source(a, key)
                source.open()
                return source
            engine = Engine([account], opener)
            try:
                engine.open(0, True)
                self.assertEqual("合成端到端", engine.document(chat)["messages"][0]["content"])
            finally:
                engine.close()
            self.assertEqual(before, hashlib.sha256(db.read_bytes()).hexdigest())
            self.assertEqual([db], list(root.rglob("*.db")))


class WebTests(unittest.TestCase):
    def setUp(self):
        self.source = FakeSource()
        self.server = LocalServer(Engine([Account("example", "win4", display_name="合成账号")], lambda _: self.source))
        self.thread = threading.Thread(target=self.server.serve_forever)
        self.thread.start()
        self.addCleanup(self.cleanup)

    def cleanup(self):
        self.server.shutdown()
        self.thread.join(timeout=3)
        self.server.server_close()

    def request(self, path, body=None, extra=None):
        conn = http.client.HTTPConnection("127.0.0.1", self.server.server_port, timeout=5)
        headers = {"Content-Type": "application/json"}
        headers.update(extra or {})
        conn.request("GET" if body is None else "POST", path,
                     None if body is None else json.dumps(body), headers=headers)
        res = conn.getresponse()
        status, response_headers, data = res.status, dict(res.getheaders()), res.read()
        conn.close()
        return status, response_headers, data

    def test_anonymous_access_without_host_origin_or_fetch_allowlists(self):
        self.assertEqual(200, self.request("/api/accounts")[0])
        self.assertEqual(200, self.request("/api/accounts", extra={"Authorization": "irrelevant"})[0])
        self.assertNotIn("#", self.server.url)
        self.assertFalse(hasattr(self.server, "token"))
        self.assertEqual(200, self.request("/api/accounts", extra={"Host": "example.invalid"})[0])
        self.assertEqual(200, self.request("/api/open", {"account": 0, "consent": True}, extra={"Origin": "https://example.invalid"})[0])
        self.assertEqual(200, self.request("/api/accounts", extra={"Sec-Fetch-Site": "cross-site"})[0])
        self.assertEqual(404, self.request("/../../secret.txt")[0])
        self.assertEqual(400, self.request("/api/open", {"account": 0, "consent": False})[0])
        for path in ("/api/stats", "/api/backup", "/api/mcp", "/api/update"):
            self.assertEqual(404, self.request(path)[0])

    def test_public_domain_mapped_port_and_tls_reverse_proxy_origin(self):
        headers = {"Host": "example.invalid:5443", "Origin": "https://example.invalid:5443", "Sec-Fetch-Site": "same-origin"}
        self.assertEqual(200, self.request("/", extra=headers)[0])
        self.assertEqual(200, self.request("/api/open", {"account": 0, "consent": True}, extra=headers)[0])
        for path, body in (("/api/messages", {"chat": "user_example"}),
                           ("/api/export", {"chat": "user_example", "format": "html"})):
            self.assertEqual(200, self.request(path, body, extra=headers)[0])
        self.assertEqual(200, self.request("/api/accounts", extra={"Host": "203.0.113.9:39000"})[0])
        self.assertEqual(200, self.request("/api/accounts", extra={"Host": "example.invalid:443", "Origin": "https://example.invalid"})[0])
        self.assertEqual(200, self.request("/api/accounts", extra={"Host": "example.invalid", "Origin": "https://other.invalid"})[0])
        self.assertEqual("0.0.0.0", self.server.server_address[0])
        self.assertNotIn("Access-Control-Allow-Credentials", self.request("/")[1])

    def test_extract_web_search_all_exports_and_close(self):
        self.assertEqual(200, self.request("/api/open", {"account": 0, "consent": True})[0])
        self.assertEqual("user_example", json.loads(self.request("/api/chats")[2])[0]["id"])
        status, headers, data = self.request("/api/messages", {"chat": "user_example", "query": "合成"})
        self.assertEqual(200, status)
        self.assertEqual(1, json.loads(data)["total"])
        self.assertEqual("no-store", headers["Cache-Control"])
        for fmt in ("html", "md", "txt", "csv", "json"):
            status, headers, data = self.request("/api/export", {"chat": "user_example", "format": fmt})
            self.assertEqual(200, status)
            self.assertIn("合成记录", data.decode())
            self.assertIn("attachment", headers["Content-Disposition"])
            if fmt == "html":
                self.assertNotIn(b"<script>", data)
        self.assertEqual(200, self.request("/api/close", {})[0])
        self.assertTrue(self.source.closed)
        self.assertEqual(400, self.request("/api/messages", {"chat": "user_example"})[0])

    def test_partial_failures_do_not_expose_keys_paths_or_traces(self):
        def fail(_):
            raise RuntimeError("synthetic-secret and private-path")
        self.server.engine.opener = fail
        status, headers, data = self.request("/api/open", {"account": 0, "consent": True})
        self.assertEqual(500, status)
        self.assertNotIn(b"synthetic-secret", data)
        self.assertNotIn(b"private-path", data)
        self.assertNotIn(b"Traceback", data)

    def test_all_interface_bind_and_anonymous_stop(self):
        # Synthetic empty engine only; no real WeChat data exposed by this bind.
        other = LocalServer(Engine([]), host="0.0.0.0")
        try:
            self.assertEqual("0.0.0.0", other.server_address[0])
            self.assertIn("127.0.0.1", other.hosts)
            self.assertTrue(all("#" not in url for url in other.urls))
        finally:
            other.server_close()
        self.assertEqual(200, self.request("/api/stop", {})[0])
        self.thread.join(timeout=3)
        self.assertFalse(self.thread.is_alive())

    def test_retained_wizard_status_logs_and_sns_http(self):
        from test_moments import MomentSource, PNG
        from siwx_safe.vendor import sns_cdn
        self.server.engine.opener = lambda _: MomentSource()
        self.assertEqual(200, self.request("/api/status")[0])
        self.assertEqual(200, self.request("/api/sns/accounts")[0])
        self.assertEqual(400, self.request("/api/sns/timeline")[0])
        self.assertEqual(200, self.request("/api/open", {"account": 0, "consent": True})[0])
        page = json.loads(self.request("/api/sns/timeline?limit=1")[2])
        post = page["timeline"][0]
        from urllib.parse import quote
        url = "/api/sns/media?url=" + quote(post["medias"][0]["url"], safe="")
        with patch.object(sns_cdn, "fetch", return_value=(PNG, {})):
            status, _, data = self.request(url)
            self.assertEqual(200, status)
            self.assertEqual(PNG, data)
            status, headers, data = self.request("/api/sns/export", {"format": "json", "keyword": "合成动态 24"})
            self.assertEqual(200, status)
            self.assertEqual(1, len(json.loads(data)))
        self.assertEqual(400, self.request("/api/sns/media?url=https://127.0.0.1/private")[0])
        status, _, data = self.request("/api/logs")
        self.assertIn("账号数据已打开", data.decode())
        self.assertNotIn("synthetic-media-token", data.decode())
        self.assertEqual(200, self.request("/api/logs/clear", {})[0])
        self.assertEqual([], json.loads(self.request("/api/logs")[2])["logs"])
        self.request("/api/close", {})
        self.assertEqual([], json.loads(self.request("/api/sns/accounts")[2])["accounts"])

    def test_original_layout_assets_and_removed_navigation(self):
        status, _, data = self.request("/")
        self.assertEqual(200, status)
        page = data.decode()
        for marker in ('class="shell"', 'id="acc-card"', 'id="c-sessions"', 'id="c-msgs"', 'id="e-sessions"', 'id="e-run"'):
            self.assertIn(marker, page)
        for marker in ('data-page="stats"', 'data-page="backup"', 'data-page="mcp"', 'id="c-stats"', 'github.com', 'wexport', '精简版'):
            self.assertNotIn(marker, page)
        for asset in ("app.css", "chat.css", "export.css", "shared.css", "style.css", "app.js"):
            self.assertEqual(200, self.request("/" + asset + "?v=test")[0])
        self.assertEqual(404, self.request("/../app.css")[0])

    def test_open_state_and_selected_export_validation(self):
        self.assertEqual({"opened": False, "account": None}, json.loads(self.request("/api/state")[2]))
        self.request("/api/open", {"account": 0, "consent": True})
        self.assertEqual({"opened": True, "account": 0}, json.loads(self.request("/api/state")[2]))
        data = json.loads(self.request("/api/messages", {"chat": "user_example", "kind": "text"})[2])
        self.assertEqual(0, data["messages"][0]["id"])
        self.assertFalse(data["messages"][0]["isSender"])
        self.assertEqual(0, data["months"][0]["offset"])
        status, _, data = self.request("/api/export", {"chat": "user_example", "format": "json", "ids": [0]})
        self.assertEqual(200, status)
        self.assertEqual(1, len(json.loads(data)["messages"]))
        status, _, data = self.request("/api/export", {"chat": "user_example", "format": "json", "ids": [0], "pack": "zip"})
        self.assertEqual(200, status)
        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            self.assertEqual(1, len(json.loads(archive.read("conversation-001.json"))["messages"]))
        for ids in ([], [True], [-1], [1], [0, 0], "0"):
            self.assertEqual(400, self.request("/api/export", {"chat": "user_example", "ids": ids})[0])
        self.assertEqual(400, self.request("/api/messages", {"chat": "user_example", "kind": "invalid"})[0])
        self.assertEqual(0, json.loads(self.request("/api/messages", {"chat": "user_example", "kind": "image"})[2])["total"])
        self.request("/api/close", {})
        self.assertEqual({"opened": False, "account": None}, json.loads(self.request("/api/state")[2]))

    def test_batch_zip_and_atomic_invalid_selection(self):
        class MultiSource(FakeSource):
            def conversations(self):
                return [Conversation("user_example", "合成联系人"), Conversation("group_example", "合成群聊", is_group=True)]
        self.server.engine.opener = lambda _: MultiSource()
        self.request("/api/open", {"account": 0, "consent": True})
        chats = json.loads(self.request("/api/chats")[2])
        self.assertEqual(["chat", "group"], [c["kind"] for c in chats])
        status, headers, data = self.request("/api/export", {"chats": ["user_example", "group_example"], "format": "json"})
        self.assertEqual(200, status)
        self.assertEqual("application/zip", headers["Content-Type"])
        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            self.assertEqual(["conversation-001.json", "conversation-002.json"], archive.namelist())
            self.assertEqual("合成群聊", json.loads(archive.read("conversation-002.json"))["session"]["displayName"])
        for selection in ([], ["user_example", "user_example"], [True], ["user_example", "missing"], "user_example"):
            status, headers, data = self.request("/api/export", {"chats": selection})
            self.assertEqual(400, status)
            self.assertNotIn("Content-Disposition", headers)
        self.assertEqual(400, self.request("/api/export", {"chats": ["user_example"], "format": "exe"})[0])


class LauncherTests(unittest.TestCase):
    def test_cli_defaults_and_no_automatic_browser(self):
        calls = []
        class FakeServer:
            server_port = 8787
            urls = ["http://127.0.0.1:8787/"]
            def __init__(self, engine, port, host):
                calls.append((port, host))
            def serve_forever(self):
                raise KeyboardInterrupt
            def server_close(self):
                calls.append("closed")
        out = io.StringIO()
        with patch("siwx_safe.live_app.sys.platform", "win32"), \
             patch("siwx_safe.live_app.discover_accounts", return_value=[]), \
             patch("siwx_safe.live_app.LocalServer", FakeServer), contextlib.redirect_stdout(out):
            self.assertEqual(0, main([]))
        self.assertEqual([(8787, "0.0.0.0"), "closed"], calls)
        self.assertIn("不使用令牌或密码", out.getvalue())
        self.assertIn("不自动打开浏览器", out.getvalue())

    def test_explicit_loopback_and_port_not_silently_changed(self):
        with patch("siwx_safe.live_app.sys.platform", "win32"), \
             patch("siwx_safe.live_app.discover_accounts", return_value=[]), \
             patch("siwx_safe.live_app.LocalServer", side_effect=OSError("port in use")) as server, \
             contextlib.redirect_stderr(io.StringIO()):
            with self.assertRaises(SystemExit) as result:
                main(["--host", "127.0.0.1", "--port", "9000"])
        self.assertEqual(1, result.exception.code)
        self.assertEqual(1, server.call_count)
        self.assertEqual((9000, "127.0.0.1"), server.call_args.args[1:])
