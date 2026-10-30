# -*- coding: utf-8 -*-
"""deepseek2api - DeepSeek 网页版(chat.deepseek.com)逆向 → OpenAI 兼容 API

与 qwen2api/doubao2api 同构的自建通道：浏览器登录抓 userToken（JWT）后直接
调用官网聊天接口，不经过 ds2api 的密码登录（那会被 RISK_DEVICE_DETECTED 风控）。

协议要点（逆向自 ds2api v4.6.1 源码，2026-05）：
  1. POST /api/v0/chat_session/create   {"agent": "chat"} → 会话 ID
  2. POST /api/v0/chat/create_pow_challenge {"target_path": "/api/v0/chat/completion"}
  3. 本地暴力求解 DeepSeekHashV1（SHA3-256 跳过 Keccak round 0）
  4. POST /api/v0/chat/completion  header x-ds-pow-response: base64(json)
     body: {chat_session_id, model_type, parent_message_id, prompt,
            ref_file_ids, thinking_enabled, search_enabled}
  5. SSE 事件流（envelope / 无路径 token / JSON patch），INCOMPLETE 时调
     /api/v0/chat/continue 接续（同一 pow 头），最多 8 轮

依赖：pip install fastapi uvicorn httpx
"""
import asyncio
import base64
import json
import os
import sys
import time
import uuid
from concurrent.futures import FIRST_COMPLETED, ProcessPoolExecutor, wait

import httpx
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, StreamingResponse

BASE = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.path.join(BASE, "config.json")

with open(CONFIG_PATH, encoding="utf-8") as f:
    CONFIG = json.load(f)

STATE = {"token": CONFIG.get("token", "").strip()}
API_KEY = CONFIG["api_key"]
ADMIN_KEY = CONFIG.get("admin_key", "")
PORT = int(CONFIG.get("port", 8304))
MIN_INTERVAL = float(CONFIG.get("min_interval_seconds", 2.0))

DS_HOST = "https://chat.deepseek.com"
CREATE_SESSION_URL = DS_HOST + "/api/v0/chat_session/create"
CREATE_POW_URL = DS_HOST + "/api/v0/chat/create_pow_challenge"
COMPLETION_URL = DS_HOST + "/api/v0/chat/completion"
CONTINUE_URL = DS_HOST + "/api/v0/chat/continue"
COMPLETION_TARGET = "/api/v0/chat/completion"

# 伪装 DeepSeek 安卓客户端（与 ds2api 一致）；token 来自网页浏览器登录，
# JWT 通用，服务端按 token 鉴权。
BASE_HEADERS = {
    "Accept": "application/json",
    "Content-Type": "application/json",
    "accept-charset": "UTF-8",
    "User-Agent": "DeepSeek/2.0.4 Android/35",
    "x-client-platform": "android",
    "x-client-version": "2.0.4",
    "x-client-locale": "zh_CN",
}

BUILTIN_MODELS = [
    "deepseek-v4-flash",
    "deepseek-v4-pro",
    "deepseek-v4-flash-nothinking",
    "deepseek-v4-pro-nothinking",
]
EXTRA_MODELS = {k.lower(): v for k, v in CONFIG.get("extra_models", {}).items()}

MAX_CONTINUE_ROUNDS = 8
RATE_HINT = ("DeepSeek 上游异常。可能原因：1) token 过期——管理台重新「抓取登录 Token」；"
             "2) 触发风控/限流——降低频率，浏览器打开 chat.deepseek.com 正常对话一次即可恢复。")

app = FastAPI(title="deepseek2api")


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


# ============ PoW：DeepSeekHashV1（SHA3-256 跳过 Keccak round 0） ============

_RC = [
    0x0000000000000001, 0x0000000000008082, 0x800000000000808A, 0x8000000080008000,
    0x000000000000808B, 0x0000000080000001, 0x8000000080008081, 0x8000000000008009,
    0x000000000000008A, 0x0000000000000088, 0x0000000080008009, 0x000000008000000A,
    0x000000008000808B, 0x800000000000008B, 0x8000000000008089, 0x8000000000008003,
    0x8000000000008002, 0x8000000000000080, 0x000000000000800A, 0x800000008000000A,
    0x8000000080008081, 0x8000000000008080, 0x0000000080000001, 0x8000000080008008,
]

_MASK = (1 << 64) - 1


