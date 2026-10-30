# -*- coding: utf-8 -*-
"""web4api 配置向导 —— 一键粘贴完成各服务的凭证配置。

用法：
    python setup_wizard.py manager   # 推荐：统一管理台 + 内置浏览器登录
    python setup_wizard.py qwen
    python setup_wizard.py doubao
    python setup_wizard.py mimo

流程：打印图文抓取教程 → 你粘贴 DevTools 里复制的内容 → 向导自动提取并写入
services/<name>/config.json → 自动生成随机 api_key/admin_key → 提示自检命令。

发布开源时本文件不含任何真实凭证；真实 config.json 已被 .gitignore 排除。
"""
import json
import os
import re
import secrets
import sys
import time

BASE = os.path.dirname(os.path.abspath(__file__))
SERVICES_DIR = os.path.join(BASE, "services")

# 各服务的抓取教程与校验规则
GUIDES = {
    "qwen": {
        "service_file": "qwen4api.py",
        "port": 8301,
        "steps": [
            "浏览器打开 https://www.qianwen.com 并登录",
            "F12 → Network（网络）标签 → 随便发一条消息",
            "找到名字为 chat 的请求（域名 chat2.qianwen.com）",
            "Request Headers（请求标头）里找到 Cookie 这一行，复制它的完整值",
        ],
        "cookie_must_contain": "tongyi_sso_ticket",
        "cookie_hint": "Cookie 值里必须包含 tongyi_sso_ticket",
        "need_url": True,
        "url_optional": True,
        "url_extracts": {"device_id": r"[?&]ut=([\w-]+)"},
    },
    "doubao": {
        "service_file": "doubao4api.py",
        "port": 8302,
        "steps": [
            "浏览器打开 https://www.doubao.com/chat 并登录",
            "F12 → Network（网络）标签 → 随便发一条消息",
            "找到名字为 completion 的请求（域名 www.doubao.com）",
            "① Request Headers 里复制 Cookie 的完整值",
            "② 右键该请求 → Copy → Copy link address，复制完整请求 URL",
        ],
        "cookie_must_contain": "sessionid",
        "cookie_hint": "Cookie 值里应包含 sessionid（若没有请确认复制的是完整值）",
        "need_url": True,
        "url_extracts": {
            "device_id": r"[?&]device_id=(\d+)",
            "fp": r"[?&]fp=(verify_[^&]+)",
            "tea_uuid": r"[?&]tea_uuid=(\d+)",
        },
    },
    "mimo": {
        "service_file": "mimo4api.py",
        "port": 8303,
        "steps": [
            "浏览器打开 https://aistudio.xiaomimimo.com 并登录",
            "F12 → Network（网络）标签 → 随便发一条消息",
            "找到名字为 chat 的请求（路径 /open-apis/bot/chat）",
            "① Request Headers 里复制 Cookie 的完整值",
            "② 右键该请求 → Copy → Copy link address，复制完整请求 URL",
        ],
        "cookie_must_contain": "serviceToken",
        "cookie_hint": "Cookie 值里应包含 serviceToken（若没有请确认复制的是完整值）",
        "need_url": True,
        "url_extracts": {
            "xiaomichatbot_ph": r"[?&]xiaomichatbot_ph=([^&]+)",
        },
    },
}


def gen_key(prefix: str) -> str:
    return f"{prefix}-{secrets.token_hex(20)}"


def load_or_create_config(name: str, guide: dict) -> dict:
    svc_dir = os.path.join(SERVICES_DIR, name + "4api")
    os.makedirs(svc_dir, exist_ok=True)
    cfg_path = os.path.join(svc_dir, "config.json")
    example = os.path.join(BASE, "config.examples", f"{name}.example.json")

    if os.path.exists(cfg_path):
        with open(cfg_path, encoding="utf-8") as f:
            return json.load(f), cfg_path
    if os.path.exists(example):
        with open(example, encoding="utf-8") as f:
            cfg = json.load(f)
    else:
        cfg = {}
    cfg.setdefault("port", guide["port"])
    cfg.setdefault("api_key", gen_key(f"sk-{name}"))
    cfg.setdefault("admin_key", secrets.token_hex(16))
    cfg.setdefault("cookie", "")
    return cfg, cfg_path


def save_config(cfg_path: str, cfg: dict):
    with open(cfg_path, "w", encoding="utf-8") as f:
        json.dump(cfg, f, ensure_ascii=False, indent=2)
        f.write("\n")


