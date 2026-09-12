"""Generate version.json from siwx.__version__ and release tag notes.

Usage:
    python scripts/generate_version_json.py [--tag v5.0.0] [--repo owner/name]

The version number is always read from siwx.__version__. Release notes are read
from the annotated tag body when available; otherwise a concise default is used.
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from siwx import __version__  # noqa: E402


def _run_git(args: list[str]) -> str:
    try:
        r = subprocess.run(["git", *args], cwd=ROOT, text=True,
                           stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                           timeout=10, check=False)
        return r.stdout.strip() if r.returncode == 0 else ""
    except Exception:
        return ""


def _tag_notes(tag: str) -> str:
    if not tag:
        return ""
    notes = _run_git(["tag", "-l", tag, "--format=%(contents)"])
    return notes[:4000].strip()


def _default_repo() -> str:
    url = _run_git(["config", "--get", "remote.origin.url"])
    if url.startswith("git@github.com:"):
        return url.split(":", 1)[1].removesuffix(".git")
    if "github.com/" in url:
        return url.split("github.com/", 1)[1].removesuffix(".git")
    return "ImUpXuu/SIWX"


def build_version_json(tag: str, repo: str) -> dict:
    version = __version__.lstrip("v")
    release_tag = tag or f"v{version}"
    notes = _tag_notes(release_tag) or f"stories-in-wx v{version}"
    base = f"https://github.com/{repo}/releases/download/v{version}"
    raw_base = f"https://raw.gh.1s.fan/{repo}/main"
    return {
        "version": version,
        "date": dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%d %H:%M:%S"),
        "notes": notes,
        "assets": {
            "windows": f"{base}/stories-in-wx-v{version}-windows-x64.exe",
            "macos_dmg": f"{base}/stories-in-wx-v{version}-macos.dmg",
        },
        "sha256": f"{base}/SHA256SUMS.txt",
        "update_scripts": {
            "windows": f"{raw_base}/scripts/update_win.bat",
            "macos": f"{raw_base}/scripts/update_mac.sh",
        },
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--tag", default="")
    ap.add_argument("--repo", default=_default_repo())
    ap.add_argument("--output", default=str(ROOT / "version.json"))
    ns = ap.parse_args()
    data = build_version_json(ns.tag, ns.repo)
    Path(ns.output).write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n",
                               encoding="utf-8")
    print(f"version.json -> v{data['version']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
