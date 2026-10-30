# -*- coding: utf-8 -*-
"""web2api 统一管理台 —— 配置、路径与平台元数据。

本模块不依赖 FastAPI，方便被管理台、浏览器抓包和测试共用。
"""
from __future__ import annotations

import json
import os
import secrets
import socket
import time
import uuid
from pathlib import Path
from urllib.parse import parse_qsl, urlsplit, urlunsplit

ROOT = Path(__file__).resolve().parent.parent
GATEWAY_DIR = ROOT / "gateway"
STATIC_DIR = GATEWAY_DIR / "static"
SERVICES_DIR = ROOT / "services"
MANAGER_CONFIG_PATH = GATEWAY_DIR / "config.json"
BROWSER_DATA_DIR = GATEWAY_DIR / "browser_data"

# ---------------------------------------------------------------------------
# 平台注册表
# ---------------------------------------------------------------------------

SERVICE_SPECS = {
    "qwen": {
        "label": "通义千问",
        "short": "Qwen",
        "icon": "千",
        "accent": "#7c6cff",
        "dir": "qwen2api",
        "script": "qwen2api.py",
        "port": 8301,
        "default_model": "qwen3.7",
        "models": ["qwen3.7", "qwen3.8-max", "qwen3.7-max", "qwen3.6-flash"],
        "login_url": "https://www.qianwen.com/",
        "capture": {
            "hosts": ["qianwen.com"],
            "paths": ["/api/v2/chat", "/api/v1/chat"],
            "cookie_markers": ["tongyi_sso_ticket"],
            "instructions": [
                "在弹出的浏览器窗口里登录通义千问（扫码或手机号均可）",
                "登录后随便发送一条消息，例如“你好”",
                "发送后会自动捕获聊天请求的完整 URL 与 Cookie",
            ],
        },
        "fields": ["device_id"],
        "field_labels": {"device_id": "设备 ID（URL 参数 ut）"},
        "min_interval": 2.0,
    },
    "doubao": {
        "label": "豆包",
        "short": "Doubao",
        "icon": "豆",
        "accent": "#2fb7ff",
        "dir": "doubao2api",
        "script": "doubao2api.py",
        "port": 8302,
        "default_model": "doubao-web",
        "models": ["doubao-web", "doubao-web-expert"],
        "login_url": "https://www.doubao.com/chat/",
        "capture": {
            "hosts": ["doubao.com"],
            "paths": ["/chat/completion"],
            "cookie_markers": ["sessionid", "sessionid_ss"],
            "instructions": [
                "在弹出的浏览器窗口里登录豆包（扫码或手机号均可）",
                "登录后随便发送一条消息，例如“你好”",
                "发送后会自动捕获聊天请求的完整 URL 与 Cookie",
            ],
        },
        "fields": ["device_id", "fp", "tea_uuid"],
        "field_labels": {
            "device_id": "设备 ID（URL 参数 device_id）",
            "fp": "浏览器指纹（URL 参数 fp）",
            "tea_uuid": "Tea UUID（URL 参数 tea_uuid）",
        },
        "min_interval": 1.0,
    },
    "mimo": {
        "label": "小米 MiMo",
        "short": "MiMo",
        "icon": "米",
        "accent": "#ff8a3d",
        "dir": "mimo2api",
        "script": "mimo2api.py",
        "port": 8303,
        "default_model": "mimo-v2.5-pro",
        "models": ["mimo-v2.5-pro", "mimo-v2.5", "mimo-web"],
        "login_url": "https://aistudio.xiaomimimo.com/",
        "capture": {
            "hosts": ["xiaomimimo.com"],
            "paths": ["/open-apis/bot/chat"],
            "cookie_markers": ["serviceToken"],
            "instructions": [
                "在弹出的浏览器窗口里登录小米 MiMo Studio",
                "登录后随便发送一条消息，例如“你好”",
                "发送后会自动捕获聊天请求的完整 URL 与 Cookie",
            ],
        },
        "fields": ["xiaomichatbot_ph"],
        "field_labels": {"xiaomichatbot_ph": "会话标识（URL 参数 xiaomichatbot_ph）"},
        "min_interval": 1.0,
    },
    "deepseek": {
        "label": "DeepSeek",
        "short": "DeepSeek",
        "icon": "DS",
        "accent": "#35d0a0",
        "dir": "deepseek2api",
        "script": "deepseek2api.py",
        "port": 8304,
        "default_model": "deepseek-v4-flash",
        "models": [
            "deepseek-v4-flash",
            "deepseek-v4-pro",
            "deepseek-v4-flash-nothinking",
            "deepseek-v4-pro-nothinking",
        ],
        "login_url": "https://chat.deepseek.com/",
        "capture": {
            "hosts": ["deepseek.com"],
            "paths": [],
            "cookie_markers": [],
            # localstorage 模式：不抓聊天请求，登录后直接读 localStorage 里的 userToken
            "mode": "localstorage",
            "ls_key": "userToken",
            "instructions": [
                "在弹出的浏览器窗口里登录 chat.deepseek.com（手机号/邮箱/验证码均可）",
                "登录成功后自动抓取登录 Token，无需发送消息",
                "Token 一般 30 天左右过期，过期后重新抓一次即可",
            ],
        },
        "fields": [],
        "field_labels": {},
        "min_interval": 2.0,
    },
}

