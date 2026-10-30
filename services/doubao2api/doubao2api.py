# -*- coding: utf-8 -*-
"""doubao2api - 豆包网页版(doubao.com)逆向 → Anthropic / OpenAI 兼容 API

原理与故障排查见仓库 README。本文件自包含，依赖：pip install fastapi uvicorn httpx

凭证：浏览器登录 doubao.com 后的完整 Cookie（含 httpOnly 的 sessionid 等）。
首次配置推荐用仓库根目录的 setup_wizard.py，或手动填 config.json 的
cookie / device_id / fp / tea_uuid（从 Network 面板的请求中提取）。

模型：
  doubao-web         豆包 快速
  doubao-web-expert  豆包 2.1 Turbo 专家
协议：
  POST /v1/messages / POST /v1/chat/completions / GET /v1/models
  POST /admin/cookie
"""
import asyncio
import json
import os
import re
import sys
import time
import uuid
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

import httpx
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, StreamingResponse

BASE = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.path.join(BASE, "config.json")
with open(CONFIG_PATH, encoding="utf-8") as f:
    CONFIG = json.load(f)

STATE = {"cookie": CONFIG.get("cookie", "").strip()}
API_KEY = CONFIG["api_key"]
ADMIN_KEY = CONFIG.get("admin_key", "")
PORT = int(CONFIG.get("port", 8302))
MIN_INTERVAL = float(CONFIG.get("min_interval_seconds", 1.0))
DEVICE_ID = CONFIG.get("device_id", "")
FP = CONFIG.get("fp", "")
TEA_UUID = CONFIG.get("tea_uuid", "")
BOT_ID = CONFIG.get("bot_id", "7338286299411103781")
CHAT_URL = CONFIG.get("chat_url", "").strip()
UA = CONFIG.get(
    "user_agent",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/139.0.0.0 Safari/537.36",
)

VARIANTS = {"fast": ("0", 3, 0), "expert": ("3", 4, 3)}
BUILTIN_MODELS = {"doubao-web": "fast", "doubao-web-expert": "expert"}
EXTRA_MODELS = {k.lower(): v for k, v in CONFIG.get("extra_models", {}).items()}

THINK_RE = re.compile(r"-(think|thinking)$", re.I)
TOOL_CALL_RE = re.compile(r"<tool_call>\s*(\{.*?\})\s*</tool_call>", re.S)
# 豆包偶尔会把开括号 "<" 吞掉（模型怪癖），宽容匹配缺左括号的调用块
TOOL_CALL_LOOSE_RE = re.compile(r"(?:<)?tool_call>\s*(\{.*?\})\s*</tool_call>", re.S)

app = FastAPI(title="doubao2api")


# ============ 统一错误 / 鉴权 / 限速 ============

class UpstreamError(RuntimeError):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


def api_error(code: str, message: str, status: int = 502):
    return JSONResponse(
        {"type": "error",
         "error": {"type": "api_error", "code": code, "message": message}},
        status_code=status,
    )


def get_api_key(request: Request) -> str:
    key = request.headers.get("x-api-key") or ""
    if not key:
        auth = request.headers.get("authorization") or ""
        if auth.lower().startswith("bearer "):
            key = auth[7:].strip()
    return key


def unauthorized():
    return JSONResponse(
        {"type": "error",
         "error": {"type": "authentication_error", "code": "invalid_api_key",
                   "message": "invalid api key"}},
        status_code=401,
    )


def no_cookie():
    return api_error(
        "cookie_missing",
        "尚未配置豆包 Cookie。用仓库根目录 setup_wizard.py 配置，"
        "或手动填写 config.json 的 cookie/device_id/fp/tea_uuid。", 503)


class RateLimiter:
    def __init__(self, min_interval: float):
        self.min_interval = min_interval
        self._last = 0.0
        self._lock = asyncio.Lock()

    async def wait(self):
        if self.min_interval <= 0:
            return
        async with self._lock:
            now = time.monotonic()
            delay = self._last + self.min_interval - now
            if delay > 0:
                await asyncio.sleep(delay)
            self._last = time.monotonic()