def _rotl64(v: int, k: int) -> int:
    return ((v << k) | (v >> (64 - k))) & _MASK


def deepseek_hash_v1(data: bytes) -> bytes:
    """32 字节摘要；与官方 wasm 的 wasm_deepseek_hash_v1 等价。"""
    s = [0] * 25
    rate = 136
    off = 0
    while off + rate <= len(data):
        for i in range(17):
            s[i] ^= int.from_bytes(data[off + i * 8:off + i * 8 + 8], "little")
        _keccak_f23(s)
        off += rate
    final = bytearray(rate)
    tail = data[off:]
    final[:len(tail)] = tail
    final[len(tail)] ^= 0x06
    final[rate - 1] ^= 0x80
    for i in range(17):
        s[i] ^= int.from_bytes(final[i * 8:i * 8 + 8], "little")
    _keccak_f23(s)
    return b"".join(s[i].to_bytes(8, "little") for i in range(4))


def _keccak_f23(s: list) -> None:
    """Keccak-f[1600] 但只做 rounds 1..23（跳过 round 0 的 iota+置换）。"""
    rc = _RC
    for r in range(1, 24):
        a0, a1, a2, a3, a4 = s[0], s[1], s[2], s[3], s[4]
        a5, a6, a7, a8, a9 = s[5], s[6], s[7], s[8], s[9]
        a10, a11, a12, a13, a14 = s[10], s[11], s[12], s[13], s[14]
        a15, a16, a17, a18, a19 = s[15], s[16], s[17], s[18], s[19]
        a20, a21, a22, a23, a24 = s[20], s[21], s[22], s[23], s[24]

        c0 = a0 ^ a5 ^ a10 ^ a15 ^ a20
        c1 = a1 ^ a6 ^ a11 ^ a16 ^ a21
        c2 = a2 ^ a7 ^ a12 ^ a17 ^ a22
        c3 = a3 ^ a8 ^ a13 ^ a18 ^ a23
        c4 = a4 ^ a9 ^ a14 ^ a19 ^ a24
        d0 = c4 ^ _rotl64(c1, 1)
        d1 = c0 ^ _rotl64(c2, 1)
        d2 = c1 ^ _rotl64(c3, 1)
        d3 = c2 ^ _rotl64(c4, 1)
        d4 = c3 ^ _rotl64(c0, 1)
        a0 ^= d0; a5 ^= d0; a10 ^= d0; a15 ^= d0; a20 ^= d0
        a1 ^= d1; a6 ^= d1; a11 ^= d1; a16 ^= d1; a21 ^= d1
        a2 ^= d2; a7 ^= d2; a12 ^= d2; a17 ^= d2; a22 ^= d2
        a3 ^= d3; a8 ^= d3; a13 ^= d3; a18 ^= d3; a23 ^= d3
        a4 ^= d4; a9 ^= d4; a14 ^= d4; a19 ^= d4; a24 ^= d4

        b0 = a0
        b10 = _rotl64(a1, 1)
        b20 = _rotl64(a2, 62)
        b5 = _rotl64(a3, 28)
        b15 = _rotl64(a4, 27)
        b16 = _rotl64(a5, 36)
        b1 = _rotl64(a6, 44)
        b11 = _rotl64(a7, 6)
        b21 = _rotl64(a8, 55)
        b6 = _rotl64(a9, 20)
        b7 = _rotl64(a10, 3)
        b17 = _rotl64(a11, 10)
        b2 = _rotl64(a12, 43)
        b12 = _rotl64(a13, 25)
        b22 = _rotl64(a14, 39)
        b23 = _rotl64(a15, 41)
        b8 = _rotl64(a16, 45)
        b18 = _rotl64(a17, 15)
        b3 = _rotl64(a18, 21)
        b13 = _rotl64(a19, 8)
        b14 = _rotl64(a20, 18)
        b24 = _rotl64(a21, 2)
        b9 = _rotl64(a22, 61)
        b19 = _rotl64(a23, 56)
        b4 = _rotl64(a24, 14)

        s[0] = b0 ^ (b1 ^ _MASK) & b2
        s[1] = b1 ^ (b2 ^ _MASK) & b3
        s[2] = b2 ^ (b3 ^ _MASK) & b4
        s[3] = b3 ^ (b4 ^ _MASK) & b0
        s[4] = b4 ^ (b0 ^ _MASK) & b1
        s[5] = b5 ^ (b6 ^ _MASK) & b7
        s[6] = b6 ^ (b7 ^ _MASK) & b8
        s[7] = b7 ^ (b8 ^ _MASK) & b9
        s[8] = b8 ^ (b9 ^ _MASK) & b5
        s[9] = b9 ^ (b5 ^ _MASK) & b6
        s[10] = b10 ^ (b11 ^ _MASK) & b12
        s[11] = b11 ^ (b12 ^ _MASK) & b13
        s[12] = b12 ^ (b13 ^ _MASK) & b14
        s[13] = b13 ^ (b14 ^ _MASK) & b10
        s[14] = b14 ^ (b10 ^ _MASK) & b11
        s[15] = b15 ^ (b16 ^ _MASK) & b17
        s[16] = b16 ^ (b17 ^ _MASK) & b18
        s[17] = b17 ^ (b18 ^ _MASK) & b19
        s[18] = b18 ^ (b19 ^ _MASK) & b15
        s[19] = b19 ^ (b15 ^ _MASK) & b16
        s[20] = b20 ^ (b21 ^ _MASK) & b22
        s[21] = b21 ^ (b22 ^ _MASK) & b23
        s[22] = b22 ^ (b23 ^ _MASK) & b24
        s[23] = b23 ^ (b24 ^ _MASK) & b20
        s[24] = b24 ^ (b20 ^ _MASK) & b21
        s[0] ^= rc[r]


