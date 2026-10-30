# -*- coding: utf-8 -*-
"""web4api 统一管理台后端。

启动：python manager.py
或：  python gateway/app.py
"""
from __future__ import annotations

import asyncio
import json
import os
import secrets
import sys
import threading
import time
import webbrowser
from contextlib import asynccontextmanager

import httpx
from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles

from . import config, firewall, oplog
from .browser_capture import CaptureManager
from .credentials import apply_credentials
from .processes import ServiceProcessManager
from . import proxy


processes = ServiceProcessManager()


async def _on_capture(name: str, result: dict):
    return await apply_credentials(name, result, processes, source="browser", restart=True)


capture = CaptureManager(on_capture=_on_capture)


@asynccontextmanager
async def lifespan(app: FastAPI):
    config.GATEWAY_DIR.mkdir(parents=True, exist_ok=True)
    config.BROWSER_DATA_DIR.mkdir(parents=True, exist_ok=True)
    config.load_manager_config()
    for name in config.PY_SERVICES:
        config.load_service_config(name)
    yield
    await capture.shutdown()


app = FastAPI(title="web4api 统一管理台", version="1.0.0", lifespan=lifespan)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

app.mount("/static", StaticFiles(directory=str(config.STATIC_DIR)), name="static")
app.include_router(proxy.router)


@app.middleware("http")
async def _oplog_middleware(request: Request, call_next):
    """所有 /api 下的写操作自动记入操作日志。"""
    response = await call_next(request)
    if (request.method in ("POST", "PATCH", "DELETE")
            and request.url.path.startswith("/api/")):
        oplog.log(request.url.path, method=request.method,
                  status=response.status_code)
    return response


@app.exception_handler(Exception)
async def _unhandled(request: Request, exc: Exception):
    return JSONResponse({"detail": str(exc)}, status_code=500)


# ---------------------------------------------------------------------------
# 页面
# ---------------------------------------------------------------------------


@app.get("/")
async def index():
    return FileResponse(config.STATIC_DIR / "index.html")


@app.get("/favicon.ico")
async def favicon():
    return Response(status_code=204)


@app.get("/healthz")
async def healthz():
    return {"service": "web4api-manager", "status": "ok", "time": config.now_iso()}


# ---------------------------------------------------------------------------
# 状态
# ---------------------------------------------------------------------------


async def _service_status(name: str) -> dict:
    spec = config.SERVICE_SPECS[name]
    cfg = config.load_service_config(name)
    probe = await processes.probe(name)
    port = config.service_port(name, cfg)
    fields = {field: cfg.get(field, "") for field in spec["fields"]}
    return {
        "id": name,
        "label": spec["label"],
        "short": spec["short"],
        "icon": spec["icon"],
        "accent": spec["accent"],
        "port": port,
        "base_url": f"http://127.0.0.1:{port}",
        "docs_url": f"http://127.0.0.1:{port}/docs",
        "running": probe["healthy"],
        "healthy": probe["healthy"],
        "starting": probe["starting"],
        "managed": probe["managed"],
        "pid": probe["pid"],
        "port_open": probe["port_open"],
        "version": (probe.get("info") or {}).get("version", ""),
        "cookie_configured": bool(cfg.get("cookie")),
        "token_configured": bool(cfg.get("token")),
        "cookie_len": len(cfg.get("cookie") or ""),
        "cookie_names": config.cookie_names(cfg.get("cookie") or ""),
        "chat_url": cfg.get("chat_url", ""),
        "fields": fields,
        "fields_ready": all(bool(fields.get(f)) for f in spec["fields"]),
        "api_key": cfg.get("api_key", ""),
        "models": config.service_models(name, cfg),
        "default_model": spec["default_model"],
        "min_interval_seconds": cfg.get("min_interval_seconds", spec["min_interval"]),
        "extra_models": cfg.get("extra_models", {}),
        "user_agent": cfg.get("user_agent", ""),
        "credential_updated_at": cfg.get("credential_updated_at") or cfg.get("cookie_updated_at", ""),
        "credential_source": cfg.get("credential_source", ""),
        "login_url": spec["login_url"],
        "capture": spec["capture"],
        "field_labels": spec["field_labels"],
        "log_path": str(config.service_log_path(name)),
    }


