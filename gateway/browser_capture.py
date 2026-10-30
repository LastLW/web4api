# -*- coding: utf-8 -*-
"""内置浏览器登录 + 自动抓取聊天请求。

流程：
1. 管理台启动 Playwright，打开对应平台登录页（使用独立的浏览器资料目录）。
2. 用户手动登录并发送一条消息。
3. 监听 Network，匹配聊天接口后抓取完整 URL、Cookie、UA 和 URL 里的
   device_id / fp / tea_uuid / xiaomichatbot_ph 等参数。
4. 通过回调自动写入服务配置，并尽量热更新正在运行的服务。
"""
from __future__ import annotations

import asyncio
import json
import os
import shutil
import subprocess
import time
from pathlib import Path
from urllib.parse import urlsplit

from . import config


# 需要浏览器登录后从 localStorage 抓 token 的平台不走聊天请求抓包，
# 在 SERVICE_SPECS 的 capture 里声明 "mode": "localstorage" + "ls_key"。
# deepseek：密码的纯 HTTP 登录会被风控 RISK_DEVICE_DETECTED 拦截，
# 改为人工登录后取 userToken（JWT）作为服务凭证。


def looks_like_token(raw: str) -> bool:
    """DeepSeek 登录凭证两种形态：
    旧版 JWT（eyJ 开头，很长）或 2026-10 起的 64 位不透明 token（base64 字符集）。"""
    raw = (raw or "").strip()
    if not raw:
        return False
    if raw.startswith("eyJ") and len(raw) > 80:
        return True
    b64chars = set("ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/=-_")
    return 64 <= len(raw) <= 88 and set(raw) <= b64chars


def parse_ls_token(raw: str) -> str:
    """userToken 可能是 JSON {"value": "..."} 或纯字符串，统一取出凭证。"""
    raw = (raw or "").strip()
    if not raw:
        return ""
    try:
        obj = json.loads(raw)
        if isinstance(obj, dict):
            raw = str(obj.get("value") or obj.get("token") or "")
    except Exception:
        pass
    raw = raw.strip().strip('"')
    return raw if looks_like_token(raw) else ""


def _kill_stale_profile_processes(user_data: Path) -> int:
    """清理锁住浏览器资料目录的残留进程（管理台重启后上次的登录浏览器）。

    只匹配命令行里带本资料目录完整路径的 Chromium 进程，不会碰到
    用户日常使用的浏览器。返回清理数量；失败返回 0。
    """
    marker = str(user_data).lower()
    try:
        ps = (
            "Get-CimInstance Win32_Process | Where-Object { $_.CommandLine -and "
            "$_.CommandLine.ToLower().Contains('%s') } | "
            "Select-Object -ExpandProperty ProcessId" % marker.replace("'", "''")
        )
        out = subprocess.run(
            ["powershell", "-NoProfile", "-Command", ps],
            capture_output=True, text=True, timeout=20,
        )
        pids = [int(x) for x in out.stdout.split() if x.strip().isdigit()]
        mine = os.getpid()
        killed = 0
        for pid in pids:
            if pid == mine:
                continue
            try:
                os.kill(pid, 9)
                killed += 1
            except Exception:
                pass
        return killed
    except Exception:
        return 0


