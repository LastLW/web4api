# -*- coding: utf-8 -*-
"""qwen4api - 千问网页版(qianwen.com)逆向 → Anthropic / OpenAI / Responses 兼容 API

原理与故障排查见仓库 README。本文件自包含，依赖：pip install fastapi uvicorn httpx

协议：
  POST /v1/messages           Anthropic Messages（流式/非流式/工具调用/思考块）
  POST /v1/chat/completions   OpenAI Chat（流式/非流式/工具调用）
  POST /v1/responses          OpenAI Responses（Codex，实验性）
  GET  /v1/models
  POST /admin/cookie          热更新凭证

模型：qwen3.7 / qwen3.8-max / qwen3.7-max / qwen3.6-flash，
任意 Qwen* 名称自动透传。
思考模式：仅 qwen3.7 支持，模型名加 -think / -thinking 后缀开启
（上游 chat_mode=expert）。思考内容（步骤标题+推理摘要）解析为
Anthropic thinking 块 / OpenAI reasoning_content 字段。
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


def _load_config():
    with open(CONFIG_PATH, encoding="utf-8") as f:
        return json.load(f)


CONFIG = _load_config()
STATE = {"cookie": CONFIG.get("cookie", "").strip()}
API_KEY = CONFIG["api_key"]
ADMIN_KEY = CONFIG.get("admin_key", "")
DEVICE_ID = CONFIG.get("device_id", "")
if not DEVICE_ID:  # 未配置时生成并持久化，保证上游可用
    DEVICE_ID = str(uuid.uuid4())
    CONFIG["device_id"] = DEVICE_ID
    _tmp = f"{CONFIG_PATH}.{os.getpid()}.tmp"
    with open(_tmp, "w", encoding="utf-8") as _f:
        json.dump(CONFIG, _f, ensure_ascii=False, indent=2)
        _f.write("\n")
    os.replace(_tmp, CONFIG_PATH)
PORT = int(CONFIG.get("port", 8301))
MIN_INTERVAL = float(CONFIG.get("min_interval_seconds", 2.0))
CHAT_URL = CONFIG.get("chat_url", "").strip()
UA = CONFIG.get(
    "user_agent",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/139.0.0.0 Safari/537.36",
)

BUILTIN_MODELS = {
    "qwen3.7": "Qwen",
    "qwen3.8-max": "Qwen3.8-Max",
    "qwen3.7-max": "Qwen3.7-Max",
    "qwen3.6-flash": "Qwen3.6-Flash",
    "qwen-web": "Qwen",
}
EXTRA_MODELS = {k.lower(): v for k, v in CONFIG.get("extra_models", {}).items()}

THINK_RE = re.compile(r"-(think|thinking)$", re.I)
TOOL_CALL_RE = re.compile(r"<tool_call>\s*(\{.*?\})\s*</tool_call>", re.S)

app = FastAPI(title="qwen4api")


# ============ 统一错误 ============

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


def unauthorized():
    return JSONResponse(
        {"type": "error",
         "error": {"type": "authentication_error", "code": "invalid_api_key",
                   "message": "invalid api key"}},
        status_code=401,
    )


def get_api_key(request: Request) -> str:
    key = request.headers.get("x-api-key") or ""
    if not key:
        auth = request.headers.get("authorization") or ""
        if auth.lower().startswith("bearer "):
            key = auth[7:].strip()
    return key


# ============ 限速器 ============

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


# ============ 模型解析 ============

def resolve_model(model: str):
    """返回 (上游model值, chat_mode)。

    -think / -thinking 后缀 = 思考模式（chat_mode=expert）。
    实测仅 qwen3.7 支持思考模式，其余模型自动忽略该后缀。
    """
    base, mode = model or "qwen3.7", "quick"
    m = THINK_RE.search(base)
    if m:
        mode = "expert"
        base = base[: m.start()]
        if base.lower() != "qwen3.7":
            mode = "quick"  # 非 qwen3.7 不支持思考，忽略后缀
    key = base.lower().strip()
    upstream = EXTRA_MODELS.get(key) or BUILTIN_MODELS.get(key)
    if upstream is None:
        upstream = ("Qwen" + base.strip()[4:]) if key.startswith("qwen") else "Qwen"
    return upstream, mode


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
    # 措辞经过实测校准：以"输出格式补充"的中性框架表述不会触发上游内容审计；
    # "拥有电脑真实操作权限"类攻击性表述会被拒绝（返回"无法回答这个问题"）
    lines = [
        "输出格式补充：当你需要执行文件写入、命令执行、信息查询等操作时，"
        "先输出如下形式的调用标签（系统收到后会执行并反馈结果，届时再继续你的回答）：",
        '<tool_call>{"name": "工具名", "arguments": {参数对象}}</tool_call>',
        "调用标签之外不要输出其他内容。收到 [工具执行结果] 后，基于真实结果继续完成任务。",
        "可用的调用标签：",
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
    """系统设定在前，工具指令紧贴生成点（放在历史之后，效果最好）。"""
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
    # 清理上游偶尔吐出的内部思考轮次标记，如 [(multimodal_chat_think_1)]
    clean = re.sub(r"\[\([^)]*\)\]", "", clean).strip()
    return clean, calls


# ============ 上游解析（纯函数，可单测） ============

def parse_qwen_stream_lines(lines):
    """输入上游 SSE 行序列，返回 (正文, 思考文本, 错误)。

    正文  = multi_load/iframe 的累积快照
    思考  = bar/workflow 里 type=bar_thinking 的步骤（标题+推理摘要），
            仅思考研究模式（chat_mode=expert）产出
    """
    text, thinking, error = "", "", None
    steps, order = {}, []
    for line in lines:
        if not line.startswith("data:"):
            continue
        try:
            ev = json.loads(line[5:])
        except Exception:
            continue
        if not isinstance(ev, dict):
            continue
        if ev.get("error_msg") or ev.get("error_code"):
            error = str(ev.get("error_msg") or ev.get("error_code"))
            break
        data = ev.get("data")
        if not isinstance(data, dict):
            continue
        for msg in data.get("messages") or []:
            if not isinstance(msg, dict):
                continue
            mt = msg.get("mime_type")
            if mt == "multi_load/iframe":
                text = msg.get("content") or text
            elif mt == "bar/workflow":
                for op in (msg.get("meta_data") or {}).get("multi_load") or []:
                    if not isinstance(op, dict) or op.get("type") != "bar_thinking":
                        continue
                    c = op.get("content") or {}
                    seq = op.get("source_seq") or c.get("title") or str(len(steps))
                    if seq not in steps:
                        order.append(seq)
                        steps[seq] = {"title": c.get("title", ""), "body": ""}
                    if c.get("body"):
                        steps[seq]["body"] = c["body"]
    if steps:
        thinking = "\n\n".join(
            f"【{steps[s]['title']}】{steps[s]['body']}" for s in order if steps[s]["body"]
        )
    return text, thinking, error


# ============ 千问上游 ============

def qwen_headers() -> dict:
    return {
        "Content-Type": "application/json",
        "accept": "text/event-stream",
        "x-platform": "pc_tongyi",
        "Origin": "https://www.qianwen.com",
        "Referer": "https://www.qianwen.com/",
        "User-Agent": UA,
        "Cookie": STATE["cookie"],
    }


def build_qwen_url() -> str:
    """优先使用浏览器登录时抓到的完整 URL，只替换动态参数。"""
    ts = str(int(time.time() * 1000))
    nonce = uuid.uuid4().hex[:11]
    if CHAT_URL:
        parts = urlsplit(CHAT_URL)
        query = dict(parse_qsl(parts.query, keep_blank_values=True))
        query["nonce"] = nonce
        query["timestamp"] = ts
        if DEVICE_ID:
            query["ut"] = DEVICE_ID
        return urlunsplit((
            parts.scheme or "https", parts.netloc, parts.path,
            urlencode(query), parts.fragment,
        ))
    return (
        "https://chat2.qianwen.com/api/v2/chat?biz_id=ai_qwen&fe_version=1.0.0"
        "&chat_client=h5&device=pc&fr=pc&pr=qwen"
        f"&ut={DEVICE_ID}&la=zh-CN&tz=Asia%2FShanghai&wv=4.5.6&ve=4.5.6"
        f"&nonce={nonce}&timestamp={ts}"
    )


def build_qwen_body(prompt: str, upstream_model: str, mode: str) -> dict:
    return {
        "req_id": uuid.uuid4().hex,
        "parent_req_id": "0",
        "messages": [{
            "mime_type": "text/plain",
            "content": prompt,
            "meta_data": {"ori_query": prompt},
            "status": "complete",
        }],
        "scene": "chat",
        "sub_scene": "",
        "scene_param": "first_turn",
        "session_id": uuid.uuid4().hex,
        "biz_id": "ai_qwen",
        "topic_id": uuid.uuid4().hex[:24],
        "model": upstream_model,
        "from": "default",
        "protocol_version": "v2",
        "messages_merge": False,
        "chat_client": "h5",
        "deep_search": None,
        "temporary": CONFIG.get("temporary", True),
        "chat_mode": mode,
        "cms_test_data_ids": "",
        "bucket": {},
    }


RISK_HINT = ("千问上游异常。可能原因：1) 触发限流/风控——降低频率，"
             "浏览器打开 qianwen.com 正常对话一次或完成滑块验证即可恢复；"
             "2) cookie 过期——重新抓取 tongyi_sso_ticket 后用 /admin/cookie 更新。")


async def qwen_chat(prompt: str, upstream_model: str, mode: str):
    """产出 ('thinking', text)、('delta', text)，结束产出 ('done', text, thinking)。"""
    url = build_qwen_url()
    await LIMITER.wait()
    async with httpx.AsyncClient(timeout=httpx.Timeout(300.0, connect=15.0)) as client:
        async with client.stream(
            "POST", url, headers=qwen_headers(),
            json=build_qwen_body(prompt, upstream_model, mode),
        ) as resp:
            if resp.status_code != 200:
                raw = (await resp.aread())[:300].decode("utf-8", "replace")
                raise UpstreamError("upstream_error",
                                    f"qwen HTTP {resp.status_code}: {raw}")
            lines = []
            async for line in resp.aiter_lines():
                lines.append(line)
    text, thinking, error = parse_qwen_stream_lines(lines)
    if error:
        raise UpstreamError("risk_control", f"qwen error: {error}. {RISK_HINT}")
    if thinking:
        yield "thinking", thinking
    if text:
        # 思考步骤完成后才开始出正文（上游为累积快照，整段一次发出）
        yield "delta", text
    yield "done", text, thinking


async def qwen_complete(prompt: str, upstream_model: str, mode: str):
    """聚合完整回复，返回 (正文, 思考)。空流自动重试一次。"""
    worked = prompt
    text, thinking = "", ""
    for attempt in (0, 1):
        text, thinking = "", ""
        async for item in qwen_chat(prompt, upstream_model, mode):
            if item[0] == "thinking":
                thinking = item[1]
            elif item[0] == "delta":
                text += item[1]
            elif item[0] == "done":
                text, thinking = item[1], item[2]
        if text.strip():
            return text, thinking
        await asyncio.sleep(1.5)
        prompt = worked + "\n\n（请直接回答用户的问题。）"
    raise UpstreamError(
        "rate_limited",
        "千问上游连续返回空流——通常是被限流或触发了风控。"
        "请：1) 降低调用频率；2) 在浏览器打开 qianwen.com 正常对话一次或完成滑块验证；"
        "3) 若浏览器里也异常，重新登录并更新 cookie（/admin/cookie）。")


async def chat_with_tools(prompt: str, upstream_model: str, mode: str, has_tools: bool):
    text, thinking = await qwen_complete(prompt, upstream_model, mode)
    if not has_tools:
        return text, thinking, []
    clean, calls = parse_tool_calls(text)
    if calls:
        return clean, thinking, calls
    # 自愈：模型没发工具调用就装傻（"我无法…"或假装已完成），追加提醒强制重发一次
    enforced = (prompt + "\n\n（系统提示：上一步尚未真正执行。"
                "请输出 tool_call 调用块来完成该操作。）")
    text2, thinking2 = await qwen_complete(enforced, upstream_model, mode)
    clean2, calls2 = parse_tool_calls(text2)
    if calls2:
        return clean2, thinking2 or thinking, calls2
    return (text2 or text).strip(), thinking or thinking2, []


# ============ Anthropic ============

def sse(obj: dict) -> str:
    return f"event: {obj['type']}\ndata: {json.dumps(obj, ensure_ascii=False)}\n\n"


@app.get("/")
async def health():
    return {"service": "qwen4api", "version": "3.2", "status": "ok",
            "cookie_configured": bool(STATE["cookie"]),
            "chat_url_captured": bool(CHAT_URL)}


@app.get("/v1/models")
async def models():
    ids = sorted(set(list(BUILTIN_MODELS.keys()) + list(EXTRA_MODELS.keys())))
    return {"data": [{"id": i, "object": "model", "owned_by": "qwen-web"} for i in ids]}


@app.post("/v1/messages")
async def anthropic_messages(request: Request):
    if get_api_key(request) != API_KEY:
        return unauthorized()
    try:
        body = await request.json()
    except Exception:
        return api_error("invalid_request", "invalid json", 400)

    model = body.get("model", "qwen3.7")
    upstream, mode = resolve_model(model)
    tools = body.get("tools") or []
    prompt = build_prompt(body.get("messages"), body.get("system"), tools)
    msg_id = "msg_" + uuid.uuid4().hex[:24]

    try:
        text, thinking, calls = await chat_with_tools(prompt, upstream, mode, bool(tools))
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
        "usage": {"input_tokens": len(prompt) // 3,
                  "output_tokens": (len(text) + len(thinking) + len(str(calls))) // 3},
    })


# ============ OpenAI Chat ============

@app.post("/v1/chat/completions")
async def openai_chat(request: Request):
    if get_api_key(request) != API_KEY:
        return unauthorized()
    try:
        body = await request.json()
    except Exception:
        return api_error("invalid_request", "invalid json", 400)
    model = body.get("model", "qwen3.7")
    upstream, mode = resolve_model(model)
    tools = body.get("tools") or []
    prompt = build_prompt(body.get("messages"), None, tools)
    cid = "chatcmpl-" + uuid.uuid4().hex[:20]

    try:
        text, thinking, calls = await chat_with_tools(prompt, upstream, mode, bool(tools))
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
        "id": cid, "object": "chat.completion", "created": int(time.time()),
        "model": model,
        "choices": [{"index": 0, "message": message, "finish_reason": finish}],
        "usage": {"prompt_tokens": len(prompt) // 3,
                  "completion_tokens": (len(text) + len(thinking)) // 3,
                  "total_tokens": (len(prompt) + len(text)) // 3},
    })


# ============ OpenAI Responses（Codex，实验性） ============

@app.post("/v1/responses")
async def openai_responses(request: Request):
    if get_api_key(request) != API_KEY:
        return unauthorized()
    try:
        body = await request.json()
    except Exception:
        return api_error("invalid_request", "invalid json", 400)
    model = body.get("model", "qwen3.7")
    upstream, mode = resolve_model(model)

    inp = body.get("input")
    items = []
    if isinstance(inp, str):
        items = [("user", inp)]
    else:
        for item in inp or []:
            typ = item.get("type", "message")
            if typ == "message":
                items.append((item.get("role", "user"),
                              content_to_text(item.get("content"))))
            elif typ == "function_call":
                items.append(("assistant", f"[助手发起工具调用] {item.get('name')} 参数: "
                                           f"{item.get('arguments', '{}')}"))
            elif typ == "function_call_output":
                items.append(("user", f"[工具执行结果]\n{item.get('output', '')}"))

    lines = []
    if body.get("instructions"):
        lines.append(f"[系统设定]\n{body['instructions']}")
    tools = body.get("tools") or []
    if tools:
        lines.append(tools_instruction(tools))
    for role, text in items:
        tag = "用户" if role == "user" else "助手"
        lines.append(f"[{tag}]\n{text}")
    lines.append("[助手]")
    prompt = "\n\n".join(lines)

    try:
        text, thinking, calls = await chat_with_tools(prompt, upstream, mode, bool(tools))
    except UpstreamError as e:
        return api_error(e.code, str(e))

    output = []
    if text:
        output.append({"type": "message", "id": "msg_" + uuid.uuid4().hex[:20],
                       "status": "completed", "role": "assistant",
                       "content": [{"type": "output_text", "text": text,
                                    "annotations": []}]})
    for c in calls:
        output.append({"type": "function_call", "id": "fc_" + uuid.uuid4().hex[:20],
                       "call_id": "call_" + uuid.uuid4().hex[:20], "name": c["name"],
                       "arguments": json.dumps(c["arguments"], ensure_ascii=False),
                       "status": "completed"})

    result = {"id": "resp_" + uuid.uuid4().hex[:24], "object": "response",
              "created_at": int(time.time()), "status": "completed", "model": model,
              "output": output, "parallel_tool_calls": False,
              "usage": {"input_tokens": len(prompt) // 3,
                        "output_tokens": (len(text) + len(str(calls))) // 3}}
    if body.get("stream"):
        async def gen():
            def evt(name, payload):
                return f"event: {name}\ndata: {json.dumps(payload, ensure_ascii=False)}\n\n"
            base = {"id": result["id"], "object": "response",
                    "created_at": result["created_at"], "status": "in_progress",
                    "model": model, "output": []}
            yield evt("response.created", {"type": "response.created", "response": base})
            for i, o in enumerate(output):
                if o["type"] == "message":
                    yield evt("response.output_item.added",
                              {"type": "response.output_item.added", "output_index": i,
                               "item": {**o, "content": []}})
                    yield evt("response.output_text.delta",
                              {"type": "response.output_text.delta", "item_id": o["id"],
                               "output_index": i, "content_index": 0,
                               "delta": o["content"][0]["text"]})
                yield evt("response.output_item.done",
                          {"type": "response.output_item.done",
                           "output_index": i, "item": o})
            yield evt("response.completed", {"type": "response.completed",
                                             "response": result})
        return StreamingResponse(gen(), media_type="text/event-stream",
                                 headers={"Cache-Control": "no-cache"})
    return JSONResponse(result)


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
    global CHAT_URL, DEVICE_ID, UA
    if not _check_admin(request):
        return JSONResponse({"error": "admin key required"}, status_code=401)
    try:
        body = await request.json()
    except Exception:
        return api_error("invalid_request", "invalid json", 400)

    cookie = (body.get("cookie") or "").strip()
    if cookie:
        if "tongyi_sso_ticket" not in cookie:
            return api_error("invalid_request", "cookie 里必须包含 tongyi_sso_ticket", 400)
        STATE["cookie"] = cookie
        CONFIG["cookie"] = cookie

    if body.get("device_id"):
        DEVICE_ID = str(body["device_id"]).strip()
        CONFIG["device_id"] = DEVICE_ID
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
            "device_id": DEVICE_ID, "chat_url": CHAT_URL}


@app.post("/admin/shutdown")
async def admin_shutdown(request: Request):
    if not _check_admin(request):
        return JSONResponse({"error": "admin key required"}, status_code=401)
    asyncio.get_running_loop().call_later(0.4, os._exit, 0)
    return {"ok": True}


def run_check():
    """--check 自检：配置 → cookie → 上游连通。"""
    print("[1/3] 配置文件 ...", "OK" if API_KEY and STATE["cookie"] else
          "FAIL（缺少 api_key 或 cookie）")
    if not (API_KEY and STATE["cookie"]):
        sys.exit(1)
    print("[2/3] cookie 格式 ...",
          "OK（含 tongyi_sso_ticket）" if "tongyi_sso_ticket" in STATE["cookie"]
          else "WARN（不含 tongyi_sso_ticket，可能不完整）")
    print("[3/3] 上游测试对话（约 5-20 秒）...")
    try:
        text, _ = asyncio.run(qwen_complete("Reply with exactly: ok",
                                            "Qwen3.6-Flash", "quick"))
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
