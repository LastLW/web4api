# -*- coding: utf-8 -*-
"""解析器单元测试：用真实抓包样本（tests/fixtures/）回归验证三个服务的上游解析逻辑。

运行：python tests/test_parsers.py
上游改版导致解析失效时，这里会第一时间红。
"""
import json
import os
import sys

BASE = os.path.dirname(os.path.abspath(__file__))
SERVICES = os.path.join(BASE, "..", "services")
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
for sub in ("qwen2api", "doubao2api", "mimo2api"):
    sys.path.insert(0, os.path.join(SERVICES, sub))

FIXTURES = os.path.join(BASE, "fixtures")


def load(name):
    with open(os.path.join(FIXTURES, name), encoding="utf-8") as f:
        return f.read().split("\n")


def test_qwen():
    import qwen2api
    lines = load("qwen_stream_sample.txt")
    text, thinking, error = qwen2api.parse_qwen_stream_lines(lines)
    assert not error, f"不应有错误: {error}"
    assert "ping" in text, f"正文应包含 ping，实际: {text[:100]!r}"
    print(f"✅ qwen 解析器: {text[:50]!r}")


def test_qwen_expert_thinking():
    import qwen2api
    lines = load("qwen_expert_stream.txt")
    text, thinking, error = qwen2api.parse_qwen_stream_lines(lines)
    assert not error, f"不应有错误: {error}"
    # 思考研究模式：bar_thinking 步骤应被解析为思考文本
    assert "bar_thinking" not in thinking or "【" in thinking
    print(f"✅ qwen expert 思考解析: thinking={len(thinking)} chars, 正文={text[:60]!r}")


def test_doubao():
    import doubao2api
    lines = load("doubao_stream_sample.txt")
    text, finished = doubao2api.parse_doubao_stream_lines(lines)
    assert finished, "流应正常结束"
    expected = "我是豆包，由字节跳动独立研发的人工智能。\n我可以陪你聊天、解答疑问，也能帮你构思文案、整理思路。"
    assert text == expected, f"正文不符:\n  实际: {text!r}\n  期望: {expected!r}"
    print(f"✅ doubao 解析器: {text[:50]!r}")


def test_mimo():
    import mimo2api
    lines = load("mimo_stream_sample.txt")
    full = mimo2api.parse_mimo_stream_lines(lines)
    thinking, answer = mimo2api.split_thinking(full)
    assert thinking.strip() == 'The user asks "1+1=?" and wants only the number as an answer.', \
        f"思考内容不符: {thinking!r}"
    assert answer == "2", f"正文应为 2，实际: {answer!r}"
    print(f"✅ mimo 解析器: thinking={len(thinking)} chars, answer={answer!r}")


def test_doubao_stream_msg_notify():
    """新版上游把首段正文放在 STREAM_MSG_NOTIFY 的 content.content_block 里。"""
    import doubao2api
    lines = [
        "event: SSE_ACK",
        'data: {"ack":1}',
        "event: STREAM_MSG_NOTIFY",
        'data: {"content":{"content_block":[{"block_type":10000,'
        '"content":{"text_block":{"text":"ok"}},"is_finish":false}]}}',
        "event: STREAM_CHUNK",
        'data: {"message_id":"x","patch_op":[{"patch_object":1,"patch_value":'
        '{"content_block":[{"block_type":10000,"content":{"text_block":{}},'
        '"is_finish":true}]}}]}',
        "event: SSE_REPLY_END",
        'data: {"end_type":1}',
    ]
    text, finished = doubao2api.parse_doubao_stream_lines(lines)
    assert text == "ok" and finished, (text, finished)
    print(f"✅ doubao STREAM_MSG_NOTIFY 解析: {text!r}")


def test_doubao_captured_url_sanitized():
    """抓包 URL 里的 msToken / a_bogus 是一次性签名，复用会触发上游风控，必须丢弃。"""
    import doubao2api
    from urllib.parse import parse_qs
    saved = (doubao2api.CHAT_URL, doubao2api.DEVICE_ID, doubao2api.FP, doubao2api.TEA_UUID)
    try:
        doubao2api.DEVICE_ID, doubao2api.FP, doubao2api.TEA_UUID = "dev", "fp", "tea"
        doubao2api.CHAT_URL = ("https://www.doubao.com/chat/completion?aid=497858"
                               "&device_id=dev&fp=fp&tea_uuid=tea&msToken=stale"
                               "&a_bogus=stale&web_tab_id=old")
        query = parse_qs(doubao2api.build_query())
        assert "msToken" not in query and "a_bogus" not in query, query
        assert query["device_id"] == ["dev"], query
    finally:
        (doubao2api.CHAT_URL, doubao2api.DEVICE_ID, doubao2api.FP, doubao2api.TEA_UUID) = saved
    print("✅ doubao 抓包 URL 丢弃一次性签名参数")


def test_tool_call_parsing():
    import importlib
    for mod_name in ("qwen2api", "doubao2api", "mimo2api"):
        mod = importlib.import_module(mod_name)
        text = '前置说明\n<tool_call>{"name": "write_file", "arguments": {"path": "/a", "content": "hi"}}</tool_call>'
        clean, calls = mod.parse_tool_calls(text)
        assert clean == "前置说明", f"{mod_name}: {clean!r}"
        assert len(calls) == 1 and calls[0]["name"] == "write_file" \
            and calls[0]["arguments"]["path"] == "/a", f"{mod_name}: {calls}"
    print("✅ 三服务 tool_call 解析一致")


if __name__ == "__main__":
    test_qwen()
    test_qwen_expert_thinking()
    test_doubao()
    test_doubao_stream_msg_notify()
    test_doubao_captured_url_sanitized()
    test_mimo()
    test_tool_call_parsing()
    print("\n全部通过 ✅")