def _local_ipv4s() -> list[str]:
    """收集本机所有对外的 IPv4（过滤虚拟/代理/回环/链路本地）。
    优先 psutil（最准），没有则 socket 兜底。"""
    import socket as _socket
    ips: list[str] = []
    # 要过滤的网段（不会被其他设备访问到的虚拟/代理网卡）
    _BLOCKED_PREFIXES = (
        "127.",       # 回环
        "169.254.",   # 链路本地
        "172.19.",    # Hyper-V Default Switch（Windows 固定段）
        "198.18.",    # RFC 6815 保留给网络基准测试，TUN/代理专用
    )
    def _keep(ip: str) -> bool:
        return not any(ip.startswith(p) for p in _BLOCKED_PREFIXES)
    # --- 路径 1：psutil ---
    try:
        import psutil  # noqa: F401
        for addrs in psutil.net_if_addrs().values():
            for a in addrs:
                if getattr(a, "family", None) != 2:
                    continue
                if _keep(a.address):
                    ips.append(a.address)
    except ImportError:
        # --- 路径 2：socket.gethostbyname_ex ---
        try:
            hostname = _socket.gethostname()
            for ip in _socket.gethostbyname_ex(hostname)[2]:
                if _keep(ip):
                    ips.append(ip)
        except OSError:
            pass
    # 127.0.0.1 始终在首位，方便本地访问
    ips = sorted(set(ips))
    if "127.0.0.1" not in ips:
        ips.insert(0, "127.0.0.1")
    else:
        ips.remove("127.0.0.1")
        ips.insert(0, "127.0.0.1")
    # 排序：192.168.* → 10.* → 其余，按你常用度排列
    def _pri(ip: str) -> int:
        if ip.startswith("127."): return -1
        if ip.startswith("192.168."): return 0
        if ip.startswith("10."): return 1
        if ip.startswith("172.16.") or ip.startswith("172.17.") or ip.startswith("172.18."): return 2
        return 9
    return sorted(ips, key=lambda x: (_pri(x), x))


@app.get("/api/overview")
async def overview():
    services = await asyncio.gather(*[_service_status(n) for n in config.PY_SERVICES])
    mgr = config.load_manager_config()
    manager_port = int(mgr.get("port", 8400))
    # 所有可访问的 Base URL：127.0.0.1 + 各网卡 IP（已在 _local_ipv4s 里去重+排序）
    all_bases = [f"http://{ip}:{manager_port}" for ip in _local_ipv4s()]
    return {
        "manager": {
            "port": manager_port,
            "unified_key": mgr.get("unified_key", ""),
            "unified_base": all_bases[0],
            "all_bases": all_bases,
            "stop_children_on_exit": bool(mgr.get("stop_children_on_exit")),
        },
        "services": services,
        "firewall": await asyncio.to_thread(firewall.check, None, True),
        "capture": capture.status(),
        "browser": config.browser_status(),
        "time": config.now_iso(),
        "python": sys.version.split()[0],
    }


# ---------------------------------------------------------------------------
# 服务进程
# ---------------------------------------------------------------------------


def _require_service(name: str):
    if name not in config.SERVICE_SPECS:
        raise HTTPException(404, f"未知服务：{name}")


@app.post("/api/services/{name}/start")
async def service_start(name: str):
    _require_service(name)
    try:
        result = await processes.start(name)
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(500, str(exc)) from exc
    return {"ok": True, **result, "status": await _service_status(name)}


@app.post("/api/services/{name}/stop")
async def service_stop(name: str):
    _require_service(name)
    try:
        result = await processes.stop(name)
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(409, str(exc)) from exc
    return {"ok": True, **result, "status": await _service_status(name)}


@app.post("/api/services/{name}/restart")
async def service_restart(name: str):
    _require_service(name)
    try:
        result = await processes.restart(name)
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(500, str(exc)) from exc
    return {"ok": True, **result, "status": await _service_status(name)}


@app.post("/api/all/start")
async def all_start():
    results = await processes.start_all()
    return {"ok": all(r.get("ok") for r in results), "results": results,
            "overview": await overview()}