LIMITER = RateLimiter(MIN_INTERVAL)


def resolve_variant(model: str) -> str:
    base = model or "doubao-web"
    m = THINK_RE.search(base)
    if m:
        base = base[: m.start()]
    key = base.lower().strip()
    if key in EXTRA_MODELS:
        return EXTRA_MODELS[key]
    return BUILTIN_MODELS.get(key, "fast")


# ============ 提示词组装 ============

def content_to_text(content) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for b in content:
            if not isinstance(b, dict):
                continue
            t = b.get("type")
            if t == "text":
                parts.append(b.get("text", ""))
            elif t == "tool_use":
                parts.append(f"[助手发起工具调用] {b.get('name')} 参数: "
                             f"{json.dumps(b.get('input', {}), ensure_ascii=False)}")
            elif t == "tool_result":
                inner = b.get("content")
                inner = content_to_text(inner) if not isinstance(inner, str) else inner
                parts.append(f"[工具执行结果]\n{inner}")
            elif t in ("input_text", "output_text"):
                parts.append(b.get("text", ""))
        return "\n".join(parts)
    return str(content or "")


def tools_instruction(tools) -> str:
    lines = [
        "# 工具调用（重要）",
        "你身处一个工具调用环境：你无法感知用户的本地环境，凡是涉及读取文件、"
        "执行命令、查询本地/实时信息的请求，都【必须】通过工具调用完成，"
        "禁止凭空编造结果。",
        "调用格式（需要时回复中只包含调用块，可连续多个）：",
        '<tool_call>{"name": "工具名", "arguments": {参数对象}}</tool_call>',
        "不需要调用工具的普通问题直接正常回答。收到 [工具执行结果] 后继续任务。",
        "",
    ]
    for t in tools or []:
        name = t.get("name") or (t.get("function") or {}).get("name")
        if not name:
            continue
        desc = t.get("description") or (t.get("function") or {}).get("description") or ""
        schema = t.get("input_schema") or (t.get("function") or {}).get("parameters")
        lines.append(f"## {name}\n{desc}")
        if schema:
            lines.append(f"参数: {json.dumps(schema, ensure_ascii=False)}")
    return "\n".join(lines)


def build_prompt(messages, system, tools) -> str:
    lines = []
    if system:
        lines.append(f"[系统设定]\n{content_to_text(system)}")
    for m in messages or []:
        role = m.get("role")
        if role == "tool":
            lines.append(f"[工具执行结果] (工具 {m.get('tool_call_id', '')})\n"
                         f"{content_to_text(m.get('content'))}")
            continue
        tag = "用户" if role == "user" else "助手"
        lines.append(f"[{tag}]\n{content_to_text(m.get('content'))}")
    if tools:
        lines.append(tools_instruction(tools))
    lines.append("[助手]")
    return "\n\n".join(lines)


def parse_tool_calls(text: str):
    calls = []

    def repl(m):
        try:
            d = json.loads(m.group(1))
            name = d.get("name")
            args = d.get("arguments", {})
            if isinstance(args, str):
                try:
                    args = json.loads(args)
                except Exception:
                    args = {"value": args}
            if name:
                calls.append({"name": name, "arguments": args or {}})
        except Exception:
            pass
        return ""

    clean = TOOL_CALL_RE.sub(repl, text).strip()
    if not calls:
        # 豆包偶尔会吞掉开括号的 "<"（输出成 tool_call>{...}</tool_call>），宽容匹配兜底
        clean = TOOL_CALL_LOOSE_RE.sub(repl, text).strip()
    return clean, calls


# ============ 上游解析（纯函数，可单测） ============

