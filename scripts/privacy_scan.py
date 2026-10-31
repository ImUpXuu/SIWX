#!/usr/bin/env python3
"""隐私红线扫描 —— 检查 diff 新增行是否混入真实个人标识。

仓库红线（AGENTS.md / CLAUDE.md）要求：真实微信号 / wxid、群 id、手机号、
64 位密钥、带用户名的本机路径、个人邮箱，永远不得进入 git。CI 在 PR 与
push 时对**新增行**执行本扫描，命中即失败。

用法：
    python scripts/privacy_scan.py --range origin/main...HEAD
    python scripts/privacy_scan.py --rev HEAD
    python scripts/privacy_scan.py --staged

命中时只输出规则名与打码片段（前 4 字符 + 长度），避免 CI 日志二次泄漏。
"""
from __future__ import annotations

import argparse
import re
import subprocess
import sys

# ── 规则 ────────────────────────────────────────────────────────────
RULES = [
    ("wxid", re.compile(r"\bwxid_[a-z0-9_]{6,}\b", re.I)),
    ("群 id", re.compile(r"\b\d{6,}@chatroom\b")),
    ("手机号", re.compile(r"(?<!\d)1[3-9]\d{9}(?!\d)")),
    ("64 位十六进制密钥", re.compile(r"\b[0-9a-fA-F]{64}\b")),
    ("本机用户路径", re.compile(r"(?:Users[\\/]+|/home/)([A-Za-z][\w.-]*)")),
    ("个人邮箱", re.compile(
        r"\b[\w.+-]+@(?:qq|gmail|163|126|outlook|foxmail|icloud|hotmail)\.com\b", re.I)),
]

# ── 白名单：仓库里已有的合成占位符（新增占位符请同步补到这里）────────
ALLOW_WXID = {
    "wxid_6abc", "wxid_6abc_6001", "wxid_6abc_9001", "wxid_6xyz_9001",
    "wxid_a_b_1234", "wxid_abc_1234", "wxid_abc_123456", "wxid_abc_6001",
    "wxid_abc_9003", "wxid_abc_d901", "wxid_api_test", "wxid_cards",
    "wxid_clean", "wxid_contact", "wxid_contact_1234", "wxid_demo",
    "wxid_demo_1234", "wxid_demo_a", "wxid_demo_b", "wxid_demo_b_1234",
    "wxid_demo_c", "wxid_demo_d", "wxid_empty", "wxid_example_01",
    "wxid_example_01_d29b", "wxid_friend", "wxid_friend_01", "wxid_friends",
    "wxid_from_cache_path", "wxid_full", "wxid_manual", "wxid_mcp_detail",
    "wxid_mcp_test", "wxid_me_123456", "wxid_missing",
    "wxid_never_backed_up", "wxid_no_msg", "wxid_nobody",
    "wxid_nonexistent", "wxid_nope", "wxid_only", "wxid_other",
    "wxid_other_5678", "wxid_owner", "wxid_owner_1234", "wxid_peer",
    "wxid_range", "wxid_redacted_a", "wxid_redacted_a_9001",
    "wxid_redacted_b_9002", "wxid_secret999", "wxid_target", "wxid_test",
    "wxid_with", "wxid_with_xxx", "wxid_without", "wxid_xxx_0000",
    "wxid_xxx_1234", "wxid_xxx_d901",
}
ALLOW_CHATROOM = {"123456789@chatroom", "111111@chatroom", "123456@chatroom"}
ALLOW_PHONE = {"13800138000", "13900000000"}
ALLOW_HEX64 = {
    "00112233445566778899aabbccddeeff1032547698badcfe0123456789abcdef",
    "abcdef1234567890abcdef1234567890abcdef1234567890abcdef1234567890",
    "fedcba9876543210fedcba9876543210fedcba9876543210fedcba9876543210",
}
ALLOW_PATH_USER = {
    "carol", "bob", "xxx", "user", "username", "example", "test", "you",
    "me", "shared", "public", "default", "all users",
}


