/**
 * Backend API client for mobile web.
 * Mirrors extension popup-api.js but without Chrome-specific code.
 */

// Derived from the page origin, so every request stays same-origin and the
// HttpOnly session cookie (and WebSocket handshake) is carried automatically
// when the password gate is enabled. See
// docs/plans/2026-05-30-web-password-auth-design.md §4.3.
const BASE_URL = `${location.protocol}//${location.host}/api`;
const DEFAULT_READ_TIMEOUT_MS = 12_000;
const QUICK_READ_TIMEOUT_MS = 5_000;
const CONFIG_WRITE_TIMEOUT_MS = 60_000;
const SAVED_READ_TIMEOUT_MS = 10_000;
const SAVED_MUTATION_TIMEOUT_MS = 10_000;
const FEEDBACK_SUBMIT_TIMEOUT_MS = 30_000;
const CSRF_HEADER = "X-OBC-Auth";
const PENDING_REQUEST_IDS_KEY = "openbiliclaw.pending_request_ids";
const pendingRequestIds = new Map();

function newRequestId() {
  if (typeof globalThis.crypto?.randomUUID === "function") return globalThis.crypto.randomUUID();
  return `${Date.now().toString(36)}-${Math.random().toString(36).slice(2)}`;
}

function loadPendingRequestIds() {
  try {
    const parsed = JSON.parse(globalThis.localStorage?.getItem(PENDING_REQUEST_IDS_KEY) || "{}");
    if (parsed && typeof parsed === "object") {
      for (const [key, value] of Object.entries(parsed)) {
        if (typeof value === "string" && value) pendingRequestIds.set(key, value);
      }
    }
  } catch { /* storage unavailable or corrupt */ }
}

function persistPendingRequestIds() {
  try {
    globalThis.localStorage?.setItem(
      PENDING_REQUEST_IDS_KEY,
      JSON.stringify(Object.fromEntries(pendingRequestIds)),
    );
  } catch { /* in-memory fallback still covers retries in this page */ }
}

function rememberPendingRequestId(namespace, identity) {
  loadPendingRequestIds();
  const key = `${namespace}:${identity}`;
  const existing = pendingRequestIds.get(key);
  if (existing) return { key, requestId: existing };
  const requestId = newRequestId();
  pendingRequestIds.set(key, requestId);
  persistPendingRequestIds();
  return { key, requestId };
}

function forgetPendingRequestId(key, requestId) {
  if (pendingRequestIds.get(key) !== requestId) return;
  pendingRequestIds.delete(key);
  persistPendingRequestIds();
}

/** Notify the shell that the session is gone so it can show the login view. */
function signalAuthRequired() {
  try {
    window.dispatchEvent(new CustomEvent("obc:auth-required"));
  } catch { /* non-browser env */ }
}

function abortError(message = "Request aborted") {
  if (typeof DOMException === "function") {
    return new DOMException(message, "AbortError");
  }
  const error = new Error(message);
  error.name = "AbortError";
  return error;
}

function withTimeout(signal, timeoutMs) {
  const hasTimeout = Number.isFinite(timeoutMs) && timeoutMs > 0;
  if (!hasTimeout && !signal) return { signal: undefined, cleanup() {} };
  if (!hasTimeout) return { signal, cleanup() {} };

  const controller = new AbortController();
  let tid = null;
  const abort = (reason) => { if (!controller.signal.aborted) controller.abort(reason || abortError()); };
  const onCaller = () => abort(signal?.reason);

  if (signal?.aborted) abort(signal.reason);
  else if (signal) signal.addEventListener("abort", onCaller, { once: true });
  tid = setTimeout(() => abort(abortError("Request timed out")), timeoutMs);

  return {
    signal: controller.signal,
    cleanup() {
      if (tid !== null) clearTimeout(tid);
      if (signal) signal.removeEventListener("abort", onCaller);
    },
  };
}

