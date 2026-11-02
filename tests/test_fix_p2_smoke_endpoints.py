"""P2-15：/api/plugins 端点群与导出 start/download 的 HTTP 级冒烟测试。

此前 grep `/api/plugins` 在 tests/ 零命中，plugin_asset 的路径穿越校验
（安全相关）与 /api/export/download 的 400/404/200 行为均无直接回归。

说明（与任务描述的路由名对齐现实）：
- /api/plugins 群实际路由为 GET ""（列表）、GET /pages、GET|POST /settings、
  GET /assets/<plugin>/<path:filename>（api_plugins.py 没有 enable/disable
  端点——插件启停靠 .disabled 文件，不走 HTTP）；
- /api/export/start 并不存在：导出任务通过 POST /api/run（mode=export）
  复用全局 /api/job 单槽（见 server.py），产物下载走 GET /api/export/download。
  本套件按真实路由覆盖：参数校验 400、起任务→轮询→下载产物（exporter mock）。

全部使用合成数据（wxid_example_01 / 临时目录），不读真实微信库，
不写真实用户配置（LOCALAPPDATA 重定向到临时目录）。
"""
import os
import shutil
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

from tests._base import IsolatedRootCase

ROOT = Path(__file__).resolve().parent.parent


# ── 测试替身：不依赖 Flask 的轻量插件模块对象（同 test_plugins.py） ──

class FakeModule:
    """模拟被 import 的插件模块（契约解析只看 __dict__）。"""

    def __init__(self, source="dir:/tmp/fake_plugin.py", **fns):
        self.__siwx_source__ = source
        self.__siwx_pages_dir__ = ""
        for k, v in fns.items():
            if k.startswith("__") and k.endswith("__"):
                continue
            setattr(self, k, v)


def _reset_server_job():
    """复位 server 的全局任务槽，避免后台线程污染同进程后续用例。"""
    from siwx import server
    server._job.update({"running": False, "mode": "", "done": False,
                        "ok": False, "logs": [], "report": None,
                        "error": None, "export_dir": None,
                        "progress": {"pct": 0, "msg": ""}})
    server._LOG_RING.clear()


class PluginsApiSmokeBase(IsolatedRootCase):
    """/api/plugins 群公共夹具：隔离根 + 注册表清空 + LOCALAPPDATA 重定向。

    LOCALAPPDATA 必须重定向：POST /settings 会经 plugins.config 落盘到
    %LOCALAPPDATA%/stories-in-wx/plugin_config/，不隔离就会写真实用户配置。
    """

    def setUp(self):
        super().setUp()
        self._old_localappdata = os.environ.get("LOCALAPPDATA")
        os.environ["LOCALAPPDATA"] = str(self.tmp / "appdata")
        self._client = None

    def tearDown(self):
        if self._old_localappdata is None:
            os.environ.pop("LOCALAPPDATA", None)
        else:
            os.environ["LOCALAPPDATA"] = self._old_localappdata
        super().tearDown()

    @property
    def client(self):
        if self._client is None:
            from siwx import server
            self._client = server.app.test_client()
        return self._client

    def _register_plugin(self, name="demo_plug", version="1.0.0",
                         pages=None, settings=None):
        """注册一个合成插件（页面 / 设置项），返回 registry 便于断言。"""
        from siwx.plugins import registry
        from siwx.plugins.contract import register_plugin
        hooks = {}
        if pages:
            hooks["pages"] = pages
        if settings:
            hooks["settings"] = settings
        counts = register_plugin(FakeModule(), registry,
                                 {"name": name, "version": version, **hooks})
        return registry, counts


