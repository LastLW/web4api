# -*- coding: utf-8 -*-
"""Windows 端口工具：防火墙（wf.msc）规则管理 + 本机监听进程检测。

规则统一命名 web4api-<port>。查询是只读的；放行/关闭端口需要管理员权限，
非管理员时通过 UAC 提权隐藏执行（只有 UAC 授权框会显示）。
"""
from __future__ import annotations

import ctypes
import json
import os
import subprocess
import tempfile
import time

from . import config

CACHE_SECONDS = 30
_cache = {"at": 0.0, "rules": {}, "error": ""}

_QUERY = (
    "Get-NetFirewallRule -ErrorAction SilentlyContinue | "
    "Where-Object { $_.DisplayName -like 'web4api-*' } | "
    "ForEach-Object { $pf = $_ | Get-NetFirewallPortFilter; "
    "\"{0}|{1}|{2}|{3}|{4}|{5}\" -f $_.DisplayName, $_.Enabled, "
    "$_.Direction, $_.Action, $pf.Protocol, $pf.LocalPort }"
)


def rule_name(port: int) -> str:
    return f"web4api-{int(port)}"


def is_admin() -> bool:
    try:
        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except Exception:
        return False


def target_ports() -> list[dict]:
    items = [{"id": "manager", "label": "统一管理台", "port": int(config.load_manager_config().get("port", 8400))}]
    for name in config.PY_SERVICES:
        items.append({"id": name, "label": config.SERVICE_SPECS[name]["label"],
                      "port": config.service_port(name)})
    return items


def _decode(data: bytes) -> str:
    # PowerShell/taskkill 输出随控制台代码页变化（中文系统常为 GBK），
    # 不依赖 subprocess 的 locale/UTF-8 假设，手动按序尝试解码。
    for enc in ("utf-8", "gbk"):
        try:
            return data.decode(enc)
        except UnicodeDecodeError:
            continue
    return data.decode("utf-8", "replace")


def _run(cmd: list[str], timeout: int) -> subprocess.CompletedProcess:
    flags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
    proc = subprocess.run(cmd, capture_output=True, timeout=timeout,
                          creationflags=flags)
    proc.stdout = _decode(proc.stdout or b"")
    proc.stderr = _decode(proc.stderr or b"")
    return proc


def _read_rules() -> tuple[dict, str]:
    proc = _run(["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass",
                 "-Command", _QUERY], 40)
    if proc.returncode != 0:
        return {}, (proc.stderr or proc.stdout or "PowerShell 查询防火墙失败").strip()[:300]
    rules = {}
    for line in proc.stdout.splitlines():
        parts = [p.strip() for p in line.strip().split("|")]
        if len(parts) != 6:
            continue
        name, enabled, direction, action, proto, port = parts
        rules[name.lower()] = {
            "enabled": enabled.lower() == "true",
            "direction": direction.lower(),
            "action": action.lower(),
            "protocol": proto.lower(),
            "port": port,
        }
    return rules, ""


def check(items: list[dict] | None = None, use_cache: bool = True) -> dict:
    if os.name != "nt":
        return {"available": False, "error": "仅支持 Windows", "admin": False,
                "ports": items or target_ports()}
    items = items or target_ports()
    now = time.time()
    if use_cache and now - _cache["at"] < CACHE_SECONDS:
        rules, error = _cache["rules"], _cache["error"]
    else:
        rules, error = _read_rules()
        _cache.update({"at": now, "rules": rules, "error": error})
    ports = []
    for item in items:
        port = int(item["port"])
        rule = rules.get(rule_name(port).lower())
        opened = bool(rule and rule["enabled"] and rule["direction"] == "inbound"
                      and rule["action"] == "allow" and rule["protocol"] == "tcp"
                      and str(port) in rule["port"].replace(" ", "").split(","))
        ports.append({**item, "rule": rule_name(port), "open": opened})
    return {"available": not error, "error": error, "admin": is_admin(),
            "ports": ports, "checked_at": config.now_iso()}


def _open_script(ports: list[int]) -> str:
    lines = ["$ErrorActionPreference='Stop'"]
    for port in ports:
        name = rule_name(port)
        lines.append(f"Remove-NetFirewallRule -DisplayName '{name}' -ErrorAction SilentlyContinue")
        lines.append(
            f"New-NetFirewallRule -DisplayName '{name}' -Direction Inbound -Action Allow "
            f"-Protocol TCP -LocalPort {port} -Profile Any -Enabled True | Out-Null")
    return "\n".join(lines)


def _close_script(ports: list[int]) -> str:
    lines = ["$ErrorActionPreference='Stop'"]
    for port in ports:
        lines.append(
            f"Remove-NetFirewallRule -DisplayName '{rule_name(port)}' -ErrorAction SilentlyContinue")
    return "\n".join(lines)


def _apply_script(script: str):
    """隐藏执行 PowerShell；需要提权时只弹 UAC 授权框。"""
    if is_admin():
        proc = _run(["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass",
                     "-Command", script], 120)
        if proc.returncode != 0:
            raise RuntimeError((proc.stderr or proc.stdout or "执行失败").strip()[:500])
    else:
        _run_elevated(script)