def parse_doubao_stream_lines(lines):
    """输入上游 SSE 行序列，返回 (累积正文, 是否正常结束)。"""
    buf, finished, cur_event = "", False, None
    for line in lines:
        if line.startswith("event:"):
            cur_event = (line[6:] if line[5:6] == " " else line[6:]).strip()
            continue
        if not line.startswith("data:"):
            continue
        raw = line[5:].strip()
        if not raw or raw == "{}":
            continue
        try:
            payload = json.loads(raw)
        except Exception:
            continue
        if cur_event == "CHUNK_DELTA":
            text = payload.get("text") or ""
            if text:
                buf += text
        elif cur_event == "SSE_REPLY_END":
            finished = True
        else:
            # 新版上游首段正文在 STREAM_MSG_NOTIFY 的 content.content_block，
            # 旧版在 STREAM_CHUNK 的 patch_op，两种都要解析，否则会误判为空流。
            blocks = []
            content = payload.get("content")
            if isinstance(content, dict):
                blocks.extend(content.get("content_block") or [])
            for op in payload.get("patch_op") or []:
                if op.get("patch_object") == 1:
                    blocks.extend((op.get("patch_value") or {}).get("content_block") or [])
            for cb in blocks:
                if cb.get("is_finish"):
                    finished = True
                    continue
                text = ((cb.get("content") or {}).get("text_block") or {}).get("text") or ""
                if not text:
                    continue
                delta = text[len(buf):] if text.startswith(buf) else text
                buf += delta
        if finished:
            break
    return buf, finished


# ============ 豆包上游 ============

def build_query() -> str:
    if not (DEVICE_ID and FP and TEA_UUID):
        raise UpstreamError(
            "config_missing",
            "缺少 device_id / fp / tea_uuid 配置。用统一管理台浏览器登录，"
            "或粘贴 Network 面板里 chat/completion 请求的完整 URL 即可自动提取。")
    if CHAT_URL:
        parts = urlsplit(CHAT_URL)
        query = dict(parse_qsl(parts.query, keep_blank_values=True))
        # msToken / a_bogus 是按抓包当次请求签名的一次性参数，复用会造成签名
        # 不匹配，上游直接回 STREAM_ERROR rate limited（实测），必须丢弃。
        for stale in ("msToken", "a_bogus"):
            query.pop(stale, None)
        query["device_id"] = DEVICE_ID
        query["fp"] = FP
        query["tea_uuid"] = TEA_UUID
        query["web_id"] = TEA_UUID
        query["web_tab_id"] = str(uuid.uuid4())
        return urlencode(query)
    return (
        f"aid=497858&device_id={DEVICE_ID}&device_platform=web"
        "&doubao_device_platform=web&doubao_pc_version=3.36.0"
        f"&fp={FP}&language=zh&pc_version=3.36.0&pkg_type=release_version"
        f"&real_aid=497858&region=CN&samantha_web=1&sys_region=CN"
        f"&tea_uuid={TEA_UUID}&tz_name=Asia%2FShanghai&use-olympus-account=1"
        f"&version_code=20800&web_id={TEA_UUID}&web_platform=browser"
        f"&web_tab_id={uuid.uuid4()}"
    )


def doubao_headers() -> dict:
    return {
        "Content-Type": "application/json",
        "Agw-Js-Conv": "str",
        "User-Agent": UA,
        "Cookie": STATE["cookie"],
    }


