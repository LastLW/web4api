# -*- coding: utf-8 -*-
"""统一 API 入口：一个 Base URL + 一把 Key，按模型名自动路由到各通道。

- /v1/models             聚合各通道的模型列表
- /v1/chat/completions   OpenAI 协议
- /v1/messages           Anthropic 协议（Claude Code）
- /v1/responses          OpenAI Responses（Codex）
"""
from __future__ import annotations

import asyncio
import json

import httpx
from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, StreamingResponse

from . import config

router = APIRouter()

# 长连接：网页版通道一轮可能 5~20 秒，读超时交给上游
_client = httpx.AsyncClient(
    timeout=httpx.Timeout(connect=15.0, read=None, write=None, pool=None),
    follow_redirects=True,
)

_FORWARD_HEADERS = (
    "accept",
    "content-type",
    "anthropic-version",
    "anthropic-beta",
    "openai-beta",
    "user-agent",
)
_EXCLUDE_RESPONSE_HEADERS = {
    "content-length",
    "transfer-encoding",
    "connection",
    "keep-alive",
    "proxy-authenticate",
    "proxy-authorization",
    "te",
    "trailer",
    "upgrade",
}


def _request_key(request: Request) -> str:
    key = request.headers.get("x-api-key") or ""
    if not key:
        auth = request.headers.get("authorization") or ""
        if auth.lower().startswith("bearer "):
            key = auth[7:].strip()
    return key


def _allowed_keys() -> set[str]:
    keys = {config.load_manager_config().get("unified_key", "")}
    for name in config.PY_SERVICES:
        cfg = config.load_service_config(name, create=False)
        if cfg.get("api_key"):
            keys.add(cfg["api_key"])
    return {k for k in keys if k}


def _unauthorized():
    return JSONResponse(
        {"error": {"message": "invalid api key", "type": "authentication_error",
                   "code": "invalid_api_key"}},
        status_code=401,
    )


def _error(message: str, status: int = 503, code: str = "upstream_unavailable"):
    return JSONResponse(
        {"error": {"message": message, "type": "api_error", "code": code}},
        status_code=status,
    )


def _check_auth(request: Request):
    if _request_key(request) not in _allowed_keys():
        return _unauthorized()
    return None


def _target(name: str) -> tuple[str, str]:
    """返回 (base_url, api_key)。"""
    cfg = config.load_service_config(name)
    key = cfg.get("api_key", "")
    if not key:
        raise RuntimeError(f"{config.SERVICE_SPECS[name]['label']} 还没有 API Key")
    return f"http://127.0.0.1:{config.service_port(name, cfg)}", key


def _upstream_headers(request: Request, key: str) -> dict:
    headers = {}
    for name in _FORWARD_HEADERS:
        value = request.headers.get(name)
        if value:
            headers[name] = value
    headers["x-api-key"] = key
    headers["authorization"] = f"Bearer {key}"
    return headers


async def _forward(request: Request, target: str, path: str, body: bytes | None = None):
    base, key = _target(target)
    url = base.rstrip("/") + path
    headers = _upstream_headers(request, key)
    try:
        upstream_request = _client.build_request(
            request.method, url, headers=headers, content=body)
        upstream = await _client.send(upstream_request, stream=True)
    except httpx.ConnectError:
        label = config.SERVICE_SPECS[target]["label"]
        return _error(f"{label} 服务未启动（{base}），请先在管理台启动。")
    except Exception as exc:  # noqa: BLE001
        return _error(f"转发到上游失败：{exc}", 502)

    response_headers = {
        name: value for name, value in upstream.headers.items()
        if name.lower() not in _EXCLUDE_RESPONSE_HEADERS
    }
    response_headers["x-web2api-upstream"] = target

    async def gen():
        try:
            async for chunk in upstream.aiter_raw():
                yield chunk
        finally:
            await upstream.aclose()

    return StreamingResponse(
        gen(),
        status_code=upstream.status_code,
        headers=response_headers,
        media_type=upstream.headers.get("content-type"),
    )


# ---------------------------------------------------------------------------
# 模型列表
# ---------------------------------------------------------------------------


