"""设置 API —— 缓存总览、自动同步与清除（解密输出 / 密钥库）。"""
import json
import time
import shutil
from pathlib import Path

from flask import Blueprint, jsonify, request

from siwx import keystore

bp = Blueprint("settings_api", __name__, url_prefix="/api/settings")


from siwx import paths as _paths


def _out_root() -> Path:
    return _paths.out_root()


_DEFAULT_AUTO_SYNC = {
    "enabled": False,
    "interval_minutes": 30,
    "last_run": 0,
    "last_ok": None,
    "last_message": "未运行",
}


def _auto_sync_file() -> Path:
    return _paths.app_root() / "auto_sync.json"


def load_auto_sync() -> dict:
    cfg = dict(_DEFAULT_AUTO_SYNC)
    try:
        data = json.loads(_auto_sync_file().read_text(encoding="utf-8"))
        if isinstance(data, dict):
            cfg.update(data)
    except Exception:
        pass
    try:
        cfg["interval_minutes"] = max(1, min(int(cfg.get("interval_minutes") or 30), 1440))
    except (TypeError, ValueError):
        cfg["interval_minutes"] = 30
    cfg["enabled"] = bool(cfg.get("enabled"))
    return cfg


def save_auto_sync(cfg: dict) -> dict:
    current = load_auto_sync()
    current.update({k: v for k, v in (cfg or {}).items() if k in _DEFAULT_AUTO_SYNC})
    current["enabled"] = bool(current.get("enabled"))
    try:
        current["interval_minutes"] = max(1, min(int(current.get("interval_minutes") or 30), 1440))
    except (TypeError, ValueError):
        current["interval_minutes"] = 30
    p = _auto_sync_file()
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".tmp")
    tmp.write_text(json.dumps(current, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(p)
    return current


def mark_auto_sync_result(ok: bool, message: str) -> dict:
    cfg = load_auto_sync()
    cfg.update({"last_run": int(time.time()), "last_ok": bool(ok), "last_message": str(message or "")})
    return save_auto_sync(cfg)


def _dir_size(p: Path) -> int:
    total = 0
    for f in p.rglob("*"):
        try:
            if f.is_file():
                total += f.stat().st_size
        except OSError:
            pass
    return total


@bp.get("/overview")
def overview():
    outputs = []
    root = _out_root()
    if root.is_dir():
        for d in sorted(root.iterdir()):
            if d.is_dir():
                manifest = d / ".siwx_cache.json"
                outputs.append({
                    "wxid": d.name,
                    "size_mb": round(_dir_size(d) / 1048576, 1),
                    "manifest": manifest.is_file(),
                })
    return jsonify({
        "keystore": {"count": len(keystore.load()), "path": str(keystore.store_path())},
        "outputs": outputs,
        "output_root": str(root),
        "auto_sync": load_auto_sync(),
    })


@bp.get("/auto-sync")
def auto_sync_get():
    return jsonify(load_auto_sync())


@bp.post("/auto-sync")
def auto_sync_set():
    data = request.get_json(silent=True) or {}
    cfg = save_auto_sync({
        "enabled": data.get("enabled", False),
        "interval_minutes": data.get("interval_minutes", 30),
    })
    return jsonify(cfg)


@bp.post("/clear")
def clear():
    data = request.get_json(silent=True) or {}
    kind = data.get("kind", "")
    wxid = data.get("wxid")
    removed = []
    if kind == "output":
        root = _out_root()
        targets = ([root / wxid] if wxid
                   else [d for d in root.iterdir() if d.is_dir()]) if root.is_dir() else []
        for t in targets:
            if t.exists():
                shutil.rmtree(t, ignore_errors=True)
                removed.append(t.name)
    elif kind == "keys":
        p = keystore.store_path()
        if p.is_file():
            p.unlink()
            removed.append("keystore.bin")
    else:
        return jsonify({"error": "未知 kind（output / keys）"}), 400
    return jsonify({"removed": removed})
