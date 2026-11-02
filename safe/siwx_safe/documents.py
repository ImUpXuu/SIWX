"""Read explicit JSON exports, normalize messages, write passive documents.

No account discovery, key extraction, decryption, server, or media resolution.
"""
from __future__ import annotations

import csv
import html
import io
import json
from pathlib import Path

from .vendor.wexport.model import Message
from .vendor.wexport.parse import parse_message

MAX_INPUT = 64 * 1024 * 1024
MAX_MESSAGES = 100_000
MAX_CONTENT = 1_000_000
FIELDS = ("createTime", "senderDisplayName", "typeName", "content")
FORMATS = ("html", "md", "txt", "csv", "json")


def load_document(path: Path) -> dict:
    """Read only the chosen file; never follow paths contained in its JSON."""
    with Path(path).open("rb") as source:
        raw = source.read(MAX_INPUT + 1)
    if len(raw) > MAX_INPUT:
        raise ValueError("单个 JSON 文件不能超过 64 MiB，请分批导出。")
    data = json.loads(raw.decode("utf-8-sig"))
    if isinstance(data, list):
        session, rows = {}, data
    elif isinstance(data, dict) and isinstance(data.get("messages"), list):
        session, rows = data.get("session", {}), data["messages"]
    else:
        raise ValueError("请选择 SIWX 会话 JSON，或 Message 对象组成的 JSON 数组。")
    if not isinstance(session, dict) or len(rows) > MAX_MESSAGES:
        raise ValueError("会话结构不正确，或消息超过十万条，请拆分文件。")
    result = []
    for row in rows:
        if not isinstance(row, dict):
            raise ValueError("消息必须是 JSON 对象。")
        # SIWX enriched content is already readable; wexport Message.raw is XML.
        content = row.get("content", row.get("text", ""))
        if not isinstance(content, str):
            raise ValueError("消息正文必须是文本。")
        raw_content = row.get("raw", row.get("rawContent", ""))
        if not isinstance(raw_content, str):
            raise ValueError("原始消息必须是文本。")
        if max(len(content), len(raw_content)) > MAX_CONTENT:
            raise ValueError("单条消息过大。")
        kind = str(row.get("typeName") or row.get("subtype") or "text")
        sender = str(row.get("senderDisplayName") or row.get("sender_name")
                     or row.get("senderUsername") or row.get("sender") or "未知")
        if not content and raw_content:
            # Reject DTD/entity declarations before invoking the upstream parser.
            if "<!DOCTYPE" in raw_content.upper() or "<!ENTITY" in raw_content.upper():
                raise ValueError("不接受含 DTD 或实体声明的消息。")
            try:
                message_type = int(row.get("type", row.get("localType", 1)))
            except (ValueError, TypeError, OverflowError) as exc:
                raise ValueError("消息类型必须是整数。") from exc
            message = parse_message(Message(
                local_id=0, type=message_type,
                raw=raw_content), media_root=None)
            content, kind = message.text, message.subtype
        result.append(dict(zip(FIELDS, (
            str(row.get("createTime", row.get("timestamp", ""))), sender, kind, content))))
    return {"session": {"displayName": str(session.get("displayName") or "聊天记录")},
            "messages": result}


def filtered(document: dict, query: str) -> dict:
    query = query.casefold()
    return {"session": document["session"], "messages": [
        m for m in document["messages"]
        if query in " ".join(str(m[k]) for k in FIELDS).casefold()]}


def _md(text: str) -> str:
    # Make imported HTML, images, and links passive text in Markdown viewers.
    for char in "\\`*_{}[]()#+-.!|><":
        text = text.replace(char, "\\" + char)
    return text


def render(document: dict, fmt: str) -> str:
    rows, title = document["messages"], document["session"]["displayName"]
    if fmt == "json":
        return json.dumps(document, ensure_ascii=False, indent=2) + "\n"
    if fmt == "html":
        esc = html.escape
        # No scripts, active links, images, external styles, or network requests.
        parts = ['<!doctype html><html lang="zh-CN"><meta charset="utf-8">',
                 '<meta name="viewport" content="width=device-width,initial-scale=1">',
                 '<meta http-equiv="Content-Security-Policy" content="default-src \'none\'; '
                 'style-src \'unsafe-inline\'; base-uri \'none\'; form-action \'none\'">',
                 '<title>' + esc(title) + '</title><style>body{max-width:900px;margin:32px auto;'
                 'padding:0 20px;font:16px/1.6 system-ui;color:#172238;background:#f4f7fa}'
                 'article{background:white;padding:16px;margin:12px 0;border-radius:12px}'
                 'pre{white-space:pre-wrap;overflow-wrap:anywhere;font:inherit}'
                 'small{color:#536174}</style><h1>' + esc(title) + '</h1>',
                 '<p>离线记录 · 浏览器 Ctrl+F 搜索 · ' + str(len(rows)) + ' 条</p>']
        for m in rows:
            parts.append('<article><b>' + esc(m["senderDisplayName"]) + '</b> <small>'
                         + esc(m["createTime"] + ' · ' + m["typeName"]) + '</small><pre>'
                         + esc(m["content"]) + '</pre></article>')
        return "\n".join(parts) + '</html>\n'
    if fmt == "csv":
        out = io.StringIO(newline="")
        writer = csv.writer(out)
        writer.writerow(FIELDS)
        for m in rows:
            # Spreadsheet formula injection may follow leading spaces/control chars.
            writer.writerow(["'" + m[k] if m[k].lstrip().startswith(("=", "+", "-", "@"))
                             or m[k].startswith(("\t", "\r", "\n")) else m[k] for k in FIELDS])
        return out.getvalue()
    if fmt in ("md", "txt"):
        escape = _md if fmt == "md" else str
        return escape(title) + "\n\n" + "\n\n".join(
            escape(m["createTime"] + " · " + m["senderDisplayName"])
            + "\n" + escape(m["content"]) for m in rows) + "\n"
    raise ValueError("不支持的导出格式。")


def export(document: dict, target: Path, fmt: str) -> None:
    """Exclusive create: never overwrite an existing file, including the input."""
    content = render(document, fmt)
    with Path(target).open("x", encoding="utf-8", newline="") as output:
        output.write(content)