export async function requestJson(path, options = {}) {
  const { timeoutMs, signal, ...fetchOptions } = options;
  const timeout = withTimeout(signal, timeoutMs);
  if (timeout.signal) fetchOptions.signal = timeout.signal;
  // Send the session cookie on every request; add the CSRF header on EVERY
  // request (incl. GET) so state-changing GETs like /api/recommendations are
  // covered. Only fetch() carries it — <img>/WebSocket don't and don't hit
  // CSRF-gated paths. Required by the gate, §4.8.
  fetchOptions.credentials = "same-origin";
  fetchOptions.headers = { ...(fetchOptions.headers || {}), [CSRF_HEADER]: "1" };
  try {
    const res = await fetch(`${BASE_URL}${path}`, fetchOptions);
    if (!res.ok) {
      let details = null;
      try { details = await res.json(); } catch { details = null; }
      if (res.status === 401) signalAuthRequired();
      const err = new Error(`${path} failed: ${res.status}`);
      err.status = res.status;
      err.details = details;
      throw err;
    }
    return res.json();
  } finally {
    timeout.cleanup();
  }
}

// ── Auth (password gate) ────────────────────────────────────
export async function fetchAuthStatus() {
  try {
    return await requestJson("/auth/status", { timeoutMs: QUICK_READ_TIMEOUT_MS });
  } catch {
    // Treat an unreachable backend as "not gated" so the normal offline UI shows.
    return { enabled: false, authenticated: true };
  }
}

export async function login(password) {
  const res = await fetch(`${BASE_URL}/auth/login`, {
    method: "POST",
    credentials: "same-origin",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ password }),
  });
  let data = null;
  try { data = await res.json(); } catch { data = null; }
  return { ok: res.ok && Boolean(data?.ok), status: res.status, data };
}

export async function logout() {
  try {
    await fetch(`${BASE_URL}/auth/logout`, { method: "POST", credentials: "same-origin" });
  } catch { /* best-effort cookie clear */ }
}

const json = (body) => ({
  method: "POST",
  headers: { "Content-Type": "application/json" },
  body: JSON.stringify(body),
});

// ── Health ──────────────────────────────────────────────────
export async function fetchHealth() {
  return requestJson("/health");
}

export async function checkHealth() {
  try {
    const res = await fetch(`${BASE_URL}/health`, { method: "GET" });
    return res.ok;
  } catch { return false; }
}

export async function fetchConfig(timeoutMs = DEFAULT_READ_TIMEOUT_MS) {
  return requestJson("/config", { timeoutMs });
}

export async function updateConfig(data, timeoutMs = CONFIG_WRITE_TIMEOUT_MS) {
  return requestJson("/config", {
    method: "PUT",
    timeoutMs,
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(data),
  });
}

// ── Recommendations ─────────────────────────────────────────
export async function fetchRecommendations() {
  const data = await requestJson("/recommendations", { timeoutMs: DEFAULT_READ_TIMEOUT_MS });
  return Array.isArray(data.items) ? data.items : [];
}

export async function fetchContentHistory(category, limit = 12, cursorOrOffset = "") {
  if (!["clicked", "shown", "removed"].includes(category)) {
    throw new TypeError(`Unknown content history category: ${category}`);
  }
  const params = new URLSearchParams({
    category,
    limit: String(Math.max(1, Math.min(50, Math.floor(Number(limit) || 12)))),
  });
  // The current UI uses an opaque keyset cursor. Numeric offsets remain
  // accepted only for older callers during migration. An empty cursor is
  // intentionally omitted because the API treats it as malformed.
  if (typeof cursorOrOffset === "number") {
    const offset = Math.max(0, Math.floor(Number(cursorOrOffset) || 0));
    if (offset > 0) params.set("offset", String(offset));
  } else {
    const cursor = String(cursorOrOffset || "").trim();
    if (cursor) params.set("cursor", cursor);
  }
  const data = await requestJson(`/content-history?${params}`, {
    timeoutMs: DEFAULT_READ_TIMEOUT_MS,
  });
  return {
    ...data,
    items: Array.isArray(data?.items) ? data.items : [],
    total: Math.max(0, Number(data?.total) || 0),
    has_more: data?.has_more === true,
    next_cursor: data?.has_more === true ? String(data?.next_cursor || "") : "",
  };
}

