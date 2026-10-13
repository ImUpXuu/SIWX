"""微信小黄脸（内置表情）权威名称表与素材加载。

素材为微信官方表情图（来源 npm wechat-emoji-parser 2.3.1 内嵌的
官方 PNG，非 unicode emoji），共 146 个名称；其中 [再见]/[抱拳]
官方各有新旧两版图，此处取聊天文本里经典名字对应的历史版本。

导出时 exporter 扫描消息文本收集实际用到的名称，html_template 把
用到的图以 base64 dataURI 注入 window.WX_FACES，渲染器把 [表情名]
替换为内联图片——导出文件保持自包含、离线可看。
"""
import base64
import functools
import re
from pathlib import Path

_ASSETS = Path(__file__).parent / "assets" / "emoji"

# 名称顺序与 assets/emoji/fNNN.png 一一对应（f000.png = 第 0 个名称）
NAMES: tuple = (
    '[微笑]', '[撇嘴]', '[色]', '[发呆]', '[得意]', '[流泪]', '[害羞]', '[闭嘴]',
    '[睡]', '[大哭]', '[尴尬]', '[发怒]', '[调皮]', '[呲牙]', '[惊讶]', '[难过]',
    '[酷]', '[囧]', '[抓狂]', '[吐]', '[偷笑]', '[愉快]', '[白眼]', '[傲慢]',
    '[饥饿]', '[困]', '[惊恐]', '[流汗]', '[憨笑]', '[悠闲]', '[奋斗]', '[咒骂]',
    '[疑问]', '[嘘]', '[晕]', '[疯了]', '[衰]', '[骷髅]', '[敲打]', '[再见]',
    '[擦汗]', '[抠鼻]', '[鼓掌]', '[糗大了]', '[坏笑]', '[左哼哼]', '[右哼哼]', '[哈欠]',
    '[鄙视]', '[委屈]', '[快哭了]', '[阴险]', '[亲亲]', '[吓]', '[可怜]', '[菜刀]',
    '[西瓜]', '[啤酒]', '[篮球]', '[乒乓]', '[咖啡]', '[饭]', '[猪头]', '[玫瑰]',
    '[凋谢]', '[嘴唇]', '[爱心]', '[心碎]', '[蛋糕]', '[闪电]', '[炸弹]', '[刀]',
    '[足球]', '[瓢虫]', '[便便]', '[月亮]', '[太阳]', '[礼物]', '[拥抱]', '[强]',
    '[弱]', '[握手]', '[胜利]', '[抱拳]', '[勾引]', '[拳头]', '[差劲]', '[爱你]',
    '[NO]', '[OK]', '[爱情]', '[飞吻]', '[跳跳]', '[发抖]', '[怄火]', '[转圈]',
    '[磕头]', '[回头]', '[跳绳]', '[投降]', '[激动]', '[乱舞]', '[献吻]', '[左太极]',
    '[右太极]', '[奸笑]', '[嘿哈]', '[捂脸]', '[机智]', '[茶]', '[红包]', '[蜡烛]',
    '[耶]', '[皱眉]', '[鸡]', '[福]', '[發]', '[小狗]', '[吃瓜]', '[加油]',
    '[汗]', '[天啊]', '[Emm]', '[社会社会]', '[旺柴]', '[好的]', '[打脸]', '[哇]',
    '[加油加油]', '[翻白眼]', '[666]', '[让我看看]', '[叹气]', '[苦涩]', '[裂开]', '[脸红]',
    '[笑脸]', '[破涕为笑]', '[烟花]', '[爆竹]', '[庆祝]', '[恐惧]', '[无语]', '[失望]',
    '[生病]', '[合十]',
)

_CACHE: dict = {}


@functools.lru_cache(maxsize=1)
def load_faces() -> dict:
    """name -> base64 编码的 PNG（不带 dataURI 前缀），进程内缓存。"""
    if not _CACHE:
        for i, name in enumerate(NAMES):
            p = _ASSETS / f"f{i:03d}.png"
            if p.exists():
                _CACHE[name] = base64.b64encode(p.read_bytes()).decode("ascii")
    return _CACHE


@functools.lru_cache(maxsize=1)
def _face_re() -> "re.Pattern":
    # 名称本身不含 [ ]，整体按转义后的字面量交替
    alts = "|".join(re.escape(n) for n in NAMES)
    return re.compile("(" + alts + ")")


def find_used(text: str) -> set:
    """从一段文本里找出实际出现的表情名（与 NAMES 一致，带方括号）。"""
    if not text:
        return set()
    return set(_face_re().findall(text))


def used_from_message(msg: dict) -> set:
    """单条消息里用到的表情名：正文 + 引用块内容（渲染器对这两处做替换）。"""
    used = find_used(msg.get("content") or "")
    q = msg.get("quote") or {}
    if isinstance(q, dict) and q.get("content"):
        used |= find_used(q["content"])
    return used


def datauris(names) -> dict:
    """name 集合 → name -> dataURI（仅保留素材表里有的名称）。"""
    faces = load_faces()
    return {n: "data:image/png;base64," + faces[n]
            for n in sorted(names) if n in faces}


def face_file(name: str):
    """聊天查看页用：表情名（不带方括号）→ 对应 PNG 路径。

    名字先查权威表定文件序号，不拼接用户输入路径；表内无此名或
    素材缺失返回 None。"""
    key = f"[{name}]"
    if key not in NAMES:
        return None
    p = _ASSETS / f"f{NAMES.index(key):03d}.png"
    return p if p.is_file() else None
