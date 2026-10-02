"""每插件配置存储 —— 原子写 + 每插件互斥锁 + schema 校验。

路径：%LOCALAPPDATA%\\stories-in-wx\\plugin_config\\<plugin>.json
（与 keystore.bin / mcp_config.json / media_key.json 同源，跨安装持久）

写入沿用项目既有的原子写模式（临时文件 + os.replace），并额外加每插件互斥锁，
避免并发 POST 设置时互相覆盖。读取失败（缺失/损坏）时回退 schema 默认值且**不落盘**
—— 配置文件懒创建。
"""
import json
import os
import threading
from pathlib import Path

_LOCKS: dict = {}
_LOCKS_GUARD = threading.Lock()


def config_root() -> Path:
    base = (os.environ.get("LOCALAPPDATA")
            or os.environ.get("USERPROFILE")
            or ".")
    return Path(base) / "stories-in-wx" / "plugin_config"


def config_path(plugin: str) -> Path:
    return config_root() / f"{_safe_plugin_name(plugin)}.json"


def _safe_plugin_name(name: str) -> str:
    """插件名只允许 [a-zA-Z0-9_.-]，防路径穿越。"""
    out = "".join(ch for ch in str(name or "")
                  if ch.isalnum() or ch in "_.-")
    return out or "_"


def _lock_for(plugin: str) -> threading.Lock:
    with _LOCKS_GUARD:
        lk = _LOCKS.get(plugin)
        if lk is None:
            lk = threading.Lock()
            _LOCKS[plugin] = lk
        return lk


# ── 校验 ────────────────────────────────────────────────────

def _coerce(field_spec: dict, value):
    """按 schema 把输入值转成正确类型；非法返回 None（调用方回退 default）。"""
    ftype = (field_spec.get("type") or "str").lower()
    try:
        if ftype == "bool":
            if isinstance(value, bool):
                return value
            if isinstance(value, str):
                return value.strip().lower() in ("1", "true", "yes", "on")
            return bool(value)
        if ftype == "int":
            iv = int(value)
            lo, hi = field_spec.get("min"), field_spec.get("max")
            if lo is not None and iv < lo:
                return None
            if hi is not None and iv > hi:
                return None
            return iv
        if ftype == "float":
            fv = float(value)
            lo, hi = field_spec.get("min"), field_spec.get("max")
            if lo is not None and fv < lo:
                return None
            if hi is not None and fv > hi:
                return None
            return fv
        if ftype == "choice":
            choices = field_spec.get("choices") or []
            if value in choices:
                return value
            return None
        # str / textarea / color 等一律转字符串
        return "" if value is None else str(value)
    except (TypeError, ValueError):
        return None


def defaults_from_schema(schema: list) -> dict:
    """从设置项声明拼出默认值字典。"""
    out = {}
    for item in schema or []:
        key = item.get("key")
        if key:
            out[key] = item.get("default")
    return out


def build_values(schema: list, stored: dict) -> dict:
    """schema 默认值 + 已存值（经类型校验），非法值回退默认。"""
    out = defaults_from_schema(schema)
    for item in schema or []:
        key = item.get("key")
        if not key or key not in (stored or {}):
            continue
        coerced = _coerce(item, stored[key])
        if coerced is not None:
            out[key] = coerced
    return out


# ── 读写 ────────────────────────────────────────────────────

def load_raw(plugin: str) -> dict:
    """读取原始配置文件；缺失/损坏返回 {}。"""
    p = config_path(plugin)
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def load(plugin: str, schema: list = None) -> dict:
    """读取并校验后的配置值。schema 为空时直接返回原始值。"""
    raw = load_raw(plugin)
    if schema:
        return build_values(schema, raw)
    return raw


def save(plugin: str, values: dict) -> None:
    """原子写入插件配置。"""
    p = config_path(plugin)
    with _lock_for(plugin):
        p.parent.mkdir(parents=True, exist_ok=True)
        tmp = p.with_suffix(".tmp")
        tmp.write_text(json.dumps(values, ensure_ascii=False, indent=1),
                       encoding="utf-8")
        os.replace(tmp, p)


def apply_update(plugin: str, schema: list, incoming: dict) -> dict:
    """按 schema 校验并合并一份更新（只接受 schema 里声明过的键）。

    返回写入后的完整配置值；非法字段回退默认并记录 warn。
    """
    from siwx import logger as log

    current = build_values(schema, load_raw(plugin))
    unknown = [k for k in (incoming or {}) if k not in current]
    if unknown:
        log.warn("plugin", f"{plugin} 设置忽略未声明字段: {', '.join(unknown[:5])}")

    by_key = {item.get("key"): item for item in (schema or []) if item.get("key")}
    changed = False
    for key, value in (incoming or {}).items():
        spec = by_key.get(key)
        if spec is None:
            continue
        coerced = _coerce(spec, value)
        if coerced is None:
            log.warn("plugin", f"{plugin}.{key} 取值非法，回退默认")
            coerced = spec.get("default")
        if current.get(key) != coerced:
            current[key] = coerced
            changed = True

    if changed:
        save(plugin, current)
    return current
