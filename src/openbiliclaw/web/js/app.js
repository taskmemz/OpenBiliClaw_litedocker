/**
 * Mobile web SPA entry — shell rendering, routing, health/stream wiring,
 * cross-view navigation. Views render their own tab content.
 */

import {
  fetchHealth,
  checkHealth,
  fetchAuthStatus,
  fetchConfig,
  updateConfig,
} from "./api.js";
import { createStreamClient } from "./stream.js";
import { state, patchState, subscribe } from "./state.js";
import { renderLoginView } from "./views/login.js";
import {
  initRecommendView,
  onStreamConnect as recStreamConnect,
  onStreamEvent as recStreamEvent,
} from "./views/recommend.js";
import { initProfileView, onStreamEvent as profileStreamEvent } from "./views/profile.js";
import {
  initChatView,
  onStreamEvent as chatStreamEvent,
  toggleMessages,
  loadNotifications,
  refreshPendingConfirmations as refreshChatPendingConfirmations,
} from "./views/chat.js";
import { initWatchLaterView, initFavoritesView } from "./views/saved.js";
import { initSettingsView } from "./views/settings.js";
import { createDialogFocusController } from "./saved-sync-runtime.js";

// ── DOM refs ─────────────────────────────────────────────────
const $app = document.getElementById("app");
const $statusBar = document.getElementById("status-bar");
const $tabBar = document.getElementById("tab-bar");

const BACK_TO_TOP_THRESHOLD = 240;

function initBackToTop() {
  const button = document.getElementById("backToTop");
  if (!(button instanceof HTMLButtonElement) || !($app instanceof HTMLElement)) return;

  const getScrollTargets = () => {
    const chatView = document.getElementById("view-chat");
    const chatActive = chatView instanceof HTMLElement && chatView.classList.contains("active");
    return [
      chatActive ? null : $app,
      chatActive ? document.getElementById("chat-messages") : null,
    ].filter((target, index, targets) => {
      if (!(target instanceof HTMLElement) || targets.indexOf(target) !== index) return false;
      if (target.closest("[hidden]") || window.getComputedStyle(target).display === "none") return false;
      return true;
    });
  };

  const getScrollTopTarget = () => {
    const targets = getScrollTargets();
    return targets.reduce(
      (current, target) => (target.scrollTop > (current?.scrollTop || 0) ? target : current),
      targets[0] || null,
    );
  };

  const sync = () => {
    const target = getScrollTopTarget();
    const scrollTop = target?.scrollTop || 0;
    button.hidden = scrollTop < BACK_TO_TOP_THRESHOLD;
  };

  const scrollToTop = () => {
    const target = getScrollTopTarget();
    if (!target) return;
    const behavior = window.matchMedia?.("(prefers-reduced-motion: reduce)")?.matches ? "auto" : "smooth";
    target.scrollTo({ top: 0, behavior });
  };

  $app.addEventListener("scroll", sync, { passive: true });
  document.addEventListener("scroll", sync, true);
  window.addEventListener("resize", sync, { passive: true });
  button.addEventListener("click", scrollToTop);
  sync();
}

