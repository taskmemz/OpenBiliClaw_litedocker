/**
 * Settings & management view — combines functionality previously only in the
 * extension popup into the standalone web app: cookie management, runtime
 * toggles, init control, autostart, updates, and backend info.
 */

import {
  fetchInitStatus, startInit, cancelInit,
  fetchConfig, updateConfig, probeConfigService,
  fetchAutostartStatus, applyAutostart,
  fetchUpdateStatus, checkUpdate, applyUpdate,
  submitBilibiliCookie, submitDouyinCookie, submitXCookie,
  fetchSourcesStatus, fetchCredentials,
  fetchRuntimeStatus, fetchHealth, fetchAuthStatus,
  authAdminSetPassword, fetchAuthStatus as fetchGateStatus,
} from "../api.js";
import { state, patchState } from "../state.js";

let $root = null;
let loaded = false;

const SUB_TABS = [
  { id: "model", label: "模型" },
  { id: "cookie", label: "Cookie" },
  { id: "runtime", label: "运行时" },
  { id: "init", label: "初始化" },
  { id: "autostart", label: "自启动" },
  { id: "update", label: "更新" },
  { id: "about", label: "关于" },
];
let activeSubTab = "cookie";

function esc(s) {
  const el = document.createElement("span");
  el.textContent = s;
  return el.innerHTML;
}

function renderSubTabs() {
  const bar = document.createElement("div");
  bar.className = "settings-sub-tabs";
  bar.setAttribute("role", "tablist");
  for (const tab of SUB_TABS) {
    const btn = document.createElement("button");
    btn.className = `settings-sub-tab${tab.id === activeSubTab ? " active" : ""}`;
    btn.textContent = tab.label;
    btn.addEventListener("click", () => {
      activeSubTab = tab.id;
      render();
    });
    bar.appendChild(btn);
  }
  return bar;
}