def _run_elevated(script: str):
    """非管理员：脚本写入临时文件，UAC 提权 + 隐藏窗口运行并等待结果。"""
    path = ""
    try:
        with tempfile.NamedTemporaryFile("w", suffix=".ps1", delete=False,
                                         encoding="utf-8-sig") as f:
            f.write(script)
            path = f.name
        proc = _run([
            "powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command",
            f"$p = Start-Process powershell -Verb RunAs -Wait -PassThru "
            f"-ArgumentList '-NoProfile','-ExecutionPolicy','Bypass',"
            f"'-WindowStyle','Hidden','-File','{path}'; "
            f"exit $p.ExitCode",
        ], 180)
        if proc.returncode != 0:
            raise RuntimeError(
                "需要管理员权限：UAC 授权被取消或执行失败。请重试，"
                "或以管理员身份运行管理台后再操作端口。")
    finally:
        if path:
            try:
                os.unlink(path)
            except Exception:
                pass


def _selected(items: list[dict] | None) -> list[dict]:
    if items is None:
        return target_ports()
    wanted = {int(x["port"]) for x in items}
    return [p for p in target_ports() if p["port"] in wanted]


def open_ports(items: list[dict] | None = None) -> dict:
    items = _selected(items)
    status = check(items, use_cache=False)
    closed = [p["port"] for p in status["ports"] if not p["open"]]
    if closed:
        _apply_script(_open_script(closed))
    _cache["at"] = 0.0
    after = check(items, use_cache=False)
    left = [p for p in after["ports"] if not p["open"]]
    warning = ""
    if left:
        warning = "仍有端口未开放：" + "、".join(f"{p['label']}({p['port']})" for p in left)
    return {**after, "opened": closed, "warning": warning}


def close_ports(items: list[dict] | None = None) -> dict:
    items = _selected(items)
    status = check(items, use_cache=False)
    opened = [p["port"] for p in status["ports"] if p["open"]]
    if opened:
        _apply_script(_close_script(opened))
    _cache["at"] = 0.0
    after = check(items, use_cache=False)
    left = [p for p in after["ports"] if p["open"]]
    warning = ""
    if left:
        warning = "仍有端口未关闭：" + "、".join(f"{p['label']}({p['port']})" for p in left)
    return {**after, "closed": opened, "warning": warning}


# ---------------------------------------------------------------------------
# 本机监听进程检测（含非管理台启动的旧实例）
# ---------------------------------------------------------------------------

def listeners() -> dict:
    """列出占用本服务端口的监听进程，标注是否管理台自身 / 是否本项目进程。"""
    targets = target_ports()
    if os.name != "nt":
        return {"available": False, "error": "仅支持 Windows", "items": targets}
    ports = [p["port"] for p in targets]
    script = (
        "$ports = @(" + ",".join(str(x) for x in ports) + "); "
        "$items = @(); "
        "Get-NetTCPConnection -State Listen -ErrorAction SilentlyContinue | "
        "Where-Object { $ports -contains $_.LocalPort } | ForEach-Object { "
        "$p = Get-CimInstance Win32_Process -Filter ('ProcessId=' + $_.OwningProcess) "
        "-ErrorAction SilentlyContinue; "
        "$items += [pscustomobject]@{ port=$_.LocalPort; pid=$_.OwningProcess; "
        "name=$p.Name; cmd=$p.CommandLine } }; "
        "ConvertTo-Json -InputObject @($items) -Depth 3 -Compress"
    )
    proc = _run(["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass",
                 "-Command", script], 40)
    raw = (proc.stdout or "").strip() if proc.returncode == 0 else ""
    if not raw.startswith("["):
        raw = f"[{raw}]" if raw else "[]"
    try:
        rows = json.loads(raw)
    except Exception:
        rows = []
    label_of = {p["port"]: p["label"] for p in targets}
    out, seen = [], set()
    root = str(config.ROOT).lower()
    for row in rows:
        try:
            pid, port = int(row.get("pid") or 0), int(row.get("port") or 0)
        except Exception:
            continue
        if not pid or (port, pid) in seen:
            continue
        seen.add((port, pid))
        cmd = str(row.get("cmd") or "")
        out.append({
            "port": port, "pid": pid,
            "name": str(row.get("name") or ""),
            "cmd": cmd,
            "label": label_of.get(port, "未知端口"),
            "self": pid == os.getpid(),
            "ours": root in cmd.lower(),
        })
    out.sort(key=lambda x: x["port"])
    return {"available": True, "error": "", "items": out}


def kill_pid(pid: int) -> dict:
    """结束指定进程树（taskkill /T /F），用于关闭占用端口的外部/旧实例。"""
    pid = int(pid)
    if pid <= 4:
        raise ValueError("非法进程 ID")
    proc = _run(["taskkill", "/PID", str(pid), "/T", "/F"], 30)
    return {"ok": proc.returncode == 0, "pid": pid,
            "detail": (proc.stdout or proc.stderr or "").strip()[:200]}
