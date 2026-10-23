"""P2-11 / P2-12 回归（修订清单 2026-10-23 批次 2，安全与更新链）。

P2-11：只看 Host 挡不住跨源表单——任意网页都能对 127.0.0.1 提交
`<form method=POST>`。写请求必须校验浏览器自带的来源头：
- Origin 的 netloc 必须与 Host 完全一致；`null` / 解析失败 / 非 http(s) → 403；
- Sec-Fetch-Site 只允许 same-origin / none（same-site、cross-site → 403）；
- 两个头都没有（curl/脚本/旧测试）→ 保持原逻辑；
- GET/HEAD/OPTIONS 不校验。
被拒请求必须在进入视图前返回，更新逻辑/任务槽不能被触达。

P2-12：远程 version.json 存在 ≠ 有新版本。
- `do_update` 必须检查 `has_update()[0]`，无更新直接返回，不调 run_update；
- `run_update` 作为安装入口自行兜底：remote ≤ current 在**下载前**拒绝，
  防代理缓存/镜像回退导致的静默降级。

全部使用占位符与合成路径，不联网、不读取真实聊天库。
"""
import unittest
from unittest import mock

from tests._base import IsolatedRootCase

REMOTE = {"version": "5.0.13", "assets": {}, "sha256": ""}


class _ServerHarness(IsolatedRootCase):
    """拿到 Flask test_client；蓝图里的更新钩子由各用例显式替换。"""

    def setUp(self):
        super().setUp()
        from siwx import server
        self.server = server
        self.client = server.app.test_client()

    def _stub_update(self, has=True, remote=REMOTE, cur="5.0.12"):
        """替换 api_update 的两个外部依赖，绝不联网、绝不下载。"""
        from siwx import api_update
        hu = mock.patch.object(api_update, "has_update",
                               return_value=(has, remote, cur))
        ru = mock.patch.object(api_update, "run_update",
                               return_value={"ok": True, "message": "stubbed"})
        return hu, ru


class TestOriginGuard(_ServerHarness):
    """P2-11：非 GET 请求的来源头校验。"""

    def _post_update(self, **headers):
        hu, ru = self._stub_update()
        with hu as hu_mock, ru as ru_mock:
            r = self.client.post("/api/update/do", headers=headers)
        return r, hu_mock, ru_mock

    def test_cross_origin_post_rejected_before_view(self):
        r, hu_mock, ru_mock = self._post_update(
            Origin="http://evil.example", **{"Sec-Fetch-Site": "cross-site"})
        self.assertEqual(r.status_code, 403)
        self.assertIn("error", r.get_json())
        # 守卫在视图之前生效：既没拉远程版本，也没进入安装流程
        hu_mock.assert_not_called()
        ru_mock.assert_not_called()

    def test_cross_origin_origin_alone_rejected(self):
        r, hu_mock, ru_mock = self._post_update(Origin="http://evil.example")
        self.assertEqual(r.status_code, 403)
        hu_mock.assert_not_called()
        ru_mock.assert_not_called()

    def test_same_site_fetch_site_rejected(self):
        for site in ("same-site", "cross-site"):
            r, _hu, ru_mock = self._post_update(**{"Sec-Fetch-Site": site})
            self.assertEqual(r.status_code, 403, site)
            ru_mock.assert_not_called()

    def test_null_and_malformed_origin_rejected(self):
        for origin in ("null", "file://localhost", "http://[::1"):
            r, hu_mock, _ru = self._post_update(Origin=origin)
            self.assertEqual(r.status_code, 403, repr(origin))
            hu_mock.assert_not_called()

    def test_origin_host_mismatch_with_port_rejected(self):
        # Host 是 localhost（test_client 默认），Origin 带端口 → netloc 不等
        r, _hu, _ru = self._post_update(Origin="http://localhost:5000")
        self.assertEqual(r.status_code, 403)

    def test_same_origin_post_reaches_view(self):
        r, hu_mock, ru_mock = self._post_update(
            Origin="http://localhost", **{"Sec-Fetch-Site": "same-origin"})
        self.assertEqual(r.status_code, 200)
        self.assertTrue(r.get_json()["ok"])
        hu_mock.assert_called_once()
        ru_mock.assert_called_once_with(REMOTE)

    def test_fetch_site_none_allowed(self):
        # 地址栏/书签触发：同源无发起者，浏览器给 none
        r, _hu, ru_mock = self._post_update(
            Origin="HTTP://LOCALHOST", **{"Sec-Fetch-Site": "none"})
        self.assertEqual(r.status_code, 200)
        ru_mock.assert_called_once()

    def test_post_without_headers_keeps_old_behavior(self):
        # curl / 脚本 / 旧客户端：没有这两个头，按原逻辑处理
        r, hu_mock, ru_mock = self._post_update()
        self.assertEqual(r.status_code, 200)
        hu_mock.assert_called_once()
        ru_mock.assert_called_once_with(REMOTE)

    def test_get_with_cross_origin_headers_unaffected(self):
        from siwx import api_update
        with mock.patch.object(api_update, "has_update",
                               return_value=(False, None, "5.0.12")) as hu:
            r = self.client.get("/api/update/check",
                                headers={"Origin": "http://evil.example",
                                         "Sec-Fetch-Site": "cross-site"})
        self.assertEqual(r.status_code, 200)
        self.assertFalse(r.get_json()["has_update"])
        hu.assert_called_once()

    def test_other_write_endpoint_also_guarded(self):
        # 任务取消走的是全局 job 槽：跨源 POST 必须在视图前被拒
        r = self.client.post("/api/job/cancel",
                             headers={"Origin": "http://evil.example",
                                      "Sec-Fetch-Site": "cross-site"})
        self.assertEqual(r.status_code, 403)
        self.assertIn("error", r.get_json())