function renderCookieTab() {
  const div = document.createElement("div");
  div.className = "settings-section";

  const note = document.createElement("p");
  note.className = "settings-note";
  note.textContent = "由于你没有安装浏览器插件，需要手动粘贴各平台的 Cookie 才能正常采集。";
  div.appendChild(note);

  // Bilibili
  const biliGroup = document.createElement("div");
  biliGroup.className = "settings-cookie-group";
  biliGroup.innerHTML = `<h4>B 站 Cookie</h4>`;
  const biliInput = document.createElement("textarea");
  biliInput.className = "settings-cookie-input";
  biliInput.placeholder = "从 bilibili.com 浏览器开发者工具复制的 Cookie 字符串";
  biliInput.rows = 3;
  const biliBtn = document.createElement("button");
  biliBtn.className = "settings-btn";
  biliBtn.textContent = "提交 B 站 Cookie";
  biliBtn.addEventListener("click", async () => {
    biliBtn.disabled = true;
    biliBtn.textContent = "提交中…";
    try {
      const r = await submitBilibiliCookie(biliInput.value);
      biliBtn.textContent = r.ok ? "✓ 已提交" : `失败: ${r.error || "未知错误"}`;
    } catch (e) {
      biliBtn.textContent = `请求失败: ${e.message}`;
    } finally {
      setTimeout(() => { biliBtn.disabled = false; biliBtn.textContent = "提交 B 站 Cookie"; }, 2000);
    }
  });
  biliGroup.appendChild(biliInput);
  biliGroup.appendChild(biliBtn);
  div.appendChild(biliGroup);

  // Douyin
  const dyGroup = document.createElement("div");
  dyGroup.className = "settings-cookie-group";
  dyGroup.innerHTML = `<h4>抖音 Cookie</h4>`;
  const dyInput = document.createElement("textarea");
  dyInput.className = "settings-cookie-input";
  dyInput.placeholder = "从 douyin.com 复制的 Cookie 字符串";
  dyInput.rows = 3;
  const dyBtn = document.createElement("button");
  dyBtn.className = "settings-btn";
  dyBtn.textContent = "提交抖音 Cookie";
  dyBtn.addEventListener("click", async () => {
    dyBtn.disabled = true;
    dyBtn.textContent = "提交中…";
    try {
      const r = await submitDouyinCookie(dyInput.value);
      dyBtn.textContent = r.ok ? "✓ 已提交" : `失败: ${r.error || "未知错误"}`;
    } catch (e) {
      dyBtn.textContent = `请求失败: ${e.message}`;
    } finally {
      setTimeout(() => { dyBtn.disabled = false; dyBtn.textContent = "提交抖音 Cookie"; }, 2000);
    }
  });
  dyGroup.appendChild(dyInput);
  dyGroup.appendChild(dyBtn);
  div.appendChild(dyGroup);

  // X (Twitter)
  const xGroup = document.createElement("div");
  xGroup.className = "settings-cookie-group";
  xGroup.innerHTML = `<h4>X / Twitter Cookie</h4>`;
  const xInput = document.createElement("textarea");
  xInput.className = "settings-cookie-input";
  xInput.placeholder = "从 x.com 复制的 Cookie 字符串（需要包含 auth_token 和 ct0）";
  xInput.rows = 3;
  const xBtn = document.createElement("button");
  xBtn.className = "settings-btn";
  xBtn.textContent = "提交 X Cookie";
  xBtn.addEventListener("click", async () => {
    xBtn.disabled = true;
    xBtn.textContent = "提交中…";
    try {
      const r = await submitXCookie(xInput.value);
      xBtn.textContent = r.ok ? "✓ 已提交" : `失败: ${r.error || "未知错误"}`;
    } catch (e) {
      xBtn.textContent = `请求失败: ${e.message}`;
    } finally {
      setTimeout(() => { xBtn.disabled = false; xBtn.textContent = "提交 X Cookie"; }, 2000);
    }
  });
  xGroup.appendChild(xInput);
  xGroup.appendChild(xBtn);
  div.appendChild(xGroup);

  // Auth gate control
  const authHeader = document.createElement("h4");
  authHeader.style.marginTop = "20px";
  authHeader.textContent = "密码门禁";
  div.appendChild(authHeader);
  const gateContainer = document.createElement("div");
  gateContainer.id = "gate-control";
  gateContainer.innerHTML = `<p class="settings-note">加载中…</p>`;
  div.appendChild(gateContainer);

  (async () => {
    try {
      const gate = await fetchGateStatus();
      if (!gate || !gate.can_manage) {
        gateContainer.innerHTML = `<p class="settings-note">${
          gate?.env_managed ? "由环境变量管理。" : "仅本机可修改密码设置（当前为远程访问）。"
        }</p>`;
        return;
      }
      const enabled = Boolean(gate.enabled);
      gateContainer.innerHTML = `
        <label class="settings-toggle-row">
          <span>启用密码门禁</span>
          <input type="checkbox" id="gate-checkbox" ${enabled ? "checked" : ""}>
        </label>
        <div id="gate-password-wrap" style="${enabled ? "" : "display:none"}">
          <input type="password" id="gate-password" class="settings-input" placeholder="设置访问密码">
          <button id="gate-save-btn" class="settings-btn">保存密码</button>
        </div>
        <p id="gate-hint" class="settings-note" style="margin-top:8px">${
          enabled ? "已开启：远程访问需要登录密码。" : "已关闭：局域网访问无需密码。"
        }</p>
      `;
      const cb = gateContainer.querySelector("#gate-checkbox");
      const pwWrap = gateContainer.querySelector("#gate-password-wrap");
      const pw = gateContainer.querySelector("#gate-password");
      const saveBtn = gateContainer.querySelector("#gate-save-btn");
      const hint = gateContainer.querySelector("#gate-hint");
      cb.addEventListener("change", () => {
        if (!cb.checked) {
          pwWrap.style.display = "none";
          authAdminSetPassword(false).then(r => {
            hint.textContent = r.ok ? "已关闭。" : "关闭失败。";
          });
        } else {
          pwWrap.style.display = "";
          pw.focus();
        }
      });
      saveBtn.addEventListener("click", async () => {
        if (!pw.value.trim()) { hint.textContent = "请输入密码。"; return; }
        saveBtn.disabled = true;
        saveBtn.textContent = "保存中…";
        const r = await authAdminSetPassword(true, pw.value);
        saveBtn.disabled = false;
        saveBtn.textContent = "保存密码";
        if (r.ok) {
          pw.value = "";
          pwWrap.style.display = "none";
          cb.checked = true;
          hint.textContent = "已开启：远程访问需要登录密码。";
        } else {
          hint.textContent = `保存失败: ${r.data?.error || "未知错误"}`;
        }
      });
    } catch { /* ignore */ }
  })();

  return div;
}

