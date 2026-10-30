# -*- coding: utf-8 -*-
"""服务进程管理：启动 / 停止 / 健康检查 / 日志。

管理台启动的进程会写入各自目录下的 service.log，
关闭管理台时默认不杀子进程，方便客户端持续使用；可在页面里一键全部停止。
"""
from __future__ import annotations

import asyncio
import os
import subprocess
import sys
import time
from pathlib import Path

import httpx

from . import config


class ServiceProcessManager:
    def __init__(self):
        self._procs: dict[str, subprocess.Popen] = {}
        self._started_at: dict[str, float] = {}
        self._locks: dict[str, asyncio.Lock] = {}

    # ------------------------------------------------------------------
    # 探测
    # ------------------------------------------------------------------

    def _lock(self, name: str) -> asyncio.Lock:
        if name not in self._locks:
            self._locks[name] = asyncio.Lock()
        return self._locks[name]

    def _managed_proc(self, name: str) -> subprocess.Popen | None:
        proc = self._procs.get(name)
        if proc and proc.poll() is None:
            return proc
        return None

    async def probe(self, name: str) -> dict:
        cfg = config.load_service_config(name)
        port = config.service_port(name, cfg)
        url = f"http://127.0.0.1:{port}/"

        proc = self._managed_proc(name)
        started = self._started_at.get(name, 0)
        info = {}
        healthy = False
        error = ""
        try:
            async with httpx.AsyncClient(timeout=httpx.Timeout(2.0, connect=0.7)) as client:
                resp = await client.get(url)
            healthy = resp.status_code < 500
            if healthy:
                try:
                    data = resp.json()
                    if isinstance(data, dict):
                        info = data
                except Exception:
                    info = {}
        except Exception as exc:  # noqa: BLE001
            error = str(exc)

        return {
            "running": healthy,
            "healthy": healthy,
            "port_open": config.port_in_use(port),
            "managed": proc is not None,
            "pid": proc.pid if proc else None,
            "starting": bool(proc and not healthy and time.time() - started < 35),
            "port": port,
            "info": info,
            "error": error,
        }

    # ------------------------------------------------------------------
    # 启动 / 停止
    # ------------------------------------------------------------------

    def _spawn_env(self, name: str) -> dict:
        env = os.environ.copy()
        env["PYTHONUNBUFFERED"] = "1"
        env["PYTHONIOENCODING"] = "utf-8"
        return env

    def _spawn_command(self, name: str) -> tuple[list[str], Path]:
        script = config.service_script(name)
        if not script.exists():
            raise RuntimeError(f"未找到服务脚本：{script}")
        return [sys.executable, str(script)], script.parent

    async def start(self, name: str) -> dict:
        async with self._lock(name):
            current = await self.probe(name)
            if current["healthy"]:
                return {"ok": True, "already_running": True, "status": current}

            cmd, cwd = self._spawn_command(name)
            config.load_service_config(name)
            log_path = config.service_log_path(name)
            log_path.parent.mkdir(parents=True, exist_ok=True)
            log_file = open(log_path, "ab", buffering=0)

            flags = 0
            if os.name == "nt":
                flags = subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.CREATE_NO_WINDOW
            try:
                proc = subprocess.Popen(
                    cmd,
                    cwd=str(cwd),
                    env=self._spawn_env(name),
                    stdin=subprocess.DEVNULL,
                    stdout=log_file,
                    stderr=subprocess.STDOUT,
                    creationflags=flags,
                )
            except Exception:
                log_file.close()
                raise
            finally:
                try:
                    log_file.close()
                except Exception:
                    pass

            self._procs[name] = proc
            self._started_at[name] = time.time()

            deadline = time.time() + 22
            while time.time() < deadline:
                if proc.poll() is not None:
                    tail = self.log_tail(name, 60)
                    self._procs.pop(name, None)
                    raise RuntimeError(
                        f"启动失败（退出码 {proc.returncode}）。\n\n{tail}".strip())
                status = await self.probe(name)
                if status["healthy"]:
                    return {"ok": True, "status": status, "log": str(log_path)}
                await asyncio.sleep(0.45)

            status = await self.probe(name)
            return {
                "ok": bool(status["starting"]),
                "starting": status["starting"],
                "warning": "进程已启动，但健康检查尚未通过，请稍后刷新或查看日志。",
                "status": status,
                "log": str(log_path),
            }

    def _kill_tree(self, proc: subprocess.Popen):
        if os.name == "nt":
            subprocess.run(
                ["taskkill", "/PID", str(proc.pid), "/T", "/F"],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                creationflags=subprocess.CREATE_NO_WINDOW,
            )
        else:
            try:
                proc.terminate()
            except Exception:
                pass

    async def _external_shutdown(self, name: str) -> bool:
        """对不是管理台启动的服务，调用其 /admin/shutdown 接口。"""
        cfg = config.load_service_config(name)
        admin_key = cfg.get("admin_key", "")
        if not admin_key:
            return False
        port = config.service_port(name, cfg)
        try:
            async with httpx.AsyncClient(timeout=3.0) as client:
                await client.post(
                    f"http://127.0.0.1:{port}/admin/shutdown",
                    headers={"x-admin-key": admin_key},
                )
        except Exception:
            return False
        for _ in range(12):
            await asyncio.sleep(0.35)
            if not (await self.probe(name))["healthy"]:
                return True
        return False

    async def stop(self, name: str) -> dict:
        async with self._lock(name):
            proc = self._managed_proc(name)
            if proc:
                self._kill_tree(proc)
                try:
                    await asyncio.to_thread(proc.wait, 5)
                except Exception:
                    pass
                self._procs.pop(name, None)
                self._started_at.pop(name, None)
                return {"ok": True, "stopped": True, "status": await self.probe(name)}

            if await self._external_shutdown(name):
                return {"ok": True, "stopped": True, "external": True,
                        "status": await self.probe(name)}

            status = await self.probe(name)
            if not status["healthy"] and not status["port_open"]:
                return {"ok": True, "already_stopped": True, "status": status}
            raise RuntimeError(
                "该服务不是由管理台启动的，无法直接停止。"
                "请在启动它的终端按 Ctrl+C，或改用管理台重新启动。")

    async def restart(self, name: str) -> dict:
        await self.stop(name)
        await asyncio.sleep(0.6)
        return await self.start(name)

    async def start_all(self) -> list[dict]:
        """一键启动：只拉起已配置好凭证的服务，未配置的跳过。"""
        results = []
        for name in config.PY_SERVICES:
            if not config.service_configured(name):
                results.append({"name": name, "ok": True, "skipped": True,
                                "reason": "未配置 Cookie / 登录 Token"})
                continue
            try:
                results.append({"name": name, **await self.start(name)})
            except Exception as exc:  # noqa: BLE001
                results.append({"name": name, "ok": False, "error": str(exc)})
        return results

    async def stop_all(self) -> list[dict]:
        results = []
        for name in config.PY_SERVICES:
            try:
                results.append({"name": name, **await self.stop(name)})
            except Exception as exc:  # noqa: BLE001
                results.append({"name": name, "ok": False, "error": str(exc)})
        return results

    # ------------------------------------------------------------------
    # 日志
    # ------------------------------------------------------------------

    def log_tail(self, name: str, lines: int = 200) -> str:
        path = config.service_log_path(name)
        if not path.exists():
            return ""
        try:
            with open(path, "rb") as f:
                f.seek(0, os.SEEK_END)
                size = f.tell()
                block = min(size, 256 * 1024)
                f.seek(size - block)
                raw = f.read()
            text = raw.decode("utf-8", "replace")
            rows = text.splitlines()
            return "\n".join(rows[-max(1, int(lines)):])
        except Exception as exc:  # noqa: BLE001
            return f"读取日志失败：{exc}"

    async def shutdown_managed(self):
        for name in list(self._procs):
            try:
                await self.stop(name)
            except Exception:
                pass
