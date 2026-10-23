"""P1-1：PyInstaller 打包版 wxgf 转码子进程入口回归。

打包后 sys.executable 是本程序的 exe（不是 python），旧实现
`[exe, "-c", <worker 源码>, <dll>]` 会被 CLI 的 argparse 当成未知参数，
打包版所有 wxgf（表情/封面/部分图片）永远转码失败。修复：frozen 下改走
`[exe, "--wxgf-worker", <dll>]`，由 run.py 在导入 cli / 构造 argparse
之前拦截，执行 media.py 里同一个 `_WXGF_WORKER` 字符串。

这里不依赖真实打包产物，用两类断言钉住：
  1. `_wxgf_worker_cmd()` 在 frozen / 源码态各返回正确命令行；
  2. 真起 `run.py --wxgf-worker <不存在的 dll>` 子进程，确认没有 argparse
     的 "unrecognized arguments"（worker 自身因 DLL 不存在而失败、非零
     退出是预期行为）。
"""
import subprocess
import sys
import tempfile
import types
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from siwx import media

RUN_PY = ROOT / "run.py"


def test_worker_cmd_frozen_uses_cli_entry(monkeypatch):
    """frozen：不能再带 "-c"（会被 argparse 拦），改走 run.py 的独立入口。"""
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    cmd = media._wxgf_worker_cmd("VoipEngine.dll")
    assert cmd == [sys.executable, "--wxgf-worker", "VoipEngine.dll"]
    assert "-c" not in cmd


def test_worker_cmd_source_mode_keeps_dash_c(monkeypatch):
    """源码态保持原样：`python -c <worker 源码> <dll>`，行为不退化。"""
    monkeypatch.delattr(sys, "frozen", raising=False)
    cmd = media._wxgf_worker_cmd("VoipEngine.dll")
    assert cmd == [sys.executable, "-c", media._WXGF_WORKER, "VoipEngine.dll"]


def test_convert_wxgf_uses_worker_cmd_and_logs_frozen(monkeypatch):
    """convert_wxgf 真的走 _wxgf_worker_cmd，且失败日志区分 frozen；重试次数不变。"""
    calls, logs = [], []

    class _FakeLogger:
        @staticmethod
        def detailed(_cat, msg, ring=True):
            logs.append(msg)

        @staticmethod
        def warn(*_a, **_k):
            pass

    def fake_run(cmd, **_kwargs):
        calls.append(cmd)
        return types.SimpleNamespace(returncode=1, stdout=b"", stderr=b"boom")

    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(media, "_find_voip_dll", lambda: "VoipEngine.dll")
    monkeypatch.setattr(media, "_media_logger", _FakeLogger)
    monkeypatch.setattr(subprocess, "run", fake_run)

    assert media.convert_wxgf(b"wxgf-fake-bytes") is None
    assert len(calls) == 2, "重试次数必须保持 2 次不变"
    assert calls[0] == [sys.executable, "--wxgf-worker", "VoipEngine.dll"]
    assert any("frozen=True" in m for m in logs), logs


def test_run_py_worker_entry_is_not_hijacked_by_argparse():
    """`python run.py --wxgf-worker <不存在的 dll>`：不得出现 argparse 报错。

    worker 自身因 DLL 不存在而失败（非零退出）属预期；关键是它已经走到
    worker 代码，而不是被 CLI 的 argparse 当成未知参数拒掉。
    """
    with tempfile.TemporaryDirectory() as td:
        missing = Path(td) / "wxid_test_missing.dll"
        proc = subprocess.run(
            [sys.executable, str(RUN_PY), "--wxgf-worker", str(missing)],
            cwd=str(ROOT), input="", capture_output=True, text=True, timeout=120)
    combined = (proc.stdout or "") + (proc.stderr or "")
    assert "unrecognized arguments" not in combined, combined
    assert "usage: siwx" not in combined, combined
    assert proc.returncode != 0, "缺少 DLL 时 worker 应以非零码退出"


def test_run_py_worker_flag_without_dll_reports_cleanly():
    """argv 长度保护：只给 --wxgf-worker 时给出明确报错，不进 argparse。"""
    proc = subprocess.run(
        [sys.executable, str(RUN_PY), "--wxgf-worker"],
        cwd=str(ROOT), input="", capture_output=True, text=True, timeout=120)
    combined = (proc.stdout or "") + (proc.stderr or "")
    assert "unrecognized arguments" not in combined, combined
    assert proc.returncode == 2, combined
