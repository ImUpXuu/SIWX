"""入口输入校验 —— 账号名与本地路径的统一规则。

历史上每个蓝图各写一遍：api_sns 有 _ACCOUNT_RE + resolve/relative_to，
而 api_settings（清除）、server.py（/api/run 的 db_dir/out_dir）、
mcp_server（账号拼接）完全没有。同一类「account 拼本地路径」的输入，
在一处被挡住、在另一处却能穿越到 out_root 之外（api_settings 的
wxid 裸取甚至能删任意目录）。这里收成一份，所有 HTTP/MCP 入口共用。
"""
from __future__ import annotations

import re
from pathlib import Path

from siwx import paths

# 单个路径分量：字母数字与 _.-@；注意 ".." 也匹配本正则，
# 因此凡是拼路径的地方都必须再过 resolve()+relative_to 兜底。
ACCOUNT_RE = re.compile(r"^[A-Za-z0-9_.@-]+$")


def valid_account(account) -> bool:
    """账号名是否为合法单段路径分量。"""
    return isinstance(account, str) and bool(ACCOUNT_RE.fullmatch(account))


def account_dir(account, *, must_exist: bool = True) -> Path | None:
    """把 account 解析为 out_root 下的目录；非法/越界返回 None。

    must_exist=True 时目录不存在也返回 None（调用方据此直接判空）。
    """
    if not valid_account(account):
        return None
    root = paths.out_root().resolve()
    try:
        p = (root / account).resolve()
        p.relative_to(root)
    except (OSError, ValueError):
        return None
    if must_exist and not p.is_dir():
        return None
    return p


def within(path, root) -> bool:
    """path 解析后是否落在 root 内（用于 out_dir 之类的写入落点约束）。"""
    if not path:
        return False
    try:
        Path(path).resolve().relative_to(Path(root).resolve())
        return True
    except (OSError, ValueError):
        return False