def _mask(s: str) -> str:
    """只露前 4 个字符 + 长度，避免 CI 日志把命中值再公开一次。"""
    return f"{s[:4]}…({len(s)} 字符)"


def _is_all_same(s: str) -> bool:
    return len(set(s.lower())) == 1


def _iter_added_lines(diff_text: str):
    """产出 (文件, 新文件行号, 行内容)；只取新增行。"""
    path = ""
    new_line = 0
    for raw in diff_text.splitlines():
        if raw.startswith("+++ b/"):
            path = raw[6:]
            continue
        if raw.startswith("+++ "):
            path = raw[4:].strip()
            continue
        m = re.match(r"^@@ -\d+(?:,\d+)? \+(\d+)(?:,\d+)? @@", raw)
        if m:
            new_line = int(m.group(1))
            continue
        if raw.startswith("+") and not raw.startswith("+++"):
            yield path, new_line, raw[1:]
            new_line += 1
        elif raw.startswith("-") and not raw.startswith("---"):
            continue
        else:
            new_line += 1


def scan(diff_text: str) -> list[tuple[str, int, str, str]]:
    findings = []
    for path, lineno, line in _iter_added_lines(diff_text):
        if not path or path.endswith((".png", ".jpg", ".jpeg", ".gif", ".dll",
                                      ".exe", ".dmg", ".db", ".zip")):
            continue
        for name, rx in RULES:
            for m in rx.finditer(line):
                val = m.group(0)
                if name == "wxid" and val.lower() in ALLOW_WXID:
                    continue
                if name == "群 id" and val in ALLOW_CHATROOM:
                    continue
                if name == "手机号" and (val in ALLOW_PHONE or _is_all_same(val)):
                    continue
                if name == "64 位十六进制密钥" and (val in ALLOW_HEX64 or _is_all_same(val)):
                    continue
                if name == "本机用户路径":
                    user = m.group(1).lower()
                    if user in ALLOW_PATH_USER or user.startswith("<"):
                        continue
                findings.append((path, lineno, name, _mask(val)))
    return findings


def _git(*args: str) -> str:
    # git 输出按 UTF-8 解码：Windows 下 text=True 默认用 cp936，
    # diff 内容含非 GBK 字节时会在解码处崩掉（与 print 的编码问题同源）。
    return subprocess.run(["git", *args], text=True, encoding="utf-8",
                          errors="replace", stdout=subprocess.PIPE,
                          stderr=subprocess.PIPE, check=True).stdout


def main() -> int:
    # Windows cp936 控制台/管道下 print("✓") 会抛 UnicodeEncodeError，
    # 导致扫描通过也以非 0 退出，且失败路径的 ✗ 同样打不出、吞掉关键诊断。
    # 统一切到 UTF-8；errors="replace" 兜底任何残余不可编码字符。
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    if hasattr(sys.stderr, "reconfigure"):
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")

    ap = argparse.ArgumentParser(description="扫描 diff 新增行中的真实个人标识")
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--range", help="git diff 范围，如 origin/main...HEAD")
    g.add_argument("--rev", help="单次提交（对比其父提交）")
    g.add_argument("--staged", action="store_true", help="扫描暂存区")
    args = ap.parse_args()

    if args.staged:
        diff = _git("diff", "--cached", "--unified=0", "--no-color")
    elif args.rev:
        diff = _git("show", "--unified=0", "--no-color", "--format=", args.rev)
    else:
        diff = _git("diff", "--unified=0", "--no-color", args.range)

    findings = scan(diff)
    if not findings:
        print("✓ 隐私红线扫描通过：新增行未发现真实个人标识")
        return 0

    print(f"✗ 隐私红线扫描命中 {len(findings)} 处：\n")
    for path, lineno, name, masked in findings:
        print(f"  {path}:{lineno}  [{name}]  {masked}")
    print("\n处理方式：改用明显虚假的占位符（如 wxid_example_01 / alias_abc12345 /"
          " user_a）；确属合成数据的，把该值加入 scripts/privacy_scan.py 的白名单。")
    return 1


if __name__ == "__main__":
    sys.exit(main())
