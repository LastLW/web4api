# -*- coding: utf-8 -*-
"""协议验收脚本 —— 对每个已配置通道实测 Anthropic 与 OpenAI 两套协议。

覆盖（每个通道 4 项）：
  Anthropic: 简单对话 / 工具调用
  OpenAI:    简单对话 / 工具调用

用法：
  python tests/check_protocols.py            # 测 qwen/doubao/mimo/deepseek（读各自 config.json）

注意：会真实调用上游（每通道 4 次请求，间隔 3 秒）。被限流的通道会如实标 ❌。
"""
import json
import glob
import os
import sys
import time

import httpx

BASE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.join(BASE, "..")
TIMEOUT = 180

TOOL = {
    "name": "write_file",
    "description": "Write text content to a file",
    "input_schema": {
        "type": "object",
        "properties": {"path": {"type": "string"}, "content": {"type": "string"}},
        "required": ["path", "content"],
    },
}
TOOL_OPENAI = {
    "type": "function",
    "function": {
        "name": "write_file",
        "description": "Write text content to a file",
        "parameters": {
            "type": "object",
            "properties": {"path": {"type": "string"}, "content": {"type": "string"}},
            "required": ["path", "content"],
        },
    },
}


def load_channels():
    """返回 [(名称, 端口, api_key, 默认模型, 跳过原因)]，未配置凭证的标记跳过。"""
    channels = []
    for name, default_model, token_mode in (
        ("qwen4api", "qwen3.6-flash", False),
        ("doubao4api", "doubao-web", False),
        ("mimo4api", "mimo-v2.5-pro", False),
        ("deepseek4api", "deepseek-v4-flash", True),
    ):
        cfg_path = os.path.join(ROOT, "services", name, "config.json")
        if not os.path.exists(cfg_path):
            channels.append((name, None, None, None, "未配置（先跑 setup_wizard.py）"))
            continue
        cfg = json.load(open(cfg_path, encoding="utf-8"))
        cred = cfg.get("token") if token_mode else cfg.get("cookie")
        if not cred:
            channels.append((name, cfg.get("port"), cfg.get("api_key"), default_model,
                             "token 未配置" if token_mode else "cookie 未配置"))
            continue
        channels.append((name, cfg.get("port"), cfg.get("api_key"), default_model, None))
    return channels


def anthropic_call(port, key, payload):
    r = httpx.post(
        f"http://127.0.0.1:{port}/v1/messages",
        headers={"x-api-key": key, "Content-Type": "application/json"},
        json=payload, timeout=TIMEOUT,
    )
    return r.status_code, r.json()


def openai_call(port, key, payload):
    r = httpx.post(
        f"http://127.0.0.1:{port}/v1/chat/completions",
        headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
        json=payload, timeout=TIMEOUT,
    )
    return r.status_code, r.json()


def test_channel(name, port, key, model):
    results = {}

    # A1 Anthropic 简单对话
    code, d = anthropic_call(port, key, {
        "model": model, "max_tokens": 50,
        "messages": [{"role": "user", "content": "Reply with exactly: ok"}]})
    txt = "".join(b.get("text", "") for b in d.get("content", []) if isinstance(b, dict))
    results["A-对话"] = ("✅" if code == 200 and txt.strip() else
                         f"❌ {d.get('error', {}).get('code', code)}")

    time.sleep(3)

    # A2 Anthropic 工具调用
    code, d = anthropic_call(port, key, {
        "model": model, "max_tokens": 400, "tools": [TOOL],
        "messages": [{"role": "user",
                      "content": "在 /tmp 创建 hello.py，内容 print('hi')，必须通过工具完成"}]})
    kinds = [b.get("type") for b in d.get("content", []) if isinstance(b, dict)]
    stop = d.get("stop_reason")
    if "tool_use" in kinds:
        results["A-工具"] = "✅"
    elif code == 200:
        results["A-工具"] = "⚠️ 模型未调用工具（文字回答）"
    else:
        results["A-工具"] = f"❌ {d.get('error', {}).get('code', code)}"

    time.sleep(3)

    # O1 OpenAI 简单对话
    code, d = openai_call(port, key, {
        "model": model, "max_tokens": 50,
        "messages": [{"role": "user", "content": "Reply with exactly: ok"}]})
    msg = (d.get("choices") or [{}])[0].get("message", {})
    results["O-对话"] = ("✅" if code == 200 and (msg.get("content") or "").strip()
                         else f"❌ {d.get('error', {}).get('code', code)}")

    time.sleep(3)

    # O2 OpenAI 工具调用
    code, d = openai_call(port, key, {
        "model": model, "max_tokens": 400, "tools": [TOOL_OPENAI],
        "messages": [{"role": "user",
                      "content": "在 /tmp 创建 hello.py，内容 print('hi')，必须通过工具完成"}]})
    m = (d.get("choices") or [{}])[0]
    if m.get("finish_reason") == "tool_calls" or m.get("message", {}).get("tool_calls"):
        results["O-工具"] = "✅"
    elif code == 200:
        results["O-工具"] = "⚠️ 模型未调用工具（文字回答）"
    else:
        results["O-工具"] = f"❌ {d.get('error', {}).get('code', code)}"

    return results


def main():
    channels = load_channels()

    matrix = {}
    for name, port, key, model, skip in channels:
        if skip:
            print(f"\n▶ {name}: ⏭ 跳过（{skip}）")
            matrix[name] = {k: "⏭" for k in ("A-对话", "A-工具", "O-对话", "O-工具")}
            continue
        print(f"\n▶ {name}（端口 {port}，模型 {model}）...")
        try:
            matrix[name] = test_channel(name, port, key, model)
        except Exception as e:  # noqa: BLE001
            matrix[name] = {k: f"❌ {e}" for k in ("A-对话", "A-工具", "O-对话", "O-工具")}

    print("\n" + "=" * 60)
    print(f"{'通道':<12}{'A-对话':<14}{'A-工具':<20}{'O-对话':<14}{'O-工具':<20}")
    print("-" * 60)
    for name, res in matrix.items():
        row = f"{name:<12}"
        for k in ("A-对话", "A-工具", "O-对话", "O-工具"):
            cell = res.get(k, "⏭")
            row += f"{cell:<14}" if len(cell) < 12 else f"{cell:<20}"
        print(row)
    print("=" * 60)
    print("图例: ✅通过  ⚠️可用但模型未走工具  ❌失败  ⏭跳过（未配置 Cookie）")


if __name__ == "__main__":
    main()