# ---------------------------------------------------------------------------
# 端口防火墙（wf.msc）
# ---------------------------------------------------------------------------


@app.get("/api/firewall")
async def firewall_status():
    return await asyncio.to_thread(firewall.check, None, False)


@app.post("/api/firewall/open")
async def firewall_open(body: dict | None = None):
    try:
        result = await asyncio.to_thread(firewall.open_ports, _port_selection(body))
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(500, str(exc)) from exc
    return {"ok": not result.get("warning"), **result}


@app.post("/api/firewall/close")
async def firewall_close(body: dict | None = None):
    try:
        result = await asyncio.to_thread(firewall.close_ports, _port_selection(body))
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(500, str(exc)) from exc
    return {"ok": not result.get("warning"), **result}


def _port_selection(body: dict | None):
    if not body or not body.get("ports"):
        return None
    wanted = {int(p) for p in body["ports"]}
    return [p for p in firewall.target_ports() if p["port"] in wanted]


@app.get("/api/ports/listeners")
async def ports_listeners():
    return await asyncio.to_thread(firewall.listeners)


@app.post("/api/ports/{pid}/kill")
async def ports_kill(pid: int):
    if pid == os.getpid():
        raise HTTPException(400, "这是管理台自身进程，请用「全部停止」或直接退出程序。")
    try:
        result = await asyncio.to_thread(firewall.kill_pid, pid)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(500, str(exc)) from exc
    if not result["ok"]:
        raise HTTPException(500, result.get("detail") or "结束进程失败")
    return {"ok": True, **result}


@app.get("/api/oplog")
async def oplog_tail(lines: int = 200):
    lines = max(10, min(2000, int(lines)))
    rows = oplog.tail(lines)
    text = "\n".join(
        f"{r.get('time', '')}  {r.get('method', '')} {r.get('action', '')}"
        + (f" → {r['status']}" if r.get("status") is not None else "")
        for r in rows)
    return {"name": "oplog", "path": str(oplog.LOG_PATH),
            "text": text or "（暂无操作记录）"}


@app.post("/api/all/stop")
async def all_stop():
    results = await processes.stop_all()
    return {"ok": all(r.get("ok") for r in results), "results": results,
            "overview": await overview()}


# ---------------------------------------------------------------------------
# 服务配置 / 凭证
# ---------------------------------------------------------------------------


@app.get("/api/services/{name}/config")
async def service_config(name: str):
    _require_service(name)
    cfg = config.load_service_config(name)
    spec = config.SERVICE_SPECS[name]
    return {
        "api_key": cfg.get("api_key", ""),
        "port": int(cfg.get("port", spec["port"])),
        "min_interval_seconds": cfg.get("min_interval_seconds", spec["min_interval"]),
        "extra_models": cfg.get("extra_models", {}),
        "user_agent": cfg.get("user_agent", ""),
        "chat_url": cfg.get("chat_url", ""),
        "fields": {f: cfg.get(f, "") for f in spec["fields"]},
        "cookie_configured": bool(cfg.get("cookie")),
        "cookie_names": config.cookie_names(cfg.get("cookie") or ""),
    }


@app.patch("/api/services/{name}/config")
async def service_config_patch(name: str, body: dict):
    _require_service(name)
    cfg = config.load_service_config(name)
    spec = config.SERVICE_SPECS[name]

    if "api_key" in body:
        key = str(body["api_key"]).strip()
        if not key:
            raise HTTPException(400, "API Key 不能为空")
        cfg["api_key"] = key
    if "port" in body:
        try:
            port = int(body["port"])
        except Exception as exc:
            raise HTTPException(400, "端口必须是数字") from exc
        if not (1 <= port <= 65535):
            raise HTTPException(400, "端口必须在 1~65535 之间")
        if port != int(cfg.get("port", spec["port"])):
            for other in config.PY_SERVICES:
                if other == name:
                    continue
                if config.service_port(other) == port:
                    raise HTTPException(400, f"端口 {port} 已被 {config.SERVICE_SPECS[other]['label']} 使用")
        cfg["port"] = port
    if "min_interval_seconds" in body:
        try:
            cfg["min_interval_seconds"] = max(0.0, float(body["min_interval_seconds"]))
        except Exception as exc:
            raise HTTPException(400, "最小间隔必须是数字") from exc
    if "user_agent" in body:
        cfg["user_agent"] = str(body["user_agent"]).strip()
    if "extra_models" in body:
        value = body["extra_models"]
        if not isinstance(value, dict):
            raise HTTPException(400, "extra_models 必须是对象")
        cfg["extra_models"] = {str(k).lower(): str(v) for k, v in value.items() if k and v}
    config.save_service_config(name, cfg)

    restarted = False
    status = await processes.probe(name)
    if status["healthy"]:
        try:
            await processes.restart(name)
            restarted = True
        except Exception as exc:  # noqa: BLE001
            return {"ok": True, "restarted": False,
                    "warning": f"配置已保存，但重启失败：{exc}",
                    "status": await _service_status(name)}
    return {"ok": True, "restarted": restarted, "status": await _service_status(name)}


