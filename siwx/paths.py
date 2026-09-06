"""统一的应用路径（cwd 无关，打包后正确）。

- 源码运行：输出写到当前工作目录
- PyInstaller 打包：输出写到 exe 所在目录（双击 exe 时 cwd 是 System32，不能用）
"""
import sys
from pathlib import Path


def app_root() -> Path:
    """数据根目录：frozen → exe 所在目录；源码 → cwd。"""
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path.cwd()


def out_root() -> Path:
    return app_root() / "output"


def exports_root() -> Path:
    return app_root() / "exports"