class TestPluginsListSmoke(PluginsApiSmokeBase):
    """GET /api/plugins：零插件红线 + 合成加载报告的形状。"""

    def test_empty_registry_returns_empty_collections(self):
        """零插件时列表返回空集合且计数全 0（前端据此静默跳过）。"""
        r = self.client.get("/api/plugins")
        self.assertEqual(r.status_code, 200)
        j = r.get_json()
        self.assertEqual(j["plugins"], [])
        self.assertEqual(j["counts"], {"ok": 0, "degraded": 0, "error": 0})
        self.assertEqual(j["hooks"], {})

    def test_list_reflects_load_report_shape(self):
        """有加载报告时列表带 name/version/status/hooks 等字段。"""
        from siwx.plugins import registry
        from siwx.plugins.report import PluginLoadReport
        rep = PluginLoadReport()
        rep.add_ok("demo_plug", "1.2.3", source="dir:/tmp/demo_plug.py",
                   hooks={"pages": 1, "settings": 2})
        registry.report = rep
        r = self.client.get("/api/plugins")
        self.assertEqual(r.status_code, 200)
        j = r.get_json()
        self.assertEqual(j["counts"]["ok"], 1)
        self.assertEqual(len(j["plugins"]), 1)
        row = j["plugins"][0]
        for key in ("name", "version", "source", "status", "hooks"):
            self.assertIn(key, row)
        self.assertEqual(row["name"], "demo_plug")
        self.assertEqual(row["version"], "1.2.3")
        self.assertEqual(row["status"], "ok")


class TestPluginsPagesSmoke(PluginsApiSmokeBase):
    """GET /api/plugins/pages：条件过滤与 ?all=1 调试视图。"""

    def test_pages_hidden_by_condition_and_all_flag_reveals(self):
        """无条件页默认可见；requires_decrypted 页隐藏，?all=1 显形并带原因。"""
        self._register_plugin(pages=[
            {"name": "always", "title": "总是显示"},
            {"name": "needs_dec", "title": "需解密",
             "condition": {"requires_decrypted": True}},
        ])
        # ConditionContext.build 会扫真实环境，mock 成无解密账号的确定上下文
        from siwx.plugins.conditions import ConditionContext
        fixed = ConditionContext(decrypted_accounts=[], wechat_running=False,
                                 version="5.0.0", plugin_config={})
        with mock.patch.object(ConditionContext, "build",
                               return_value=fixed):
            r = self.client.get("/api/plugins/pages")
            self.assertEqual(r.status_code, 200)
            pages = r.get_json()["pages"]
            self.assertEqual([p["name"] for p in pages], ["always"])
            item = pages[0]
            self.assertEqual(item["id"], "demo_plug:always")
            self.assertEqual(item["plugin"], "demo_plug")
            self.assertEqual(item["base"], "/plugin-pages/demo_plug")
            self.assertTrue(item["conditions_met"])

            r2 = self.client.get("/api/plugins/pages?all=1")
            j2 = r2.get_json()
            by_name = {p["name"]: p for p in j2["pages"]}
            self.assertEqual(set(by_name), {"always", "needs_dec"})
            self.assertFalse(by_name["needs_dec"]["conditions_met"])
            self.assertEqual(by_name["needs_dec"]["conditions"],
                             {"requires_decrypted": True})
            self.assertIn("requires_decrypted",
                          by_name["needs_dec"]["reason"])