def generic_guide():
    """任意其他平台（Gemini/Grok/Kimi...）的通用逆向方法论入口。"""
    print("=== 通用平台接入（方法论） ===")
    print()
    print("本仓库内置了 DeepSeek/千问/豆包/MiMo 四个通道。其他平台")
    print("（Gemini、Grok、Kimi、Poe……）可以按五步方法论自行接入：")
    print()
    print("  1. 抓包：F12 → Network → 找到聊天请求并复制 URL")
    print("  2. 鉴权分析：只靠 Cookie/Token 即可重放 = 可做；需签名头 = 建议放弃")
    print("  3. 重放验证：用 curl 在浏览器外重放，200 且有流式内容 = 成功")
    print("  4. 响应分析：找正文增量字段和结束标记")
    print("  5. 套模板：复制 services/doubao4api/ 改 4 个函数")
    print()
    print("  不知道 Cookie 在哪？完整新手教程：docs/generic-guide.md 第 0 步")
    print()
    print("完整教程（含可直接粘贴给你自己的 AI 助手的协作提示词）：")
    print("  docs/generic-guide.md")
    print()


def manager_guide():
    """推荐入口：统一管理台负责启动服务、浏览器登录抓包和统一 API。"""
    print("=== web4api 统一管理台 ===")
    print()
    print("一个页面管理 DeepSeek / 千问 / 豆包 / 小米 MiMo 四个通道：")
    print("  · 一键启动 / 停止服务，查看日志和测试对话")
    print("  · 全部通道都用内置浏览器登录，自动抓取 Cookie 或登录 Token")
    print("  · 对外只暴露一个 Base URL 和一把 Key，按模型名自动路由")
    print()
    print("第一步：安装依赖（建议使用项目 .venv）")
    print("  python -m venv .venv")
    print(r"  .venv\Scripts\python.exe -m pip install -r requirements.txt")
    print(r"  .venv\Scripts\python.exe -m playwright install chromium   # 可选，Edge/Chrome 可跳过")
    print()
    print("第二步：启动管理台")
    print(r"  .venv\Scripts\python.exe manager.py")
    print("  浏览器会自动打开 http://127.0.0.1:8400/")
    print()
    print("第三步：在页面里点「浏览器登录」")
    print("  弹出的浏览器窗口完成登录，凭证会自动保存（DeepSeek 无需发消息）；")
    print("  然后点「启动」即可。")
    print()
    print("客户端接入：")
    print("  Base URL : http://127.0.0.1:8400/v1")
    print("  API Key  : 管理台首页「统一 API 入口」里显示的那把")
    print()
    print("仍然想手动配置单个服务？运行：python setup_wizard.py qwen|doubao|mimo")
    print()


def main():
    if len(sys.argv) < 2:
        print(f"用法: python setup_wizard.py [manager|generic|{'|'.join(GUIDES)}]")
        sys.exit(1)
    name = sys.argv[1]
    if name == "manager":
        manager_guide()
        return
    if name == "generic":
        generic_guide()
        return
    if name not in GUIDES:
        print(f"没有这个服务: {name}，可选: manager / generic / {'|'.join(GUIDES)}")
        sys.exit(1)
    guide = GUIDES[name]

    print(f"=== {name} 配置向导 ===\n")
    print("第一步：在浏览器里抓取凭证")
    for i, step in enumerate(guide["steps"], 1):
        print(f"  {i}. {step}")
    print()

    cookie = input("第二步：粘贴完整 Cookie 值 > ").strip().strip('"')
    if guide["cookie_must_contain"] not in cookie:
        print(f"\n⚠️  {guide['cookie_hint']}，当前粘贴的内容里没找到，"
              f"确认后重新运行。")
        sys.exit(1)

    cfg, cfg_path = load_or_create_config(name, guide)
    cfg["cookie"] = cookie

    if guide.get("need_url"):
        url = input("第三步：粘贴完整请求 URL（可直接回车跳过）> ").strip()
        if url:
            cfg["chat_url"] = url
            for field, pattern in guide["url_extracts"].items():
                m = re.search(pattern, url)
                if m:
                    cfg[field] = m.group(1)
                    print(f"  已提取 {field}")
                else:
                    print(f"  ⚠️ 未从 URL 中提取到 {field}（可手动填入 config.json）")
            cfg["credential_updated_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
            cfg["credential_source"] = "setup_wizard"

    save_config(cfg_path, cfg)
    print(f"\n✅ 配置已写入 {cfg_path}")
    print(f"   API Key : {cfg['api_key']}")
    print(f"   本地端口: {cfg.get('port')}")
    print(f"\n第三步：自检（会向上游发一条测试消息）")
    print(f"  python services\\{guide['service_file']} --check")
    print(f"\n第四步：启动服务")
    print(f"  python services\\{guide['service_file']}")
    print(f"\n之后即可把以下配置填入任意客户端：")
    print(f"  Base URL : http://127.0.0.1:{cfg.get('port')}")
    print(f"  API Key  : {cfg['api_key']}")


if __name__ == "__main__":
    main()