def build_doubao_body(prompt: str, variant: str) -> dict:
    model_item_key, effort, deep_think = VARIANTS[variant]
    now_ms = int(time.time() * 1000)
    return {
        "client_meta": {
            "local_conversation_id": "local_" + str(now_ms),
            "conversation_id": "",
            "bot_id": BOT_ID,
            "last_section_id": "",
            "last_message_index": None,
            "local_permissions": [],
        },
        "messages": [{
            "local_message_id": str(uuid.uuid4()),
            "content_block": [{
                "block_type": 10000,
                "content": {"text_block": {"text": prompt, "icon_url": "",
                                           "icon_url_dark": "", "summary": ""},
                            "pc_event_block": ""},
                "block_id": str(uuid.uuid4()),
                "parent_id": "",
                "meta_info": [],
                "append_fields": [],
            }],
            "message_status": 0,
        }],
        "option": {
            "send_message_scene": "", "create_time_ms": now_ms, "collect_id": "",
            "is_audio": False, "answer_with_suggest": False, "agent_mode": 2,
            "tts_switch": False, "need_deep_think": deep_think,
            "click_clear_context": False, "from_suggest": False,
            "is_regen": False, "is_replace": False,
            "is_from_click_option": False, "is_from_click_softlink": False,
            "disable_sse_cache": False, "select_text_action": "",
            "is_select_text": False, "resend_for_regen": False, "scene_type": 0,
            "unique_key": str(uuid.uuid4()), "start_seq": 0,
            "need_create_conversation": True,
            "conversation_init_option": {"need_ack_conversation": True},
            "conversation_init_ext": {"model_item_key": model_item_key,
                                      "reasoning_effort": str(effort), "mode_id": "1"},
            "regen_query_id": [], "edit_query_id": [], "regen_instruction": "",
            "no_replace_for_regen": False, "message_from": 0,
            "shared_app_name": "", "shared_app_id": "",
            "sse_recv_event_options": {"support_chunk_delta": True},
            "is_ai_playground": False, "is_old_user": False,
            "recovery_option": {"is_recovery": False,
                                "req_create_time_sec": now_ms // 1000,
                                "append_sse_event_scene": 0},
            "message_storage_type": 0, "related_deleted_message_ids": {},
            "connector_info_list": [],
            "model_config": {"model_item_key": model_item_key,
                             "model_extra_params": {}, "reasoning_effort": effort},
            "aggregate_params": {"conversation_mode": "1", "mode_id": "1",
                                 "model_item_key": model_item_key,
                                 "agent_mode": "2", "reasoning_effort": str(effort),
                                 "provider_id": ""},
            "conversation_mode": 1,
        },
        "user_context": [],
        "ext": {"agent_mode": "2",
                "use_deep_think": "1" if deep_think else "0",
                "sub_conv_firstmet_type": "1", "collection_id": "",
                "is_finish": "1", "commerce_credit_config_enable": "0"},
    }


def doubao_chat_url() -> str:
    if CHAT_URL:
        parts = urlsplit(CHAT_URL)
        return urlunsplit((
            parts.scheme or "https", parts.netloc, parts.path,
            build_query(), parts.fragment,
        ))
    return "https://www.doubao.com/chat/completion?" + build_query()


