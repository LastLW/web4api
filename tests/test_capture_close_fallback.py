# -*- coding: utf-8 -*-
"""close() 回退逻辑自检：请求的平台没有活跃窗口时，应关闭实际打开的那个登录浏览器。

运行：python tests/test_capture_close_fallback.py
"""
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from gateway.browser_capture import CaptureManager, CaptureSession


async def main():
    mgr = CaptureManager()

    # 什么都没开时 close 任意平台：不报错、无事发生
    res = await mgr.close("qwen")
    assert res == {"active": False}, res

    # qwen 的浏览器开着（state=waiting_login 视为活跃），但用户在 doubao 弹窗点"关闭浏览器"
    a = CaptureSession("qwen")
    a.state = "waiting_login"
    mgr.sessions["qwen"] = a
    res = await mgr.close("doubao")
    assert res["provider"] == "qwen" and res["state"] == "closed", res
    assert mgr.status()["active"] is False, mgr.status()

    print("close fallback OK")


asyncio.run(main())