function renderRuntimeTab() {
  const div = document.createElement("div");
  div.className = "settings-section";

  // Embedding status
  const embedHeader = document.createElement("h4");
  embedHeader.textContent = "向量模型状态";
  div.appendChild(embedHeader);

  const embedStatus = document.createElement("p");
  embedStatus.id = "embedding-status";
  embedStatus.className = "settings-note";
  embedStatus.textContent = "检测中…";
  div.appendChild(embedStatus);

  // Runtime toggles
  const toggleHeader = document.createElement("h4");
  toggleHeader.style.marginTop = "16px";
  toggleHeader.textContent = "运行时控制";
  div.appendChild(toggleHeader);

  const toggles = document.createElement("div");
  toggles.id = "runtime-toggles";
  toggles.innerHTML = `<p class="settings-note">加载中…</p>`;
  div.appendChild(toggles);

  (async () => {
    try {
      const [health, config] = await Promise.all([fetchHealth(), fetchConfig()]);
      // Embedding status
      const er = health?.embedding_ready;
      const es = document.getElementById("embedding-status");
      if (es) {
        es.innerHTML = er
          ? `<span style="color:var(--success)">● 就绪</span>`
          : `<span style="color:var(--danger)">○ 未就绪</span>` +
            (health?.embedding_provider ? ` (${esc(health.embedding_provider)})` : "");
      }

      // Runtime status
      const rs = await fetchRuntimeStatus();
      const pauseOnDisconnect = config?.scheduler?.pause_on_extension_disconnect ?? false;
      const tc = document.getElementById("runtime-toggles");
      if (tc) {
        tc.innerHTML = `
          <label class="settings-toggle-row">
            <span>断开后暂停调度</span>
            <input type="checkbox" id="toggle-pause-disc" ${pauseOnDisconnect ? "checked" : ""}>
          </label>
          <p class="settings-note" style="font-size:12px;margin-top:4px">
            插件断开连接后自动暂停后台调度，等待恢复。
          </p>
          <div style="margin-top:12px;font-size:13px;color:var(--text-secondary)">
            <div>发现池: ${esc(String(rs?.pool_available_count ?? "?"))} 条</div>
            <div>待评估: ${esc(String(rs?.pool_pending_eval_count ?? "?"))} 条</div>
            <div>版本: ${esc(String(rs?.current_version ?? "?"))}</div>
          </div>
        `;
        const discCb = tc.querySelector("#toggle-pause-disc");
        discCb.addEventListener("change", async () => {
          try {
            await updateConfig({ scheduler: { pause_on_extension_disconnect: discCb.checked } });
          } catch { discCb.checked = !discCb.checked; }
        });
      }
    } catch { /* ignore */ }
  })();

  return div;
}