@app.post("/api/services/{name}/credentials")
async def service_credentials(name: str, body: dict):
    _require_service(name)
    try:
        result = await apply_credentials(name, body, processes, source=body.get("source", "manual"))
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(500, str(exc)) from exc
    return {**result, "status": await _service_status(name)}


# ---------------------------------------------------------------------------
# 测试 / 日志
# ---------------------------------------------------------------------------


def _extract_text(data: dict) -> str:
    if not isinstance(data, dict):
        return str(data)
    if data.get("error"):
        err = data["error"]
        if isinstance(err, dict):
            return f"[{err.get('code', 'error')}] {err.get('message', err)}"
        return str(err)
    choices = data.get("choices") or []
    if choices:
        msg = choices[0].get("message") or {}
        text = msg.get("content") or msg.get("reasoning_content") or ""
        if text:
            return text
    content = data.get("content") or []
    if isinstance(content, list):
        parts = []
        for block in content:
            if isinstance(block, dict) and block.get("text"):
                parts.append(block["text"])
        if parts:
            return "\n".join(parts)
    return json.dumps(data, ensure_ascii=False)[:2000]


async def _run_test(base: str, key: str, model: str, prompt: str) -> dict:
    started = time.time()
    payload = {
        "model": model,
        "messages": [{"role": "user", "content": prompt}],
        "max_tokens": 300,
    }
    try:
        async with httpx.AsyncClient(timeout=180.0) as client:
            resp = await client.post(
                base.rstrip("/") + "/v1/chat/completions",
                headers={"x-api-key": key, "authorization": f"Bearer {key}"},
                json=payload,
            )
        try:
            data = resp.json()
        except Exception:
            data = {"error": {"message": resp.text[:1000]}}
        return {
            "ok": resp.status_code == 200 and not data.get("error"),
            "status": resp.status_code,
            "model": model,
            "elapsed": round(time.time() - started, 2),
            "text": _extract_text(data),
            "raw": data if not data.get("choices") and not data.get("content") else None,
        }
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": str(exc), "elapsed": round(time.time() - started, 2)}


@app.post("/api/services/{name}/test")
async def service_test(name: str, body: dict | None = None):
    _require_service(name)
    body = body or {}
    cfg = config.load_service_config(name)
    model = body.get("model") or config.SERVICE_SPECS[name]["default_model"]
    prompt = body.get("prompt") or "Reply with exactly: ok"
    status = await processes.probe(name)
    if not status["healthy"]:
        raise HTTPException(409, f"{config.SERVICE_SPECS[name]['label']} 未启动")
    port = config.service_port(name, cfg)
    return await _run_test(f"http://127.0.0.1:{port}", cfg.get("api_key", ""), model, prompt)


@app.get("/api/services/{name}/logs")
async def service_logs(name: str, lines: int = 200):
    _require_service(name)
    lines = max(10, min(2000, int(lines)))
    return {"name": name, "path": str(config.service_log_path(name)),
            "text": processes.log_tail(name, lines)}


# ---------------------------------------------------------------------------
# 浏览器登录 / 抓取
# ---------------------------------------------------------------------------


@app.get("/api/login/status")
async def login_status():
    return capture.status()