// ── Status Bar ───────────────────────────────────────────────
function renderStatusBar() {
  $statusBar.innerHTML = "";

  const title = document.createElement("span");
  title.className = "status-title";
  title.innerHTML = '<img class="status-brand-icon" src="icon-maskable-192.png" alt="" aria-hidden="true"><span>OpenBiliClaw</span>';

  const right = document.createElement("div");
  right.className = "status-right";

  // Connection status dot + text
  const dot = document.createElement("span");
  dot.className = `status-dot ${state.online ? "online" : "offline"}`;
  right.appendChild(dot);

  const statusText = document.createElement("span");
  statusText.style.cssText = "font-size:11px;color:var(--text-muted);margin-right:4px";
  if (state.degraded) {
    statusText.textContent = "AI 服务待修复";
    statusText.style.color = "var(--danger)";
  } else {
    statusText.textContent = state.online ? "在线" : "离线";
  }
  right.appendChild(statusText);

  // Messages bell + badge
  const bell = document.createElement("button");
  bell.className = "badge-btn";
  bell.type = "button";
  bell.setAttribute("aria-label", "查看消息");
  bell.innerHTML = `<svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M6 8a6 6 0 0 1 12 0c0 7 3 9 3 9H3s3-2 3-9"/><path d="M10.3 21a1.94 1.94 0 0 0 3.4 0"/></svg>`;
  const unread = state.messages.notifications.length + state.messages.delights.length;
  const badge = document.createElement("span");
  badge.className = "badge-count";
  badge.dataset.count = unread;
  badge.textContent = unread > 0 ? (unread > 99 ? "99+" : String(unread)) : "";
  bell.appendChild(badge);
  bell.addEventListener("click", () => toggleMessages());
  right.appendChild(bell);

  const settings = document.createElement("button");
  settings.id = "mobile-settings-button";
  settings.className = "badge-btn";
  settings.type = "button";
  settings.setAttribute("aria-label", "打开保存与同步设置");
  settings.innerHTML = '<svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" aria-hidden="true"><circle cx="12" cy="12" r="3"/><path d="M19.4 15a1.7 1.7 0 0 0 .3 1.8l.1.1-2.8 2.8-.1-.1a1.7 1.7 0 0 0-1.8-.3 1.7 1.7 0 0 0-1 1.5v.2h-4v-.2a1.7 1.7 0 0 0-1-1.5 1.7 1.7 0 0 0-1.8.3l-.1.1-2.8-2.8.1-.1a1.7 1.7 0 0 0 .3-1.8 1.7 1.7 0 0 0-1.5-1H3v-4h.2a1.7 1.7 0 0 0 1.5-1 1.7 1.7 0 0 0-.3-1.8l-.1-.1 2.8-2.8.1.1a1.7 1.7 0 0 0 1.8.3 1.7 1.7 0 0 0 1-1.5V3h4v.2a1.7 1.7 0 0 0 1 1.5 1.7 1.7 0 0 0 1.8-.3l.1-.1 2.8 2.8-.1.1a1.7 1.7 0 0 0-.3 1.8 1.7 1.7 0 0 0 1.5 1h.2v4h-.2a1.7 1.7 0 0 0-1.4 1z"/></svg>';
  settings.addEventListener("click", () => { void openMobileSettings(settings); });
  right.appendChild(settings);

  $statusBar.appendChild(title);
  $statusBar.appendChild(right);

  // Degraded banner
  let existing = document.getElementById("degraded-banner");
  if (state.degraded) {
    if (!existing) {
      existing = document.createElement("div");
      existing.id = "degraded-banner";
      existing.style.cssText =
        "background:var(--warning-soft);color:#d97706;font-size:12px;padding:6px 16px;text-align:center";
      $statusBar.after(existing);
    }
    existing.textContent = state.degradedReason || "AI 服务配置有误，修复并保存前大部分功能不可用";
  } else if (existing) {
    existing.remove();
  }
}

