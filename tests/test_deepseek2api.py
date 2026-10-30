# -*- coding: utf-8 -*-
"""deepseek2api 自检：DeepSeekHashV1 已知向量、PoW 求解、SSE 解析、模型解析。

运行：python tests/test_deepseek2api.py
"""
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SERVICE_DIR = ROOT / "services" / "deepseek2api"
sys.path.insert(0, str(SERVICE_DIR))

# 模块导入时读取 config.json；不存在则临时生成（测试结束后删除）
cfg_path = SERVICE_DIR / "config.json"
created_cfg = not cfg_path.exists()
if created_cfg:
    cfg_path.write_text(json.dumps(
        {"api_key": "sk-test", "admin_key": "test-admin", "port": 8304}), encoding="utf-8")

import deepseek2api as d  # noqa: E402


def test_hash():
    # 向量来自 ds2api 直接调用 DeepSeek 官方 WASM 的测试
    vecs = [
        (b"", "e594808bc5b7151ac160c6d39a02e0a8e261ed588578403099e3561dc40c26b3"),
        (b"testsalt_1700000000_42",
         "d4a2ea58c89e40887c933484868380c6f803eaa8dc53a3b9df8e431b921a4f09"),
        (b"testsalt_1700000000_100000",
         "abea2f35796b65486e9be1b36f7878c66cab021e96faa473fdf4decd31f9ba30"),
        (b"abc123salt_1700000000_12345",
         "74b3b7452745b70e85eb32ee7f0a9ec0381d42dd5137b695da915e104fc390e1"),
    ]
    for data, want in vecs:
        got = d.deepseek_hash_v1(data).hex()
        assert got == want, f"hash({data!r}) = {got}, want {want}"
    print("hash vectors OK")


def test_solve_inline():
    # 小难度走单进程路径
    answer = 500
    h = d.deepseek_hash_v1(f"testsalt_1700000000_{answer}".encode())
    got = d.solve_pow(h.hex(), "testsalt", 1700000000, 2000)
    assert got == answer, f"solve got {got}, want {answer}"

    answer2 = 12345
    h2 = d.deepseek_hash_v1(f"abc123salt_1700000000_{answer2}".encode())
    got2 = d.solve_pow(h2.hex(), "abc123salt", 1700000000, 20000)
    assert got2 == answer2, f"solve got {got2}, want {answer2}"
    print("solve_pow (inline) OK")


def test_solve_pool():
    # 大难度走进程池路径（覆盖多进程分块逻辑）
    answer = 9000
    h = d.deepseek_hash_v1(f"poolsalt_1700000000_{answer}".encode())
    got = d.solve_pow(h.hex(), "poolsalt", 1700000000, 30000)
    assert got == answer, f"solve got {got}, want {answer}"
    print("solve_pow (pool) OK")


def test_pow_header():
    challenge = {"algorithm": "DeepSeekHashV1", "challenge": "ab" * 32, "salt": "s",
                 "expire_at": 123, "difficulty": 10, "signature": "sig",
                 "target_path": "/api/v0/chat/completion"}
    header = d.build_pow_header(challenge, 42)
    import base64
    m = json.loads(base64.b64decode(header))
    assert m["answer"] == 42 and m["algorithm"] == "DeepSeekHashV1" and m["target_path"] == "/api/v0/chat/completion"
    print("pow header OK")


def feed(parser, lines):
    for line in lines:
        parser.feed_line(line)


def test_parser_basic():
    p = d.DeepSeekStreamParser()
    feed(p, [
        'data: {"v":{"response":{"message_id":2,"status":"WIP","thinking_enabled":true,'
        '"fragments":[{"id":2,"type":"THINK","content":""}]}}}',
        'data: {"p":"response/fragments/-1/content","o":"APPEND","v":"思考中"}',
        'data: {"p":"response/fragments","o":"APPEND","v":[{"id":3,"type":"RESPONSE","content":""}]}',
        'data: {"v":"你好"}',
        'data: {"v":"，世界"}',
        'data: {"p":"response","o":"BATCH","v":[{"p":"accumulated_token_usage","v":211},{"p":"quasi_status","v":"FINISHED"}]}',
        'data: {"p":"response/status","o":"SET","v":"FINISHED"}',
        'data: [DONE]',
    ])
    text, think, status = p.result()
    assert text == "你好，世界", text
    assert think == "思考中", think
    assert status == "FINISHED" and p.message_id == 2
    assert p.finished() and not p.should_continue()
    print("parser basic OK")


def test_parser_content_filter():
    p = d.DeepSeekStreamParser()
    feed(p, [
        'data: {"v":{"response":{"message_id":5,"status":"WIP","fragments":[]}}}',
        'data: {"v":[{"p":"ban_regenerate","v":true},{"p":"status","v":"CONTENT_FILTER"},'
        '{"p":"fragments","v":[{"id":9,"type":"TEMPLATE_RESPONSE","content":"无法回答"}]}]}',
        'data: [DONE]',
    ])
    text, _, status = p.result()
    assert text == "无法回答" and status == "CONTENT_FILTER" and p.finished()
    print("parser content_filter OK")


def test_parser_snapshot_dedup():
    # continue 接续轮快照重放：相同 fragment id 覆盖，不重复拼接
    p = d.DeepSeekStreamParser()
    feed(p, [
        'data: {"v":{"response":{"message_id":7,"status":"WIP",'
        '"fragments":[{"id":1,"type":"RESPONSE","content":"第一段"}]}}}',
        'data: {"p":"response/fragments/-1/content","o":"APPEND","v":"续"}',
    ])
    assert p.result()[0] == "第一段续"
    p.begin_round()  # continue 接续轮开始（与 deepseek_chat 主循环一致）
    feed(p, [
        # 第二轮（continue）：快照重放同 id 完整内容
        'data: {"v":{"response":{"message_id":7,"status":"INCOMPLETE",'
        '"fragments":[{"id":1,"type":"RESPONSE","content":"第一段续"}]}}}',
        'data: {"p":"response/fragments/-1/content","o":"APPEND","v":"更多"}',
    ])
    text, _, status = p.result()
    assert text == "第一段续更多", text
    assert p.should_continue() and p.message_id == 7
    print("parser snapshot dedup OK")


def test_resolve_model():
    assert d.resolve_model("deepseek-v4-flash") == ("default", True, False)
    assert d.resolve_model("deepseek-v4-pro") == ("expert", True, False)
    assert d.resolve_model("deepseek-v4-pro-nothinking") == ("expert", False, False)
    assert d.resolve_model("deepseek-v4-flash-search-nothinking") == ("default", False, True)
    assert d.resolve_model("deepseek-v4-vision")[0] == "vision"
    assert d.resolve_model("unknown-model") == ("default", True, False)
    print("resolve_model OK")


if __name__ == "__main__":
    try:
        test_hash()
        test_solve_inline()
        test_solve_pool()
        test_pow_header()
        test_parser_basic()
        test_parser_content_filter()
        test_parser_snapshot_dedup()
        test_resolve_model()
        print("ALL OK")
    finally:
        if created_cfg:
            cfg_path.unlink(missing_ok=True)
