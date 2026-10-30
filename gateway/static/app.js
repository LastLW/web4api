/* web4api 统一管理台前端（无构建依赖） */
(() => {
  "use strict";

  const $ = (sel, root = document) => root.querySelector(sel);
  const $$ = (sel, root = document) => Array.from(root.querySelectorAll(sel));
  const state = {
    overview: null,
    signature: "",
    loading: false,
    loginProvider: null,
    loginTimer: null,
    modalCloseHandler: null,
  };

  // ---------------------------------------------------------------------
  // 基础工具
  // ---------------------------------------------------------------------

  function esc(value) {
    return String(value ?? "")
      .replaceAll("&", "&amp;")
      .replaceAll("<", "&lt;")
      .replaceAll(">", "&gt;")
      .replaceAll('"', "&quot;")
      .replaceAll("'", "&#39;");
  }

  function maskKey(key) {
    key = key || "";
    if (key.length <= 20) return key || "未生成";
    return `${key.slice(0, 12)}…${key.slice(-6)}`;
  }

  function shortUrl(url, max = 72) {
    if (!url) return "";
    return url.length > max ? `${url.slice(0, max)}…` : url;
  }

  function toast(message, type = "", title = "") {
    const root = $("#toast");
    const el = document.createElement("div");
    el.className = `toast ${type}`;
    el.innerHTML = `${title ? `<div class="t-title">${esc(title)}</div>` : ""}<div class="t-body">${esc(message)}</div>`;
    root.appendChild(el);
    setTimeout(() => {
      el.style.opacity = "0";
      el.style.transform = "translateY(6px)";
      el.style.transition = "0.2s ease";
      setTimeout(() => el.remove(), 220);
    }, type === "bad" ? 7000 : 4200);
  }

  async function copyText(text) {
    try {
      await navigator.clipboard.writeText(text);
    } catch {
      const ta = document.createElement("textarea");
      ta.value = text;
      ta.style.position = "fixed";
      ta.style.opacity = "0";
      document.body.appendChild(ta);
      ta.select();
      try { document.execCommand("copy"); } catch { /* ignore */ }
      ta.remove();
    }
    toast("已复制到剪贴板", "ok");
  }

  async function api(path, options = {}) {
    const opts = { ...options };
    opts.headers = { "Content-Type": "application/json", ...(options.headers || {}) };
    if (opts.body && typeof opts.body !== "string") opts.body = JSON.stringify(opts.body);
    const resp = await fetch(path, opts);
    let data = null;
    try { data = await resp.json(); } catch { /* ignore */ }
    if (!resp.ok) {
      const detail = data?.detail || data?.error?.message || data?.error || resp.statusText;
      const error = new Error(typeof detail === "string" ? detail : JSON.stringify(detail));
      error.status = resp.status;
      error.data = data;
      throw error;
    }
    return data;
  }

  function serviceById(id) {
    return (state.overview?.services || []).find((item) => item.id === id);
  }

  function stateMeta(item) {
    if (item.running) return { cls: "ok", text: "运行中" };
    if (item.starting) return { cls: "warn", text: "启动中" };
    if (item.port_open) return { cls: "warn", text: "端口占用" };
    return { cls: "bad", text: "已停止" };
  }

  function fieldStatus(item) {
    const labels = item.field_labels || {};
    return (item.capture ? Object.keys(labels) : []).map((field) => {
      const ok = Boolean(item.fields?.[field]);
      return `<span title="${esc(labels[field] || field)}">${ok ? "✓" : "✗"} ${esc(field)}</span>`;
    }).join("");
  }

  // ---------------------------------------------------------------------
  // 渲染
  // ---------------------------------------------------------------------

  function allModelIds(o) {
    return modelGroups(o).flatMap((g) => g.models);
  }

  function modelGroups(o) {
    return (o.services || [])
      .filter((s) => (s.models || []).length)
      .map((s) => ({ id: s.id, label: s.label, models: s.models }));
  }

  function renderOverview() {
    const o = state.overview;
    if (!o) return;
    // Base URL 多行（本机所有对外地址）
    const bases = (o.manager.all_bases || [o.manager.unified_base]).map((b) => `${b}/v1`);
    $("#unifiedBaseList").innerHTML = bases.map((b) => `
      <div class="base-row">
        <code class="mono" data-copy-model="${esc(b)}" title="点击复制">${esc(b)}</code>
        <button class="icon-btn" data-copy-model="${esc(b)}" title="复制">⧉</button>
      </div>`).join("");
    $("#unifiedKey").textContent = o.manager.unified_key;

    const running = o.services.filter((x) => x.running).length;
    const total = o.services.length;
    const configured = o.services.filter((x) => x.capture?.mode === "localstorage"
      ? x.token_configured
      : x.cookie_configured && x.fields_ready).length;
    const activeCapture = o.capture?.active;
    const fwClosed = (o.firewall?.available ? o.firewall.ports || [] : []).filter((p) => !p.open).length;

    $("#globalStatus").innerHTML = `<span class="status-dot ${running ? "ok" : "warn"} ${activeCapture ? "live" : ""}"></span>${
      esc(`${running}/${total} 运行中`) + (fwClosed ? ` · 防火墙 ${fwClosed} 端口未开放` : "")
    }${activeCapture ? ` · ${esc(o.capture.label || "登录")}登录中` : ""}`;

    $("#stats").innerHTML = [
      { icon: "◈", num: total, lbl: "接入通道" },
      { icon: "●", num: running, lbl: "正在运行", cls: running ? "ok" : "warn" },
      { icon: "✓", num: configured, lbl: "凭证已就绪" },
    ].map((item) => `
      <div class="stat">
        <div class="stat-icon" style="color:${item.cls === "ok" ? "var(--ok)" : item.cls === "warn" ? "var(--warn)" : "var(--text)"}">${item.icon}</div>
        <div><div class="num">${esc(item.num)}</div><div class="lbl">${esc(item.lbl)}</div></div>
      </div>`).join("");

    $("#serviceGrid").innerHTML = o.services.map(renderServiceCard).join("");
    const groups = modelGroups(o);
    if (!groups.some((g) => g.id === state.unifiedGroup)) {
      state.unifiedGroup = groups[0]?.id || null;
    }
    const active = groups.find((g) => g.id === state.unifiedGroup);
    $("#unifiedModels").innerHTML = `
      <div style="display:flex;flex-wrap:wrap;gap:7px">
        ${groups.map((g) => `
          <span class="model-chip ${g.id === state.unifiedGroup ? "active" : ""}" data-model-group="${esc(g.id)}"
            title="查看${esc(g.label)}的模型" style="cursor:pointer">${esc(g.label)}</span>`).join("")}
      </div>
      ${active ? `
        <div style="display:flex;flex-wrap:wrap;gap:7px;margin-top:7px">
          ${active.models.map((m) => `
            <span class="model-chip" data-copy-model="${esc(m)}" title="点击复制模型 ID" style="cursor:pointer">${esc(m)}</span>`).join("")}
        </div>` : ""}`;
  }

  function renderServiceCard(item) {
    const st = stateMeta(item);
    const tokenMode = item.capture?.mode === "localstorage";
    const cookieOk = tokenMode ? item.token_configured : item.cookie_configured;
    const fieldsOk = item.fields_ready;
    const fieldsComplete = fieldsOk && Boolean(item.chat_url);
    const defaultModel = item.default_model || "";
    // 模型 ID 列表（排除 default，避免重复显示）
    const allModels = item.models || [];
    const modelIds = allModels.filter((m) => m !== defaultModel);
    const urlLine = tokenMode ? "" : (item.chat_url
      ? `<div class="cred-line"><span class="ok">✓</span><span title="${esc(item.chat_url)}">已抓取完整聊天 URL</span><button class="mini-link" data-action="view-url" data-service="${item.id}" style="border:none;background:none;padding:0">查看</button></div>`
      : `<div class="cred-line"><span class="warn">!</span>尚未抓取聊天 URL</div>`);
    return `
      <article class="service-card ${item.running ? "running" : ""}" style="--accent:${esc(item.accent)}" data-service="${item.id}">
        <div class="service-head">
          <div class="service-title">
            <div class="service-icon">${esc(item.icon)}</div>
            <div><h3>${esc(item.label)}</h3><div class="sub">${esc(item.short)} · 端口 ${esc(item.port)}</div></div>
          </div>
          <div class="service-state ${st.cls}"><span class="status-dot ${st.cls} ${item.starting ? "live" : ""}"></span>${st.text}</div>
        </div>
        <div class="meta-grid">
          <div class="meta"><div class="k">Base URL</div><div class="v mono" title="${esc(item.base_url)}">${esc(item.base_url)}</div></div>
          <div class="meta"><div class="k">凭证</div><div class="v ${cookieOk ? "ok" : "warn"}">${tokenMode
            ? (cookieOk ? "Token 已配置" : "未抓取 Token")
            : (cookieOk ? `已配置 · ${item.cookie_len} 字符` : "未登录")}</div></div>
          ${tokenMode ? "" : `<div class="meta"><div class="k">URL 参数</div><div class="v ${fieldsComplete ? "ok" : "warn"}">${fieldsComplete ? "完整" : fieldsOk ? "待抓取 URL" : "待补全"}</div></div>`}
          <div class="meta"><div class="k">最近更新</div><div class="v">${esc(item.credential_updated_at || "—")}</div></div>
        </div>
        <div class="key-box">
          <span class="key-label">API Key</span>
          <code data-copy-service="${item.id}" title="点击复制完整 Key">${esc(maskKey(item.api_key))}</code>
          <button class="icon-btn" data-action="copy-key" data-service="${item.id}" title="复制 API Key">⧉</button>
        </div>
        ${defaultModel ? `
        <div class="default-model-row">
          <span class="dm-label">主模型</span>
          <span class="model-chip" data-copy-model="${esc(defaultModel)}" title="点击复制模型 ID">${esc(defaultModel)}</span>
        </div>` : ""}
        ${modelIds.length ? `
        <div class="model-chips">
          ${modelIds.map((m) => `<span class="model-chip" data-copy-model="${esc(m)}" title="点击复制">${esc(m)}</span>`).join("")}
          ${modelIds.length > 6 ? `<span class="model-chip">+${modelIds.length - 6}</span>` : ""}
        </div>` : ""}
        ${urlLine}
        ${tokenMode ? "" : `
        <div class="cred-line" title="${esc(Object.entries(item.fields || {}).map(([k, v]) => `${k}=${v}`).join("  "))}">
          <span class="${fieldsOk ? "ok" : "warn"}">${fieldsOk ? "✓" : "!"}</span>
          <span>${fieldStatus(item) || "无额外参数"}</span>
        </div>`}
        <div class="actions">
          <button class="btn btn-primary" data-action="login" data-service="${item.id}">浏览器登录</button>
          <button class="btn" data-action="toggle" data-service="${item.id}" data-running="${item.running ? "1" : "0"}">${item.running ? "停止" : "启动"}</button>
          <button class="btn" data-action="manual" data-service="${item.id}">手动粘贴</button>
          <button class="btn" data-action="test" data-service="${item.id}">测试</button>
          <button class="btn btn-ghost" data-action="config" data-service="${item.id}">配置</button>
          <button class="btn btn-ghost" data-action="logs" data-service="${item.id}">日志</button>
        </div>
      </article>`;
  }

  // ---------------------------------------------------------------------
  // 主数据加载
  // ---------------------------------------------------------------------

  function overviewSignature(o) {
    // 只比较会显示在页面上的字段，避免每 5 秒无意义地重建 DOM。
    return JSON.stringify({
      manager: [o.manager.port, o.manager.unified_key],
      services: (o.services || []).map((s) => [
        s.id, s.running, s.starting, s.port_open, s.port, s.api_key,
        s.cookie_configured, s.token_configured, s.cookie_len, s.fields_ready, s.fields,
        s.chat_url, s.credential_updated_at, s.models,
      ]),
      firewall: (o.firewall?.ports || []).map((p) => [p.port, p.open]),
      capture: [o.capture?.active, o.capture?.provider, o.capture?.state],
    });
  }

  async function load(showError = false) {
    if (state.loading) return;
    state.loading = true;
    try {
      const overview = await api("/api/overview");
      const signature = overviewSignature(overview);
      state.overview = overview;
      if (signature !== state.signature) {
        state.signature = signature;
        renderOverview();
      }
    } catch (error) {
      $("#globalStatus").innerHTML = `<span class="status-dot bad"></span>管理台连接失败`;
      if (showError) toast(error.message, "bad", "加载失败");
    } finally {
      state.loading = false;
    }
  }

  async function serviceAction(id, action, btn) {
    const label = serviceById(id)?.label || id;
    const old = btn?.textContent;
    if (btn) { btn.disabled = true; btn.textContent = "处理中…"; }
    try {
      const result = await api(`/api/services/${id}/${action}`, { method: "POST" });
      if (action === "start") {
        toast(result.already_running ? `${label} 已在运行` : `${label} 已启动`, "ok");
      } else if (action === "stop") {
        toast(`${label} 已停止`, "ok");
      } else if (action === "restart") {
        toast(`${label} 已重启`, "ok");
      }
      await load();
    } catch (error) {
      toast(error.message, "bad", `${label} ${action === "start" ? "启动" : action === "stop" ? "停止" : "操作"}失败`);
    } finally {
      if (btn) { btn.disabled = false; btn.textContent = old; }
    }
  }

  // ---------------------------------------------------------------------
  // 弹窗
  // ---------------------------------------------------------------------

  function closeModal() {
    if (state.loginTimer) {
      clearInterval(state.loginTimer);
      state.loginTimer = null;
    }
    state.loginProvider = null;
    $("#modalRoot").innerHTML = "";
    if (state.modalCloseHandler) {
      document.removeEventListener("keydown", state.modalCloseHandler);
      state.modalCloseHandler = null;
    }
  }

  function openModal(html, { wide = false } = {}) {
    closeModal();
    $("#modalRoot").innerHTML = `
      <div class="modal-backdrop" data-backdrop="1">
        <div class="modal ${wide ? "wide" : ""}" role="dialog" aria-modal="true">${html}</div>
      </div>`;
    $("[data-backdrop]").addEventListener("click", (event) => {
      if (event.target === event.currentTarget) closeModal();
    });
    state.modalCloseHandler = (event) => { if (event.key === "Escape") closeModal(); };
    document.addEventListener("keydown", state.modalCloseHandler);
    $$("[data-modal-close]").forEach((btn) => btn.addEventListener("click", closeModal));
  }

  function modalHead(title, subtitle = "") {
    return `
      <div class="modal-head">
        <div><h3>${esc(title)}</h3>${subtitle ? `<p>${esc(subtitle)}</p>` : ""}</div>
        <button class="modal-close" data-modal-close title="关闭">✕</button>
      </div>`;
  }

  // ---------------------------------------------------------------------
  // 浏览器登录
  // ---------------------------------------------------------------------

  async function openLogin(id) {
    const item = serviceById(id);
    if (!item) return;
    const steps = item.capture?.instructions || [];
    openModal(`
      ${modalHead(`浏览器登录 · ${item.label}`, "独立浏览器资料，不会影响你日常使用的浏览器")}
      <div class="modal-body">
        <div class="login-layout">
          <div class="login-steps">
            <h4>操作步骤</h4>
            <ol>${steps.map((s) => `<li>${esc(s)}</li>`).join("")}</ol>
            <hr class="sep">
            <div class="tiny muted">登录成功后管理台会自动保存凭证；如果窗口一直停在加载页，可以手动刷新页面。</div>
          </div>
          <div class="login-live">
            <div class="live-head"><span class="status-dot warn live" id="loginDot"></span><span id="loginState">正在启动浏览器…</span></div>
            <div class="live-message" id="loginMessage">请稍候，正在打开 ${esc(item.label)} 登录页。</div>
            <div id="loginBanner"></div>
            <div id="loginCapture"></div>
            <div class="flex wrap" style="margin-top:auto">
              <button class="btn btn-primary btn-sm" id="loginSaveBtn" disabled>保存并关闭</button>
              <button class="btn btn-sm" id="loginCloseBrowserBtn">关闭浏览器</button>
              <button class="btn btn-sm btn-ghost" id="loginResetBtn" title="切换账号时使用">清除登录数据</button>
            </div>
          </div>
        </div>
      </div>`);
    state.loginProvider = id;
    if (item.capture?.mode === "localstorage") $("#loginSaveBtn").style.display = "none";

    $("#loginSaveBtn").addEventListener("click", async () => {
      const btn = $("#loginSaveBtn");
      btn.disabled = true;
      btn.textContent = "保存中…";
      try {
        const result = await api(`/api/login/${id}/save`, { method: "POST" });
        toast("凭证已保存", "ok");
        updateLoginModal(result.status || (await api(`/api/login/${id}/status`)));
        await load();
      } catch (error) {
        toast(error.message, "bad", "保存失败");
        btn.disabled = false;
        btn.textContent = "保存并关闭";
      }
    });

    $("#loginCloseBrowserBtn").addEventListener("click", async () => {
      try {
        await api(`/api/login/${id}/close`, { method: "POST" });
      } catch { /* ignore */ }
      closeModal();
      await load();
    });

    $("#loginResetBtn").addEventListener("click", async () => {
      if (!confirm("确定清除该平台保存的浏览器登录数据？下次需要重新登录。")) return;
      try {
        await api(`/api/login/${id}/reset`, { method: "POST" });
        toast("浏览器登录数据已清除", "ok");
        closeModal();
        await load();
      } catch (error) {
        toast(error.message, "bad", "清除失败");
      }
    });

    try {
      const started = await api(`/api/login/${id}/start`, {
        method: "POST",
        body: { auto_save: true },
      });
      updateLoginModal(started);
      pollLogin(id);
    } catch (error) {
      const banner = $("#loginBanner");
      if (banner) banner.innerHTML = `<div class="error-banner">${esc(error.message)}</div>`;
      const msg = $("#loginMessage");
      if (msg) msg.textContent = "浏览器启动失败，请按下方提示排查。";
    }
  }

  function pollLogin(id) {
    if (state.loginTimer) clearInterval(state.loginTimer);
    state.loginTimer = setInterval(async () => {
      if (state.loginProvider !== id) return;
      try {
        const status = await api(`/api/login/${id}/status`);
        updateLoginModal(status);
        if (status.state === "saved") {
          clearInterval(state.loginTimer);
          state.loginTimer = null;
          await load();
          return;
        }
        if (!status.active && ["closed", "error"].includes(status.state)) {
          clearInterval(state.loginTimer);
          state.loginTimer = null;
        }
      } catch {
        /* 管理台重启等短暂错误，下一轮继续 */
      }
    }, 1200);
  }

  function updateLoginModal(status) {
    if (!status || !$("#loginState")) return;
    const dot = $("#loginDot");
    const stateText = $("#loginState");
    const message = $("#loginMessage");
    const banner = $("#loginBanner");
    const captureBox = $("#loginCapture");
    const saveBtn = $("#loginSaveBtn");
    const stateName = status.state || "unknown";

    const stateMap = {
      starting: ["warn", "正在启动浏览器…"],
      waiting_login: ["warn", "等待登录"],
      waiting_chat: ["warn", "等待发送消息"],
      captured: ["ok", "已捕获，正在保存"],
      saved: ["ok", "已保存"],
      closed: ["", "浏览器已关闭"],
      error: ["bad", "出错了"],
    };
    const [cls, text] = stateMap[stateName] || ["", stateName];
    dot.className = `status-dot ${cls || ""} ${["starting", "waiting_login", "waiting_chat"].includes(stateName) ? "live" : ""}`;
    stateText.textContent = text;
    message.textContent = status.message || "";

    banner.innerHTML = "";
    if (status.error) {
      banner.innerHTML = `<div class="error-banner">${esc(status.error)}</div>`;
    } else if (stateName === "saved") {
      banner.innerHTML = `<div class="saved-banner">凭证已自动写入服务配置${status.result?.fields ? `，URL 参数：${esc(Object.keys(status.result.fields).join("、") || "无")}` : ""}。可以关闭浏览器了。</div>`;
    } else if (status.warning) {
      banner.innerHTML = `<div class="warn-banner">${esc(status.warning)}</div>`;
    }

    if (status.result) {
      const r = status.result;
      if (r.token_preview) {
        captureBox.innerHTML = `
          <div class="capture-box">
            <div><div class="cap-k">DeepSeek Token（点击复制，当 API Key 用）</div><code class="cookie" data-copy-model="${esc(r.token || "")}" title="点击复制完整 Token" style="cursor:pointer">${esc(r.token_preview)}</code></div>
          </div>`;
      } else {
        const fields = Object.entries(r.fields || {}).map(([k, v]) => `<span title="${esc(k)}=${esc(v)}">✓ ${esc(k)}</span>`).join("");
        captureBox.innerHTML = `
          <div class="capture-box">
            <div><div class="cap-k">完整聊天 URL</div><code>${esc(r.url || "—")}</code></div>
            <div><div class="cap-k">Cookie（${r.cookie_len || 0} 字符）</div><code class="cookie">${esc(r.cookie_preview || "—")}</code></div>
            ${fields ? `<div><div class="cap-k">URL 参数</div><div class="fields">${fields}</div></div>` : ""}
            ${r.user_agent ? `<div><div class="cap-k">User-Agent</div><code>${esc(shortUrl(r.user_agent, 110))}</code></div>` : ""}
          </div>`;
      }
    } else {
      captureBox.innerHTML = `<div class="capture-box"><div class="tiny muted">捕获结果会显示在这里：完整 URL、Cookie 预览和 URL 参数。</div></div>`;
    }

    saveBtn.disabled = !(status.result && !status.saved);
    saveBtn.textContent = status.saved ? "已保存" : "保存并关闭";
  }

  // ---------------------------------------------------------------------
  // 手动 / 配置 / 测试 / 日志
  // ---------------------------------------------------------------------

  function openManual(id) {
    const item = serviceById(id);
    const fields = item?.fields || {};
    const tokenMode = item?.capture?.mode === "localstorage";
    openModal(`
      ${modalHead(`手动粘贴凭证 · ${item?.label || id}`, tokenMode
        ? "从浏览器 DevTools 的 localStorage 里复制 userToken 的值粘贴到这里"
        : "不想用内置浏览器时，可以从 DevTools 复制 Cookie 和聊天请求 URL")}
      <div class="modal-body">
        ${tokenMode ? `
        <div class="field">
          <label>DeepSeek 登录 Token（JWT，eyJ 开头）</label>
          <textarea id="manualToken" placeholder="eyJhbGciOiJIUzI1NiIs...">${esc("")}</textarea>
          <div class="hint">登录 chat.deepseek.com 后，F12 → Application → Local Storage → userToken → 复制 value。</div>
        </div>` : `
        <div class="field">
          <label>完整 Cookie</label>
          <textarea id="manualCookie" placeholder="粘贴 Request Headers 里的 Cookie 完整值">${esc("")}</textarea>
          <div class="hint">${item?.cookie_configured ? "已配置 Cookie；留空则只更新 URL/参数。" : "必填。需包含平台登录态字段。"}</div>
        </div>
        <div class="field">
          <label>聊天请求完整 URL</label>
          <input type="text" id="manualUrl" placeholder="例如 https://chat2.qianwen.com/api/v2/chat?..." value="${esc(item?.chat_url || "")}">
          <div class="hint">从 Network 面板右键聊天请求 → Copy → Copy link address。</div>
        </div>
        <div class="meta-grid">
          ${Object.entries(fields).map(([key, value]) => `
            <div class="field" style="margin-bottom:0">
              <label>${esc(item.field_labels?.[key] || key)}</label>
              <input type="text" id="manualField-${esc(key)}" value="${esc(value || "")}" placeholder="可从 URL 自动提取">
            </div>`).join("")}
        </div>`}
        <div class="modal-foot">
          <button class="btn" data-modal-close>取消</button>
          <button class="btn btn-primary" id="manualSaveBtn">保存凭证</button>
        </div>
      </div>`);

    $("#manualSaveBtn").addEventListener("click", async () => {
      const btn = $("#manualSaveBtn");
      if (tokenMode) {
        const token = $("#manualToken").value.trim();
        if (!token) {
          toast("请粘贴登录 Token", "bad");
          return;
        }
        btn.disabled = true;
        btn.textContent = "保存中…";
        try {
          await api(`/api/services/${id}/credentials`, {
            method: "POST",
            body: { token, source: "manual" },
          });
          toast("Token 已保存，服务已重启", "ok");
          closeModal();
          await load();
        } catch (error) {
          toast(error.message, "bad", "保存失败");
          btn.disabled = false;
          btn.textContent = "保存凭证";
        }
        return;
      }
      const cookie = $("#manualCookie").value.trim();
      const chatUrl = $("#manualUrl").value.trim();
      if (!cookie && !chatUrl) {
        toast("请至少填写 Cookie 或聊天请求完整 URL", "bad");
        return;
      }
      btn.disabled = true;
      btn.textContent = "保存中…";
      const body = {
        cookie,
        chat_url: chatUrl,
        source: "manual",
      };
      Object.keys(fields).forEach((key) => {
        const input = $(`#manualField-${CSS.escape(key)}`);
        if (input?.value.trim()) body[key] = input.value.trim();
      });
      try {
        const result = await api(`/api/services/${id}/credentials`, { method: "POST", body });
        toast("凭证已保存" + (result.restarted ? "，服务已重启" : ""), "ok");
        if (result.warnings?.length) toast(result.warnings.join("\n"), "", "提示");
        closeModal();
        await load();
      } catch (error) {
        toast(error.message, "bad", "保存失败");
        btn.disabled = false;
        btn.textContent = "保存凭证";
      }
    });
  }

  async function openConfig(id) {
    const item = serviceById(id);
    let cfg;
    try {
      cfg = await api(`/api/services/${id}/config`);
    } catch (error) {
      toast(error.message, "bad", "读取配置失败");
      return;
    }
    openModal(`
      ${modalHead(`服务配置 · ${item?.label || id}`, "修改后会自动重启服务；API Key 用于客户端鉴权")}
      <div class="modal-body">
        <div class="field">
          <label>API Key</label>
          <input type="text" id="cfgApiKey" value="${esc(cfg.api_key)}">
          <div class="hint">客户端使用；统一入口也接受各服务的 Key。</div>
        </div>
        <div class="meta-grid">
          <div class="field">
            <label>端口</label>
            <input type="number" id="cfgPort" value="${esc(cfg.port)}" min="1" max="65535">
          </div>
          <div class="field">
            <label>最小请求间隔（秒）</label>
            <input type="number" id="cfgInterval" value="${esc(cfg.min_interval_seconds)}" min="0" step="0.5">
          </div>
        </div>
        <div class="field">
          <label>User-Agent（可选）</label>
          <input type="text" id="cfgUa" value="${esc(cfg.user_agent || "")}" placeholder="留空使用默认 UA">
        </div>
        <div class="field">
          <label>额外模型映射（JSON）</label>
          <textarea id="cfgExtra">${esc(JSON.stringify(cfg.extra_models || {}, null, 2))}</textarea>
          <div class="hint">格式：{"自定义模型名": "上游模型值"}</div>
        </div>
        <div class="modal-foot">
          <button class="btn" data-modal-close>取消</button>
          <button class="btn btn-primary" id="cfgSaveBtn">保存并重启</button>
        </div>
      </div>`);

    $("#cfgSaveBtn").addEventListener("click", async () => {
      const btn = $("#cfgSaveBtn");
      btn.disabled = true;
      btn.textContent = "保存中…";
      let extra = {};
      try {
        extra = JSON.parse($("#cfgExtra").value.trim() || "{}");
      } catch {
        toast("额外模型映射不是合法 JSON", "bad");
        btn.disabled = false;
        btn.textContent = "保存并重启";
        return;
      }
      const body = {
        api_key: $("#cfgApiKey").value.trim(),
        port: Number($("#cfgPort").value),
        min_interval_seconds: Number($("#cfgInterval").value),
        user_agent: $("#cfgUa").value.trim(),
        extra_models: extra,
      };
      try {
        const result = await api(`/api/services/${id}/config`, { method: "PATCH", body });
        toast(result.restarted ? "配置已保存，服务已重启" : "配置已保存", "ok");
        if (result.warning) toast(result.warning, "", "提示");
        closeModal();
        await load();
      } catch (error) {
        toast(error.message, "bad", "保存失败");
        btn.disabled = false;
        btn.textContent = "保存并重启";
      }
    });
  }

  function openTest(id) {
    const item = serviceById(id);
    const models = item?.models || [];
    openModal(`
      ${modalHead(`测试对话 · ${item?.label || id}`, "会真实向上游发送一条消息，可能触发平台限流")}
      <div class="modal-body">
        <div class="meta-grid">
          <div class="field">
            <label>模型</label>
            <select id="testModel">${models.map((m) => `<option value="${esc(m)}">${esc(m)}</option>`).join("")}</select>
          </div>
          <div class="field">
            <label>提示词</label>
            <input type="text" id="testPrompt" value="Reply with exactly: ok">
          </div>
        </div>
        <div class="modal-foot" style="justify-content:space-between">
          <span class="tiny muted">网页版通道通常需要 5~20 秒。</span>
          <button class="btn btn-primary" id="testRunBtn">开始测试</button>
        </div>
        <div class="test-result" id="testResult"></div>
      </div>`);

    $("#testRunBtn").addEventListener("click", async () => {
      const btn = $("#testRunBtn");
      const resultBox = $("#testResult");
      btn.disabled = true;
      btn.textContent = "测试中…";
      resultBox.innerHTML = `<div class="empty">正在等待上游回复…</div>`;
      try {
        const result = await api(`/api/services/${id}/test`, {
          method: "POST",
          body: { model: $("#testModel").value, prompt: $("#testPrompt").value },
        });
        resultBox.innerHTML = `
          <div class="test-meta">
            <span class="badge ${result.ok ? "ok" : "warn"}">${result.ok ? "成功" : "失败"}</span>
            <span class="badge">HTTP ${esc(result.status ?? "—")}</span>
            <span class="badge">${esc(result.elapsed ?? 0)} 秒</span>
            <span class="badge">${esc(result.model || "")}</span>
          </div>
          <pre class="log">${esc(result.text || result.error || "无返回内容")}</pre>`;
      } catch (error) {
        resultBox.innerHTML = `<div class="error-banner">${esc(error.message)}</div>`;
      } finally {
        btn.disabled = false;
        btn.textContent = "开始测试";
      }
    });
  }

  async function openLogs(path, title) {
    openModal(`
      ${modalHead(title, "显示最近 200 行；服务输出会持续追加到日志文件")}
      <div class="modal-body">
        <pre class="log" id="logBox">正在读取…</pre>
        <div class="modal-foot">
          <button class="btn" id="logRefreshBtn">刷新</button>
          <button class="btn btn-primary" data-modal-close>关闭</button>
        </div>
      </div>`);
    const box = $("#logBox");
    async function refresh() {
      try {
        const data = await api(`${path}?lines=400`);
        box.textContent = data.text || `（暂无日志）\n${data.path || ""}`;
        box.scrollTop = box.scrollHeight;
      } catch (error) {
        box.textContent = `读取失败：${error.message}`;
      }
    }
    $("#logRefreshBtn").addEventListener("click", refresh);
    await refresh();
  }

  // ---------------------------------------------------------------------
  // 设置 / DeepSeek
  // ---------------------------------------------------------------------

  async function openSettings() {
    let settings;
    try {
      settings = await api("/api/settings");
    } catch (error) {
      toast(error.message, "bad", "读取设置失败");
      return;
    }
    const browser = settings.browser || {};
    openModal(`
      ${modalHead("全局设置", "统一入口 Key 与管理台行为")}
      <div class="modal-body">
        <div class="field">
          <label>统一 API Key</label>
          <div class="flex">
            <input type="text" id="setUnifiedKey" value="${esc(settings.unified_key)}">
            <button class="btn btn-sm" id="setRotateKey" style="flex:none">重新生成</button>
          </div>
          <div class="hint">客户端只需要这一把 Key；也可以使用各服务自己的 Key。</div>
        </div>
        <div class="field">
          <label>浏览器状态</label>
          <div class="tiny ${browser.playwright ? "muted" : ""}">${browser.playwright ? "Playwright 已安装。" : "Playwright 未安装，浏览器登录不可用。"}${
            browser.default ? ` 默认使用 ${esc(browser.default.name)}。` : " 未检测到系统 Edge/Chrome。"
          }</div>
          ${browser.install_hint ? `<div class="hint">${esc(browser.install_hint)}</div>` : ""}
        </div>
        <div class="modal-foot">
          <span class="tiny muted" style="margin-right:auto">保存后立即生效。</span>
          <button class="btn" data-modal-close>取消</button>
          <button class="btn btn-primary" id="settingsSaveBtn">保存</button>
        </div>
      </div>`);

    $("#setRotateKey").addEventListener("click", async () => {
      try {
        const result = await api("/api/settings/rotate-unified-key", { method: "POST" });
        $("#setUnifiedKey").value = result.unified_key;
        toast("统一 Key 已重新生成", "ok");
        await load();
      } catch (error) {
        toast(error.message, "bad", "生成失败");
      }
    });

    $("#settingsSaveBtn").addEventListener("click", async () => {
      const btn = $("#settingsSaveBtn");
      btn.disabled = true;
      btn.textContent = "保存中…";
      try {
        await api("/api/settings", {
          method: "PATCH",
          body: { unified_key: $("#setUnifiedKey").value.trim() },
        });
        toast("设置已保存", "ok");
        closeModal();
        await load();
      } catch (error) {
        toast(error.message, "bad", "保存失败");
        btn.disabled = false;
        btn.textContent = "保存";
      }
    });
  }

  // ---------------------------------------------------------------------
  // 端口防火墙 / 监听检测
  // ---------------------------------------------------------------------

  async function openFirewall() {
    // 先立即弹出加载态，再并行拉取防火墙规则与监听进程，避免长时间无反馈
    openModal(`
      ${modalHead("防火墙与端口", "wf.msc 入站规则与本机端口占用；放行/关闭需要提权时仅弹 UAC 授权，窗口隐藏执行")}
      <div class="modal-body" id="fwBody"><div class="empty">正在读取防火墙与端口状态…</div></div>`);
    const box = $("#fwBody");
    let fw, ls;
    try {
      [fw, ls] = await Promise.all([api("/api/firewall"), api("/api/ports/listeners")]);
    } catch (error) {
      box.innerHTML = `<div class="error-banner">${esc(error.message)}</div>`;
      return;
    }
    const ports = fw.ports || [];
    const byPort = Object.fromEntries(ports.map((p) => [p.port, p]));
    const items = (ls.items || []).map((it) => ({ ...it, svc: byPort[it.port] }));
    const closed = ports.filter((p) => !p.open);
    const opened = ports.filter((p) => p.open);

    box.innerHTML = `
      ${fw.available ? "" : `<div class="error-banner">${esc(fw.error || "无法读取防火墙状态")}</div>`}
      <div class="k" style="color:var(--muted-2);font-size:10px;font-weight:700;letter-spacing:.6px;text-transform:uppercase;margin-bottom:8px">端口监听 / 进程占用</div>
      <div class="account-list">
        ${items.length ? items.map((it) => `
          <div class="account-row">
            <div class="who">${esc(it.label)} :${esc(it.port)}
              <small>${esc(it.name || "?")} · PID ${esc(it.pid)} · ${esc(shortUrl(it.cmd || "", 90))}</small>
            </div>
            <div class="flex">
              ${it.self ? `<span class="badge ok">本管理台</span>`
                        : it.ours ? `<span class="badge warn">web4api 进程</span>`
                                  : `<span class="badge">外部进程</span>`}
              <button class="btn btn-sm btn-danger" data-action="kill-pid" data-pid="${esc(it.pid)}"
                ${it.self ? 'disabled title="管理台自身，请用「全部停止」或退出程序"' : ""}>关闭</button>
            </div>
          </div>`).join("")
        : `<div class="empty">这些端口当前没有监听进程。</div>`}
      </div>
      <div class="k" style="color:var(--muted-2);font-size:10px;font-weight:700;letter-spacing:.6px;text-transform:uppercase;margin:16px 0 8px">防火墙入站规则（web4api-*）</div>
      <div class="account-list">
        ${ports.map((p) => `
          <div class="account-row">
            <div class="who">${esc(p.label)}<small>TCP ${esc(p.port)} · 规则 ${esc(p.rule)}</small></div>
            <div class="flex">
              <span class="badge ${p.open ? "ok" : "warn"}">${p.open ? "已开放" : "未开放"}</span>
              <button class="btn btn-sm" data-action="fw-toggle" data-port="${esc(p.port)}" data-open="${p.open ? "1" : "0"}">${p.open ? "关闭" : "放行"}</button>
            </div>
          </div>`).join("")}
      </div>
      ${closed.length ? `<div class="warn-banner" style="margin-top:12px">有 ${closed.length} 个端口未放行，其他设备访问会被拦截。${fw.admin ? "" : "操作需要管理员权限，会弹出 UAC 授权。"}</div>` : ""}
      <div class="modal-foot">
        <button class="btn" data-modal-close>关闭</button>
        <button class="btn" id="fwCloseAll" ${opened.length && fw.available ? "" : "disabled"}>关闭已开放端口</button>
        <button class="btn btn-primary" id="fwOpenAll" ${closed.length && fw.available ? "" : "disabled"}>开放未开放端口</button>
      </div>`;

    const run = async (fn, btn) => {
      btn.disabled = true;
      btn.textContent = "处理中…";
      try {
        const result = await fn();
        if (result.warning) toast(result.warning, "bad", "部分端口未生效");
        else toast("防火墙规则已更新", "ok");
      } catch (error) {
        toast(error.message, "bad", "操作失败");
      }
      await openFirewall();
    };
    $("#fwOpenAll").addEventListener("click", (e) =>
      run(() => api("/api/firewall/open", { method: "POST" }), e.currentTarget));
    $("#fwCloseAll").addEventListener("click", (e) =>
      run(() => api("/api/firewall/close", { method: "POST" }), e.currentTarget));
  }

  // ---------------------------------------------------------------------
  // 全局事件
  // ---------------------------------------------------------------------

  document.addEventListener("click", async (event) => {
    const groupChip = event.target.closest("[data-model-group]");
    if (groupChip) {
      state.unifiedGroup = groupChip.dataset.modelGroup;
      renderOverview();
      return;
    }
    const modelChip = event.target.closest("[data-copy-model]");
    if (modelChip) {
      await copyText(modelChip.dataset.copyModel);
      return;
    }
    const copyTarget = event.target.closest("[data-copy-target]");
    if (copyTarget) {
      const el = document.getElementById(copyTarget.dataset.copyTarget);
      if (el) await copyText(el.textContent);
      return;
    }
    const copyService = event.target.closest("[data-copy-service]");
    if (copyService) {
      await copyText(serviceById(copyService.dataset.copyService)?.api_key || "");
      return;
    }

    const btn = event.target.closest("[data-action]");
    if (!btn) return;
    const action = btn.dataset.action;
    const id = btn.dataset.service || btn.closest("[data-service]")?.dataset.service;

    try {
      if (action === "toggle") {
        const running = btn.dataset.running === "1";
        await serviceAction(id, running ? "stop" : "start", btn);
      } else if (action === "login") {
        await openLogin(id);
      } else if (action === "manual") {
        openManual(id);
      } else if (action === "config") {
        await openConfig(id);
      } else if (action === "test") {
        openTest(id);
      } else if (action === "logs") {
        await openLogs(`/api/services/${id}/logs`, `${serviceById(id)?.label || id} 日志`);
      } else if (action === "copy-key") {
        await copyText(serviceById(id)?.api_key || "");
      } else if (action === "view-url") {
        const item = serviceById(id);
        openModal(`
          ${modalHead(`${item?.label || id} · 已抓取的聊天请求 URL`, "服务会使用这个 URL 作为上游请求模板")}
          <div class="modal-body">
            <pre class="log">${esc(item?.chat_url || "尚未抓取")}</pre>
            <div class="meta-grid" style="margin-top:14px">
              ${Object.entries(item?.fields || {}).map(([k, v]) => `<div class="meta"><div class="k">${esc(item.field_labels?.[k] || k)}</div><div class="v mono">${esc(v || "—")}</div></div>`).join("")}
            </div>
            <div class="modal-foot">
              <button class="btn" data-modal-close>关闭</button>
              <button class="btn btn-primary" id="copyCapturedUrl">复制 URL</button>
            </div>
          </div>`);
        $("#copyCapturedUrl").addEventListener("click", () => copyText(item?.chat_url || ""));
      } else if (action === "fw-toggle") {
        const open = btn.dataset.open === "1";
        btn.disabled = true;
        btn.textContent = "处理中…";
        try {
          const result = await api(open ? "/api/firewall/close" : "/api/firewall/open", {
            method: "POST",
            body: { ports: [Number(btn.dataset.port)] },
          });
          if (result.warning) toast(result.warning, "bad", "部分端口未生效");
          else toast(open ? "端口已关闭" : "端口已放行", "ok");
        } catch (error) {
          toast(error.message, "bad", "操作失败");
        }
        await openFirewall();
      } else if (action === "kill-pid") {
        if (!confirm(`确定结束进程 ${btn.dataset.pid}？正在使用它的客户端会断开。`)) return;
        btn.disabled = true;
        btn.textContent = "关闭中…";
        try {
          await api(`/api/ports/${btn.dataset.pid}/kill`, { method: "POST" });
          toast(`进程 ${btn.dataset.pid} 已结束，可点「启动」用最新代码重新拉起服务`, "ok");
          await load();
          await openFirewall();
        } catch (error) {
          toast(error.message, "bad", "关闭失败");
          btn.disabled = false;
          btn.textContent = "关闭";
        }
      }
    } catch (error) {
      toast(error.message, "bad");
    }
  });

  // 顶部按钮
  $("#refreshBtn").addEventListener("click", async () => {
    await load(true);
    toast("已刷新", "ok");
  });
  $("#startAllBtn").addEventListener("click", async () => {
    const btn = $("#startAllBtn");
    btn.disabled = true;
    btn.textContent = "启动中…";
    try {
      const result = await api("/api/all/start", { method: "POST" });
      const failed = (result.results || []).filter((r) => !r.ok);
      const skipped = (result.results || []).filter((r) => r.skipped);
      const labelOf = (id) => serviceById(id)?.label || id;
      if (failed.length) toast(failed.map((r) => `${r.name}: ${r.error}`).join("\n"), "bad", "部分服务启动失败");
      else if (skipped.length) toast(
        `已启动 ${result.results.length - skipped.length} 个，跳过 ${skipped.length} 个未配置：${skipped.map((r) => labelOf(r.name)).join("、")}`,
        "ok", "已按配置启动");
      else toast("全部服务已启动", "ok");
      await load();
    } catch (error) {
      toast(error.message, "bad", "一键启动失败");
    } finally {
      btn.disabled = false;
      btn.textContent = "一键启动";
    }
  });
  $("#stopAllBtn").addEventListener("click", async () => {
    if (!confirm("确定停止全部服务？正在使用 API 的客户端会断开。")) return;
    const btn = $("#stopAllBtn");
    btn.disabled = true;
    btn.textContent = "停止中…";
    try {
      const result = await api("/api/all/stop", { method: "POST" });
      const failed = (result.results || []).filter((r) => !r.ok);
      if (failed.length) toast(failed.map((r) => `${r.name}: ${r.error}`).join("\n"), "bad", "部分服务未能停止");
      else toast("全部服务已停止", "ok");
      await load();
    } catch (error) {
      toast(error.message, "bad", "全部停止失败");
    } finally {
      btn.disabled = false;
      btn.textContent = "全部停止";
    }
  });
  $("#settingsBtn").addEventListener("click", openSettings);
  $("#firewallBtn").addEventListener("click", openFirewall);
  $("#oplogBtn").addEventListener("click", () => openLogs("/api/oplog", "操作日志"));
  $("#copyAllConfigBtn").addEventListener("click", async () => {
    const o = state.overview;
    if (!o) return;
    const text = [
      "API 格式: OpenAI Chat Completions",
      `请求地址: ${o.manager.unified_base}/v1`,
      `API 密钥: ${o.manager.unified_key}`,
      `模型 ID: ${allModelIds(o).join(", ")}`,
    ].join("\n");
    await copyText(text);
  });
  $("#rotateKeyBtn").addEventListener("click", async () => {
    if (!confirm("重新生成统一 API Key？使用旧 Key 的客户端需要更新。")) return;
    try {
      const result = await api("/api/settings/rotate-unified-key", { method: "POST" });
      toast("统一 Key 已更新", "ok");
      $("#unifiedKey").textContent = result.unified_key;
      await load();
    } catch (error) {
      toast(error.message, "bad", "生成失败");
    }
  });

  // 启动：2 秒轮询，状态近乎实时（签名不变时不重建 DOM）
  load(true);
  setInterval(() => load(false), 2000);
})();
