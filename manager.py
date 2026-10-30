# -*- coding: utf-8 -*-
"""web4api 统一管理台启动器。

用法：
    python manager.py
    python manager.py --port 8400 --no-open

启动后浏览器打开 http://127.0.0.1:8400/。
"""
from __future__ import annotations

import sys

from gateway.app import main

if __name__ == "__main__":
    main(sys.argv[1:])
