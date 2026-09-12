"""MCP (Model Context Protocol) stdio 服务器。

把 stories-in-wx 的能力以 MCP 工具暴露给 AI 客户端
（Claude Desktop / ZCode 等任何支持 MCP 的宿主）。

传输: stdio, newline-delimited JSON-RPC 2.0（MCP 2024-11-05 规范）
启动: python run.py mcp
说明: 与 Web 控制台共用密钥库与解密产物，只读查询，不做解密操作。
工具开关配置: %LOCALAPPDATA%/stories-in-wx/mcp_config.json
"""
import hashlib
import json
import logging
import os
import sqlite3
import sys
import time
from logging.handlers import RotatingFileHandler
from pathlib import Path

from siwx import paths
from siwx.api_chat import (
    _contact_names, _decode_content, build_messages, message_tables_by_shard,
)

SERVER_NAME = "stories-in-wx"
SERVER_VERSION = "0.2.5"
PROTOCOL_VERSION = "2024-11-05"
SCAN_CAP = 200_000          # 全库搜索最多扫描的行数
JSON = "application/json"

# ── MCP 专用日志（轮转文件 + 详细调用记录）──────────────────────

def _mcp_log_path() -> Path:
    base = (os.environ.get("LOCALAPPDATA")
            or os.environ.get("USERPROFILE") or ".")
    return Path(base) / "stories-in-wx" / "mcp.log"

def _setup_mcp_logger() -> logging.Logger:
    logger = logging.getLogger("siwx.mcp")
    if logger.handlers:
        return logger
    logger.setLevel(logging.DEBUG)
    p = _mcp_log_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    fh = RotatingFileHandler(p, maxBytes=2 * 1024 * 1024, backupCount=3,
                              encoding="utf-8")
    fh.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(message)s",
                                      datefmt="%Y-%m-%d %H:%M:%S"))
    logger.addHandler(fh)
    return logger

mcp_log = _setup_mcp_logger()


# ── 配置（工具开关）─────────────────────────────────────────────

def config_path() -> Path:
    base = (os.environ.get("LOCALAPPDATA")
            or os.environ.get("USERPROFILE") or ".")
    return Path(base) / "stories-in-wx" / "mcp_config.json"


def load_config() -> dict:
    try:
        return json.loads(config_path().read_text(encoding="utf-8"))
    except Exception:
        return {}


def save_config(cfg: dict) -> None:
    p = config_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".tmp")
    tmp.write_text(json.dumps(cfg, ensure_ascii=False, indent=1), encoding="utf-8")
    os.replace(tmp, p)


def tool_enabled(name: str) -> bool:
    return bool(load_config().get("tools", {}).get(name, True))


# ── 数据助手（与 api_chat 同源，独立于 flask）──────────────────

def _accounts() -> list:
    root = paths.out_root()
    out = []
    if root.is_dir():
        for d in sorted(root.iterdir()):
            if (d / "message").is_dir():
                out.append(d.name)
    return out


def _session_list(account: str) -> list:
    acc_dir = paths.out_root() / account
    if not (acc_dir / "message").is_dir():
        raise ValueError(f"账号不存在或未解密: {account}")
    names = _contact_names(acc_dir)
    items = {}
    sdb = acc_dir / "session" / "session.db"
    if sdb.is_file():
        conn = sqlite3.connect(sdb)
        try:
            for un, summary, ts in conn.execute(
                    "SELECT username, summary, sort_timestamp FROM SessionTable"):
                un = (un or "").strip()
                if un:
                    items[un] = {"summary": (summary or "").strip(), "ts": ts or 0}
        except sqlite3.Error:
            pass
        finally:
            conn.close()
    out = [{"username": un, "display": names.get(un) or un,
            "is_group": un.endswith("@chatroom"),
            "preview": (it["summary"] or "")[:80], "last_time": it["ts"]}
           for un, it in items.items()]
    out.sort(key=lambda x: x["last_time"], reverse=True)
    return out


def _json(obj) -> str:
    return json.dumps(obj, ensure_ascii=False, indent=1)


# ── 工具实现 ────────────────────────────────────────────────────

def tool_get_status(_args) -> str:
    from siwx import keystore
    accs = []
    for acc in _accounts():
        n = len(list((paths.out_root() / acc / "message").glob("message_*.db")))
        accs.append({"wxid": acc, "message_dbs": n})
    store = keystore.load()
    wechat_running = False
    try:
        from siwx.discover import find_wechat_pids
        wechat_running = bool(find_wechat_pids())
    except Exception:
        pass
    return _json({"wechat_running": wechat_running,
                  "accounts": accs, "keystore_salts": len(store)})


def tool_list_accounts(_args) -> str:
    return _json({"accounts": _accounts()})


def tool_list_sessions(args) -> str:
    account = (args or {}).get("account", "")
    limit = min(int((args or {}).get("limit", 100) or 100), 500)
    ss = _session_list(account)
    return _json({"account": account, "total": len(ss),
                  "sessions": ss[:limit]})