async def doubao_chat(prompt: str, variant: str):
    url = doubao_chat_url()
    await LIMITER.wait()
    async with httpx.AsyncClient(timeout=httpx.Timeout(300.0, connect=15.0)) as client:
        async with client.stream("POST", url, headers=doubao_headers(),
                                 json=build_doubao_body(prompt, variant)) as resp:
            if resp.status_code != 200:
                raw = (await resp.aread())[:300].decode("utf-8", "replace")
                raise UpstreamError("upstream_error",
                                    f"doubao HTTP {resp.status_code}: {raw}")
            ct = resp.headers.get("content-type", "")
            if "event-stream" not in ct:
                raw = (await resp.aread())[:300].decode("utf-8", "replace")
                raise UpstreamError(
                    "cookie_expired",
                    f"doubao 返回非流式响应（{ct}）：{raw} —— Cookie 大概率过期，请重新抓取更新")
            prev = ""
            finished = False
            cur_event = None
            async for line in resp.aiter_lines():
                if line.startswith("event:"):
                    cur_event = (line[6:] if line[5:6] == " " else line[6:]).strip()
                    continue
                if not line.startswith("data:"):
                    continue
                raw = line[5:].strip()
                if not raw or raw == "{}":
                    continue
                try:
                    payload = json.loads(raw)
                except Exception:
                    continue
                if cur_event == "CHUNK_DELTA":
                    text = payload.get("text") or ""
                    if text:
                        prev += text
                        yield "delta", text
                elif cur_event == "STREAM_ERROR":
                    # 上游把限流/风控放在 SSE 事件里，HTTP 仍是 200，必须显式抛出
                    code = payload.get("error_code")
                    msg = str(payload.get("error_msg") or "doubao 上游返回错误")
                    extra = json.dumps(payload.get("extra") or {}, ensure_ascii=False)
                    verify = "，需要滑块验证" if "verify" in extra else ""
                    if code == 710022004 or "rate limit" in msg.lower():
                        raise UpstreamError(
                            "rate_limited",
                            f"豆包上游限流：{msg}（错误码 {code}）{verify}。"
                            "请降低请求频率，在浏览器打开 doubao.com 正常对话一次后重试。")
                    raise UpstreamError(
                        "upstream_error", f"豆包上游错误 {code}：{msg}{verify}")
                elif cur_event == "SSE_REPLY_END":
                    finished = True
                else:
                    # 新版上游首段正文在 STREAM_MSG_NOTIFY 的 content.content_block，
                    # 旧版在 STREAM_CHUNK 的 patch_op，两种都要解析，否则会误判为空流。
                    blocks = []
                    content = payload.get("content")
                    if isinstance(content, dict):
                        blocks.extend(content.get("content_block") or [])
                    for op in payload.get("patch_op") or []:
                        if op.get("patch_object") == 1:
                            blocks.extend((op.get("patch_value") or {}).get("content_block") or [])
                    for cb in blocks:
                        if cb.get("is_finish"):
                            finished = True
                            continue
                        text = ((cb.get("content") or {}).get("text_block") or {}).get("text") or ""
                        if not text:
                            continue
                        delta = text[len(prev):] if text.startswith(prev) else text
                        prev += delta
                        if delta:
                            yield "delta", delta
                if finished:
                    break
            yield "done", prev


async def doubao_complete(prompt: str, variant: str) -> str:
    worked = prompt
    final = ""
    for attempt in (0, 1):
        final = ""
        async for kind, payload in doubao_chat(prompt, variant):
            if kind == "delta":
                final += payload
            else:
                final = payload or final
        if final.strip():
            return final
        await asyncio.sleep(1.5)
        prompt = worked + "\n\n（请直接回答用户的问题。）"
    raise UpstreamError(
        "rate_limited",
        "豆包上游连续返回空流——可能被限流或风控。请降低频率，"
        "在浏览器打开 doubao.com 正常对话一次后重试；若浏览器也异常请重新登录并更新 Cookie。")


async def chat_with_tools(prompt: str, variant: str, has_tools: bool):
    text = await doubao_complete(prompt, variant)
    if not has_tools:
        return text, []
    return parse_tool_calls(text)


# ============ Anthropic ============

def sse(obj: dict) -> str:
    return f"event: {obj['type']}\ndata: {json.dumps(obj, ensure_ascii=False)}\n\n"


@app.get("/")
async def health():
    return {"service": "doubao2api", "version": "1.2", "status": "ok",
            "cookie_configured": bool(STATE["cookie"]),
            "chat_url_captured": bool(CHAT_URL)}


@app.get("/v1/models")
async def models():
    ids = sorted(set(list(BUILTIN_MODELS.keys()) + list(EXTRA_MODELS.keys())))
    return {"data": [{"id": i, "object": "model", "owned_by": "doubao-web"} for i in ids]}