export async function reshuffleRecommendations(excludedBvids = []) {
  const data = await requestJson(
    "/recommendations/reshuffle",
    json({ excluded_bvids: excludedBvids }),
  );
  return { ...data, items: Array.isArray(data.items) ? data.items : [] };
}

export async function appendRecommendations(excludedBvids = []) {
  const data = await requestJson("/recommendations/append", json({ excluded_bvids: excludedBvids }));
  return { ...data, items: Array.isArray(data.items) ? data.items : [] };
}

export async function reportClick(payload) {
  const stableRecommendationId = payload?.recommendation_id ?? null;
  const stableContentId = String(payload?.content_id || payload?.bvid || "").trim();
  let fallbackUrl = "";
  if (stableRecommendationId == null && !stableContentId) {
    const rawUrl = String(payload?.content_url || payload?.url || "").trim();
    try {
      const normalizedUrl = new URL(rawUrl, globalThis.location?.href);
      normalizedUrl.hash = "";
      fallbackUrl = normalizedUrl.toString();
    } catch { fallbackUrl = rawUrl; }
  }
  const identity = JSON.stringify([
    stableRecommendationId,
    stableContentId || fallbackUrl,
  ]);
  const pending = payload?.request_id
    ? null
    : rememberPendingRequestId("recommendation-click", identity);
  const body = { ...payload, request_id: payload?.request_id || pending?.requestId || "" };
  try {
    await requestJson("/recommendation-click", json(body));
    if (pending) forgetPendingRequestId(pending.key, pending.requestId);
    return true;
  } catch { return false; }
}

// ── Runtime Status ──────────────────────────────────────────
export async function fetchRuntimeStatus() {
  return requestJson("/runtime-status", { timeoutMs: QUICK_READ_TIMEOUT_MS });
}

// ── Delight ─────────────────────────────────────────────────
export async function fetchDelightBatch(limit = null) {
  const params = new URLSearchParams();
  if (typeof limit === "number" && Number.isFinite(limit)) {
    params.set("limit", String(Math.max(1, Math.min(100, Math.floor(limit)))));
  }
  const qs = params.toString();
  const data = await requestJson(`/delight/pending-batch${qs ? `?${qs}` : ""}`, { timeoutMs: DEFAULT_READ_TIMEOUT_MS });
  return Array.isArray(data?.items) ? data.items : [];
}

export async function respondToDelight(bvid, responseType, title = "", message = "") {
  const durableReaction = ["like", "dislike", "dismiss"].includes(responseType);
  const pending = durableReaction
    ? rememberPendingRequestId(
      "delight-response",
      JSON.stringify([bvid, responseType]),
    )
    : null;
  const result = await requestJson("/delight/respond", {
    ...json({
      bvid,
      response: responseType,
      title,
      message,
      request_id: pending?.requestId || "",
    }),
    timeoutMs: 35_000,
  });
  if (pending) forgetPendingRequestId(pending.key, pending.requestId);
  return result;
}

// ── Profile ─────────────────────────────────────────────────
export async function fetchProfileSummary({ limit, cursor } = {}) {
  const params = new URLSearchParams();
  if (typeof limit === "number") params.set("limit", String(limit));
  if (typeof cursor === "string" && cursor.trim()) params.set("cursor", cursor.trim());
  const qs = params.toString();
  return requestJson(`/profile-summary${qs ? `?${qs}` : ""}`);
}

export async function fetchEditState() {
  return requestJson("/profile/edit-state");
}

export async function submitProfileEdit({ target, op, value = null, parent = "", weight = null }) {
  return requestJson("/profile/edit", {
    ...json({ target, op, value, parent, weight }),
    timeoutMs: 35_000,
  });
}

export async function submitInsightFeedback(hypothesis, signal) {
  return requestJson("/insights/feedback", {
    ...json({ hypothesis, signal }),
    timeoutMs: 35_000,
  });
}

// ── Notifications ───────────────────────────────────────────
export async function fetchPendingNotifications() {
  return requestJson("/notifications/pending");
}

