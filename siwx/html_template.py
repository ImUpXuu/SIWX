"""HTML 导出模板 —— 数据驱动交互式查看器（参考 CipherTalk 风格）。

把消息数据嵌入 window.CHAT_DATA，JS 端渲染：
统计面板 / 类型筛选 / 日期范围 / 搜索 / 日期跳转 / 明暗主题 / 灯箱。
支持所有消息类型的微信风格渲染。

模板可替换框架：模板 = 模板包目录（manifest.json + head.html +
renderer.js），head.html 承载样式与页面骨架，renderer.js 承载渲染与
交互；数据契约（window.CHAT_DATA / WX_FACES / WX_MAPS / MSG_COUNT /
CHAT_TITLE）由本模块统一注入，模板不负责 JSON 拼装。解析顺序：
<SIWX_ROOT>/templates/<name>（用户自定义）→ siwx/templates/<name>（内置）。
"""
import functools
import json
from datetime import datetime
from pathlib import Path

_BUILTIN_TEMPLATES = Path(__file__).parent / "templates"


def user_templates_root() -> Path:
    """用户自定义模板根目录（<SIWX_ROOT>/templates/，同名覆盖内置）。"""
    from siwx import paths
    return paths.templates_root()


class TemplatePackage:
    """一个模板包：head.html（样式+骨架）+ renderer.js（渲染器）。"""

    def __init__(self, root: Path):
        mf = json.loads((root / "manifest.json").read_text(encoding="utf-8"))
        self.root = root
        self.name = mf.get("name") or root.name
        self.label = mf.get("label") or self.name
        self.version = mf.get("version") or ""
        self.head = (root / "head.html").read_text(encoding="utf-8")
        self.renderer = (root / "renderer.js").read_text(encoding="utf-8")


@functools.lru_cache(maxsize=8)
def _load_template_cached(name: str, user_root: str) -> TemplatePackage:
    # user_root 进缓存键：SIWX_ROOT 变化（测试/多实例）时失效缓存
    for base in (Path(user_root), _BUILTIN_TEMPLATES):
        root = base / name
        if (root / "manifest.json").is_file():
            return TemplatePackage(root)
    raise RuntimeError(
        f"HTML 模板不存在: {name}（内置可用: "
        f"{', '.join(p.name for p in _BUILTIN_TEMPLATES.iterdir() if p.is_dir())}）")


def get_template(name: str | None = None) -> TemplatePackage:
    name = name or "default"
    return _load_template_cached(name, str(user_templates_root()))