@app.post("/v1/messages")
async def anthropic_messages(request: Request):
    if get_api_key(request) != API_KEY:
        return unauthorized()
    if not STATE["cookie"]:
        return api_error("cookie_missing",
                         "尚未配置豆包 Cookie。用 setup_wizard.py 配置或填 config.json。", 503)
    try:
        body = await request.json()
    except Exception:
        return api_error("invalid_request", "invalid json", 400)

    model = body.get("model", "doubao-web")
    variant = resolve_variant(model)
    tools = body.get("tools") or []
    prompt = build_prompt(body.get("messages"), body.get("system"), tools)
    msg_id = "msg_" + uuid.uuid4().hex[:24]

    try:
        text, calls = await chat_with_tools(prompt, variant, bool(tools))
    except UpstreamError as e:
        return api_error(e.code, str(e))

    content = []
    if text:
        content.append({"type": "text", "text": text})
    for c in calls:
        content.append({"type": "tool_use", "id": "toolu_" + uuid.uuid4().hex[:20],
                        "name": c["name"], "input": c["arguments"]})
    return JSONResponse({
        "id": msg_id, "type": "message", "role": "assistant", "model": model,
        "content": content or [{"type": "text", "text": "(空回复)"}],
        "stop_reason": "tool_use" if calls else "end_turn",
        "stop_sequence": None,
        "usage": {"input_tokens": len(prompt) // 3,
                  "output_tokens": (len(text) + len(str(calls))) // 3},
    })


# ============ OpenAI Chat ============

@app.post("/v1/chat/completions")
async def openai_chat(request: Request):
    if get_api_key(request) != API_KEY:
        return unauthorized()
    if not STATE["cookie"]:
        return api_error("cookie_missing",
                         "尚未配置豆包 Cookie。用 setup_wizard.py 配置或填 config.json。", 503)
    try:
        body = await request.json()
    except Exception:
        return api_error("invalid_request", "invalid json", 400)
    model = body.get("model", "doubao-web")
    variant = resolve_variant(model)
    tools = body.get("tools") or []
    prompt = build_prompt(body.get("messages"), None, tools)
    cid = "chatcmpl-" + uuid.uuid4().hex[:20]

    try:
        text, calls = await chat_with_tools(prompt, variant, bool(tools))
    except UpstreamError as e:
        return api_error(e.code, str(e))

    if calls:
        message = {"role": "assistant", "content": text or None,
                   "tool_calls": [{"id": "call_" + uuid.uuid4().hex[:20],
                                   "type": "function",
                                   "function": {"name": c["name"],
                                                "arguments": json.dumps(c["arguments"],
                                                                        ensure_ascii=False)}}
                                  for c in calls]}
        finish = "tool_calls"
    else:
        message = {"role": "assistant", "content": text}
        finish = "stop"

    if not body.get("stream"):
        return JSONResponse({
            "id": cid, "object": "chat.completion", "created": int(time.time()),
            "model": model,
            "choices": [{"index": 0, "message": message, "finish_reason": finish}],
            "usage": {"prompt_tokens": len(prompt) // 3,
                      "completion_tokens": (len(text) + len(str(calls))) // 3,
                      "total_tokens": (len(prompt) + len(text)) // 3},
        })

    async def gen():
        def chunk(delta, finish=None):
            return {"id": cid, "object": "chat.completion.chunk",
                    "created": int(time.time()), "model": model,
                    "choices": [{"index": 0, "delta": delta, "finish_reason": finish}]}
        yield f"data: {json.dumps(chunk({'role': 'assistant'}), ensure_ascii=False)}\n\n"
        if message.get("content"):
            yield f"data: {json.dumps(chunk({'content': message['content']}), ensure_ascii=False)}\n\n"
        for tc in message.get("tool_calls") or []:
            yield f"data: {json.dumps(chunk({'tool_calls': [tc]}), ensure_ascii=False)}\n\n"
        yield f"data: {json.dumps(chunk({}, finish), ensure_ascii=False)}\n\n"
        yield "data: [DONE]\n\n"

    return StreamingResponse(gen(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache"})


# ============ 管理 ============

def _check_admin(request: Request) -> bool:
    key = request.headers.get("x-admin-key") or ""
    return bool(ADMIN_KEY) and key == ADMIN_KEY


def _persist():
    """原子写回配置；保留管理台写入的额外字段，避免互相覆盖。"""
    data = dict(CONFIG)
    try:
        with open(CONFIG_PATH, encoding="utf-8") as f:
            disk = json.load(f)
        if isinstance(disk, dict):
            # 这些字段只由管理台修改，运行中的服务不覆盖它们
            for key in ("api_key", "admin_key", "port",
                        "min_interval_seconds", "extra_models"):
                if key in disk:
                    data[key] = disk[key]
            for key, value in disk.items():
                data.setdefault(key, value)
    except Exception:
        pass
    tmp = f"{CONFIG_PATH}.{os.getpid()}.tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
        f.write("\n")
    os.replace(tmp, CONFIG_PATH)


@app.post("/admin/cookie")
@app.post("/admin/credentials")
async def admin_credentials(request: Request):
    global CHAT_URL, DEVICE_ID, FP, TEA_UUID, UA
    if not _check_admin(request):
        return JSONResponse({"error": "admin key required"}, status_code=401)
    try:
        body = await request.json()
    except Exception:
        return api_error("invalid_request", "invalid json", 400)

    cookie = (body.get("cookie") or "").strip()
    if cookie:
        if len(cookie) < 50:
            return api_error("invalid_request",
                             "cookie 看起来不完整（应包含 sessionid 等完整字段）", 400)
        STATE["cookie"] = cookie
        CONFIG["cookie"] = cookie

    for field in ("device_id", "fp", "tea_uuid", "bot_id"):
        value = body.get(field)
        if value:
            CONFIG[field] = str(value).strip()
            if field == "device_id":
                DEVICE_ID = CONFIG[field]
            elif field == "fp":
                FP = CONFIG[field]
            elif field == "tea_uuid":
                TEA_UUID = CONFIG[field]
    if body.get("user_agent"):
        UA = str(body["user_agent"]).strip()
        CONFIG["user_agent"] = UA
    if body.get("chat_url"):
        CHAT_URL = str(body["chat_url"]).strip()
        CONFIG["chat_url"] = CHAT_URL
    if body.get("captured_at"):
        CONFIG["credential_updated_at"] = str(body["captured_at"])
    _persist()
    return {"ok": True, "message": "凭证已更新并持久化",
            "cookie_configured": bool(STATE["cookie"]),
            "device_id": DEVICE_ID, "fp": FP, "tea_uuid": TEA_UUID,
            "chat_url": CHAT_URL}


@app.post("/admin/shutdown")
async def admin_shutdown(request: Request):
    if not _check_admin(request):
        return JSONResponse({"error": "admin key required"}, status_code=401)
    asyncio.get_running_loop().call_later(0.4, os._exit, 0)
    return {"ok": True}


def run_check():
    print("[1/3] 配置文件 ...", "OK" if API_KEY else "FAIL")
    if not API_KEY:
        sys.exit(1)
    if not STATE["cookie"]:
        print("[2/3] cookie ...", "未配置（setup_wizard.py 或 config.json）")
        print("[3/3] 跳过上游测试")
        sys.exit(1)
    print("[2/3] cookie ...", f"OK（长度 {len(STATE['cookie'])}）")
    if not (DEVICE_ID and FP and TEA_UUID):
        print("[3/3] 跳过：缺少 device_id/fp/tea_uuid（setup_wizard.py 提取）")
        sys.exit(1)
    print("[3/3] 上游测试对话 ...")
    try:
        text = asyncio.run(doubao_complete("Reply with exactly: ok", "fast"))
        print("      上游回复:", text[:50] or "(空)")
        print("结果: PASS")
    except UpstreamError as e:
        print(f"      [{e.code}] {e}")
        print("结果: FAIL")
        sys.exit(1)


if __name__ == "__main__":
    if "--check" in sys.argv:
        run_check()
        sys.exit(0)
    import uvicorn
    uvicorn.run(app, host="127.0.0.1", port=PORT, log_level="info")
