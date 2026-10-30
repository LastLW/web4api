# Cookie 获取图文教程（文字版）

四个服务的凭证获取是同一套流程，区别只在网址和要复制的字段。

## 通用步骤

1. 用浏览器（Chrome/Edge 均可）登录对应网页版
2. 按 `F12` 打开开发者工具，切到 **Network**（网络）标签
3. 在网页上随便发一条消息，让 Network 里出现聊天请求
4. 找到下表对应的请求 → 点击 → **Headers**（标头）→ **Request Headers**（请求标头）
5. 找到 `Cookie:` 这一行，**右键 → Copy value**（复制值），得到完整 Cookie

| 服务 | 网址 | 要找的请求 | 特殊说明 |
|------|------|-----------|---------|
| 千问 | qianwen.com | `chat`（域名 chat2.qianwen.com） | Cookie 必须含 `tongyi_sso_ticket` |
| 豆包 | doubao.com/chat | `completion`（域名 www.doubao.com） | Cookie 必须含 `sessionid`；**同时复制该请求的完整 URL**（里面含 device_id/fp/tea_uuid） |
| MiMo | aistudio.xiaomimimo.com | `chat`（路径 /open-apis/bot/chat） | Cookie 必须含 `serviceToken`；**同时复制完整 URL**（含 xiaomichatbot_ph） |
| DeepSeek | 不需要抓 Cookie | 登录 Token 由管理台浏览器登录自动抓取 | 在管理台点「浏览器登录」即可 |

## 为什么必须从 Network 面板复制？

真正的登录凭证（`tongyi_sso_ticket`、`sessionid`、`serviceToken`）都被浏览器标记为
**httpOnly**——网页里的 JavaScript 读不到它们，只有 DevTools 这种特权工具能看到。
所以任何"自动获取 Cookie"的脚本都拿不到完整凭证，手动复制是最可靠的方式。

## 备注

- Cookie 等同于账号登录态，**不要泄露给任何人**，也不要提交到 git
- 退出登录会使 Cookie 立即失效
- 多设备登录一般不会使旧 Cookie 失效，但平台风控可能随时调整策略