export async function ackNotification(bvid) {
  return requestJson("/notifications/sent", json({ bvid }));
}

// ── Cognition Updates ───────────────────────────────────────
export async function fetchPendingCognitionUpdates() {
  return requestJson("/cognition-updates/pending");
}

export async function markCognitionSeen(id) {
  return requestJson(`/cognition-updates/${encodeURIComponent(id)}/seen`, { method: "POST" });
}

// ── Activity Feed ───────────────────────────────────────────
export async function fetchActivityFeed({ limit, before } = {}) {
  const params = new URLSearchParams();
  if (typeof limit === "number") params.set("limit", String(limit));
  if (before) params.set("before", before);
  const qs = params.toString();
  return requestJson(`/activity-feed${qs ? `?${qs}` : ""}`, { timeoutMs: QUICK_READ_TIMEOUT_MS });
}

// ── Chat ────────────────────────────────────────────────────
export async function startChatTurn({
  turnId = "",
  session = "popup",
  scope = "chat",
  subjectId = "",
  subjectTitle = "",
  replyToTurnId = "",
  message,
}) {
  const payload = {
    turn_id: turnId,
    session,
    scope,
    subject_id: subjectId,
    subject_title: subjectTitle,
    message,
  };
  if (replyToTurnId) payload.reply_to_turn_id = replyToTurnId;
  return requestJson("/chat/turns", json(payload));
}

export async function fetchChatTurn(turnId, { signal, timeoutMs = 10_000 } = {}) {
  return requestJson(`/chat/turns/${encodeURIComponent(turnId)}`, { signal, timeoutMs });
}

export async function fetchChatTurns({ session = "popup", scope = "", limit = 50 } = {}) {
  const params = new URLSearchParams();
  params.set("session", session);
  if (scope) params.set("scope", scope);
  if (typeof limit === "number") params.set("limit", String(Math.max(1, Math.floor(limit))));
  return requestJson(`/chat/turns?${params.toString()}`);
}

export async function fetchChatContext(turnId, { signal, timeoutMs = QUICK_READ_TIMEOUT_MS } = {}) {
  return requestJson(`/chat/contexts/${encodeURIComponent(turnId)}`, { signal, timeoutMs });
}

export async function fetchPendingConfirmations({ session = "popup" } = {}) {
  const params = new URLSearchParams({ session });
  return requestJson(`/chat/pending-confirmations?${params.toString()}`);
}

export async function openPendingConfirmation(ref, { session = "popup", signal } = {}) {
  return requestJson(`/chat/pending-confirmations/${encodeURIComponent(String(ref || ""))}/open`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ session }),
    signal,
  });
}

export async function actOnChatCard(turnId, action, { signal } = {}) {
  return requestJson(`/chat/cards/${encodeURIComponent(String(turnId || ""))}/action`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ action }),
    signal,
    timeoutMs: 60_000,
  });
}

// ── Feedback ───────────────────────────────────────────────
export async function submitFeedback(payload) {
  const identity = JSON.stringify([
    payload?.recommendation_id ?? null,
    payload?.feedback_type || "",
    payload?.note || "",
  ]);
  const pending = payload?.request_id ? null : rememberPendingRequestId("feedback", identity);
  const body = { ...payload, request_id: payload?.request_id || pending?.requestId || "" };
  const result = await requestJson("/feedback", {
    ...json(body),
    timeoutMs: FEEDBACK_SUBMIT_TIMEOUT_MS,
  });
  if (pending) forgetPendingRequestId(pending.key, pending.requestId);
  return result;
}

// ── Content-based feedback (saved lists have no recommendation_id) ──
export async function sendBehaviorEvents(events, { retryKey = "" } = {}) {
  let pending = null;
  if (retryKey && events.length === 1 && !String(events[0]?.event_id || "").trim()) {
    pending = rememberPendingRequestId("behavior-command", retryKey);
    events[0].event_id = pending.requestId;
  }
  events.forEach((event) => {
    const existing = String(event?.event_id || "").trim();
    event.event_id = existing || newRequestId();
  });
  const result = await requestJson("/events", json({ events }));
  if (pending && Number(result?.accepted || 0) >= 1) {
    forgetPendingRequestId(pending.key, pending.requestId);
  }
  return result;
}