function renderInitTab() {
  const div = document.createElement("div");
  div.className = "settings-section";

  const header = document.createElement("h4");
  header.textContent = "初始化向导";
  div.appendChild(header);

  const statusContainer = document.createElement("div");
  statusContainer.id = "init-status-area";
  statusContainer.innerHTML = `<p class="settings-note">加载中…</p>`;
  div.appendChild(statusContainer);

  (async () => {
    try {
      const init = await fetchInitStatus();
      const sc = document.getElementById("init-status-area");
      if (!sc) return;

      if (init?.initialized) {
        sc.innerHTML = `<p class="settings-note" style="color:var(--success)">✓ 初始化已完成</p>`;
        return;
      }
      if (init?.running) {
        sc.innerHTML = `
          <p class="settings-note">初始化进行中… (${init.current_stage ?? "?"}/${init.total_stages ?? "?"})</p>
          <div style="margin-top:8px">
            <button id="init-cancel-btn" class="settings-btn settings-btn-danger">取消初始化</button>
          </div>
        `;
        const cancelBtn = sc.querySelector("#init-cancel-btn");
        if (cancelBtn) {
          cancelBtn.addEventListener("click", async () => {
            try { await cancelInit(); } catch { /* ignore */ }
          });
        }
        return;
      }
      if (!init?.can_start) {
        const reason = init?.reason || "未知原因";
        sc.innerHTML = `
          <p class="settings-note" style="color:var(--danger)">无法开始初始化: ${esc(reason)}</p>
          ${init?.prerequisites ? renderPrereqList(init.prerequisites) : ""}
        `;
        return;
      }
      // Can start
      sc.innerHTML = `
        <p class="settings-note">后端就绪，可以开始初始化。</p>
        ${init?.prerequisites ? renderPrereqList(init.prerequisites) : ""}
        <div style="margin-top:12px">
          <label style="display:flex;align-items:center;gap:8px;margin-bottom:8px;font-size:13px">
            <input type="checkbox" id="init-source-bilibili" checked> B 站
          </label>
          <label style="display:flex;align-items:center;gap:8px;margin-bottom:8px;font-size:13px">
            <input type="checkbox" id="init-source-xhs"> 小红书
          </label>
          <label style="display:flex;align-items:center;gap:8px;margin-bottom:8px;font-size:13px">
            <input type="checkbox" id="init-source-douyin"> 抖音
          </label>
          <label style="display:flex;align-items:center;gap:8px;margin-bottom:8px;font-size:13px">
            <input type="checkbox" id="init-source-youtube"> YouTube
          </label>
          <label style="display:flex;align-items:center;gap:8px;margin-bottom:8px;font-size:13px">
            <input type="checkbox" id="init-source-twitter"> X / Twitter
          </label>
          <label style="display:flex;align-items:center;gap:8px;margin-bottom:8px;font-size:13px">
            <input type="checkbox" id="init-source-zhihu"> 知乎
          </label>
        </div>
        <button id="init-start-btn" class="settings-btn" style="margin-top:8px">开始初始化</button>
      `;
      const startBtn = sc.querySelector("#init-start-btn");
      if (startBtn) {
        startBtn.addEventListener("click", async () => {
          const sources = [];
          for (const id of ["bilibili", "xhs", "douyin", "youtube", "twitter", "zhihu"]) {
            const cb = sc.querySelector(`#init-source-${id}`);
            if (cb?.checked) sources.push(id);
          }
          startBtn.disabled = true;
          startBtn.textContent = "启动中…";
          try {
            await startInit(sources);
            startBtn.textContent = "已启动";
          } catch (e) {
            startBtn.textContent = `失败: ${e.message}`;
            startBtn.disabled = false;
          }
        });
      }
    } catch (e) {
      const sc = document.getElementById("init-status-area");
      if (sc) sc.innerHTML = `<p class="settings-note" style="color:var(--danger)">加载失败: ${esc(e.message)}</p>`;
    }
  })();

  return div;
}

function renderPrereqList(prereq) {
  if (!prereq || typeof prereq !== "object") return "";
  const items = [];
  if ("bilibili_logged_in" in prereq) {
    items.push(`<li style="color:${prereq.bilibili_logged_in ? "var(--success)" : "var(--danger)"}">
      B站登录: ${prereq.bilibili_logged_in ? "✓" : "✗"}</li>`);
  }
  if ("llm_ready" in prereq) {
    items.push(`<li style="color:${prereq.llm_ready ? "var(--success)" : "var(--danger)"}">
      AI服务: ${prereq.llm_ready ? "✓" : "✗"}</li>`);
  }
  if ("embedding_ready" in prereq) {
    items.push(`<li style="color:${prereq.embedding_ready ? "var(--success)" : "var(--text-muted)"}">
      向量模型: ${prereq.embedding_ready ? "✓" : "✗ (可选)"}</li>`);
  }
  return items.length ? `<ul style="font-size:12px;margin:8px 0;padding-left:16px">${items.join("")}</ul>` : "";
}

function renderAutostartTab() {
  const div = document.createElement("div");
  div.className = "settings-section";
  div.innerHTML = `<h4>开机自启动</h4><p class="settings-note">加载中…</p>`;

  (async () => {
    try {
      const as = await fetchAutostartStatus();
      div.innerHTML = `
        <h4>开机自启动</h4>
        <label class="settings-toggle-row">
          <span>启用开机自启动</span>
          <input type="checkbox" id="autostart-checkbox" ${as?.enabled ? "checked" : ""}
            ${as?.can_manage ? "" : "disabled"}>
        </label>
        <p class="settings-note" style="font-size:12px;margin-top:4px">
          ${as?.detail || (as?.supported ? "支持此平台的自启动。" : "当前环境不支持自启动。")}
          ${as?.platform ? `(平台: ${as.platform})` : ""}
        </p>
      `;
      const cb = div.querySelector("#autostart-checkbox");
      if (cb && as?.can_manage) {
        cb.addEventListener("change", async () => {
          try {
            await applyAutostart(cb.checked);
          } catch { cb.checked = !cb.checked; }
        });
      }
    } catch { div.innerHTML = `<h4>开机自启动</h4><p class="settings-note">加载失败</p>`; }
  })();

  return div;
}