async function openMobileSettings(opener) {
  document.getElementById("mobile-settings-overlay")?.remove();
  const overlay = document.createElement("section");
  overlay.id = "mobile-settings-overlay";
  overlay.className = "mobile-settings-overlay";
  overlay.setAttribute("aria-label", "保存与同步设置");
  overlay.setAttribute("role", "dialog");
  overlay.setAttribute("aria-modal", "true");
  overlay.tabIndex = -1;
  const card = document.createElement("div");
  card.className = "mobile-settings-card";
  card.innerHTML = `
    <div class="mobile-settings-head">
      <div><p class="eyebrow">Settings</p><h2>保存与同步</h2></div>
      <button class="mobile-settings-close" type="button" aria-label="关闭设置">×</button>
    </div>
    <label class="mobile-settings-field" for="mobile-saved-auto-sync">
      <input id="mobile-saved-auto-sync" type="checkbox">
      <span>保存时自动同步到对应平台</span>
    </label>
    <p class="mobile-settings-hint">默认关闭。收藏和稍后再看始终先保存在本地；关闭时仍可在列表页手动同步。</p>
    <p class="mobile-settings-status" aria-live="polite"></p>
    <button class="mobile-settings-retry btn btn-outline" type="button" hidden>重试加载</button>
    <div class="mobile-settings-actions"><button class="mobile-settings-save btn btn-brand" type="button">保存设置</button></div>`;
  overlay.append(card);
  document.body.append(overlay);
  const close = card.querySelector(".mobile-settings-close");
  const toggle = card.querySelector("#mobile-saved-auto-sync");
  const save = card.querySelector(".mobile-settings-save");
  const retry = card.querySelector(".mobile-settings-retry");
  const status = card.querySelector(".mobile-settings-status");
  save.disabled = true;
  toggle.disabled = true;
  let focusController = null;
  const closeDialog = () => {
    focusController?.deactivate();
    overlay.remove();
  };
  focusController = createDialogFocusController({
    dialog: overlay,
    opener,
    onClose: closeDialog,
  });
  focusController.activate();
  close.addEventListener("click", closeDialog);
  let storedValue = false;
  let configLoaded = false;
  const loadConfig = async () => {
    configLoaded = false;
    save.disabled = true;
    toggle.disabled = true;
    retry.hidden = true;
    status.removeAttribute("role");
    status.textContent = "正在加载设置…";
    try {
      const config = await fetchConfig();
      storedValue = config.saved_sync?.auto_sync_enabled === true;
      toggle.checked = storedValue;
      configLoaded = true;
      save.disabled = false;
      toggle.disabled = false;
      status.textContent = "设置已加载。";
    } catch (error) {
      status.setAttribute("role", "alert");
      status.textContent = error?.message || "配置加载失败，请稍后重试。";
      retry.hidden = false;
    }
  };
  retry.addEventListener("click", () => { void loadConfig(); });
  close.focus();
  await loadConfig();
  toggle.addEventListener("change", () => {
    if (!toggle.checked || storedValue) return;
    const warning = "开启后，在 OpenBiliClaw 点击收藏或稍后再看会修改对应平台账号中的收藏、书签、Saved、播放列表或稍后观看。";
    if (!window.confirm(warning)) {
      toggle.checked = false;
      status.textContent = "已取消，自动同步仍为关闭。";
    }
  });
  save.addEventListener("click", async () => {
    if (save.disabled || !configLoaded) return;
    save.disabled = true;
    save.textContent = "保存中…";
    status.removeAttribute("role");
    status.textContent = "正在保存设置…";
    try {
      await updateConfig({ saved_sync: { auto_sync_enabled: toggle.checked } });
      storedValue = toggle.checked;
      status.textContent = "设置已保存。手动同步始终可用。";
    } catch (error) {
      status.setAttribute("role", "alert");
      status.textContent = error?.message || "设置保存失败，请重试。";
    } finally {
      save.disabled = false;
      save.textContent = "保存设置";
    }
  });
}

// ── Tab Bar ──────────────────────────────────────────────────
const TABS = [
  { id: "recommend", icon: "\u2728", label: "\u63A8\u8350" },
  { id: "watchLater", icon: "🕐", label: "稍后" },
  { id: "favorites", icon: "⭐", label: "收藏" },
  { id: "profile", icon: "\u{1F9E0}", label: "\u753B\u50CF" },
  { id: "chat", icon: "\u{1F4AC}", label: "\u5BF9\u8BDD" },
  { id: "settings", icon: "\u2699\uFE0F", label: "\u8BBE\u7F6E" },
];

function renderTabBar() {
  $tabBar.innerHTML = "";
  $tabBar.setAttribute("role", "tablist");
  for (const tab of TABS) {
    const isActive = state.activeTab === tab.id;
    const pendingCount = tab.id === "chat"
      ? Math.max(0, Number(state.pendingConfirmationCount) || 0)
      : 0;
    const visibleCount = pendingCount > 99 ? "99+" : String(pendingCount);
    const el = document.createElement("button");
    el.className = `tab-item${isActive ? " active" : ""}`;
    el.setAttribute("role", "tab");
    el.setAttribute("aria-selected", String(isActive));
    el.setAttribute(
      "aria-label",
      pendingCount > 0 ? `${tab.label}，${pendingCount} 条待聊确认` : tab.label,
    );
    el.tabIndex = isActive ? 0 : -1;
    el.innerHTML = `<span class="tab-icon" aria-hidden="true">${tab.icon}</span><span class="tab-count-badge" aria-hidden="true" data-count="${pendingCount}"${pendingCount > 0 ? "" : " hidden"}>${visibleCount}</span><span class="tab-label">${tab.label}</span>`;
    el.addEventListener("click", () => navigateToTab(tab.id));
    el.addEventListener("keydown", (e) => {
      let target = null;
      if (e.key === "ArrowRight") target = TABS[(TABS.indexOf(tab) + 1) % TABS.length];
      else if (e.key === "ArrowLeft") target = TABS[(TABS.indexOf(tab) - 1 + TABS.length) % TABS.length];
      if (target) { e.preventDefault(); navigateToTab(target.id); $tabBar.querySelector(`[aria-selected="true"]`)?.focus(); }
    });
    $tabBar.appendChild(el);
  }
}

// ── Views ────────────────────────────────────────────────────
const views = {};