@app.post("/api/login/{name}/start")
async def login_start(name: str, body: dict | None = None):
    _require_service(name)
    body = body or {}
    try:
        result = await capture.start(name, auto_save=bool(body.get("auto_save", True)))
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(500, str(exc)) from exc
    if result.get("error"):
        raise HTTPException(409, result["error"])
    return result


@app.get("/api/login/{name}/status")
async def login_status_one(name: str):
    _require_service(name)
    return capture.status(name)


@app.post("/api/login/{name}/save")
async def login_save(name: str):
    _require_service(name)
    result = await capture.save(name)
    if not result.get("ok"):
        raise HTTPException(400, result.get("error", "保存失败"))
    return result


@app.post("/api/login/{name}/close")
async def login_close(name: str):
    _require_service(name)
    return await capture.close(name)


@app.post("/api/login/{name}/reset")
async def login_reset(name: str):
    _require_service(name)
    result = await capture.reset(name)
    if not result.get("ok"):
        raise HTTPException(500, result.get("error", "清理失败"))
    return result


# ---------------------------------------------------------------------------
# 设置
# ---------------------------------------------------------------------------


@app.get("/api/settings")
async def settings_get():
    mgr = config.load_manager_config()
    return {
        "port": int(mgr.get("port", 8400)),
        "unified_key": mgr.get("unified_key", ""),
        "auto_open_browser": bool(mgr.get("auto_open_browser", True)),
        "stop_children_on_exit": bool(mgr.get("stop_children_on_exit", False)),
        "browser": config.browser_status(),
    }


@app.patch("/api/settings")
async def settings_patch(body: dict):
    mgr = config.load_manager_config()
    updates = {}
    if "unified_key" in body:
        key = str(body["unified_key"]).strip()
        if not key:
            raise HTTPException(400, "统一 API Key 不能为空")
        updates["unified_key"] = key
    if "auto_open_browser" in body:
        updates["auto_open_browser"] = bool(body["auto_open_browser"])
    if "stop_children_on_exit" in body:
        updates["stop_children_on_exit"] = bool(body["stop_children_on_exit"])
    config.update_manager_config(**updates)
    return {"ok": True, **await settings_get()}


@app.post("/api/settings/rotate-unified-key")
async def rotate_unified_key():
    key = config.gen_key("sk-web4api")
    config.update_manager_config(unified_key=key)
    return {"ok": True, "unified_key": key}


# ---------------------------------------------------------------------------
# 启动入口
# ---------------------------------------------------------------------------


def _open_browser_later(port: int):
    def _open():
        time.sleep(1.0)
        webbrowser.open(f"http://127.0.0.1:{port}/")

    threading.Thread(target=_open, daemon=True).start()


def main(argv: list[str] | None = None):
    import argparse

    import uvicorn

    mgr = config.load_manager_config()
    parser = argparse.ArgumentParser(description="web4api 统一管理台")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=int(mgr.get("port", 8400)))
    parser.add_argument("--no-open", action="store_true")
    args = parser.parse_args(argv)

    port = args.port
    if config.port_in_use(port):
        # 已经是管理台在运行？直接打开页面
        try:
            import urllib.request
            with urllib.request.urlopen(f"http://127.0.0.1:{port}/healthz", timeout=1.5) as resp:
                data = json.loads(resp.read().decode("utf-8"))
            if data.get("service") == "web4api-manager":
                print(f"管理台已经在运行：http://127.0.0.1:{port}/")
                if not args.no_open:
                    webbrowser.open(f"http://127.0.0.1:{port}/")
                return
        except Exception:
            pass
        new_port = config.find_free_port(port + 1)
        print(f"端口 {port} 被占用，管理台改用 {new_port}")
        port = new_port
    if port != int(mgr.get("port", 8400)):
        config.update_manager_config(port=port)

    url = f"http://127.0.0.1:{port}/"
    print("=" * 58)
    print("  web4api 统一管理台")
    print(f"  管理页面 : {url}")
    print(f"  统一入口 : {url}v1")
    print("  提示     : 关闭本窗口不会自动停止已启动的服务")
    print("=" * 58)
    if not args.no_open:
        _open_browser_later(port)
    uvicorn.run(app, host=args.host, port=port, log_level="info")


if __name__ == "__main__":
    main()
