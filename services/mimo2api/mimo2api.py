# -*- coding: utf-8 -*-
"""mimo2api - 小米 MiMo Studio(aistudio.xiaomimimo.com)逆向 → Anthropic / OpenAI 兼容 API

凭证：浏览器登录后的完整 Cookie（含 httpOnly 的 serviceToken）。
首次配置推荐仓库根目录 setup_wizard.py，或手动填 config.json。

模型：mimo-v2.5-pro（旗舰，默认）/ mimo-v2.5（全模态）；-think 后缀开启思考开关。
思考内容自动解析为 Anthropic thinking 块 / OpenAI reasoning_content 字段。

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
PORT = int(CONFIG.get("port", 8303))
MIN_INTERVAL = float(CONFIG.get("min_interval_seconds", 1.0))
PH = CONFIG.get("xiaomichatbot_ph", "")
CHAT_URL = CONFIG.get("chat_url", "").strip()
UA = CONFIG.get(
    "user_agent",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/139.0.0.0 Safari/537.36",
)

BUILTIN_MODELS = {
    "mimo-web": "mimo-v2.5-pro",
    "mimo-v2.5-pro": "mimo-v2.5-pro",
    "mimo-v2.5": "mimo-v2.5",
}
EXTRA_MODELS = {k.lower(): v for k, v in CONFIG.get("extra_models", {}).items()}

THINK_RE = re.compile(r"-(think|thinking)$", re.I)
TOOL_CALL_RE = re.compile(r"<tool_call>\s*(\{.*?\})\s*</tool_call>", re.S)
THINK_SPLIT_RE = re.compile(r"<think>\x00?(.*?)</think>\x00?", re.S)

app = FastAPI(title="mimo2api")


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
        "尚未配置 MiMo Cookie。用仓库根目录 setup_wizard.py 配置，"
        "或手动填写 config.json 的 cookie 与 xiaomichatbot_ph。", 503)


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


def resolve_model(model: str):
    base = model or "mimo-web"
    thinking = bool(THINK_RE.search(base))
    if thinking:
        base = THINK_RE.sub("", base)
    key = base.lower().strip()
    upstream = EXTRA_MODELS.get(key) or BUILTIN_MODELS.get(key, "mimo-v2.5-pro")
    return upstream, thinking


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
    return clean, calls


def split_thinking(text: str):
    m = THINK_SPLIT_RE.search(text)
    if not m:
        return "", text
    thinking = m.group(1).strip()
    answer = (text[: m.start()] + text[m.end():]).strip()
    return thinking, answer


# ============ 上游解析（纯函数，可单测） ============

def parse_mimo_stream_lines(lines):
    """输入上游 SSE 行序列，返回 (完整原始文本)。思考用 <think>..</think> 包裹。"""
    full, cur_event = "", None
    for line in lines:
        if line.startswith("event:"):
            cur_event = line[6:].strip() if line[5:6] == " " else line[6:].strip()
            continue
        if not line.startswith("data:"):
            continue
        try:
            payload = json.loads(line[5:].strip())
        except Exception:
            continue
        if cur_event == "message" and isinstance(payload, dict) \
                and payload.get("type") == "text":
            full += payload.get("content") or ""
        elif cur_event == "finish":
            break
    return full


# ============ MiMo 上游 ============

def mimo_headers() -> dict:
    return {
        "Content-Type": "application/json",
        "Accept-Language": "system",
        "x-timeZone": "Asia/Shanghai",
        "User-Agent": UA,
        "Cookie": STATE["cookie"],
    }


def mimo_chat_url() -> str:
    if CHAT_URL:
        parts = urlsplit(CHAT_URL)
        query = dict(parse_qsl(parts.query, keep_blank_values=True))
        query["xiaomichatbot_ph"] = PH
        return urlunsplit((
            parts.scheme or "https", parts.netloc, parts.path,
            urlencode(query), parts.fragment,
        ))
    return f"https://aistudio.xiaomimimo.com/open-apis/bot/chat?xiaomichatbot_ph={PH}"


async def mimo_chat(query: str, upstream_model: str, thinking: bool):
    if not PH:
        raise UpstreamError(
            "config_missing",
            "缺少 xiaomichatbot_ph 配置。用统一管理台浏览器登录，"
            "或粘贴 bot/chat 请求 URL 自动提取。")
    body = {
        "msgId": uuid.uuid4().hex,
        "conversationId": uuid.uuid4().hex,
        "query": query,
        "isEditedQuery": False,
        "modelConfig": {"enableThinking": thinking,
                        "webSearchStatus": "disabled",
                        "model": upstream_model},
        "multiMedias": [],
    }
    url = mimo_chat_url()
    await LIMITER.wait()
    async with httpx.AsyncClient(timeout=httpx.Timeout(300.0, connect=15.0)) as client:
        async with client.stream("POST", url, headers=mimo_headers(),
                                 json=body) as resp:
            if resp.status_code != 200:
                raw = (await resp.aread())[:300].decode("utf-8", "replace")
                code = "cookie_expired" if resp.status_code == 401 else "upstream_error"
                raise UpstreamError(
                    code, f"mimo HTTP {resp.status_code}: {raw} "
                          f"（401 时重新登录 MiMo Studio 并更新 Cookie）")
            full = ""
            cur_event = None
            async for line in resp.aiter_lines():
                if line.startswith("event:"):
                    cur_event = line[6:].strip() if line[5:6] == " " else line[6:].strip()
                    continue
                if not line.startswith("data:"):
                    continue
                try:
                    payload = json.loads(line[5:].strip())
                except Exception:
                    continue
                if cur_event == "message" and isinstance(payload, dict) \
                        and payload.get("type") == "text":
                    full += payload.get("content") or ""
                elif cur_event == "finish":
                    break
            a, t = split_thinking(full)
            yield "done", a, t


async def mimo_complete(query: str, upstream_model: str, thinking: bool):
    answer = thinking = ""
    for attempt in (0, 1):
        async for kind, a, t in mimo_chat(query, upstream_model, thinking):
            if kind == "done":
                answer, thinking = a, t
        if answer.strip():
            return answer, thinking
        await asyncio.sleep(1.5)
        query = query + "\n\n（请直接回答用户的问题。）"
    raise UpstreamError(
        "rate_limited",
        "MiMo 上游连续返回空回复——可能被限流。请降低频率，"
        "在浏览器打开 MiMo Studio 正常对话一次后重试；若浏览器也异常请重新登录并更新 Cookie。")


async def chat_with_tools(prompt: str, upstream_model: str, thinking: bool,
                          has_tools: bool):
    text, think = await mimo_complete(prompt, upstream_model, thinking)
    if not has_tools:
        return text, think, []
    # MiMo 有时把 <tool_call> 写进思考块里，正文只是叙述，所以两处都查
    clean, calls = parse_tool_calls(text)
    if not calls and think:
        clean, calls = parse_tool_calls(think)
        if calls:
            text = ""  # 调用块已提取，正文叙述对客户端无意义
    return clean, think, calls


# ============ Anthropic ============

def sse(obj: dict) -> str:
    return f"event: {obj['type']}\ndata: {json.dumps(obj, ensure_ascii=False)}\n\n"


@app.get("/")
async def health():
    return {"service": "mimo2api", "version": "1.2", "status": "ok",
            "cookie_configured": bool(STATE["cookie"]),
            "chat_url_captured": bool(CHAT_URL)}


@app.get("/v1/models")
async def models():
    ids = sorted(set(list(BUILTIN_MODELS.keys()) + list(EXTRA_MODELS.keys())))
    return {"data": [{"id": i, "object": "model", "owned_by": "mimo-web"} for i in ids]}


@app.post("/v1/messages")
async def anthropic_messages(request: Request):
    if get_api_key(request) != API_KEY:
        return unauthorized()
    if not STATE["cookie"]:
        return no_cookie()
    try:
        body = await request.json()
    except Exception:
        return api_error("invalid_request", "invalid json", 400)

    model = body.get("model", "mimo-web")
    upstream, want_think = resolve_model(model)
    tools = body.get("tools") or []
    prompt = build_prompt(body.get("messages"), body.get("system"), tools)
    msg_id = "msg_" + uuid.uuid4().hex[:24]

    try:
        text, thinking, calls = await chat_with_tools(prompt, upstream, want_think,
                                                      bool(tools))
    except UpstreamError as e:
        return api_error(e.code, str(e))

    content = []
    if thinking:
        content.append({"type": "thinking", "thinking": thinking})
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
        "usage": {"input_tokens": 0, "output_tokens": (len(text) + len(thinking)) // 3},
    })


# ============ OpenAI Chat ============

@app.post("/v1/chat/completions")
async def openai_chat(request: Request):
    if get_api_key(request) != API_KEY:
        return unauthorized()
    if not STATE["cookie"]:
        return no_cookie()
    try:
        body = await request.json()
    except Exception:
        return api_error("invalid_request", "invalid json", 400)
    model = body.get("model", "mimo-web")
    upstream, want_think = resolve_model(model)
    tools = body.get("tools") or []
    prompt = build_prompt(body.get("messages"), None, tools)

    try:
        text, thinking, calls = await chat_with_tools(prompt, upstream, want_think,
                                                      bool(tools))
    except UpstreamError as e:
        return api_error(e.code, str(e))

    if calls:
        message = {"role": "assistant", "content": text or None,
                   "reasoning_content": thinking or None,
                   "tool_calls": [{"id": "call_" + uuid.uuid4().hex[:20],
                                   "type": "function",
                                   "function": {"name": c["name"],
                                                "arguments": json.dumps(c["arguments"],
                                                                        ensure_ascii=False)}}
                                  for c in calls]}
        finish = "tool_calls"
    else:
        message = {"role": "assistant", "content": text,
                   "reasoning_content": thinking or None}
        finish = "stop"

    return JSONResponse({
        "id": "chatcmpl-" + uuid.uuid4().hex[:20], "object": "chat.completion",
        "created": int(time.time()), "model": model,
        "choices": [{"index": 0, "message": message, "finish_reason": finish}],
        "usage": {"prompt_tokens": 0,
                  "completion_tokens": (len(text) + len(thinking)) // 3,
                  "total_tokens": (len(text) + len(thinking)) // 3},
    })


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
    global CHAT_URL, PH, UA
    if not _check_admin(request):
        return JSONResponse({"error": "admin key required"}, status_code=401)
    try:
        body = await request.json()
    except Exception:
        return api_error("invalid_request", "invalid json", 400)

    cookie = (body.get("cookie") or "").strip()
    if cookie:
        if "serviceToken" not in cookie and len(cookie) < 80:
            return api_error(
                "invalid_request",
                "cookie 需要包含小米登录态（serviceToken 等），请从 Network 面板复制完整 Cookie",
                400)
        STATE["cookie"] = cookie
        CONFIG["cookie"] = cookie

    if body.get("xiaomichatbot_ph"):
        PH = str(body["xiaomichatbot_ph"]).strip()
        CONFIG["xiaomichatbot_ph"] = PH
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
            "xiaomichatbot_ph": PH, "chat_url": CHAT_URL}


@app.post("/admin/shutdown")
async def admin_shutdown(request: Request):
    if not _check_admin(request):
        return JSONResponse({"error": "admin key required"}, status_code=401)
    asyncio.get_running_loop().call_later(0.4, os._exit, 0)
    return {"ok": True}


def run_check():
    print("[1/3] 配置文件 ...", "OK" if API_KEY else "FAIL")
    if not STATE["cookie"]:
        print("[2/3] cookie ...", "未配置（setup_wizard.py 或 config.json）")
        sys.exit(1)
    print("[2/3] cookie ...", f"OK（长度 {len(STATE['cookie'])}）")
    if not PH:
        print("[3/3] 跳过：缺少 xiaomichatbot_ph（setup_wizard.py 提取）")
        sys.exit(1)
    print("[3/3] 上游测试对话 ...")
    try:
        answer, _ = asyncio.run(mimo_complete("Reply with exactly: ok",
                                              "mimo-v2.5-pro", False))
        print("      上游回复:", answer[:50] or "(空)")
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
