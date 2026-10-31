#!/usr/bin/env python3
"""版本号一致性检查（发版红线）。

发版清单要求以下五处版本号完全一致：
  1. siwx/__init__.py            __version__
  2. README.md                   徽章 badge/version-X.Y.Z-
  3. docs/README.md              "> **版本**: vX.Y.Z"
  4. version.json                version
  5. tests/test_regressions.py   TestVersionSource 钉定字面量

用法：
    python scripts/check_version_consistency.py
    python scripts/check_version_consistency.py --expect v5.0.11

发版 commit（chore(release): prepare vX.Y.Z）由 CI 自动带 --expect 校验，
确保 commit 声称的版本与五处实际值一致。
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def _read(rel: str) -> str:
    return (ROOT / rel).read_text(encoding="utf-8")


def collect() -> dict:
    """返回 {位置: 版本字符串}；解析失败的值为 None。"""
    out = {}

    m = re.search(r'__version__\s*=\s*["\']([^"\']+)["\']',
                  _read("siwx/__init__.py"))
    out["siwx/__init__.py"] = m.group(1) if m else None

    m = re.search(r"badge/version-([0-9]+\.[0-9]+\.[0-9]+)-", _read("README.md"))
    out["README.md 徽章"] = m.group(1) if m else None

    m = re.search(r">\s*\*\*版本\*\*:\s*v?([0-9]+\.[0-9]+\.[0-9]+)",
                  _read("docs/README.md"))
    out["docs/README.md"] = m.group(1) if m else None

    try:
        out["version.json"] = json.loads(_read("version.json")).get("version")
    except (ValueError, OSError):
        out["version.json"] = None

    m = re.search(r'self\.assertEqual\(__version__,\s*["\']([^"\']+)["\']\)',
                  _read("tests/test_regressions.py"))
    out["tests/test_regressions.py"] = m.group(1) if m else None

    return out


def main() -> int:
    # Windows cp936 控制台/管道下 print("✓") 会抛 UnicodeEncodeError，
    # 导致检查通过也以非 0 退出（CI/hook 以退出码消费时绿色被误判为红色）。
    # 统一切到 UTF-8；errors="replace" 兜底任何残余不可编码字符。
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    if hasattr(sys.stderr, "reconfigure"):
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")

    ap = argparse.ArgumentParser(description="检查五处版本号是否一致")
    ap.add_argument("--expect", help="期望版本（可带 v 前缀，如 v5.0.11）")
    args = ap.parse_args()

    versions = collect()
    missing = [k for k, v in versions.items() if not v]
    distinct = sorted({v for v in versions.values() if v})

    for k, v in versions.items():
        print(f"  {k:32s} {v or '（未解析到）'}")

    ok = True
    if missing:
        print(f"\n✗ 以下位置未解析到版本号：{', '.join(missing)}")
        ok = False
    if len(distinct) > 1:
        print(f"\n✗ 版本号不一致：{distinct}")
        ok = False

    if args.expect:
        expect = args.expect.lstrip("v")
        if distinct != [expect]:
            print(f"\n✗ 发版 commit 声称 v{expect}，实际五处为 {distinct or '（空）'}")
            ok = False
        else:
            print(f"\n✓ 发版 commit v{expect} 与五处版本号一致")

    if ok and len(distinct) == 1:
        print(f"\n✓ 版本号一致：{distinct[0]}")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