class CaptureSession:
    def __init__(self, provider: str):
        self.provider = provider
        self.spec = config.SERVICE_SPECS[provider]
        self.state = "starting"
        self.message = "正在启动浏览器…"
        self.result: dict | None = None
        self.error = ""
        self.warning = ""
        self.saved = False
        self.started_at = time.time()
        self.updated_at = time.time()
        self.closed = False
        self.context_closed = False
        self.context = None
        self.page = None
        self.task: asyncio.Task | None = None
        self.lock = asyncio.Lock()
        self.browser_name = ""
        self.page_url = ""
        self.auto_save = True

    def public(self) -> dict:
        active = not self.closed and self.state not in ("closed", "error")
        result = None
        if self.result:
            result = {
                "url": self.result.get("url", ""),
                "cookie_preview": self.result.get("cookie_preview", ""),
                "cookie_len": len(self.result.get("cookie", "")),
                "cookie_names": self.result.get("cookie_names", []),
                "token": self.result.get("token", ""),
                "token_preview": self.result.get("token_preview", ""),
                "fields": self.result.get("fields", {}),
                "user_agent": self.result.get("user_agent", ""),
                "captured_at": self.result.get("captured_at", ""),
                "valid": self.result.get("valid", False),
                "saved": self.saved,
            }
        return {
            "active": active,
            "provider": self.provider,
            "label": self.spec["label"],
            "state": self.state,
            "message": self.message,
            "error": self.error,
            "warning": self.warning,
            "started_at": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(self.started_at)),
            "browser": self.browser_name,
            "page_url": self.page_url,
            "auto_save": self.auto_save,
            "result": result,
            "saved": self.saved,
        }