PY_SERVICES = tuple(SERVICE_SPECS)

# ---------------------------------------------------------------------------
# 基础工具
# ---------------------------------------------------------------------------


def now_iso() -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S")


def gen_key(prefix: str) -> str:
    return f"{prefix}-{secrets.token_hex(20)}"


def read_json(path: Path, default=None):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return default


def write_json(path: Path, data: dict):
    """原子写入，避免服务/管理台同时读写时读到半个文件。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f"{path.name}.{os.getpid()}.{secrets.token_hex(4)}.tmp")
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
            f.write("\n")
        os.replace(tmp, path)
    finally:
        try:
            if tmp.exists():
                tmp.unlink()
        except Exception:
            pass


def port_in_use(port: int, host: str = "127.0.0.1") -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.settimeout(0.4)
        return s.connect_ex((host, int(port))) == 0


def find_free_port(start: int = 8400, limit: int = 50) -> int:
    for port in range(start, start + limit):
        if not port_in_use(port):
            return port
    raise RuntimeError("找不到空闲端口")


# ---------------------------------------------------------------------------
# 管理台配置
# ---------------------------------------------------------------------------


def load_manager_config(create: bool = True) -> dict:
    cfg = read_json(MANAGER_CONFIG_PATH, {}) if MANAGER_CONFIG_PATH.exists() else {}
    if not isinstance(cfg, dict):
        cfg = {}
    changed = False

    def ensure(key, value):
        nonlocal changed
        if key not in cfg or cfg[key] in (None, ""):
            cfg[key] = value
            changed = True

    ensure("port", 8400)
    ensure("unified_key", gen_key("sk-web2api"))
    ensure("auto_open_browser", True)
    ensure("stop_children_on_exit", False)
    if changed and create:
        write_json(MANAGER_CONFIG_PATH, cfg)
    return cfg


def update_manager_config(**updates) -> dict:
    cfg = load_manager_config()
    cfg.update({k: v for k, v in updates.items() if v is not None})
    write_json(MANAGER_CONFIG_PATH, cfg)
    return cfg


# ---------------------------------------------------------------------------
# 三个 Python 服务配置
# ---------------------------------------------------------------------------


def service_dir(name: str) -> Path:
    return SERVICES_DIR / SERVICE_SPECS[name]["dir"]


def service_script(name: str) -> Path:
    return service_dir(name) / SERVICE_SPECS[name]["script"]


def service_config_path(name: str) -> Path:
    return service_dir(name) / "config.json"


def service_log_path(name: str) -> Path:
    return service_dir(name) / "service.log"


def _default_service_config(name: str) -> dict:
    spec = SERVICE_SPECS[name]
    cfg = {
        "cookie": "",
        "api_key": gen_key(f"sk-{name}"),
        "admin_key": secrets.token_hex(16),
        "port": spec["port"],
        "min_interval_seconds": spec["min_interval"],
        "extra_models": {},
        "credential_updated_at": "",
        "credential_source": "",
    }
    if name == "qwen":
        cfg["device_id"] = str(uuid.uuid4())
    elif name == "doubao":
        cfg.update({
            "device_id": "",
            "fp": "",
            "tea_uuid": "",
            "bot_id": "7338286299411103781",
        })
    elif name == "mimo":
        cfg["xiaomichatbot_ph"] = ""
    elif name == "deepseek":
        cfg["token"] = ""
    return cfg


def load_service_config(name: str, create: bool = True) -> dict:
    path = service_config_path(name)
    existed = path.exists()
    cfg = read_json(path, None) if existed else None
    changed = False
    if not isinstance(cfg, dict):
        if not create:
            return {}
        if existed:
            # 文件存在但暂时读不出来（例如服务正在写配置）：先留底，
            # 绝不静默覆盖用户已有的 Cookie / API Key。
            backup = path.with_name(
                f"{path.name}.corrupt-{time.strftime('%Y%m%d-%H%M%S')}")
            try:
                os.replace(path, backup)
            except Exception:
                pass
        cfg = _default_service_config(name)
        changed = True

    def ensure(key, value):
        nonlocal changed
        if key not in cfg or cfg[key] is None:
            cfg[key] = value
            changed = True

    ensure("api_key", gen_key(f"sk-{name}"))
    ensure("admin_key", secrets.token_hex(16))
    ensure("port", SERVICE_SPECS[name]["port"])
    ensure("cookie", "")
    ensure("min_interval_seconds", SERVICE_SPECS[name]["min_interval"])
    ensure("extra_models", {})
    if name == "qwen":
        ensure("device_id", str(uuid.uuid4()))
    elif name == "doubao":
        ensure("device_id", "")
        ensure("fp", "")
        ensure("tea_uuid", "")
        ensure("bot_id", "7338286299411103781")
    elif name == "mimo":
        ensure("xiaomichatbot_ph", "")
    elif name == "deepseek":
        ensure("token", "")

    if changed and create:
        write_json(path, cfg)
    return cfg


def save_service_config(name: str, cfg: dict):
    write_json(service_config_path(name), cfg)


def service_port(name: str, cfg: dict | None = None) -> int:
    cfg = cfg or load_service_config(name)
    return int(cfg.get("port") or SERVICE_SPECS[name]["port"])


def service_configured(name: str) -> bool:
    """是否已配置好可用凭证（一键启动只拉起这类服务）。"""
    spec = SERVICE_SPECS[name]
    cfg = load_service_config(name, create=False)
    if spec.get("capture", {}).get("mode") == "localstorage":
        return bool(cfg.get("token"))  # DeepSeek 走登录 Token，不走 Cookie
    return bool(cfg.get("cookie")) and all(cfg.get(f) for f in spec["fields"])


def service_models(name: str, cfg: dict | None = None) -> list[str]:
    cfg = cfg or load_service_config(name)
    models = list(SERVICE_SPECS[name]["models"])
    for extra in (cfg.get("extra_models") or {}):
        if extra not in models:
            models.append(extra)
    return models


# ---------------------------------------------------------------------------
# URL / Cookie 解析（纯函数，可单测）
# ---------------------------------------------------------------------------


def extract_capture_fields(name: str, url: str) -> dict:
    try:
        query = dict(parse_qsl(urlsplit(url).query, keep_blank_values=True))
    except Exception:
        return {}
    wanted = {
        "qwen": {"device_id": "ut"},
        "doubao": {"device_id": "device_id", "fp": "fp", "tea_uuid": "tea_uuid"},
        "mimo": {"xiaomichatbot_ph": "xiaomichatbot_ph"},
    }.get(name, {})
    return {field: query[param] for field, param in wanted.items() if query.get(param)}


def is_chat_url(name: str, url: str) -> bool:
    if not url:
        return False
    try:
        parts = urlsplit(url)
    except Exception:
        return False
    if parts.scheme not in ("http", "https"):
        return False
    capture = SERVICE_SPECS[name]["capture"]
    host = (parts.hostname or "").lower()
    path = parts.path or ""
    host_ok = any(host == h or host.endswith("." + h) for h in capture["hosts"])
    path_ok = any(p in path for p in capture["paths"])
    return host_ok and path_ok


def validate_cookie(name: str, cookie: str) -> tuple[bool, str]:
    cookie = (cookie or "").strip()
    if not cookie:
        return False, "Cookie 为空"
    if name == "qwen":
        if "tongyi_sso_ticket" not in cookie:
            return False, "Cookie 里缺少 tongyi_sso_ticket，请确认已在页面登录成功"
        return True, ""
    if name == "doubao":
        if len(cookie) < 50:
            return False, "Cookie 太短，可能没有复制完整"
        if "sessionid" not in cookie:
            return False, "Cookie 里缺少 sessionid，请确认已登录并发送过消息"
        return True, ""
    if name == "mimo":
        if "serviceToken" not in cookie and len(cookie) < 80:
            return False, "Cookie 里缺少 serviceToken，请确认已登录小米 MiMo Studio"
        return True, ""
    return True, ""


def validate_capture(name: str, cookie: str, url: str) -> tuple[bool, str]:
    ok, msg = validate_cookie(name, cookie)
    if not ok:
        return False, msg
    if not is_chat_url(name, url):
        return False, f"URL 不是 {SERVICE_SPECS[name]['label']} 的聊天请求地址"
    fields = extract_capture_fields(name, url)
    missing = [f for f in SERVICE_SPECS[name]["fields"] if not fields.get(f)]
    if missing:
        labels = SERVICE_SPECS[name]["field_labels"]
        return False, "URL 里缺少参数：" + "、".join(labels.get(f, f) for f in missing)
    return True, ""


def cookie_preview(cookie: str, head: int = 42, tail: int = 16) -> str:
    cookie = cookie or ""
    if len(cookie) <= head + tail + 5:
        return cookie
    return f"{cookie[:head]}…{cookie[-tail:]}"


def cookie_names(cookie: str) -> list[str]:
    names = []
    for part in (cookie or "").split(";"):
        part = part.strip()
        if not part or "=" not in part:
            continue
        name = part.split("=", 1)[0].strip()
        if name:
            names.append(name)
    return names


# ---------------------------------------------------------------------------
# 模型路由 / 浏览器探测
# ---------------------------------------------------------------------------


def model_to_service(model: str | None) -> str:
    """按模型名路由到服务；未匹配返回空串（调用方负责报错）。"""
    m = (model or "").strip().lower()
    if m.startswith("qwen") or m.startswith("qw-"):
        return "qwen"
    if m.startswith("doubao") or m.startswith("db-"):
        return "doubao"
    if m.startswith("mimo"):
        return "mimo"
    if m.startswith("deepseek"):
        return "deepseek"
    return ""


def browser_candidates() -> list[dict]:
    """返回本机可用的 Chromium 内核浏览器，优先 Edge/Chrome。"""
    env_path = os.environ.get("WEB2API_BROWSER_PATH", "").strip()
    candidates = []
    if env_path:
        candidates.append({"name": "自定义浏览器", "path": env_path})
    program_files = os.environ.get("ProgramFiles", r"C:\Program Files")
    program_files_x86 = os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)")
    local_appdata = os.environ.get("LOCALAPPDATA", "")
    candidates.extend([
        {"name": "Microsoft Edge", "path": os.path.join(program_files, r"Microsoft\Edge\Application\msedge.exe")},
        {"name": "Microsoft Edge", "path": os.path.join(program_files_x86, r"Microsoft\Edge\Application\msedge.exe")},
        {"name": "Google Chrome", "path": os.path.join(program_files, r"Google\Chrome\Application\chrome.exe")},
        {"name": "Google Chrome", "path": os.path.join(program_files_x86, r"Google\Chrome\Application\chrome.exe")},
        {"name": "Google Chrome", "path": os.path.join(local_appdata, r"Google\Chrome\Application\chrome.exe")},
        {"name": "Microsoft Edge", "path": os.path.join(local_appdata, r"Microsoft\Edge\Application\msedge.exe")},
    ])
    seen = set()
    result = []
    for item in candidates:
        path = item["path"]
        if not path or path in seen:
            continue
        seen.add(path)
        if os.path.isfile(path):
            result.append(item)
    return result


def pick_browser() -> tuple[str | None, str]:
    """返回 (可执行文件路径, 展示名)；没有系统浏览器时返回 (None, 'Playwright Chromium')。"""
    items = browser_candidates()
    if items:
        return items[0]["path"], items[0]["name"]
    return None, "Playwright Chromium"


def browser_status() -> dict:
    try:
        import playwright  # noqa: F401
        has_playwright = True
    except Exception:
        has_playwright = False
    items = browser_candidates()
    if not has_playwright:
        hint = "pip install playwright && python -m playwright install chromium"
    elif not items:
        hint = "未检测到 Edge/Chrome，将使用 Playwright 自带 Chromium（若未安装请执行 python -m playwright install chromium）"
    else:
        hint = ""
    return {
        "playwright": has_playwright,
        "browsers": items,
        "default": items[0] if items else None,
        "install_hint": hint,
    }
