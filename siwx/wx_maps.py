"""位置消息（type 48）静态地图缩略图。

导出时解析位置消息 rawContent 的 <location x=纬 y=经> 坐标，按需下载
所在地图瓦片，以 base64 dataURI 注入 window.WX_MAPS，渲染器在位置卡上
显示缩略图（替换 📍 图标）。

数据源用高德无 key 瓦片服务：腾讯静态图 API 需要开发者 key（不可用），
且微信位置消息坐标是 GCJ-02，高德瓦片同为 GCJ-02，不会像 WGS-84 图源
（OpenStreetMap）那样偏移几百米。

下载是尽力而为：离线/超时/失败都回退到纯文字卡 + 跳转链接（原有行为），
首次失败后本进程内不再尝试（避免多会话导出时逐会话付超时），单次导出
新下载的瓦片数有上限，防止位置消息密集的会话拖慢导出。
"""
import base64
import math
import re
import time
import urllib.request

_ZOOM = 15
_TIMEOUT = 4
# 单次 datauris() 调用新下载的瓦片上限（去重后仍超出的沿用文字卡）
_MAX_NEW_FETCH = 24
_TILE_URL = ("https://wprd01.is.autonavi.com/appmaptile"
             "?lang=zh_cn&size=1&style=7&x={x}&y={y}&z={z}")
_UA = "Mozilla/5.0 (SIWX HTML export; location thumbnail)"

# (zoom, x, y) -> bytes | None（None = 下载失败，同样进缓存避免重试）
_CACHE: dict = {}
# 熔断：首次下载异常后暂停尝试，但**带时效**而不是永久——一次抖动（CDN 限流、
# 4s 超时）就把常驻 serve 进程后续所有导出的缩略图永久关掉，只有重启才恢复。
_disabled_until = 0.0
_DISABLE_COOLDOWN = 300.0     # 秒；到点放行一次探测


def _is_disabled(now: float) -> bool:
    return now < _disabled_until


def reset():
    """清空缓存与熔断标记（测试、长驻进程、新导出任务启动时用）。"""
    global _disabled_until
    _CACHE.clear()
    _disabled_until = 0.0


# 逐属性解析（与渲染器 xmlAttr 一致，不依赖 x/y 在 XML 里的先后顺序）
# x=纬度、y=经度（微信 XML 约定）
def _xml_attr(raw: str, tag: str, attr: str) -> str:
    m = re.search(r'<%s\b[^>]*?\s%s="([^"]*)"' % (tag, attr), raw)
    return m.group(1) if m else ""


def tile_key(lat: float, lng: float, zoom: int = _ZOOM) -> str:
    """经纬度 → slippy map 瓦片坐标键 "z/x/y"（渲染器 JS 同算法）。"""
    lat = max(-85.05112878, min(85.05112878, lat))
    lng = max(-180.0, min(180.0, lng))
    n = 2 ** zoom
    x = max(0, min(n - 1, int((lng + 180.0) / 360.0 * n)))
    rad = math.radians(lat)
    y = max(0, min(n - 1,
            int((1.0 - math.log(math.tan(rad) + 1.0 / math.cos(rad)) / math.pi)
                / 2.0 * n)))
    return f"{zoom}/{x}/{y}"


def used_from_message(msg: dict) -> set:
    """单条位置消息的瓦片键；非 48 / 无坐标 / 坐标非法返回空集。"""
    if msg.get("localType", msg.get("type")) != 48:
        return set()
    raw = msg.get("rawContent") or ""
    lat = _xml_attr(raw, "location", "x")
    lng = _xml_attr(raw, "location", "y")
    if not lat or not lng:
        return set()
    try:
        lat, lng = float(lat), float(lng)
    except ValueError:
        return set()
    if not (-85.1 <= lat <= 85.1 and -180.1 <= lng <= 180.1):
        return set()
    return {tile_key(lat, lng)}


def _fetch_tile(key: str) -> bytes | None:
    z, x, y = key.split("/")
    req = urllib.request.Request(
        _TILE_URL.format(x=x, y=y, z=z),
        headers={"User-Agent": _UA})
    with urllib.request.urlopen(req, timeout=_TIMEOUT) as r:
        data = r.read()
    return data if data and len(data) > 100 else None


def datauris(keys) -> dict:
    """瓦片键集合 → key → dataURI（尽力下载，失败静默跳过）。"""
    global _disabled_until
    out: dict = {}
    fetched = 0
    for key in sorted(keys):
        if key in _CACHE:
            data = _CACHE[key]
            if data:
                out[key] = "data:image/png;base64," + base64.b64encode(data).decode("ascii")
            continue
        # 熔断带时效：HTTPError（如限流 403）说明网络其实是通的，只暂停冷却期，
        # 到点后放行一次探测；命中就恢复，未命中就再进冷却。
        if fetched >= _MAX_NEW_FETCH or _is_disabled(time.time()):
            continue
        fetched += 1
        try:
            data = _fetch_tile(key)
        except Exception as e:
            data = None
            _disabled_until = time.time() + _DISABLE_COOLDOWN
            try:
                from siwx import logger
                logger.warn("export", f"地图缩略图下载失败，{int(_DISABLE_COOLDOWN)} 秒内不再尝试: {e}")
            except Exception:
                pass
        _CACHE[key] = data
        if data:
            out[key] = "data:image/png;base64," + base64.b64encode(data).decode("ascii")
    return out