async def _fetch_models(name: str) -> list[dict]:
    try:
        base, key = _target(name)
    except Exception:
        return []
    try:
        async with httpx.AsyncClient(timeout=3.0) as client:
            resp = await client.get(
                base + "/v1/models",
                headers={"x-api-key": key, "authorization": f"Bearer {key}"},
            )
        if resp.status_code != 200:
            return []
        data = resp.json().get("data") or []
        return [item for item in data if isinstance(item, dict) and item.get("id")]
    except Exception:
        return []


@router.get("/v1/models")
async def list_models(request: Request):
    denied = _check_auth(request)
    if denied:
        return denied

    results = await asyncio.gather(*[_fetch_models(name) for name in config.PY_SERVICES])
    available: dict[str, dict] = {}
    for items in results:
        for item in items:
            available[item["id"]] = item

    data = []
    for name in config.PY_SERVICES:
        for model in config.service_models(name):
            if model in available:
                item = dict(available[model])
                item["x_available"] = True
            else:
                item = {"id": model, "object": "model", "owned_by": f"{name}-web",
                        "x_available": False}
            data.append(item)
    # 上游动态返回的新模型也带上
    for model, item in available.items():
        if not any(row["id"] == model for row in data):
            item = dict(item)
            item["x_available"] = True
            data.append(item)
    return {"object": "list", "data": data}


@router.get("/v1/models/{model:path}")
async def model_detail(model: str, request: Request):
    denied = _check_auth(request)
    if denied:
        return denied
    target = config.model_to_service(model)
    return {"id": model, "object": "model",
            "owned_by": f"{target or 'unknown'}-web", "x_available": bool(target)}


# ---------------------------------------------------------------------------
# 对话 / 消息 / Responses
# ---------------------------------------------------------------------------


async def _body_and_model(request: Request):
    raw = await request.body()
    try:
        body = json.loads(raw) if raw else {}
    except Exception:
        return raw, None
    return raw, body.get("model") if isinstance(body, dict) else None


async def _route_conversation(request: Request):
    denied = _check_auth(request)
    if denied:
        return denied
    raw, model = await _body_and_model(request)
    if not model:
        return _error("请求缺少 model 字段", 400, "invalid_request")
    target = config.model_to_service(model)
    if not target:
        available = [m for name in config.PY_SERVICES for m in config.service_models(name)]
        return _error(f"未知模型：{model}。可用模型：{', '.join(available)}", 404, "model_not_found")
    path = request.url.path
    if path.startswith("/anthropic/"):
        path = path[len("/anthropic"):]
    return await _forward(request, target, path, raw)


@router.post("/v1/chat/completions")
async def chat_completions(request: Request):
    return await _route_conversation(request)


@router.post("/v1/messages")
async def anthropic_messages(request: Request):
    return await _route_conversation(request)


@router.post("/anthropic/v1/messages")
async def anthropic_messages_alias(request: Request):
    return await _route_conversation(request)


def _estimate_tokens(data) -> int:
    """给网页版通道一个够用的 token 估算，避免 Claude Code 因 404 中断。"""
    text = json.dumps(data, ensure_ascii=False)
    ascii_chars = sum(1 for ch in text if ord(ch) < 128)
    other_chars = len(text) - ascii_chars
    return max(1, int(ascii_chars / 4 + other_chars))


async def _route_count_tokens(request: Request):
    denied = _check_auth(request)
    if denied:
        return denied
    raw, model = await _body_and_model(request)
    if not model:
        return _error("请求缺少 model 字段", 400, "invalid_request")
    try:
        data = json.loads(raw) if raw else {}
    except Exception:
        data = {}
    return JSONResponse({"input_tokens": _estimate_tokens(data)})


@router.post("/v1/messages/count_tokens")
async def anthropic_count_tokens(request: Request):
    return await _route_count_tokens(request)


@router.post("/anthropic/v1/messages/count_tokens")
async def anthropic_count_tokens_alias(request: Request):
    return await _route_count_tokens(request)


@router.post("/v1/responses")
async def openai_responses(request: Request):
    return await _route_conversation(request)
