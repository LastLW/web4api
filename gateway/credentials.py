# -*- coding: utf-8 -*-
"""把浏览器抓到的凭证写入服务配置，并尽量热更新正在运行的进程。"""
from __future__ import annotations

import httpx

from . import config


async def apply_credentials(
    name: str,
    data: dict,
    manager,
    source: str = "manual",
    restart: bool = True,
) -> dict:
    if name not in config.SERVICE_SPECS:
        raise ValueError(f"不支持的服务：{name}")
    spec = config.SERVICE_SPECS[name]
    cfg = config.load_service_config(name)

    token = (data.get("token") or "").strip()
    cookie = (data.get("cookie") or "").strip()
    url = (data.get("chat_url") or data.get("url") or "").strip()
    token_mode = spec.get("capture", {}).get("mode") == "localstorage"

    if token_mode:
        if not token:
            raise ValueError("没有抓到登录 Token")
        from .browser_capture import looks_like_token
        if not looks_like_token(token):
            raise ValueError("Token 不是有效的 DeepSeek 登录凭证（JWT 或 64 位 token）")
        cfg["token"] = token
        cfg["token_updated_at"] = data.get("captured_at") or config.now_iso()
    else:
        if not cookie and not url and not any(data.get(f) for f in spec["fields"]):
            raise ValueError("没有可保存的内容：请提供 Cookie 或聊天请求 URL")

        if cookie:
            ok, msg = config.validate_cookie(name, cookie)
            if not ok:
                raise ValueError(msg)
            cfg["cookie"] = cookie
            cfg["cookie_updated_at"] = data.get("captured_at") or config.now_iso()

        for field in spec["fields"]:
            value = data.get(field)
            if value:
                cfg[field] = str(value).strip()

        if url:
            if not config.is_chat_url(name, url):
                raise ValueError(
                    f"URL 不是「{spec['label']}」的聊天请求地址，请确认复制的是 "
                    f"{' / '.join(spec['capture']['paths'])} 请求")
            cfg["chat_url"] = url
            # URL 是请求的真实参数来源，优先级高于表单里的旧值。
            for field, value in config.extract_capture_fields(name, url).items():
                cfg[field] = value

    if data.get("user_agent"):
        cfg["user_agent"] = str(data["user_agent"]).strip()

    cfg["credential_source"] = source
    cfg["credential_updated_at"] = data.get("captured_at") or config.now_iso()
    config.save_service_config(name, cfg)

    warnings = []
    missing = [f for f in spec["fields"] if not cfg.get(f)]
    if missing:
        labels = spec["field_labels"]
        warnings.append(
            "以下参数尚未获取：" + "、".join(labels.get(f, f) for f in missing)
            + "。可重新用浏览器登录并发送一条消息自动补齐。")

    status = await manager.probe(name)
    hot_updated = False
    restarted = False
    if status["healthy"]:
        port = config.service_port(name, cfg)
        payload = {
            "cookie": cookie or None,
            "token": token or None,
            "chat_url": url or None,
            "user_agent": data.get("user_agent") or None,
            "captured_at": cfg.get("credential_updated_at"),
            **{f: cfg.get(f) or None for f in spec["fields"]},
        }
        payload = {k: v for k, v in payload.items() if v is not None}
        try:
            async with httpx.AsyncClient(timeout=8.0) as client:
                resp = await client.post(
                    f"http://127.0.0.1:{port}/admin/credentials",
                    headers={"x-admin-key": cfg.get("admin_key", "")},
                    json=payload,
                )
            hot_updated = resp.status_code == 200
        except Exception:
            hot_updated = False
        if hot_updated:
            # 服务热更新时会原子重写自己的 config.json；重新读回后再补写
            # 管理台专属字段，保证 credential_source 等元数据不会丢失。
            try:
                disk_cfg = config.load_service_config(name)
                disk_cfg["credential_source"] = source
                disk_cfg["credential_updated_at"] = cfg.get(
                    "credential_updated_at", "")
                if cfg.get("cookie_updated_at"):
                    disk_cfg["cookie_updated_at"] = cfg["cookie_updated_at"]
                config.save_service_config(name, disk_cfg)
                cfg = disk_cfg
            except Exception as exc:  # noqa: BLE001
                warnings.append(f"凭证已生效，但更新配置元数据失败：{exc}")
        if not hot_updated and restart:
            try:
                await manager.restart(name)
                restarted = True
            except Exception as exc:  # noqa: BLE001
                warnings.append(f"凭证已写入，但服务重启失败：{exc}")

    return {
        "ok": True,
        "saved": True,
        "hot_updated": hot_updated,
        "restarted": restarted,
        "warnings": warnings,
        "cookie_configured": bool(cfg.get("cookie")),
        "fields": {f: cfg.get(f, "") for f in spec["fields"]},
        "chat_url": cfg.get("chat_url", ""),
    }