def _solve_range(args) -> int:
    """在 [start, stop) 内找 nonce 使 DeepSeekHashV1(prefix+str(nonce)) == challenge。"""
    challenge_hex, salt, expire_at, start, stop = args
    target = bytes.fromhex(challenge_hex)
    t0 = int.from_bytes(target[0:8], "little")
    t1 = int.from_bytes(target[8:16], "little")
    t2 = int.from_bytes(target[16:24], "little")
    t3 = int.from_bytes(target[24:32], "little")

    prefix = f"{salt}_{expire_at}_".encode()
    rate = 136
    s = [0] * 25
    off = 0
    while off + rate <= len(prefix):
        for i in range(17):
            s[i] ^= int.from_bytes(prefix[off + i * 8:off + i * 8 + 8], "little")
        _keccak_f23(s)
        off += rate
    tail = prefix[off:]
    tail_len = len(tail)

    for n in range(start, stop):
        num = str(n).encode()
        block = bytearray(rate)
        block[:tail_len] = tail
        total = tail_len + len(num)
        if total < rate:
            block[tail_len:total] = num
            block[total] = 0x06
            block[rate - 1] ^= 0x80
            s2 = s[:]
            for i in range(17):
                s2[i] ^= int.from_bytes(block[i * 8:i * 8 + 8], "little")
            _keccak_f23(s2)
            if s2[0] == t0 and s2[1] == t1 and s2[2] == t2 and s2[3] == t3:
                return n
        # nonce 撑破一个 block 的场景按难度上限不会出现（difficulty < 10^11 字节长度足够小），
        # 真出现时视为该区间无解，交给其他区间。
    return -1


_EXECUTOR = None


def _get_executor() -> ProcessPoolExecutor:
    global _EXECUTOR
    if _EXECUTOR is None:
        _EXECUTOR = ProcessPoolExecutor(
            max_workers=max(1, min(8, os.cpu_count() or 1)))
    return _EXECUTOR


