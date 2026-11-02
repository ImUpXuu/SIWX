import csv
import hashlib
import io
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

from siwx_safe.documents import export, filtered, load_document, render


class OfflineTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)

    def load(self, data):
        p = self.root / "input.json"
        p.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
        return load_document(p)

    def test_siwx_all_formats_and_source_unchanged(self):
        document = self.load({"session": {"displayName": "合成会话"}, "messages": [
            {"createTime": "2026-11-02", "senderDisplayName": "测试甲", "content": "中文测试"}]})
        source = self.root / "input.json"
        before = hashlib.sha256(source.read_bytes()).digest()
        for fmt in ("html", "md", "txt", "csv", "json"):
            p = self.root / ("output." + fmt)
            export(document, p, fmt)
            self.assertIn("中文测试", p.read_text(encoding="utf-8"))
        self.assertEqual(before, hashlib.sha256(source.read_bytes()).digest())
        with self.assertRaises(FileExistsError):
            export(document, source, "json")
        self.assertEqual(document, load_document(self.root / "output.json"))

    def test_wexport_xml_and_large_type(self):
        doc = self.load([{"type": (57 << 32) | 49, "raw":
            '<msg><appmsg><title>合成链接</title><type>5</type><url>https://example.invalid/</url></appmsg></msg>'}])
        self.assertIn("合成链接", doc["messages"][0]["content"])
        self.assertEqual(1, len(filtered(doc, "合成")["messages"]))
        self.assertEqual(0, len(filtered(doc, "不存在")["messages"]))

    def test_active_content_is_passive(self):
        payload = '<script>alert(1)</script><img src="https://example.invalid/pixel">[x](https://example.invalid)'
        doc = self.load([{"text": payload}])
        result = render(doc, "html")
        self.assertNotIn("<script>", result)
        self.assertNotIn("<img", result)
        self.assertIn("default-src 'none'", result)
        self.assertIn("&lt;script&gt;", result)
        self.assertIn(r"\[x\]\(", render(doc, "md"))

    def test_csv_formula_in_both_sender_and_content(self):
        doc = self.load([{"sender_name": "=1+1", "text": "  @SUM(1)"}])
        row = list(csv.reader(io.StringIO(render(doc, "csv"))))[1]
        self.assertTrue(row[1].startswith("'"))
        self.assertTrue(row[3].startswith("'"))

    def test_malformed_and_entity_input_rejected(self):
        for value in ({}, {"messages": {}}, {"session": [], "messages": []},
                      [None], [{"content": {}}], [{"raw": "test", "type": None}],
                      [{"raw": '<!DOCTYPE msg><msg/>'}]):
            with self.subTest(value=value), self.assertRaises(ValueError):
                self.load(value)

    def test_paths_are_not_followed(self):
        doc = self.load([{"text": "safe", "mediaFile": "../../secret.txt",
                          "source": "https://example.invalid/secret"}])
        self.assertEqual("safe", doc["messages"][0]["content"])
        self.assertNotIn("secret", render(doc, "html"))

    def test_cli_roundtrip_and_no_legacy_import(self):
        self.load([{"text": "合成记录"}])
        proc = subprocess.run([sys.executable, "app.py", str(self.root / "input.json"),
                               "--out", str(self.root / "result.html")], capture_output=True, text=True)
        self.assertEqual(0, proc.returncode, proc.stderr)
        self.assertIn("合成记录", (self.root / "result.html").read_text(encoding="utf-8"))
        proc = subprocess.run([sys.executable, "-c",
            "import sys; from siwx_safe.app import main; "
            "assert not any(n.split('.')[0] in {'siwx','wexport','psutil','flask','ctypes','socket'} for n in sys.modules)"],
            capture_output=True, text=True)
        self.assertEqual(0, proc.returncode, proc.stderr)

    def test_legacy_commands_are_unavailable(self):
        for command in ("auto", "keys", "serve"):
            proc = subprocess.run([sys.executable, "app.py", command], capture_output=True)
            self.assertNotEqual(0, proc.returncode)


if __name__ == "__main__":
    unittest.main()