def tool_get_messages(args) -> str:
    account = args["account"]
    chat = args["chat"]
    limit = min(int(args.get("limit", 100) or 100), 500)
    acc_dir = paths.out_root() / account
    if not (acc_dir / "message").is_dir():
        raise ValueError(f"账号不存在或未解密: {account}")
    msgs = build_messages(acc_dir, chat, account=account)
    page = msgs[-limit:] if len(msgs) > limit else msgs
    slim = [{"ts": m["createTime"], "sender": m["senderDisplayName"],
             "is_me": bool(m["isSend"]), "type": m["typeName"],
             "content": m["content"]} for m in page]
    return _json({"chat": chat, "total": len(msgs), "returned": len(slim),
                  "note": "ts 为 Unix 秒; 时间正序; rawContent 未包含以节省 token",
                  "messages": slim})


def tool_search_messages(args) -> str:
    account = args["account"]
    kw = args["keyword"]
    chat = args.get("chat") or None
    limit = min(int(args.get("limit", 30) or 30), 100)
    acc_dir = paths.out_root() / account
    if not (acc_dir / "message").is_dir():
        raise ValueError(f"账号不存在或未解密: {account}")
    kw_l = kw.lower()

    if chat:
        msgs = build_messages(acc_dir, chat, account=account)
        hits = [{"chat": chat, "ts": m["createTime"],
                 "sender": m["senderDisplayName"], "type": m["typeName"],
                 "content": (m["content"] or "")[:300]}
                for m in msgs
                if kw_l in (m.get("content") or "").lower()
                or kw_l in (m.get("rawContent") or "").lower()]
        return _json({"scope": chat, "scanned": len(msgs), "matches": hits[:limit]})

    # 全库搜索：建 md5(chat) → chat 反查表
    names = _contact_names(acc_dir)
    cands = set(names)
    try:
        for s in _session_list(account):
            cands.add(s["username"])
    except ValueError:
        pass
    table_map = {"Msg_" + hashlib.md5(un.encode()).hexdigest(): un for un in cands}

    hits, scanned = [], 0
    # 分片索引：跳过不含 Msg_ 表的库（media_*/fts/resource/weclaw 等）。
    # smap 是分片级的，原来写在表循环内部 —— biz_message_0.db 有 67 张表，
    # 等于把 Name2Id 重查了 67 次。此处提到分片循环外。
    for db, tables in message_tables_by_shard(acc_dir).items():
        if len(hits) >= limit or scanned >= SCAN_CAP:
            break
        conn = sqlite3.connect(db)
        try:
            smap = {}
            try:
                smap = {rid: un for rid, un in
                        conn.execute("SELECT rowid, user_name FROM Name2Id")}
            except sqlite3.Error:
                pass
            for t in tables:
                if len(hits) >= limit or scanned >= SCAN_CAP:
                    break
                chat_name = table_map.get(t, "")
                for _lid, _sid, ltype, ts, rsid, content in conn.execute(
                        f"SELECT local_id, server_id, local_type, create_time, "
                        f"real_sender_id, message_content FROM [{t}] "
                        f"ORDER BY create_time"):
                    scanned += 1
                    if scanned >= SCAN_CAP or len(hits) >= limit:
                        break
                    text = _decode_content(content)
                    if not text or kw_l not in text.lower():
                        continue
                    sender = smap.get(int(rsid), "") if rsid else ""
                    hits.append({"chat": chat_name, "ts": ts or 0,
                                 "sender": sender, "type": ltype & 0xFFFF,
                                 "content": text[:300]})
        except sqlite3.Error:
            pass
        finally:
            conn.close()
    return _json({"scope": "全部会话", "scanned": scanned, "matches": hits[:limit],
                  "note": f"扫描上限 {SCAN_CAP} 行，命中即停"})


def tool_export_chat(args) -> str:
    from siwx import exporter
    account = args["account"]
    chat = args["chat"]
    fmt = args.get("format", "json")
    acc_dir = paths.out_root() / account
    if not (acc_dir / "message").is_dir():
        raise ValueError(f"账号不存在或未解密: {account}")
    res = exporter.run_export(acc_dir, account, chat, "", fmt,
                              want_messages=True,
                              want_media=bool(args.get("media", False)),
                              want_avatars=bool(args.get("avatars", False)),
                              export_root=paths.exports_root(),
                              pack=args.get("pack", "single"))
    return _json(res)