class TestPluginsSettingsSmoke(PluginsApiSmokeBase):
    """GET/POST /api/plugins/settings：schema 下发、读写回路与参数校验。"""

    SETTINGS = [
        {"key": "limit", "type": "int", "label": "上限",
         "default": 5, "min": 0, "max": 100, "group": "常规"},
        {"key": "flag", "type": "bool", "label": "开关", "default": False},
    ]

    def test_get_settings_returns_schema_with_current_values(self):
        self._register_plugin(name="cfg_plug", settings=self.SETTINGS)
        r = self.client.get("/api/plugins/settings")
        self.assertEqual(r.status_code, 200)
        groups = r.get_json()["plugins"]
        self.assertEqual([g["plugin"] for g in groups], ["cfg_plug"])
        items = {i["key"]: i for i in groups[0]["items"]}
        self.assertEqual(set(items), {"limit", "flag"})
        self.assertEqual(items["limit"]["value"], 5)          # 缺省值
        self.assertEqual(items["limit"]["min"], 0)
        self.assertEqual(items["flag"]["type"], "bool")

    def test_post_settings_roundtrip_and_validation(self):
        """合法更新落盘并可读回；缺 plugin 400、未知插件 404、values 非 400。"""
        self._register_plugin(name="cfg_plug", settings=self.SETTINGS)
        r = self.client.post("/api/plugins/settings",
                             json={"plugin": "cfg_plug",
                                   "values": {"limit": 42, "flag": True}})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.get_json()["values"]["limit"], 42)

        # 读回路：schema 接口返回更新后的当前值
        j = self.client.get("/api/plugins/settings").get_json()
        items = {i["key"]: i for i in j["plugins"][0]["items"]}
        self.assertEqual(items["limit"]["value"], 42)
        self.assertTrue(items["flag"]["value"])

        # 缺 plugin → 400
        r400 = self.client.post("/api/plugins/settings",
                                json={"values": {"limit": 1}})
        self.assertEqual(r400.status_code, 400)
        self.assertIn("plugin", r400.get_json()["error"])
        # 未知插件 → 404
        r404 = self.client.post("/api/plugins/settings",
                                json={"plugin": "no_such_plug",
                                      "values": {"limit": 1}})
        self.assertEqual(r404.status_code, 404)
        # values 不是对象 → 400
        rbad = self.client.post("/api/plugins/settings",
                                json={"plugin": "cfg_plug",
                                      "values": ["not", "a", "dict"]})
        self.assertEqual(rbad.status_code, 400)

    def test_post_settings_rejects_out_of_schema_keys(self):
        """未在 schema 声明的键不得落盘（防任意写）。"""
        self._register_plugin(name="cfg_plug", settings=self.SETTINGS)
        r = self.client.post("/api/plugins/settings",
                             json={"plugin": "cfg_plug",
                                   "values": {"limit": 7, "hacker": "boom"}})
        self.assertEqual(r.status_code, 200)
        self.assertNotIn("hacker", r.get_json()["values"])
        self.assertNotIn("hacker",
                         self.client.get("/api/plugins/settings")
                         .get_json()["plugins"][0]["items"][0])


class TestPluginAssetTraversal(PluginsApiSmokeBase):
    """GET /api/plugins/assets/<plugin>/<file>：资源服务与路径穿越拒绝。"""

    def _make_plugin_with_assets(self):
        assets = self.tmp / "assets_demo_plug"
        assets.mkdir(parents=True, exist_ok=True)
        (assets / "index.html").write_text("<div>demo page</div>",
                                           encoding="utf-8")
        # 放一个"机密"文件在插件资源目录之外（同级的越界目标）
        (self.tmp / "secret.txt").write_text("TOP SECRET",
                                             encoding="utf-8")
        self._register_plugin(pages=[{"name": "pg", "title": "演示页"}])
        from siwx.plugins import registry
        registry.pages.get("pg").pages_dir = str(assets)
        return assets

    def test_asset_served_for_known_plugin(self):
        self._make_plugin_with_assets()
        r = self.client.get("/api/plugins/assets/demo_plug/index.html")
        self.assertEqual(r.status_code, 200)
        self.assertIn(b"demo page", r.data)

    def test_asset_unknown_plugin_404(self):
        self._make_plugin_with_assets()
        r = self.client.get("/api/plugins/assets/no_such_plug/index.html")
        self.assertEqual(r.status_code, 404)
        self.assertIn("未知插件", r.get_json()["error"])

    def test_asset_blocks_path_traversal(self):
        """../ 越界必须被拒绝（安全边界）：拿不到 200、也拿不到机密内容。"""
        self._make_plugin_with_assets()
        for evil in ("../secret.txt", "....//secret.txt",
                     "sub/../../secret.txt", "..%2Fsecret.txt"):
            r = self.client.get(f"/api/plugins/assets/demo_plug/{evil}")
            self.assertEqual(r.status_code, 404, evil)
            self.assertNotIn(b"TOP SECRET", r.data, evil)

    def test_asset_rejects_invalid_plugin_name(self):
        """插件名带非法字符（路径分隔符等）→ 一律 404，不进目录解析。"""
        self._make_plugin_with_assets()
        for bad in ("..", "a.b/c", "demo_plug%00"):
            r = self.client.get(f"/api/plugins/assets/{bad}/index.html")
            self.assertEqual(r.status_code, 404, bad)


