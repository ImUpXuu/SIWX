#!/usr/bin/env python3
"""stories-in-wx — 微信 4.x 密钥提取与解密 (Python 自研重写)。

全自动流水线：目录扫描 → 密钥提取 → DPAPI 加密保存 → 数据库解密。
用法：
    python run.py auto                     # 全自动（推荐）
    python run.py keys extract [--db-dir X] [--json]
    python run.py keys list
    python run.py decrypt [--db-dir X] [--out DIR]
    python run.py serve [--port 8787]
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from siwx.cli import main

if __name__ == "__main__":
    sys.exit(main())
