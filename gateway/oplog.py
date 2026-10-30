# -*- coding: utf-8 -*-
"""管理台操作日志：所有 /api 下的 POST/PATCH/DELETE 操作都追加到 JSONL 文件。

文件位于 gateway/.cache/ops.log（已被 .gitignore 排除）。
"""
from __future__ import annotations

import json
import threading

from . import config

LOG_PATH = config.GATEWAY_DIR / ".cache" / "ops.log"
_lock = threading.Lock()


def log(action: str, **extra):
    entry = {"time": config.now_iso(), "action": action, **extra}
    try:
        LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
        with _lock, open(LOG_PATH, "a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")
    except Exception:
        pass  # 日志失败不能影响主流程


def tail(lines: int = 200) -> list[dict]:
    try:
        with open(LOG_PATH, encoding="utf-8") as f:
            rows = f.readlines()
    except FileNotFoundError:
        return []
    out = []
    for row in rows[-max(1, min(2000, int(lines))):]:
        try:
            item = json.loads(row)
        except Exception:
            continue
        if isinstance(item, dict):
            out.append(item)
    return out
