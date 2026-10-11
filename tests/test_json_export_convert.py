"""json_export_convert 回归：离线 JSON 导出 → MD/HTML 就地补生成。

覆盖：同名输出、消息数对账、manifest converted 段追加且不动 parse/media、
mediaFile 缺失计数、已生成跳过与 --overwrite 重写、坏会话目录不进队列。
"""
import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from siwx.json_export_convert import _session_dirs, convert_dir  # noqa: E402


def _msg(local_id, ts, text, ltype=1, **extra):
    m = {
        "localId": local_id, "platformMessageId": str(local_id),
        "createTime": ts, "localType": ltype,
        "typeName": "文本" if ltype == 1 else "图片",
        "rawContent": text, "content": text, "isSend": 0,
        "senderUsername": "wxid_test_friend", "senderDisplayName": "测试好友",
        "md5": None, "bubbleMd5": None, "voice": None, "sticker": None,
        "channels": None, "quote": None, "link": None, "record": None,
    }
    m.update(extra)
    return m


class TestJsonExportConvert(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name) / "export"
        sess = self.root / "01_测试会话"
        sess.mkdir(parents=True)
        (sess / "media").mkdir()
        (sess / "media" / "0001_abc.jpg").write_bytes(b"jpg")
        self.json_path = sess / "测试会话_20261011_120000_000.json"
        self.json_path.write_text(json.dumps({
            "exportInfo": {"version": "1.0", "generator": "stories-in-wx"},
            "session": {"wxid": "wxid_test_friend",
                        "displayName": "测试会话", "type": "私聊",
                        "platform": "wechat", "isGroup": False,
                        "ownerId": "wxid_test_owner",
                        "firstTimestamp": 1000, "lastTimestamp": 2000,
                        "messageCount": 3},
            "messages": [
                _msg(1, 1000, "你好"),
                _msg(2, 1500, "[图片]", ltype=3, mediaFile="media/0001_abc.jpg",
                     md5="a" * 32),
                _msg(3, 2000, "[图片]", ltype=3, mediaFile="media/9999_missing.jpg",
                     md5="b" * 32),
            ],
        }, ensure_ascii=False), encoding="utf-8")
        (sess / "manifest.json").write_text(json.dumps({
            "generator": "stories-in-wx", "written_count": 3,
            "parse": {"quote_parsed": 1}, "media": {"images_ok": 1},
        }, ensure_ascii=False), encoding="utf-8")

    def tearDown(self):
        self._tmp.cleanup()

    def test_session_dirs_requires_manifest_and_single_json(self):
        dirs = list(_session_dirs(self.root))
        self.assertEqual(len(dirs), 1)
        self.assertEqual(dirs[0][1], self.json_path)

    def test_convert_md_html_outputs(self):
        rc = convert_dir(self.root, ["md", "html"])
        self.assertEqual(rc, 0)
        sess = self.root / "01_测试会话"
        md = sess / "测试会话_20261011_120000_000.md"
        html = sess / "测试会话_20261011_120000_000.html"
        self.assertTrue(md.is_file() and html.is_file())
        md_text = md.read_text(encoding="utf-8")
        self.assertIn("# 聊天记录：测试会话", md_text)
        self.assertIn("[媒体文件](media/0001_abc.jpg)", md_text)
        html_text = html.read_text(encoding="utf-8")
        self.assertIn("window.CHAT_DATA", html_text)
        self.assertIn('"messageCount":3', html_text.replace(" ", ""))
        self.assertIn("media/0001_abc.jpg", html_text)
        # renderer 脚本注入（与原生导出同一渲染器）
        self.assertIn("renderContent", html_text)
        # 头像映射按 md5(username).jpg 确定性规则（此处无头像文件 → 空映射）
        self.assertNotIn("avatars/", html_text)

    def test_manifest_keeps_parse_media_and_appends_converted(self):
        convert_dir(self.root, ["md"])
        mf = json.loads((self.root / "01_测试会话" / "manifest.json")
                        .read_text(encoding="utf-8"))
        self.assertEqual(mf["parse"], {"quote_parsed": 1})
        self.assertEqual(mf["media"], {"images_ok": 1})
        conv = mf["converted"]
        self.assertEqual(conv["message_count"], 3)
        self.assertEqual(conv["media_refs"], 2)
        self.assertEqual(conv["media_missing"], 1)
        self.assertIn("md", conv["formats"])

    def test_skip_existing_and_overwrite(self):
        convert_dir(self.root, ["md"])
        md = self.root / "01_测试会话" / "测试会话_20261011_120000_000.md"
        first = md.read_text(encoding="utf-8")
        md.write_text("stale", encoding="utf-8")
        rc = convert_dir(self.root, ["md"])
        self.assertEqual(rc, 0)
        self.assertEqual(md.read_text(encoding="utf-8"), "stale")  # 跳过
        convert_dir(self.root, ["md"], overwrite=True)
        self.assertEqual(md.read_text(encoding="utf-8"), first)   # 重写

    def test_broken_session_dir_skipped(self):
        bad = self.root / "02_坏目录"
        bad.mkdir()
        (bad / "a.json").write_text("{}", encoding="utf-8")
        (bad / "b.json").write_text("{}", encoding="utf-8")
        self.assertEqual([d.name for d, _ in _session_dirs(self.root)],
                         ["01_测试会话"])


if __name__ == "__main__":
    unittest.main()