TOOLS = [
    {"name": "get_status",
     "description": "获取运行状态：微信是否在线、已解密账号列表、密钥库条数",
     "inputSchema": {"type": "object", "properties": {}}},
    {"name": "list_accounts",
     "description": "列出已解密的账号 wxid 列表",
     "inputSchema": {"type": "object", "properties": {}}},
    {"name": "list_sessions",
     "description": "列出某账号的全部会话（聊天列表），含最后消息预览与时间",
     "inputSchema": {"type": "object", "required": ["account"],
                     "properties": {
                         "account": {"type": "string", "description": "账号 wxid"},
                         "limit": {"type": "integer", "description": "返回条数上限，默认 100"}}}},
    {"name": "get_messages",
     "description": "读取某会话的消息（最新 N 条，按时间正序返回）",
     "inputSchema": {"type": "object", "required": ["account", "chat"],
                     "properties": {
                         "account": {"type": "string", "description": "账号 wxid"},
                         "chat": {"type": "string", "description": "会话 username，来自 list_sessions"},
                         "limit": {"type": "integer", "description": "条数上限，默认 100"}}}},
    {"name": "search_messages",
     "description": "按关键词搜索消息；指定 chat 只搜该会话（快），不指定则全库扫描",
     "inputSchema": {"type": "object", "required": ["account", "keyword"],
                     "properties": {
                         "account": {"type": "string"},
                         "keyword": {"type": "string", "description": "搜索关键词"},
                         "chat": {"type": "string", "description": "可选，限定会话"},
                         "limit": {"type": "integer", "description": "命中条数上限，默认 30"}}}},
    {"name": "export_chat",
     "description": "导出某会话聊天记录到文件（json/html/txt/csv/markdown/toml/sqlite/xlsx），返回路径",
     "inputSchema": {"type": "object", "required": ["account", "chat"],
                     "properties": {
                         "account": {"type": "string"},
                         "chat": {"type": "string"},
                         "format": {"type": "string", "description": "默认 json"},
                         "media": {"type": "boolean", "description": "是否解密图片，默认 false"},
                         "avatars": {"type": "boolean", "description": "是否提取头像，默认 false"}}}},
]

_HANDLERS = {
    "get_status": tool_get_status,
    "list_accounts": tool_list_accounts,
    "list_sessions": tool_list_sessions,
    "get_messages": tool_get_messages,
    "search_messages": tool_search_messages,
    "export_chat": tool_export_chat,
}


# ── JSON-RPC 主循环 ─────────────────────────────────────────────

def _tool_call(name: str, args: dict) -> str:
    if not tool_enabled(name):
        raise ValueError(f"工具 {name} 已在 MCP 配置页禁用")
    h = _HANDLERS.get(name)
    if not h:
        raise ValueError(f"未知工具: {name}")
    mcp_log.info("调用 %s args=%s", name, json.dumps(args, ensure_ascii=False)[:500])
    t0 = time.time()
    try:
        result = h(args or {})
        dt = time.time() - t0
        mcp_log.info("完成 %s 耗时 %.2fs 返回 %d 字符", name, dt, len(result))
        return result
    except Exception as e:
        dt = time.time() - t0
        mcp_log.error("失败 %s 耗时 %.2fs: %s", name, dt, e)
        raise


def run_mcp_server() -> None:
    # Windows 管道默认 GBK，MCP 协议要求 UTF-8
    try:
        sys.stdin.reconfigure(encoding="utf-8")
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass

    def _send(obj) -> None:
        sys.stdout.write(json.dumps(obj, ensure_ascii=False) + "\n")
        sys.stdout.flush()

    def _reply(msg, result=None, error=None) -> None:
        if "id" not in msg:      # notification，不回复
            return
        resp = {"jsonrpc": "2.0", "id": msg["id"]}
        if error is not None:
            resp["error"] = error
        else:
            resp["result"] = result
        _send(resp)

    print(f"[mcp] {SERVER_NAME} {SERVER_VERSION} ready (stdio)",
          file=sys.stderr, flush=True)

    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(msg, dict):
            continue
        method = msg.get("method", "")
        try:
            if method == "initialize":
                pv = (msg.get("params") or {}).get("protocolVersion")
                _reply(msg, result={
                    "protocolVersion": pv if isinstance(pv, str) else PROTOCOL_VERSION,
                    "capabilities": {"tools": {}},
                    "serverInfo": {"name": SERVER_NAME, "version": SERVER_VERSION},
                })
            elif method == "ping":
                _reply(msg, result={})
            elif method == "tools/list":
                _reply(msg, result={"tools": [t for t in TOOLS
                                              if tool_enabled(t["name"])]})
            elif method == "tools/call":
                params = msg.get("params") or {}
                name = params.get("name", "")
                args = params.get("arguments") or {}
                try:
                    out = _tool_call(name, args)
                    _reply(msg, result={"content": [{"type": "text", "text": out}]})
                except Exception as e:
                    _reply(msg, result={"content": [{"type": "text",
                                                     "text": f"错误: {e}"}],
                                        "isError": True})
            elif method.startswith("notifications/"):
                pass                       # 客户端通知，忽略
            else:
                _reply(msg, error={"code": -32601,
                                   "message": f"未知方法: {method}"})
        except Exception as e:
            _reply(msg, error={"code": -32603, "message": str(e)})


if __name__ == "__main__":
    run_mcp_server()
