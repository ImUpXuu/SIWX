"""统一的应用路径 —— 与 cwd 完全解耦。

- PyInstaller 打包：exe 所在目录
- 源码运行：项目根目录（siwx/ 的上一级），从 __file__ 推导
"""
import sys
from pathlib import Path


def app_root() -> Path:
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent.parent


def out_root() -> Path:
    return app_root() / "output"


def exports_root() -> Path:
    return app_root() / "exports"