class TestExportDownload(IsolatedRootCase):
    """GET /api/export/download：参数校验 400、越界 404、产物 200。"""

    def setUp(self):
        super().setUp()
        self.addCleanup(_reset_server_job)

    def test_download_requires_path(self):
        from siwx import server
        client = server.app.test_client()
        r = client.get("/api/export/download")
        self.assertEqual(r.status_code, 400)
        self.assertIn("path", r.get_json()["error"])

    def test_download_rejects_outside_root_and_missing_file(self):
        from siwx import paths, server
        client = server.app.test_client()
        # 越界：exports 根之外的既有文件必须 404
        outside = self.tmp / "outside.txt"
        outside.write_text("x", encoding="utf-8")
        r = client.get("/api/export/download",
                       query_string={"path": str(outside)})
        self.assertEqual(r.status_code, 404)
        # 根内但不存在的文件 → 404
        root = paths.exports_root()
        root.mkdir(parents=True, exist_ok=True)
        r2 = client.get("/api/export/download",
                        query_string={"path": str(root / "nope.json")})
        self.assertEqual(r2.status_code, 404)

    def test_download_serves_artifact_inside_root(self):
        from siwx import paths, server
        client = server.app.test_client()
        root = paths.exports_root()
        art = root / "demo_20260101" / "demo_20260101.json"
        art.parent.mkdir(parents=True, exist_ok=True)
        art.write_text('{"session": {"id": "demo"}, "messages": []}',
                       encoding="utf-8")
        r = client.get("/api/export/download",
                       query_string={"path": str(art)})
        self.assertEqual(r.status_code, 200)
        self.assertIn(b'"messages"', r.data)
        self.assertIn("attachment", r.headers.get("Content-Disposition", ""))