function ensureView(id) {
  if (views[id]) return views[id];
  const el = document.createElement("div");
  el.className = "view";
  el.id = `view-${id}`;
  $app.appendChild(el);
  views[id] = el;
  return el;
}

function initActiveView() {
  const id = state.activeTab;
  if (id === "recommend") initRecommendView(views.recommend);
  else if (id === "watchLater") initWatchLaterView(views.watchLater);
  else if (id === "favorites") initFavoritesView(views.favorites);
  else if (id === "profile") initProfileView(views.profile);
  else if (id === "chat") initChatView(views.chat);
  else if (id === "settings") initSettingsView(views.settings);
}

/**
 * Navigate to a tab. Exported for cross-view use (e.g. delight "聊一聊" → chat).
 */
export function navigateToTab(id) {
  if (!TABS.find((t) => t.id === id)) return;
  location.hash = `#/${id}`;
  patchState({ activeTab: id });
  document.body.classList.toggle("chat-view-active", id === "chat");
  for (const [key, el] of Object.entries(views)) {
    el.classList.toggle("active", key === id);
  }
  renderTabBar();
  initActiveView();
}

// ── Hash Router ──────────────────────────────────────────────
function readHash() {
  const hash = location.hash.replace("#/", "").replace("#", "");
  return TABS.find((t) => t.id === hash) ? hash : "recommend";
}

// ── WebSocket ────────────────────────────────────────────────
const stream = createStreamClient({
  onConnect() {
    patchState({ online: true });
    recStreamConnect();
    void refreshChatPendingConfirmations({ renderNow: false });
  },
  onDisconnect() {
    patchState({ online: false, pendingConfirmationCount: 0 });
  },
  onEvent(payload) {
    patchState({ runtimeEvent: payload });
    recStreamEvent(payload);
    profileStreamEvent(payload);
    chatStreamEvent(payload);
  },
});

// ── State subscription — re-render shell on relevant changes ─
subscribe((_state, changed) => {
  if ("online" in changed || "degraded" in changed || "degradedReason" in changed || "messages" in changed) {
    renderStatusBar();
  }
  if ("activeTab" in changed || "pendingConfirmationCount" in changed) {
    renderTabBar();
  }
});

// ── Badge update hook (backward compat for chat.js) ──────────
export function setUnreadCount(n) {
  // Chat view updates messages directly in state now, but keep this
  // as a convenience bridge during transition.
  renderStatusBar();
}

// ── Init ─────────────────────────────────────────────────────
let _appStarted = false;

async function startApp() {
  document.body.classList.remove("auth-locked");
  for (const tab of TABS) ensureView(tab.id);

  renderStatusBar();
  renderTabBar();

  // Health check with degraded detection
  try {
    const health = await fetchHealth();
    patchState({
      online: true,
      degraded: health.status === "degraded",
      degradedReason: health.reason || "",
    });
  } catch {
    const alive = await checkHealth();
    patchState({ online: alive });
  }

  stream.connect();
  loadNotifications(); // eagerly load badge count on all tabs
  void refreshChatPendingConfirmations({ renderNow: false });

  if (!_appStarted) {
    _appStarted = true;
    window.addEventListener("hashchange", () => navigateToTab(readHash()));
  }
  navigateToTab(readHash());
}

function showLogin() {
  patchState({ needsLogin: true });
  document.body.classList.add("auth-locked");
  $statusBar.innerHTML = "";
  $tabBar.innerHTML = "";
  renderLoginView($app, {
    // Reload after a successful login instead of re-running startApp() in place:
    // renderLoginView cleared #app (detaching cached view nodes that ensureView
    // would otherwise return stale), and the view modules aren't safe to re-init.
    // A reload re-runs boot() cleanly — the cookie is set, so /api/auth/status
    // returns authenticated and the app starts fresh with no login view left
    // behind. (Same approach as the desktop overlay.)
    onSuccess() {
      location.reload();
    },
  });
}

// Session lost mid-use (token expired / revoked) → drop the stream and re-gate.
window.addEventListener("obc:auth-required", () => {
  if (state.needsLogin) return;
  try { stream.disconnect(); } catch { /* ignore */ }
  patchState({ authenticated: false });
  showLogin();
});

(async function boot() {
  initBackToTop();
  const status = await fetchAuthStatus();
  const enabled = Boolean(status.enabled);
  const authenticated = status.authenticated !== false;
  patchState({ authEnabled: enabled, authenticated });
  if (enabled && !authenticated) {
    showLogin();
    return;
  }
  startApp();
})();
