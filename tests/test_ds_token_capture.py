# -*- coding: utf-8 -*-
"""parse_ls_token 自检：localStorage 里 userToken 的两种形态都能取出 JWT。

运行：python tests/test_ds_token_capture.py
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from gateway.browser_capture import parse_ls_token

jwt = "eyJ" + "a" * 120 + "." + "b" * 40 + "." + "c" * 40

cases = [
    # JSON 形态（chat.deepseek.com 当前格式）
    ('{"value": "%s", "expire": "2099-01-01"}' % jwt, jwt),
    # 纯字符串形态（兼容旧版）
    (jwt, jwt),
    # 带引号包裹
    ('"%s"' % jwt, jwt),
    # 未登录 / 空值
    ("", ""),
    (None, ""),
    # 非 JWT 内容不算
    ("hello world this is not a token at all......", ""),
    ('{"other": 1}', ""),
    ('"short"', ""),
]

for raw, expect in cases:
    got = parse_ls_token(raw)
    assert got == expect, f"parse_ls_token({raw[:40]!r}) = {got[:40]!r}, expect {expect[:40]!r}"

# DeepSeek 2026-10 起的 64 位不透明 token（base64 字符集，含 /）
opaque = "3tW0n18RCnI1gfDO9xKmQzLpVbNyHsJuTcWeAfGhRiEkXlMoPqZaSdCvBgTnUm/r"
assert parse_ls_token('{"value": "%s", "__version": "0"}' % opaque) == opaque
assert parse_ls_token(opaque) == opaque

from gateway import config
assert config.SERVICE_SPECS["deepseek"]["capture"]["ls_key"] == "userToken"
assert config.SERVICE_SPECS["deepseek"]["capture"]["mode"] == "localstorage"
print("parse_ls_token OK")