def solve_pow(challenge_hex: str, salt: str, expire_at: int, difficulty: int) -> int:
    """难度即搜索上界（答案在 [0, difficulty) 内均匀随机）。

    单核纯 Python 约 4k hash/s，多进程按小块并行，先完成者胜；
    剩余任务 cancel 掉，正在跑的小块最多再跑几秒。
    """
    if len(challenge_hex) != 64:
        raise UpstreamError("upstream_error", f"pow challenge 长度异常：{challenge_hex[:20]}")
    workers = max(1, min(8, os.cpu_count() or 1))
    if workers <= 1 or difficulty <= 4096:
        got = _solve_range((challenge_hex, salt, expire_at, 0, difficulty))
    else:
        ex = _get_executor()
        chunk = max(256, difficulty // (workers * 4))
        futs = {
            ex.submit(_solve_range, (challenge_hex, salt, expire_at, s, min(s + chunk, difficulty)))
            for s in range(0, difficulty, chunk)
        }
        got = -1
        errors = 0
        while futs:
            done, _ = wait(futs, return_when=FIRST_COMPLETED)
            for f in done:
                futs.discard(f)
                try:
                    r = f.result()
                except Exception:
                    errors += 1
                    continue
                if r >= 0:
                    got = r
                    break
            if got >= 0:
                break
        for f in futs:
            f.cancel()
    if got < 0:
        msg = "PoW 求解失败：搜索空间内无解"
        if errors:
            msg += f"（{errors} 个求解任务异常）"
        raise UpstreamError("upstream_error", msg)
    return got


def build_pow_header(challenge: dict, answer: int) -> str:
    payload = {
        "algorithm": challenge["algorithm"],
        "challenge": challenge["challenge"],
        "salt": challenge["salt"],
        "answer": answer,
        "signature": challenge["signature"],
        "target_path": challenge.get("target_path") or COMPLETION_TARGET,
    }
    return base64.b64encode(json.dumps(payload).encode()).decode()


# ============ SSE 解析（纯逻辑，可单测） ============

VISIBLE_TYPES = {"RESPONSE", "TEMPLATE_RESPONSE"}
THINK_TYPE = "THINK"


class DeepSeekStreamParser:
    """把 DeepSeek SSE 行流重建为 (正文, 思考, 终态)。

    支持：初始化 envelope、无路径 {"v":"token"}（按官方逆向文档视为可见正文候选，
    追加到最后一个 fragment）、fragments 容器 APPEND/SET、fragments/-N/content
    APPEND、BATCH 嵌套、顶层 batch（无 p 字段）。continue 接续轮快照重放：
    相同 fragment id 以新内容覆盖，不产生重复。
    """

    def __init__(self):
        self.fragments: dict[int, dict] = {}   # id -> {"type","content"}
        self.order: list[int] = []             # fragment 展示顺序
        self.status = ""                       # 最近一次状态信号（含 AUTO_CONTINUE）
        self.message_id = 0
        self.loose_text = ""                   # 还没有任何 fragment 时的无路径 token
        self.saw_done = False

    # ---- fragment 维护 ----
    def _upsert(self, frag_id, ftype, content=""):
        if frag_id in self.fragments:
            if content:
                self.fragments[frag_id]["content"] = content  # 快照覆盖
        else:
            self.fragments[frag_id] = {"type": ftype or "RESPONSE", "content": content}
            self.order.append(frag_id)

    def _append_to(self, frag_id, text):
        if not text:
            return
        frag = self.fragments.get(frag_id)
        if frag is None:
            return
        frag["content"] += text

    def _set_status(self, value):
        value = str(value or "").strip()
        if value:
            self.status = value

    def begin_round(self):
        """continue 接续轮开始：清空上一轮状态信号（fragment 快照按 id 覆盖）。"""
        self.status = ""

    # ---- 事件处理 ----
    def feed_line(self, line: str):
        line = line.strip()
        if not line.startswith("data:"):
            return
        data = line[5:].strip()
        if not data:
            return
        if data == "[DONE]":
            self.saw_done = True
            return
        try:
            ev = json.loads(data)
        except Exception:
            return
        if not isinstance(ev, dict):
            return
        if isinstance(ev.get("response_message_id"), int):
            self.message_id = ev["response_message_id"]
        p = ev.get("p")
        o = ev.get("o")
        v = ev.get("v")
        if p is None and o is None:
            self._apply_value(v)
        else:
            self._apply_patch(str(p or ""), v, str(o or "SET"))

    @staticmethod
    def _norm_sub_path(path: str) -> str:
        """batch 子补丁的相对路径补全为 response/ 前缀。"""
        return "response/" + path if path and "/" not in path else path

    def _apply_value(self, v):
        if isinstance(v, str):
            # 无路径可见 token：追加到当前最后一个 fragment
            if self.order:
                self._append_to(self.order[-1], v)
            else:
                self.loose_text += v
            return
        if isinstance(v, list):
            for sub in v:
                if isinstance(sub, dict):
                    self._apply_patch(self._norm_sub_path(sub.get("p") or ""),
                                      sub.get("v"), str(sub.get("o") or "SET"))
            return
        if isinstance(v, dict):
            # 对象形态可见 token：{"text": "..."} / {"content": "..."}（2026-04 格式）
            if not ({"status", "fragments", "message_id", "response", "message"} & v.keys()):
                t = v.get("text") or v.get("content")
                if isinstance(t, str):
                    if self.order:
                        self._append_to(self.order[-1], t)
                    else:
                        self.loose_text += t
                    return
            # message envelope：完整的 response 状态对象（或包在 message 键里）
            resp = v.get("response") or (v.get("message") or {}).get("response") or v
            if not isinstance(resp, dict):
                return
            if isinstance(resp.get("message_id"), int):
                self.message_id = resp["message_id"]
            self._set_status(resp.get("status"))
            if resp.get("auto_continue") is True:
                self._set_status("AUTO_CONTINUE")
            frs = resp.get("fragments")
            if isinstance(frs, list):
                for frag in frs:
                    if isinstance(frag, dict):
                        self._upsert(frag.get("id"), frag.get("type"),
                                     str(frag.get("content") or ""))

    def _apply_patch(self, path: str, v, o: str):
        path = path.strip("/")
        if path in ("response/status", "status", "response/quasi_status", "quasi_status"):
            self._set_status(v)
            return
        if path in ("response/auto_continue", "auto_continue"):
            if v is True:
                self._set_status("AUTO_CONTINUE")
            return
        if path in ("response/message_id", "message_id") and isinstance(v, int):
            self.message_id = v
            return
        if path in ("response/fragments", "fragments"):
            if isinstance(v, list):
                for frag in v:
                    if isinstance(frag, dict):
                        self._upsert(frag.get("id"), frag.get("type"),
                                     str(frag.get("content") or ""))
            return
        if path.startswith("response/fragments/-") or path.startswith("fragments/-"):
            tail = path.split("/")[-2:]
            try:
                idx = int(tail[0])  # JSONPath 负索引：-1 = 最后一个
            except ValueError:
                return
            if len(tail) < 2 or tail[1] != "content":
                return  # status / results / elapsed_secs 等不参与正文
            if not self.order:
                return
            try:
                frag_id = self.order[idx]
            except IndexError:
                return
            if o == "APPEND":
                self._append_to(frag_id, str(v or ""))
            elif frag_id in self.fragments:
                self.fragments[frag_id]["content"] = str(v or "")
            return
        if path == "response" and o == "BATCH" and isinstance(v, list):
            for sub in v:
                if isinstance(sub, dict):
                    self._apply_patch(self._norm_sub_path(sub.get("p") or ""),
                                      sub.get("v"), str(sub.get("o") or "SET"))
            return

    def result(self):
        """返回 (正文, 思考, 状态)。THINK → 思考，RESPONSE/TEMPLATE_RESPONSE → 正文。"""
        text = self.loose_text
        think_parts = []
        for fid in self.order:
            frag = self.fragments[fid]
            if frag["type"] == THINK_TYPE:
                think_parts.append(frag["content"])
            elif frag["type"] in VISIBLE_TYPES:
                text += frag["content"]
        return text, "".join(think_parts), self.status

    def should_continue(self) -> bool:
        """ds2api 同款判定：只有显式 INCOMPLETE/AUTO_CONTINUE 才续写，普通 WIP 不算。"""
        return self.message_id > 0 and not self.finished() and \
            self.status.upper() in ("INCOMPLETE", "AUTO_CONTINUE")

    def finished(self) -> bool:
        return self.status.upper() in ("FINISHED", "CONTENT_FILTER")


# ============ 提示词组装（与 qwen2api 同思路：单 prompt 全量上下文） ============

def content_to_text(content) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for b in content:
            if not isinstance(b, dict):
                continue
            t = b.get("type")
            if t in ("text", "input_text", "output_text"):
                parts.append(b.get("text", ""))
            elif t == "tool_use":
                parts.append(f"[助手发起工具调用] {b.get('name')} 参数: "
                             f"{json.dumps(b.get('input', {}), ensure_ascii=False)}")
            elif t == "tool_result":
                inner = b.get("content")
                inner = content_to_text(inner) if not isinstance(inner, str) else inner
                parts.append(f"[工具执行结果]\n{inner}")
        return "\n".join(parts)
    return str(content or "")


def build_prompt(messages, system) -> str:
    lines = []
    if system:
        lines.append(f"[系统设定]\n{content_to_text(system)}")
    for m in messages or []:
        role = m.get("role")
        if role == "tool":
            lines.append(f"[工具执行结果]\n{content_to_text(m.get('content'))}")
            continue
        tag = "用户" if role == "user" else "助手"
        lines.append(f"[{tag}]\n{content_to_text(m.get('content'))}")
    lines.append("[助手]")
    return "\n\n".join(lines)


def resolve_model(model: str):
    """返回 (model_type, thinking, search)。未知名称按 flash/default 处理。"""
    base = (model or "deepseek-v4-flash").lower().strip()
    thinking, search = True, False
    if base.endswith("-nothinking"):
        thinking = False
        base = base[: -len("-nothinking")]
    if base.endswith("-search"):
        search = True
        base = base[: -len("-search")]
    mtype = {"deepseek-v4-pro": "expert", "deepseek-v4-vision": "vision"}.get(base, "default")
    return mtype, thinking, search


# ============ DeepSeek 上游 ============

def ds_headers(token: str) -> dict:
    h = dict(BASE_HEADERS)
    h["authorization"] = "Bearer " + token
    return h


def _biz(resp: dict, err: str):
    code = resp.get("code")
    biz_code = (resp.get("data") or {}).get("biz_code") if isinstance(resp.get("data"), dict) else None
    if resp.get("code") not in (0, None) or biz_code not in (0, None):
        msg = resp.get("msg") or ""
        biz_msg = ""
        data = resp.get("data")
        if isinstance(data, dict):
            biz_msg = data.get("biz_msg") or ""
        raise UpstreamError("risk_control" if "risk" in (msg + biz_msg).lower() else "upstream_error",
                            f"DeepSeek {err} 失败：{msg or biz_msg or resp}. {RATE_HINT}")
    data = resp.get("data") or {}
    return data.get("biz_data") or {}


async def _post_json(client: httpx.AsyncClient, url: str, token: str, body: dict) -> dict:
    try:
        resp = await client.post(url, headers=ds_headers(token), json=body)
    except Exception as exc:
        raise UpstreamError("upstream_error", f"DeepSeek 请求失败：{exc}. {RATE_HINT}") from exc
    if resp.status_code in (401, 403):
        raise UpstreamError("not_configured",
                            f"DeepSeek token 无效或已过期（HTTP {resp.status_code}）。"
                            "请在管理台重新「抓取登录 Token」。")
    try:
        return resp.json()
    except Exception:
        raise UpstreamError("upstream_error",
                            f"DeepSeek 返回非 JSON（HTTP {resp.status_code}）：{resp.text[:200]}") from None


async def _create_session(client: httpx.AsyncClient, token: str) -> str:
    biz = _biz(await _post_json(client, CREATE_SESSION_URL, token, {"agent": "chat"}), "创建会话")
    sess = biz.get("id") or (biz.get("chat_session") or {}).get("id")
    if not sess:
        raise UpstreamError("upstream_error", f"创建会话失败：未拿到 session id。{RATE_HINT}")
    return sess


async def _get_pow_header(client: httpx.AsyncClient, token: str, target: str = COMPLETION_TARGET) -> str:
    resp = _biz(await _post_json(client, CREATE_POW_URL, token, {"target_path": target}),
                "获取 PoW challenge")
    challenge = resp.get("challenge") or {}
    if challenge.get("algorithm") != "DeepSeekHashV1":
        raise UpstreamError("upstream_error", f"未知 PoW 算法：{challenge.get('algorithm')}")
    answer = await asyncio.to_thread(
        solve_pow, challenge["challenge"], challenge.get("salt", ""),
        int(challenge.get("expire_at") or 0), int(challenge.get("difficulty") or 144000))
    return build_pow_header(challenge, answer)


async def _parse_completion_stream(resp, parser: DeepSeekStreamParser):
    """消费一条 SSE 流，写入 parser；返回是否出现过 finish/[DONE]。"""
    async for line in resp.aiter_lines():
        parser.feed_line(line)
        if line.strip() == "data: [DONE]":
            return True
    return parser.saw_done


async def deepseek_chat(prompt: str, mtype: str, thinking: bool, search: bool):
    """产出 ('thinking', text) / ('delta', text) / ('done', text, thinking)。"""
    token = STATE["token"]
    if not token:
        raise UpstreamError("not_configured", "还没有配置 DeepSeek 登录 Token，请在管理台用浏览器登录抓取")
    payload = {
        "chat_session_id": "",
        "model_type": mtype,
        "parent_message_id": None,
        "prompt": prompt,
        "ref_file_ids": [],
        "thinking_enabled": thinking,
        "search_enabled": search,
    }
    headers = ds_headers(token)
    async with httpx.AsyncClient(timeout=httpx.Timeout(300.0, connect=15.0)) as client:
        payload["chat_session_id"] = await _create_session(client, token)
        pow_header = await _get_pow_header(client, token)
        headers["x-ds-pow-response"] = pow_header

        parser = DeepSeekStreamParser()
        for round_no in range(MAX_CONTINUE_ROUNDS + 1):
            parser.begin_round()
            url = COMPLETION_URL if round_no == 0 else CONTINUE_URL
            body = payload if round_no == 0 else {
                "chat_session_id": payload["chat_session_id"],
                "message_id": parser.message_id,
                "fallback_to_resume": True,
            }
            async with client.stream("POST", url, headers=headers, json=body) as resp:
                if resp.status_code != 200:
                    raw = (await resp.aread())[:300].decode("utf-8", "replace")
                    raise UpstreamError("upstream_error", f"DeepSeek HTTP {resp.status_code}: {raw}")
                await _parse_completion_stream(resp, parser)
            if parser.finished() or not parser.should_continue():
                break

    text, think, _ = parser.result()
    if parser.status.upper() == "CONTENT_FILTER":
        raise UpstreamError("risk_control",
                            f"DeepSeek 风控拒答（CONTENT_FILTER）.{RATE_HINT}")
    if thinking and think:
        yield "thinking", think
    if text:
        yield "delta", text
    yield "done", text, think


async def deepseek_complete(prompt: str, mtype: str, thinking: bool, search: bool):
    """聚合完整回复，返回 (正文, 思考)。空流自动重试一次。"""
    original = prompt
    text, think = "", ""
    for attempt in (0, 1):
        text, think = "", ""
        async for item in deepseek_chat(prompt, mtype, thinking, search):
            if item[0] == "thinking":
                think = item[1]
            elif item[0] == "delta":
                text += item[1]
            elif item[0] == "done":
                text, think = item[1], item[2]
        if text.strip():
            return text, think
        await asyncio.sleep(1.5)
        prompt = original + "\n\n（请直接回答用户的问题。）"
    raise UpstreamError(
        "rate_limited",
        "DeepSeek 上游连续返回空流——通常是限流或 token 失效。"
        "请：1) 降低调用频率；2) 管理台重新「抓取登录 Token」。")


# ============ OpenAI 兼容层 ============

@app.get("/")
@app.get("/healthz")
async def health():
    return {"service": "deepseek2api", "version": "1.0", "status": "ok",
            "token_configured": bool(STATE["token"])}


@app.get("/v1/models")
async def models():
    ids = sorted(set(BUILTIN_MODELS) | set(EXTRA_MODELS.keys()))
    return {"data": [{"id": i, "object": "model", "owned_by": "deepseek-web"} for i in ids]}


@app.post("/v1/chat/completions")
async def openai_chat(request: Request):
    if get_api_key(request) != API_KEY:
        return unauthorized()
    try:
        body = await request.json()
    except Exception:
        return api_error("invalid_request", "invalid json", 400)
    model = body.get("model", "deepseek-v4-flash")
    mtype, thinking, search = resolve_model(model)
    prompt = build_prompt(body.get("messages"), body.get("system"))
    cid = "chatcmpl-" + uuid.uuid4().hex[:20]

    want_stream = bool(body.get("stream"))
    try:
        if want_stream:
            return StreamingResponse(
                _stream_response(cid, model, prompt, mtype, thinking, search),
                media_type="text/event-stream",
                headers={"Cache-Control": "no-cache"})
        text, think = await deepseek_complete(prompt, mtype, thinking, search)
    except UpstreamError as e:
        return api_error(e.code, str(e))

    message = {"role": "assistant", "content": text}
    if think:
        message["reasoning_content"] = think
    return JSONResponse({
        "id": cid, "object": "chat.completion", "created": int(time.time()),
        "model": model,
        "choices": [{"index": 0, "message": message, "finish_reason": "stop"}],
        "usage": {"prompt_tokens": len(prompt) // 3,
                  "completion_tokens": (len(text) + len(think)) // 3,
                  "total_tokens": (len(prompt) + len(text)) // 3},
    })


async def _stream_response(cid: str, model: str, prompt: str, mtype: str, thinking: bool, search: bool):
    """qwen2api 同款聚合后伪流：先等完整回复，再按块吐出。"""

    def chunk(delta: dict, finish=None):
        return "data: " + json.dumps({
            "id": cid, "object": "chat.completion.chunk", "created": int(time.time()),
            "model": model,
            "choices": [{"index": 0, "delta": delta, "finish_reason": finish}],
        }, ensure_ascii=False) + "\n\n"

    text, think = "", ""
    try:
        async for item in deepseek_chat(prompt, mtype, thinking, search):
            if item[0] == "thinking":
                think = item[1]
            elif item[0] == "delta":
                text += item[1]
            elif item[0] == "done":
                text, think = item[1], item[2]
    except UpstreamError as e:
        yield chunk({"role": "assistant", "content": str(e)}, "stop")
        yield "data: [DONE]\n\n"
        return

    yield chunk({"role": "assistant"})
    if think:
        # 整段思考一次性给出（上游本身是聚合快照）
        yield chunk({"reasoning_content": think})
    for i in range(0, len(text), 200):
        yield chunk({"content": text[i:i + 200]})
        await asyncio.sleep(0.01)
    yield chunk({}, "stop")
    yield "data: [DONE]\n\n"


# ============ 管理 ============

def _check_admin(request: Request) -> bool:
    key = request.headers.get("x-admin-key") or ""
    return bool(ADMIN_KEY) and key == ADMIN_KEY


def _persist():
    data = dict(CONFIG)
    try:
        with open(CONFIG_PATH, encoding="utf-8") as f:
            disk = json.load(f)
        if isinstance(disk, dict):
            for key in ("api_key", "admin_key", "port", "min_interval_seconds", "extra_models"):
                if key in disk:
                    data[key] = disk[key]
            for key, value in disk.items():
                data.setdefault(key, value)
    except Exception:
        pass
    data["token"] = STATE["token"]
    tmp = f"{CONFIG_PATH}.{os.getpid()}.tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
        f.write("\n")
    os.replace(tmp, CONFIG_PATH)
    CONFIG.update({k: v for k, v in data.items()})


@app.post("/admin/credentials")
@app.post("/admin/cookie")
async def admin_credentials(request: Request):
    if not _check_admin(request):
        return JSONResponse({"error": "admin key required"}, status_code=401)
    try:
        body = await request.json()
    except Exception:
        return api_error("invalid_request", "invalid json", 400)
    token = (body.get("token") or "").strip()
    if not token:
        return api_error("invalid_request", "缺少 token")
    # DeepSeek 凭证：旧版 JWT（eyJ 开头）或 2026-10 起的 64 位不透明 token（base64 字符集）
    is_jwt = token.startswith("eyJ")
    b64chars = set("ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/=-_")
    is_opaque = 64 <= len(token) <= 88 and set(token) <= b64chars
    if not (is_jwt or is_opaque):
        return api_error("invalid_request", "token 不是有效的 DeepSeek 登录凭证")
    STATE["token"] = token
    CONFIG["token"] = token
    if body.get("captured_at"):
        CONFIG["credential_updated_at"] = str(body["captured_at"])
    _persist()
    return {"ok": True, "message": "token 已更新并持久化",
            "token_configured": bool(STATE["token"])}


@app.post("/admin/shutdown")
async def admin_shutdown(request: Request):
    if not _check_admin(request):
        return JSONResponse({"error": "admin key required"}, status_code=401)
    asyncio.get_running_loop().call_later(0.4, os._exit, 0)
    return {"ok": True}


if __name__ == "__main__":
    if "--check" in sys.argv:
        # 自检：已知答案向量（来自 ds2api 直接调用官方 WASM 的测试）+ 一次真实上游对话
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
            got = deepseek_hash_v1(data).hex()
            assert got == want, f"hash({data!r}) = {got}, want {want}"
        print("OK（DeepSeekHashV1 已知向量通过）")
        print("[3/3] 上游测试对话（约 5-20 秒，含 PoW）...")
        try:
            mtype, thinking, search = resolve_model("deepseek-v4-flash-nothinking")
            text, _ = asyncio.run(deepseek_complete("Reply with exactly: ok", mtype, thinking, search))
            print("      上游回复:", text[:50] or "(空)")
            print("结果: PASS")
        except UpstreamError as e:
            print(f"      [{e.code}] {e}")
            print("结果: FAIL")
            sys.exit(1)
        sys.exit(0)
    import uvicorn
    uvicorn.run(app, host="127.0.0.1", port=PORT, log_level="info")