function renderUpdateTab() {
  const div = document.createElement("div");
  div.className = "settings-section";
  div.innerHTML = `<h4>后端更新</h4><p class="settings-note">检查中…</p>`;

  (async () => {
    try {
      const u = await fetchUpdateStatus();
      const be = u?.backend || {};
      div.innerHTML = `
        <h4>后端更新</h4>
        <div style="font-size:13px;margin-bottom:12px">
          <div>当前版本: ${esc(be?.current_version || "?")}</div>
          <div>最新版本: ${esc(be?.latest_version || "?")}</div>
          <div>状态: ${esc(be?.state || "?")}</div>
          ${be?.last_error ? `<div style="color:var(--danger)">上次错误: ${esc(be.last_error)}</div>` : ""}
        </div>
        <div style="display:flex;gap:8px">
          <button id="update-check-btn" class="settings-btn">检查更新</button>
          ${be?.state === "ready" ? `<button id="update-apply-btn" class="settings-btn">应用更新</button>` : ""}
        </div>
      `;
      const checkBtn = div.querySelector("#update-check-btn");
      if (checkBtn) {
        checkBtn.addEventListener("click", async () => {
          checkBtn.disabled = true;
          checkBtn.textContent = "检查中…";
          try {
            await checkUpdate();
            checkBtn.textContent = "已检查，请刷新页面查看";
          } catch (e) {
            checkBtn.textContent = `检查失败: ${e.message}`;
          } finally {
            setTimeout(() => { checkBtn.disabled = false; checkBtn.textContent = "检查更新"; }, 3000);
          }
        });
      }
      const applyBtn = div.querySelector("#update-apply-btn");
      if (applyBtn) {
        applyBtn.addEventListener("click", async () => {
          applyBtn.disabled = true;
          applyBtn.textContent = "应用中…";
          try {
            await applyUpdate();
            applyBtn.textContent = "已触发更新";
          } catch (e) {
            applyBtn.textContent = `失败: ${e.message}`;
            applyBtn.disabled = false;
          }
        });
      }
    } catch { div.innerHTML = `<h4>后端更新</h4><p class="settings-note">加载失败</p>`; }
  })();

  return div;
}

function renderModelTab() {
  const div = document.createElement("div");
  div.className = "settings-section";
  div.innerHTML = `<h4>LLM 模型配置</h4><p class="settings-note">加载当前配置…</p>`;

  (async () => {
    try {
      const cfg = await fetchConfig();
      const llm = cfg?.llm || {};
      const providers = ["openai", "claude", "gemini", "deepseek", "openrouter", "ollama", "openai_compatible"];
      const currentProvider = llm?.openai?.provider || "openai";

      div.innerHTML = `
        <h4>LLM 模型配置</h4>
        <p class="settings-note">配置 AI 服务后后端才能正常运行，配置完点击"保存并测试"。</p>
        <div style="margin-bottom:12px">
          <label style="display:block;font-size:13px;font-weight:500;margin-bottom:4px">Provider</label>
          <select id="model-provider" class="settings-input" style="margin-bottom:8px">
            ${providers.map(p => `<option value="${p}"${p === currentProvider ? " selected" : ""}>${p}</option>`).join("")}
          </select>

          <label style="display:block;font-size:13px;font-weight:500;margin-bottom:4px">API Key</label>
          <input id="model-apikey" class="settings-input" type="password" placeholder="输入 API Key" value="${esc(llm?.openai?.api_key || "")}">

          <label style="display:block;font-size:13px;font-weight:500;margin-bottom:4px">模型名称（可选）</label>
          <input id="model-name" class="settings-input" placeholder="例如 gpt-4o, claude-sonnet-4" value="${esc(llm?.openai?.model || "")}">

          <label style="display:block;font-size:13px;font-weight:500;margin-bottom:4px">Base URL（可选）</label>
          <input id="model-baseurl" class="settings-input" placeholder="例如 https://api.openai.com/v1" value="${esc(llm?.openai?.base_url || "")}">
        </div>
        <div style="display:flex;gap:8px;flex-wrap:wrap">
          <button id="model-save-btn" class="settings-btn">保存并测试</button>
          <span id="model-status" class="settings-note" style="align-self:center"></span>
        </div>
      `;

      const providerSel = div.querySelector("#model-provider");
      const apiKeyInput = div.querySelector("#model-apikey");
      const modelInput = div.querySelector("#model-name");
      const baseUrlInput = div.querySelector("#model-baseurl");
      const saveBtn = div.querySelector("#model-save-btn");
      const statusEl = div.querySelector("#model-status");

      saveBtn.addEventListener("click", async () => {
        const provider = providerSel.value;
        const apiKey = apiKeyInput.value.trim();
        const model = modelInput.value.trim();
        const baseUrl = baseUrlInput.value.trim();

        if (!apiKey && provider !== "ollama") {
          statusEl.textContent = "请输入 API Key（Ollama 可留空）";
          return;
        }

        saveBtn.disabled = true;
        saveBtn.textContent = "保存中…";
        statusEl.textContent = "";

        try {
          const providerConfig = {};
          if (apiKey) providerConfig.api_key = apiKey;
          if (model) providerConfig.model = model;
          if (baseUrl) providerConfig.base_url = baseUrl;

          const partial = {
            llm: {
              default_provider: provider,
              [provider]: providerConfig,
            },
          };

          const result = await updateConfig(partial);
          statusEl.textContent = result?.ok ? "配置已保存" : "保存失败";

          // Test the connection
          if (result?.ok) {
            saveBtn.textContent = "测试中…";
            const probe = await probeConfigService("llm", { provider });
            statusEl.textContent = probe?.ok
              ? `✓ 连接成功 (${probe.model || ""} ${probe.latency_ms || ""}ms)`
              : `✗ 连接失败: ${probe?.error || probe?.message || "未知错误"}`;
          }
        } catch (e) {
          statusEl.textContent = `请求失败: ${e.message}`;
        } finally {
          saveBtn.disabled = false;
          saveBtn.textContent = "保存并测试";
        }
      });
    } catch (e) {
      div.innerHTML = `<h4>LLM 模型配置</h4><p class="settings-note" style="color:var(--danger)">加载失败: ${esc(e.message)}</p>`;
    }
  })();

  return div;
}

