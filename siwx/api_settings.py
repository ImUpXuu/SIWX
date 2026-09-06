"""设置 API —— 缓存总览与清除（解密输出 / 密钥库）。"""
import shutil
from pathlib import Path

from flask import Blueprint, jsonify, request

from siwx import keystore

bp = Blueprint("settings_api", __name__, url_prefix="/api/settings")


from siwx import paths as _paths


def _out_root() -> Path:
    return _paths.out_root()


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
    })


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