class TestUpdateRequiresNewerVersion(_ServerHarness):
    """P2-12：do_update 先看有无更新，run_update 自己拒绝非升级。"""

    def test_do_update_without_update_skips_run_update(self):
        hu, ru = self._stub_update(has=False)
        with hu, ru as ru_mock:
            r = self.client.post("/api/update/do")
        self.assertEqual(r.status_code, 200)
        body = r.get_json()
        self.assertFalse(body["ok"])
        self.assertIn("无需更新", body["message"])
        self.assertIn("5.0.12", body["message"])
        ru_mock.assert_not_called()

    def test_do_update_with_update_calls_run_update(self):
        hu, ru = self._stub_update(has=True)
        with hu, ru as ru_mock:
            r = self.client.post("/api/update/do")
        self.assertEqual(r.status_code, 200)
        self.assertTrue(r.get_json()["ok"])
        ru_mock.assert_called_once_with(REMOTE)

    def test_do_update_remote_missing_keeps_original_message(self):
        hu, ru = self._stub_update(has=False, remote=None)
        with hu, ru as ru_mock:
            body = self.client.post("/api/update/do").get_json()
        self.assertFalse(body["ok"])
        self.assertIn("无法获取远程版本信息", body["message"])
        ru_mock.assert_not_called()


class TestRunUpdateVersionGate(IsolatedRootCase):
    """run_update 在下载前比较版本，同版本/旧版本一律拒绝。"""

    def _run(self, ver, current="5.0.12"):
        from siwx import auto_update
        with mock.patch.object(auto_update, "is_frozen", return_value=True), \
             mock.patch.object(auto_update, "system_platform",
                               return_value="windows"), \
             mock.patch.object(auto_update, "current_version",
                               return_value=current), \
             mock.patch.object(auto_update.tempfile, "gettempdir",
                               return_value=str(self.tmp)), \
             mock.patch.object(auto_update, "_download_asset",
                               return_value=False) as dl:
            result = auto_update.run_update({"version": ver})
        return result, dl

    def test_same_or_older_version_rejected_before_download(self):
        for ver in ("5.0.12", "5.0.11", "5.0.0"):
            result, dl = self._run(ver)
            self.assertFalse(result["ok"], ver)
            self.assertIn("不高于当前版本", result["message"])
            dl.assert_not_called()

    def test_newer_version_passes_gate(self):
        # 防过度拦截：正常升级必须继续走到下载环节（这里让下载失败即止）
        result, dl = self._run("5.0.13")
        self.assertFalse(result["ok"])
        self.assertIn("下载失败", result["message"])
        dl.assert_called_once()

    def test_v_prefix_still_compares_numerically(self):
        from siwx import auto_update
        self.assertEqual(auto_update._version_tuple("v5.0.12"), (5, 0, 12))
        self.assertLess(auto_update._version_tuple("v5.0.9"),
                        auto_update._version_tuple("5.0.12"))

    def test_source_mode_short_circuit_unchanged(self):
        from siwx import auto_update
        with mock.patch.object(auto_update, "is_frozen", return_value=False):
            result = auto_update.run_update({"version": "5.0.13"})
        self.assertFalse(result["ok"])
        self.assertIn("git pull", result["message"])


if __name__ == "__main__":
    unittest.main()