function renderAboutTab() {
  const div = document.createElement("div");
  div.className = "settings-section";
  div.innerHTML = `<h4>关于 OpenBiliClaw</h4><p class="settings-note">加载中…</p>`;

  (async () => {
    try {
      const [health, config] = await Promise.all([fetchHealth(), fetchConfig()]);
      let version = health?.version || "";
      if (!version) {
        const rs = await fetchRuntimeStatus();
        version = rs?.current_version || "";
      }
      div.innerHTML = `
        <h4>关于 OpenBiliClaw</h4>
        <div style="font-size:13px;line-height:1.8">
          <div>后端版本: ${esc(version || "?")}</div>
          <div>运行模式: ${config?.degraded ? `<span style="color:var(--danger)">降级模式</span>` : "正常"}</div>
          <div>数据目录: ${esc(config?.data_dir || "?")}</div>
        </div>
        <div style="margin-top:16px;display:flex;gap:8px;flex-wrap:wrap">
          <a class="settings-btn" href="https://github.com/whiteguo233/OpenBiliClaw" target="_blank" rel="noopener">GitHub 仓库</a>
          <a class="settings-btn" href="https://github.com/whiteguo233/OpenBiliClaw/issues" target="_blank" rel="noopener">反馈问题</a>
        </div>
        <p class="settings-note" style="margin-top:16px;font-size:11px">
          OpenBiliClaw — MIT 协议开源
        </p>
      `;
    } catch {
      div.innerHTML = `
        <h4>关于 OpenBiliClaw</h4>
        <p class="settings-note">部分信息加载失败</p>
        <div style="margin-top:12px">
          <a class="settings-btn" href="https://github.com/whiteguo233/OpenBiliClaw" target="_blank" rel="noopener">GitHub 仓库</a>
        </div>
      `;
    }
  })();

  return div;
}

function render() {
  if (!$root) return;
  $root.innerHTML = "";
  const frag = document.createDocumentFragment();
  frag.appendChild(renderSubTabs());
  const content = document.createElement("div");
  content.className = "settings-content";
  switch (activeSubTab) {
    case "model": content.appendChild(renderModelTab()); break;
    case "cookie": content.appendChild(renderCookieTab()); break;
    case "runtime": content.appendChild(renderRuntimeTab()); break;
    case "init": content.appendChild(renderInitTab()); break;
    case "autostart": content.appendChild(renderAutostartTab()); break;
    case "update": content.appendChild(renderUpdateTab()); break;
    case "about": content.appendChild(renderAboutTab()); break;
  }
  frag.appendChild(content);
  $root.appendChild(frag);
}

export function initSettingsView(rootEl) {
  if (!rootEl) return;
  $root = rootEl;
  if (!loaded) {
    loaded = true;
  }
  render();
}