// ── Delight Ack ────────────────────────────────────────────
export async function markDelightSent(bvid) {
  return requestJson("/delight/sent", json({ bvid }));
}

// ── Refresh ────────────────────────────────────────────────
export async function refreshRecommendations() {
  return requestJson("/recommendations/refresh", { method: "POST" });
}

// ── Interest Probes ─────────────────────────────────────────
export async function fetchPendingProbes() {
  const data = await requestJson("/interest-probes/pending");
  return Array.isArray(data?.items) ? data.items : [];
}

export async function respondToProbe(domain, responseType, options = {}) {
  const payload = { domain, response: responseType, message: "" };
  if (typeof options === "string") {
    payload.message = options;
  } else if (options && typeof options === "object") {
    payload.message = options.message || "";
    if (options.surface) payload.surface = options.surface;
    if (options.confirmation_source) payload.confirmation_source = options.confirmation_source;
  }
  return requestJson("/interest-probes/respond", {
    ...json(payload),
    timeoutMs: 35_000,
  });
}

// ── Avoidance Probes ────────────────────────────────────────
export async function fetchPendingAvoidanceProbes() {
  const data = await requestJson("/avoidance-probes/pending");
  return Array.isArray(data?.items) ? data.items : [];
}

export async function respondToAvoidanceProbe(domain, responseType, message = "") {
  return requestJson("/avoidance-probes/respond", {
    ...json({ domain, response: responseType, message }),
    timeoutMs: 35_000,
  });
}

// ── Watch-later ──────────────────────────────────────────────────

function savedListPath(listKind) {
  if (listKind !== "favorite" && listKind !== "watch_later") {
    throw new TypeError(`Unknown saved list: ${listKind}`);
  }
  return `/saved/${listKind}`;
}

export function normalizeSavedItemInput(item = {}) {
  const sourcePlatform = String(item.source_platform || item.platform || "bilibili").trim();
  const legacyId = String(item.bvid || "").trim();
  const contentId = String(
    item.content_id || (legacyId && !legacyId.includes(":") ? legacyId : ""),
  ).trim();
  return {
    source_platform: sourcePlatform,
    content_id: contentId,
    content_url: String(item.content_url || item.url || "").trim(),
    content_type: String(
      item.content_type || (sourcePlatform === "bilibili" && contentId ? "video" : ""),
    ).trim(),
    title: String(item.title || "").trim(),
    author_name: String(item.author_name || item.up_name || item.author || "").trim(),
    cover_url: String(item.cover_url || item.cover || item.pic || item.thumbnail_url || item.thumbnail || item.image_url || "").trim(),
    note: String(item.note || "").trim(),
  };
}

export async function saveItem(listKind, item, timeoutMs = SAVED_MUTATION_TIMEOUT_MS) {
  return requestJson(savedListPath(listKind), {
    ...json(normalizeSavedItemInput(item)), timeoutMs,
  });
}

export async function removeSavedItem(listKind, itemKey, timeoutMs = SAVED_MUTATION_TIMEOUT_MS) {
  return requestJson(`${savedListPath(listKind)}/remove`, {
    ...json({ item_key: String(itemKey || "").trim() }), timeoutMs,
  });
}

export async function fetchSavedItems(listKind, limit = 50, offset = 0, timeoutMs = SAVED_READ_TIMEOUT_MS) {
  return requestJson(
    `${savedListPath(listKind)}?limit=${encodeURIComponent(limit)}&offset=${encodeURIComponent(offset)}`,
    { timeoutMs },
  );
}

export async function savedItemStatus(listKind, itemKey, timeoutMs = SAVED_READ_TIMEOUT_MS) {
  const query = new URLSearchParams({ item_key: String(itemKey || "").trim() });
  return requestJson(`${savedListPath(listKind)}/status?${query}`, { timeoutMs });
}

