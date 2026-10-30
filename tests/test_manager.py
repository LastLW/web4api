# -*- coding: utf-8 -*-
"""统一管理台的纯函数测试：URL 解析、平台匹配、模型路由、Cookie 校验。

运行：python tests/test_manager.py
"""
import os
import sys

BASE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(BASE, ".."))
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from gateway import config  # noqa: E402


QWEN_URL = ("https://chat2.qianwen.com/api/v2/chat?biz_id=ai_qwen"
            "&ut=dev-123&nonce=abc&timestamp=123")
DOUBAO_URL = ("https://www.doubao.com/chat/completion?aid=497858"
              "&device_id=123456&fp=verify_abc&tea_uuid=999&web_tab_id=x")
MIMO_URL = ("https://aistudio.xiaomimimo.com/open-apis/bot/chat"
            "?xiaomichatbot_ph=ph_xyz")


def test_extract_fields():
    assert config.extract_capture_fields("qwen", QWEN_URL) == {"device_id": "dev-123"}
    assert config.extract_capture_fields("doubao", DOUBAO_URL) == {
        "device_id": "123456", "fp": "verify_abc", "tea_uuid": "999"}
    assert config.extract_capture_fields("mimo", MIMO_URL) == {
        "xiaomichatbot_ph": "ph_xyz"}
    print("✅ URL 参数提取")


def test_chat_url_matching():
    assert config.is_chat_url("qwen", QWEN_URL)
    assert config.is_chat_url("doubao", DOUBAO_URL)
    assert config.is_chat_url("mimo", MIMO_URL)
    assert not config.is_chat_url("qwen", "https://www.qianwen.com/")
    assert not config.is_chat_url("doubao", "https://www.doubao.com/chat/")
    assert not config.is_chat_url("mimo", "https://aistudio.xiaomimimo.com/")
    print("✅ 聊天 URL 匹配")


def test_validate_capture():
    ok, msg = config.validate_capture(
        "qwen", "tongyi_sso_ticket=abc; other=1", QWEN_URL)
    assert ok, msg
    ok, msg = config.validate_capture(
        "doubao", "sessionid=abc; " + "x" * 60, DOUBAO_URL)
    assert ok, msg
    ok, msg = config.validate_capture(
        "mimo", "serviceToken=abc; " + "x" * 40, MIMO_URL)
    assert ok, msg
    ok, _ = config.validate_capture("qwen", "foo=bar", QWEN_URL)
    assert not ok
    ok, _ = config.validate_capture("doubao", "sessionid=abc; " + "x" * 60,
                                    "https://www.doubao.com/chat/")
    assert not ok
    print("✅ 凭证校验")


def test_model_routing():
    cases = {
        "qwen3.7": "qwen",
        "Qwen3.8-Max": "qwen",
        "doubao-web-expert": "doubao",
        "mimo-v2.5-pro": "mimo",
        "deepseek-v4-pro": "deepseek",
        "deepseek-v4-flash-nothinking": "deepseek",
        "gpt-4o": "",
        "claude-sonnet-4": "",
    }
    for model, expected in cases.items():
        actual = config.model_to_service(model)
        assert actual == expected, f"{model}: {actual} != {expected}"
    print("✅ 模型路由")


def test_cookie_helpers():
    cookie = "a=1; b=2; c=3"
    assert config.cookie_names(cookie) == ["a", "b", "c"]
    preview = config.cookie_preview("x" * 100)
    assert "…" in preview and len(preview) < 100
    print("✅ Cookie 工具")


def test_oplog_roundtrip():
    """操作日志写入后 tail 能按序读回。"""
    import tempfile
    from pathlib import Path
    from gateway import oplog
    old = oplog.LOG_PATH
    oplog.LOG_PATH = Path(tempfile.mkdtemp()) / "ops.log"
    try:
        oplog.log("test.op", method="POST", status=200, foo=1)
        oplog.log("test.op2", method="POST", status=500)
        items = oplog.tail(10)
        assert items[-2]["action"] == "test.op" and items[-2]["foo"] == 1
        assert items[-1]["action"] == "test.op2" and items[-1]["status"] == 500
        assert len(oplog.tail(1)) == 1
    finally:
        oplog.LOG_PATH = old
    print("✅ 操作日志读写")


if __name__ == "__main__":
    test_extract_fields()
    test_chat_url_matching()
    test_validate_capture()
    test_model_routing()
    test_cookie_helpers()
    test_oplog_roundtrip()
    print("\n全部通过 ✅")