class CaptureManager:
    def __init__(self, on_capture=None):
        self.sessions: dict[str, CaptureSession] = {}
        self.on_capture = on_capture

    @staticmethod
    def _token_mode(session: CaptureSession) -> bool:
        return session.spec.get("capture", {}).get("mode") == "localstorage"

    # ------------------------------------------------------------------
    # 对外接口
    # ------------------------------------------------------------------

    def status(self, provider: str | None = None) -> dict:
        if provider:
            session = self.sessions.get(provider)
            return session.public() if session else {"active": False, "provider": provider}
        for session in self.sessions.values():
            public = session.public()
            if public["active"]:
                return public
        return {"active": False}

    async def start(self, provider: str, auto_save: bool = True) -> dict:
        if provider not in config.SERVICE_SPECS:
            raise ValueError(f"不支持的平台：{provider}")
        current = self.sessions.get(provider)
        if current and current.public()["active"]:
            return current.public()

        for other, session in self.sessions.items():
            if other != provider and session.public()["active"]:
                return {
                    "active": False,
                    "error": f"已有「{session.spec['label']}」登录窗口正在进行，请点击下方「关闭浏览器」关闭它。",
                }

        session = CaptureSession(provider)
        session.auto_save = auto_save
        self.sessions[provider] = session
        session.task = asyncio.create_task(self._run(session))
        return session.public()

    async def save(self, provider: str) -> dict:
        session = self.sessions.get(provider)
        if not session or not session.result:
            return {"ok": False, "error": "还没有捕获到聊天请求"}
        if not session.result.get("valid"):
            return {"ok": False, "error": session.result.get("warning") or "捕获结果不完整"}
        if session.saved:
            return {"ok": True, "already_saved": True, "status": session.public()}
        await self._save_result(session)
        return {"ok": session.saved, "error": session.error, "status": session.public()}

    async def close(self, provider: str) -> dict:
        session = self.sessions.get(provider)
        if not session or not session.public()["active"]:
            # 该平台没有活跃窗口时，退而关闭当前打开的那个登录浏览器
            # （start 保证同时最多只有一个活跃会话）
            session = next((s for s in self.sessions.values() if s.public()["active"]), None)
            if not session:
                return {"active": False}
        session.closed = True
        session.state = "closed"
        session.message = "登录窗口已关闭"
        session.updated_at = time.time()
        if session.context:
            try:
                await session.context.close()
            except Exception:
                pass
        if session.task and not session.task.done():
            session.task.cancel()
            try:
                await session.task
            except (asyncio.CancelledError, Exception):
                pass
        return session.public()

    async def reset(self, provider: str) -> dict:
        """清除该平台的浏览器资料（切换账号时用）。"""
        await self.close(provider)
        folder = config.BROWSER_DATA_DIR / provider
        if not folder.exists():
            return {"ok": True, "cleared": False}
        last_error = None
        for _ in range(8):
            try:
                shutil.rmtree(folder)
                return {"ok": True, "cleared": True}
            except Exception as exc:  # noqa: BLE001
                last_error = exc
                await asyncio.sleep(0.35)
        return {"ok": False, "error": f"删除浏览器资料失败：{last_error}"}

    async def shutdown(self):
        for provider in list(self.sessions):
            try:
                await self.close(provider)
            except Exception:
                pass

    # ------------------------------------------------------------------
    # 浏览器会话
    # ------------------------------------------------------------------

    async def _run(self, session: CaptureSession):
        session.state = "starting"
        session.message = "正在启动浏览器…"
        try:
            from playwright.async_api import async_playwright
        except Exception:
            session.state = "error"
            session.error = "未安装 playwright"
            session.message = "浏览器登录需要 playwright：pip install playwright"
            session.closed = True
            return

        try:
            async with async_playwright() as playwright:
                user_data = config.BROWSER_DATA_DIR / session.provider
                user_data.mkdir(parents=True, exist_ok=True)
                await self._launch(playwright, session, user_data)
                if not session.context:
                    return

                session.context.on("close", lambda *_: setattr(session, "context_closed", True))
                if not self._token_mode(session):
                    session.context.on(
                        "request",
                        lambda request: self._schedule_capture(session, request),
                    )

                session.state = "waiting_login"
                if self._token_mode(session):
                    session.message = "请在浏览器中完成登录，登录成功后自动抓取 Token"
                else:
                    session.message = "请在浏览器中完成登录，并随便发送一条消息"
                page = session.page
                if page is None:
                    session.state = "error"
                    session.error = "浏览器没有可用页面"
                    session.message = "浏览器启动失败：没有可用页面"
                    return

                page.on("close", lambda *_: setattr(session, "context_closed", True))
                page.on("framenavigated", lambda frame: self._on_navigate(session, frame))

                try:
                    await page.goto(
                        session.spec["login_url"],
                        wait_until="domcontentloaded",
                        timeout=60000,
                    )
                except Exception:
                    # 某些站点首屏加载慢/被拦截，不影响用户手动操作
                    pass

                if self._token_mode(session):
                    await self._run_token_mode(session, page)
                    return

                ticks = 0
                while not session.closed and not session.context_closed:
                    await asyncio.sleep(0.6)
                    ticks += 1
                    if ticks % 5 != 0 or session.result:
                        continue
                    try:
                        cookies = await session.context.cookies()
                        names = {c.get("name") for c in cookies}
                        markers = session.spec["capture"]["cookie_markers"]
                        if any(marker in names for marker in markers):
                            session.state = "waiting_chat"
                            session.message = "已检测到登录态，请在网页里发送一条消息"
                    except Exception:
                        pass
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001
            session.state = "error"
            session.error = str(exc)
            session.message = f"浏览器登录失败：{exc}"
        finally:
            session.closed = True
            if session.context:
                try:
                    await session.context.close()
                except Exception:
                    pass
            if session.state not in ("error",):
                session.state = "closed"
                session.message = "登录窗口已关闭"
            session.updated_at = time.time()

    async def _launch(self, playwright, session: CaptureSession, user_data: Path):
        exe, browser_name = config.pick_browser()
        session.browser_name = browser_name
        kwargs = {
            "user_data_dir": str(user_data),
            "headless": False,
            "args": [
                "--disable-blink-features=AutomationControlled",
                "--start-maximized",
                "--no-first-run",
                "--no-default-browser-check",
            ],
            "no_viewport": True,
            "locale": "zh-CN",
            "timezone_id": "Asia/Shanghai",
            "ignore_https_errors": True,
        }
        if exe:
            kwargs["executable_path"] = exe
        try:
            session.context = await playwright.chromium.launch_persistent_context(**kwargs)
        except Exception as first:
            # 管理台重启后上个登录浏览器可能还开着并锁住资料目录：
            # 只清理命令行里带本资料目录路径的进程，不会碰用户自己的浏览器
            killed = _kill_stale_profile_processes(user_data)
            if killed:
                session.message = f"检测到上次残留的登录浏览器（{killed} 个进程），已清理并重试…"
                session.updated_at = time.time()
                await asyncio.sleep(1.0)
                try:
                    session.context = await playwright.chromium.launch_persistent_context(**kwargs)
                except Exception as retry_failed:
                    first = retry_failed
        if not session.context:
            if not exe:
                raise RuntimeError(
                    "无法启动浏览器（资料目录可能被残留进程占用）。\n"
                    "请关闭残留的登录浏览器窗口后重试，或执行 python -m playwright install chromium")
            session.message = "系统浏览器启动失败，正在尝试 Playwright 自带 Chromium…"
            kwargs.pop("executable_path", None)
            try:
                session.context = await playwright.chromium.launch_persistent_context(**kwargs)
                session.browser_name = "Playwright Chromium"
            except Exception as second:
                raise RuntimeError(
                    f"无法启动浏览器：{second}\n"
                    "请关闭残留的登录浏览器窗口后重试，"
                    "或设置环境变量 WEB2API_BROWSER_PATH 指向 msedge.exe，"
                    "或执行 python -m playwright install chromium") from second

        pages = session.context.pages
        session.page = pages[0] if pages else await session.context.new_page()

    async def _run_token_mode(self, session: CaptureSession, page):
        """轮询浏览器里的登录 Token（用户人工登录，指纹正常不被风控）。

        不依赖单一存储位置：localStorage 约定键 → 其他键 → Cookie，
        只要出现 JWT 形态（eyJ 开头）即认为登录成功；
        等待期间把页面 URL / 键名 / Cookie 名写进状态，便于排查。
        """
        ls_key = session.spec["capture"].get("ls_key", "userToken")
        js_scan = (
            "() => { const out = [];"
            " try { for (let i = 0; i < localStorage.length; i++) {"
            "  const k = localStorage.key(i); const v = localStorage.getItem(k) || '';"
            "  if (v.indexOf('eyJ') >= 0 || k.toLowerCase().indexOf('token') >= 0) out.push([k, v]); }"
            " } catch (e) {} return out; }"
        )
        ticks = 0
        while not session.closed and not session.context_closed:
            await asyncio.sleep(0.8)
            if session.result:
                continue
            ticks += 1
            token = ""
            try:
                # 1) localStorage：所有标签页/iframe，约定键优先，其余键兜底
                for pg in session.context.pages:
                    for frame in pg.frames:
                        try:
                            entries = await frame.evaluate(js_scan)
                        except Exception:
                            continue
                        for k, v in entries:
                            if k == ls_key:
                                token = parse_ls_token(v) or token
                        if not token:
                            for k, v in entries:
                                if k != ls_key:
                                    token = parse_ls_token(v) or token
                        if token:
                            break
                    if token:
                        break
                # 2) Cookie：token 形态，名字含 token 的优先
                cookies = await session.context.cookies()
                if not token:
                    for c in cookies:
                        val = c.get("value", "")
                        if looks_like_token(val) and "token" in c.get("name", "").lower():
                            token = val
                            break
                if not token:
                    for c in cookies:
                        val = c.get("value", "")
                        if looks_like_token(val):
                            token = val
                            break
                # 诊断：约 5 秒起周期性展示页面状态（只显示键名，不显示值）
                if not token and ticks % 6 == 0:
                    try:
                        url = page.url
                        keys = await page.evaluate(
                            "() => { try { return Object.keys(localStorage).slice(0, 12); }"
                            " catch (e) { return []; } }")
                        names = [c.get("name", "") for c in cookies][:10]
                        session.message = (
                            f"已打开 {url}，等待登录中…"
                            f"（localStorage 键：{', '.join(keys) or '无'}；"
                            f"Cookie：{', '.join(names) or '无'}）")
                        session.updated_at = time.time()
                    except Exception:
                        pass
            except Exception:
                continue
            if not token:
                continue
            session.result = {
                "provider": session.provider,
                "token": token,
                "token_preview": config.cookie_preview(token),
                "captured_at": config.now_iso(),
                "valid": True,
            }
            session.updated_at = time.time()
            session.state = "captured"
            session.message = "已抓取登录 Token，正在保存…"
            if session.auto_save:
                await self._save_result(session)
            else:
                session.message = "已抓取登录 Token，请点击保存"

    # ------------------------------------------------------------------
    # 抓包
    # ------------------------------------------------------------------

    def _schedule_capture(self, session: CaptureSession, request):
        if session.closed or session.saved:
            return
        try:
            url = request.url
        except Exception:
            return
        if not config.is_chat_url(session.provider, url):
            return
        asyncio.create_task(self._capture(session, request))

    def _on_navigate(self, session: CaptureSession, frame):
        try:
            if frame == session.page.main_frame:
                session.page_url = frame.url
                session.updated_at = time.time()
        except Exception:
            pass

    async def _capture(self, session: CaptureSession, request):
        async with session.lock:
            if session.saved:
                return
            url = ""
            headers = {}
            try:
                url = request.url
                headers = await request.all_headers()
            except Exception:
                url = getattr(request, "url", "") or ""

            cookie = (headers.get("cookie") or "").strip()
            if not cookie and session.context:
                cookie = await self._cookie_header(session.context, url)
            if not cookie:
                session.state = "waiting_chat"
                session.message = "已捕获聊天请求，但没有拿到 Cookie，请刷新页面后重试"
                return

            fields = config.extract_capture_fields(session.provider, url)
            ok, warning = config.validate_capture(session.provider, cookie, url)
            result = {
                "provider": session.provider,
                "url": url,
                "cookie": cookie,
                "cookie_preview": config.cookie_preview(cookie),
                "cookie_names": config.cookie_names(cookie),
                "fields": fields,
                "user_agent": headers.get("user-agent", ""),
                "captured_at": config.now_iso(),
                "valid": ok,
                "warning": warning,
            }
            session.result = result
            session.updated_at = time.time()
            session.warning = warning

            if not ok:
                session.state = "waiting_chat"
                session.message = f"已捕获请求，但凭证不完整：{warning}"
                return

            session.state = "captured"
            session.message = "已捕获完整 URL 与 Cookie，正在保存…"
            if session.auto_save:
                await self._save_result(session)
            else:
                session.message = "已捕获完整 URL 与 Cookie，请点击保存"

    async def _save_result(self, session: CaptureSession):
        if not session.result or session.saved:
            return
        try:
            if self.on_capture:
                await self.on_capture(session.provider, session.result)
            session.saved = True
            session.state = "saved"
            session.message = "凭证已自动保存，服务已更新；可以关闭浏览器了"
            session.error = ""
        except Exception as exc:  # noqa: BLE001
            session.saved = False
            session.state = "captured"
            session.error = str(exc)
            session.message = f"已捕获，但保存失败：{exc}"
        session.updated_at = time.time()

    async def _cookie_header(self, context, url: str) -> str:
        try:
            cookies = await context.cookies(url)
        except Exception:
            cookies = []
        if not cookies:
            try:
                cookies = await context.cookies()
            except Exception:
                cookies = []
        if not cookies:
            return ""
        host = (urlsplit(url).hostname or "").lower()

        def matches(domain: str) -> bool:
            domain = (domain or "").lstrip(".").lower()
            if not domain or not host:
                return True
            return host == domain or host.endswith("." + domain) or domain.endswith("." + host)

        values: dict[str, str] = {}
        for cookie in cookies:
            if not matches(cookie.get("domain", "")):
                continue
            name = cookie.get("name")
            if name:
                values[name] = cookie.get("value", "")
        return "; ".join(f"{name}={value}" for name, value in values.items())