export async function syncSavedItems(listKind, itemKeys = [], timeoutMs = SAVED_MUTATION_TIMEOUT_MS) {
  return requestJson(`${savedListPath(listKind)}/sync`, {
    ...json({
      item_keys: Array.from(new Set(itemKeys.map((key) => String(key || "").trim()).filter(Boolean))),
    }),
    timeoutMs,
  });
}

export async function pollSavedSyncTask(taskId, timeoutMs = SAVED_READ_TIMEOUT_MS) {
  return requestJson(`/saved-sync/tasks/${encodeURIComponent(String(taskId || "").trim())}`, {
    timeoutMs,
  });
}

export async function addToWatchLater(bvid) {
  return requestJson("/watch-later", { ...json({ bvid }), method: "POST" });
}

export async function removeFromWatchLater(bvid) {
  return requestJson(`/watch-later/${encodeURIComponent(bvid)}`, { method: "DELETE" });
}

export async function watchLaterStatus(bvid) {
  return requestJson(`/watch-later/${encodeURIComponent(bvid)}`);
}

export async function fetchWatchLater(limit = 50, offset = 0) {
  return requestJson(`/watch-later?limit=${limit}&offset=${offset}`);
}

// ── Favorites (收藏夹) ────────────────────────────────────────────

export async function addToFavorite(bvid) {
  return requestJson("/favorites", { ...json({ bvid }), method: "POST" });
}

export async function removeFromFavorite(bvid) {
  return requestJson(`/favorites/${encodeURIComponent(bvid)}`, { method: "DELETE" });
}

export async function favoriteStatus(bvid) {
  return requestJson(`/favorites/${encodeURIComponent(bvid)}`);
}

export async function fetchFavorites(limit = 50, offset = 0) {
  return requestJson(`/favorites?limit=${limit}&offset=${offset}`);
}

// ── Init ────────────────────────────────────────────────────
export async function fetchInitStatus() {
  return requestJson("/init-status");
}

export async function startInit(sources = null) {
  const body = {};
  if (Array.isArray(sources) && sources.length > 0) {
    body.sources = sources;
  }
  return requestJson("/init", json(body));
}

export async function cancelInit() {
  return requestJson("/init/cancel", { method: "POST" });
}

// ── Config probes ───────────────────────────────────────────
export async function probeConfigService(kind, config) {
  return requestJson("/config/probe-service", json({ kind, config }));
}

// ── Autostart ───────────────────────────────────────────────
export async function fetchAutostartStatus() {
  return requestJson("/autostart-status");
}

export async function applyAutostart(enabled) {
  return requestJson("/autostart/apply", json({ enabled }));
}

// ── Update ──────────────────────────────────────────────────
export async function fetchUpdateStatus() {
  return requestJson("/update-status");
}

export async function checkUpdate() {
  return requestJson("/update/check", { method: "POST" });
}

export async function applyUpdate() {
  return requestJson("/update/apply", { method: "POST", timeoutMs: 120_000 });
}

// ── Cookie Management (for standalone client) ───────────────
export async function submitBilibiliCookie(cookie) {
  return requestJson("/bilibili/cookie", json({ cookie, source: "web" }));
}

export async function submitDouyinCookie(cookie) {
  return requestJson("/sources/dy/cookie", json({ cookie, source: "web" }));
}

export async function submitXCookie(cookie) {
  return requestJson("/sources/x/cookie", json({ cookie, source: "web" }));
}

// ── Sources Status ──────────────────────────────────────────
export async function fetchSourcesStatus() {
  return requestJson("/sources/status");
}

export async function fetchCredentials() {
  return requestJson("/sources/credentials");
}

// ── Auth Admin ──────────────────────────────────────────────
export async function authAdminSetPassword(enabled, password = "") {
  const body = { enabled };
  if (password) body.password = password;
  const res = await fetch(`${BASE_URL}/auth/admin`, {
    method: "POST",
    credentials: "same-origin",
    headers: { "Content-Type": "application/json", [CSRF_HEADER]: "1" },
    body: JSON.stringify(body),
  });
  let data = null;
  try { data = await res.json(); } catch { data = null; }
  return { ok: res.ok && Boolean(data?.ok), status: res.status, data };
}