def list_templates() -> list:
    """可用模板（同名用户模板优先），供 UI 下拉与 CLI 列举。"""
    out, seen = [], set()
    for base, builtin in ((user_templates_root(), False),
                          (_BUILTIN_TEMPLATES, True)):
        if not base.is_dir():
            continue
        for d in sorted(base.iterdir()):
            if not d.is_dir() or d.name in seen:
                continue
            mf = d / "manifest.json"
            if not mf.is_file():
                continue
            try:
                info = json.loads(mf.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            seen.add(d.name)
            out.append({"name": d.name,
                        "label": info.get("label") or d.name,
                        "version": info.get("version") or "",
                        "builtin": builtin})
    return out


def _fmt_time(ts):
    return datetime.fromtimestamp(ts or 0).strftime("%Y-%m-%d %H:%M:%S")


def _fmt_date(ts):
    return datetime.fromtimestamp(ts or 0).strftime("%Y年%m月%d日")


def _msg_entry(m):
    """单条消息 → CHAT_DATA entry（build_chat_data 与流式写出共用）。"""
    entry = {
        "timestamp": m["createTime"],
        "sender": m["senderUsername"],
        "senderName": m["senderDisplayName"],
        "type": m["localType"],
        "content": m["content"],
        "rawContent": m["rawContent"][:8000],
        "isSend": m["isSend"],
    }
    if m.get("mediaFile"):
        entry["mediaPath"] = m["mediaFile"]
    if m.get("quote"):
        entry["quote"] = {"sender": m["quote"]["displayname"],
                          "content": m["quote"]["content"],
                          "ts": m["quote"].get("ts") or 0}
    if m.get("link"):
        entry["link"] = {"title": m["link"]["title"],
                         "url": m["link"]["url"] or ""}
    # D5 导出侧：合并转发（appmsg type=19 recordinfo）逐条结构，
    # 由 export_stream.parse_quote_or_link 预解析（全量原文，不受截断影响）
    if m.get("record"):
        entry["record"] = m["record"]
    return entry


def build_chat_data(session, msgs, avatar_map):
    """把内部消息格式转为 CHAT_DATA 嵌入格式。"""
    members_map = {}
    members = []
    for m in msgs:
        un = m["senderUsername"]
        if un and un not in members_map:
            name = m["senderDisplayName"] or un
            members_map[un] = name
            members.append({"id": un, "name": name,
                            "avatar": avatar_map.get(un, "")})

    messages = [_msg_entry(m) for m in msgs]

    is_group = session.get("isGroup", False)
    return {
        "meta": {
            "sessionId": session["wxid"],
            "sessionName": session["displayName"],
            "isGroup": is_group,
            "exportTime": int(datetime.now().timestamp() * 1000),
            "messageCount": len(messages),
            "dateRange": {"start": session["firstTimestamp"],
                          "end": session["lastTimestamp"]},
            "ownerId": session.get("ownerId", ""),
        },
        "members": members,
        "avatarFiles": avatar_map,
        "messages": messages,
    }


def _meta_dict(session, message_count):
    is_group = session.get("isGroup", False)
    return {
        "sessionId": session["wxid"],
        "sessionName": session["displayName"],
        "isGroup": is_group,
        "exportTime": int(datetime.now().timestamp() * 1000),
        "messageCount": message_count,
        "dateRange": {"start": session["firstTimestamp"],
                      "end": session["lastTimestamp"]},
        "ownerId": session.get("ownerId", ""),
    }


def stream_html_head(f, session, members, avatar_map, template=None):
    """流式渲染（P1 修复）：写 HTML 头 + CHAT_DATA 前缀。

    此前 _write_html_streaming 把全部消息 accumulate 成 lines 再一次性
    render_html，3 万条会话内存翻倍 + 单个巨型字符串，与"万条不 OOM"
    目标矛盾。现在消息体边消费边写文件句柄，内存 O(1)（除成员表）。
    约定：head 之后用 stream_html_msg 逐条写、stream_html_tail 收尾。
    """
    tpl = get_template(template)
    head_obj = {"meta": _meta_dict(session, session.get("messageCount", 0)),
                "members": members, "avatarFiles": avatar_map}
    head = _script_safe_json(head_obj)
    # 去掉结尾 }，拼 ,"messages":[ —— 与 IncrementalJSONWriter 同手法
    f.write(tpl.head)
    f.write(f"\n<script>window.CHAT_DATA = {head[:-1]},\"messages\":[")
    f.flush()


def stream_html_msg(f, m, first: bool):
    """写单条消息 entry；first=False 时前置逗号。"""
    if not first:
        f.write(",")
    f.write(_script_safe_json(_msg_entry(m)))


def stream_html_tail(f, count: int, title: str, faces: dict | None = None,
                     maps: dict | None = None, template=None):
    """写 CHAT_DATA 收尾 + 渲染器脚本。count 为实际写出的消息数。

    faces: 本会话用到的微信小黄脸 name → dataURI（由 exporter 收集传入），
    注入 window.WX_FACES 供渲染器把 [表情名] 替换为内联官方表情图。
    maps: 位置消息瓦片键 → dataURI（wx_maps 尽力下载），注入
    window.WX_MAPS 供渲染器在位置卡显示地图缩略图。
    """
    tpl = get_template(template)
    f.write("]};</script>\n")
    if faces:
        f.write(f"\n<script>window.WX_FACES = {_script_safe_json(faces)};</script>\n")
    if maps:
        f.write(f"\n<script>window.WX_MAPS = {_script_safe_json(maps)};</script>\n")
    f.write(f"\n<script>\nconst MSG_COUNT = {count};\n"
            f"const CHAT_TITLE = {_script_safe_json(title)};\n")
    f.write(tpl.renderer + "\n</script>\n</body>\n</html>")


def _script_safe_json(obj) -> str:
    """json.dumps 后转义会在 <script> 内提前终止/改变解析状态的序列：
    - `</`：消息含 `</script>` 会提前闭合 script 标签，导出的自包含
      网页从此损坏；
    - `<!--`：HTML 规范中 script 进入"双转义状态"，模板真正写出的
      </script> 闭标签会被吞掉——只转义 `</` 而不顾 `<!--` 等于没修。
    `\\/` 与 `\\u0021` 都是合法 JSON 转义，JSON.parse 结果与原文完全
    等价，正常数据零影响。"""
    return (json.dumps(obj, ensure_ascii=False)
            .replace("</", "<\\/")
            .replace("<!--", "<\\u0021--"))


def render_html(chat_data: dict, faces: dict | None = None,
                maps: dict | None = None, template=None) -> str:
    """生成自包含 HTML。data = CHAT_DATA dict。

    faces 为 name → dataURI；缺省时从 chat_data 自行扫描（非流式路径）。
    maps 为瓦片键 → dataURI；缺省时同样自行扫描并尽力下载。
    """
    if faces is None:
        from siwx import wx_faces
        used: set = set()
        for m in chat_data.get("messages", []):
            used |= wx_faces.used_from_message(m)
        faces = wx_faces.datauris(used)
    if maps is None:
        from siwx import wx_maps
        keys: set = set()
        for m in chat_data.get("messages", []):
            keys |= wx_maps.used_from_message(m)
        maps = wx_maps.datauris(keys)
    tpl = get_template(template)
    data_json = _script_safe_json(chat_data)
    title = chat_data["meta"]["sessionName"]
    count = chat_data["meta"]["messageCount"]

    faces_script = (f"\n<script>window.WX_FACES = {_script_safe_json(faces)};</script>\n"
                    if faces else "")
    maps_script = (f"\n<script>window.WX_MAPS = {_script_safe_json(maps)};</script>\n"
                   if maps else "")
    return tpl.head + f"\n<script>window.CHAT_DATA = {data_json};</script>\n" + \
        faces_script + maps_script + \
        f"\n<script>\nconst MSG_COUNT = {count};\nconst CHAT_TITLE = {_script_safe_json(title)};\n" + \
        tpl.renderer + "\n</script>\n</body>\n</html>"