class TestExportStartViaRun(IsolatedRootCase):
    """POST /api/run（mode=export，即导出任务的 start 入口）→ /api/job 轮询
    → /api/export/download。exporter 与 db_dir 校验 mock，其余走真实路由。"""

    def setUp(self):
        super().setUp()
        self.addCleanup(_reset_server_job)

    def _make_decrypted_account(self):
        """造一个最小"已解密账号"目录（out_root/<account>/message/）。"""
        from siwx import paths
        acc = paths.out_root() / "wxid_example_01"
        (acc / "message").mkdir(parents=True, exist_ok=True)
        return acc

    def _wait_job_done(self, client, timeout_s=15.0):
        deadline = time.time() + timeout_s
        while time.time() < deadline:
            st = client.get("/api/job").get_json()
            if st["done"]:
                return st
            time.sleep(0.05)
        self.fail("导出任务未在限时内结束")

    def test_run_rejects_invalid_db_dir_and_out_dir(self):
        """参数校验失败必须回 400 且不启动任务（db_dir / out_dir 两条边界）。"""
        from siwx import paths, server
        client = server.app.test_client()
        # db_dir 不存在 → 400
        r = client.post("/api/run", json={
            "mode": "export",
            "db_dir": str(self.tmp / "no_such_dir")})
        self.assertEqual(r.status_code, 400)
        self.assertIn("db_dir", r.get_json()["error"])
        # out_dir 越出输出根 → 400
        r2 = client.post("/api/run", json={
            "mode": "decrypt",
            "out_dir": str(self.tmp / "outside_out")})
        self.assertEqual(r2.status_code, 400)
        self.assertIn("out_dir", r2.get_json()["error"])
        # 两次都没有启动任务
        self.assertFalse(client.get("/api/job").get_json()["running"])

    def test_run_export_fails_cleanly_without_accounts(self):
        """无账号可用：start 受理、任务以 ok=False 收场且错误进 error 字段。"""
        from siwx import server
        client = server.app.test_client()
        with mock.patch("siwx.server.find_wechat_data_dirs",
                        return_value=[]):
            r = client.post("/api/run", json={
                "mode": "export",
                "export_opts": {"account": "wxid_example_01",
                                "chats": [{"chat": "wxid_friend_01",
                                           "display": "B"}]}})
            self.assertTrue(r.get_json().get("started"), r.get_json())
            st = self._wait_job_done(client)
        self.assertFalse(st["ok"])
        self.assertIn("未找到微信数据目录", st["error"])
        self.assertIsNone(st["report"])

    def test_export_start_poll_download_happy_path(self):
        """start→轮询→download 全链路：mock 的导出器落一个 JSON 产物，
        任务报告带产物目录，download 端点能原样取回内容。"""
        from siwx import paths, server
        acc = self._make_decrypted_account()

        def fake_run_export_multi(acc_dir, account, chats, **kw):
            self.assertEqual(account, "wxid_example_01")
            # CI 环境下 8.3 短路径（RUNNER~1）与 /var 符号链接会改变字面形式，
            # 一律 resolve() 后再比，只验证指向同一真实目录
            self.assertEqual(Path(acc_dir).resolve(), Path(acc).resolve())
            self.assertEqual(len(chats), 1)
            export_root = Path(kw["export_root"])
            art_dir = export_root / "demo_20260101"
            art_dir.mkdir(parents=True, exist_ok=True)
            art = art_dir / "demo_20260101.json"
            art.write_text('{"session": {"id": "demo"}, "messages": []}',
                           encoding="utf-8")
            return {"ok_count": 1, "total": 1, "message_count": 1,
                    "media_count": 0, "duration_ms": 1,
                    "total_dir": str(art_dir)}

        db_dir = self.tmp / "wxid_example_01" / "db_storage"
        db_dir.mkdir(parents=True, exist_ok=True)
        client = server.app.test_client()
        with mock.patch("siwx.discover.validate_db_path",
                        return_value={"ok": True}), \
                mock.patch("siwx.exporter.run_export_multi",
                           side_effect=fake_run_export_multi) as rm:
            r = client.post("/api/run", json={
                "mode": "export",
                "db_dir": str(db_dir),
                "export_opts": {
                    "account": "wxid_example_01",
                    "chats": [{"chat": "wxid_friend_01", "display": "B"}],
                    "format": "json"}})
            self.assertEqual(r.status_code, 200)
            self.assertTrue(r.get_json().get("started"), r.get_json())

            st = self._wait_job_done(client)
        self.assertTrue(st["ok"], st.get("error"))
        self.assertEqual(st["report"]["kind"], "export")
        self.assertEqual(st["report"]["ok_count"], 1)
        self.assertEqual(st["report"]["total_dir"],
                         str(paths.exports_root() / "demo_20260101"))
        self.assertEqual(rm.call_count, 1)

        # download 端点取回任务产物
        art = paths.exports_root() / "demo_20260101" / "demo_20260101.json"
        r2 = client.get("/api/export/download",
                        query_string={"path": str(art)})
        self.assertEqual(r2.status_code, 200)
        self.assertIn(b'"id": "demo"', r2.data)

    def test_run_rejects_second_job_while_running(self):
        """单槽语义：任务进行中再 start 必须 409。"""
        from siwx import server
        client = server.app.test_client()
        gate = threading.Event()

        def slow_export(*a, **kw):
            gate.wait(10)
            return {"ok_count": 0, "total": 0, "message_count": 0,
                    "media_count": 0, "duration_ms": 0, "total_dir": ""}

        self._make_decrypted_account()
        db_dir = self.tmp / "wxid_example_01" / "db_storage"
        db_dir.mkdir(parents=True, exist_ok=True)
        with mock.patch("siwx.discover.validate_db_path",
                        return_value={"ok": True}), \
                mock.patch("siwx.exporter.run_export_multi",
                           side_effect=slow_export):
            r = client.post("/api/run", json={
                "mode": "export", "db_dir": str(db_dir),
                "export_opts": {"account": "wxid_example_01",
                                "chats": [{"chat": "wxid_friend_01",
                                           "display": "B"}]}})
            self.assertTrue(r.get_json().get("started"))
            # 等任务真正进入 running 态再抢槽
            deadline = time.time() + 10
            while time.time() < deadline:
                if client.get("/api/job").get_json()["running"]:
                    break
                time.sleep(0.05)
            r2 = client.post("/api/run", json={"mode": "export"})
            self.assertEqual(r2.status_code, 409)
            gate.set()
            self._wait_job_done(client)


if __name__ == "__main__":
    unittest.main(verbosity=2)
