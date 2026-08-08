/**
 * Chat view — message history, input with placeholder carousel,
 * AI thinking state, messages overlay (probe + delight notifications),
 * contextual chat entry from delight/probe.
 */

import {
  startChatTurn,
  fetchChatContext,
  fetchChatTurn,
  fetchChatTurns,
  fetchPendingConfirmations,
  openPendingConfirmation,
  actOnChatCard,
  fetchProfileSummary,
  fetchActivityFeed,
  fetchPendingNotifications,
  fetchPendingProbes,
  fetchPendingAvoidanceProbes,
  ackNotification,
  fetchDelightBatch,
  respondToDelight,
  markDelightSent,
  respondToProbe,
  respondToAvoidanceProbe,
} from "../api.js";
import { setUnreadCount, navigateToTab } from "../app.js";
import {
  forgetHandledProbe,
  mergeProbeNotifications,
  probeNotificationKey,
  rememberHandledProbe,
  removeProbeFromNotifications,
  shouldDisplayProbeFromWebSocket,
} from "./probe-notification-helpers.js";
import {
  normalizeChatTurn,
  normalizeProfileSummary,
  normalizeActivityFeed,
  normalizeDelightCandidate,
  getDelightActionState,
  getDelightMessageActions,
  getProbeMessageActions,
  getAvoidanceProbeMessageActions,
  getMobileChatSession,
  getCoverImageAttrs,
  getSourceLabel,
  buildContentUrl,
} from "../view-models.js";
import { openContentUrl } from "../app-launch.js";
import { state, patchState } from "../state.js";

const dialogueConfirmation = globalThis.OpenBiliClawDialogueConfirmation;
if (!dialogueConfirmation) {
  throw new Error("dialogue-confirmation shared helper did not load");
}
const {
  activateReplyQuote,
  clearContextSelection,
  contextBarMarkup,
  contextErrorCode,
  contextErrorMessage,
  contextSelectionFromTurn,
  executeCardAction,
  executePendingConfirmationOpen,
  isCardTurn,
  isTerminalCardTurn,
  isQuestionTurn,
  normalizeContextPreview,
  readContextSelection,
  replyQuoteMarkup,
  renderMarkdown,
  renderPendingListMarkup,
  renderTurnMarkup,
  selectDialogueTurns,
  writeContextSelection,
} = dialogueConfirmation;

let $root = null;
let loaded = false;
let turns = [];
let sending = false;
let pendingTurnId = null;
let pollTimer = null;
let userScrolledUp = false;
const CHAT_HISTORY_REFRESH_INTERVAL_MS = 2500;
let historyRefreshTimer = null;
let historyRefreshInFlight = false;
let lastHistorySignature = null;
let pendingConfirmationRefreshTimer = null;
let dialogueStatus = { message: "", tone: "info" };
let retainedDraft = "";
let dialogueContextSelection = readContextSelection(
  (() => {
    try { return globalThis.localStorage; } catch { return null; }
  })(),
  "mobile-web",
);
const dialogueTurnsById = new Map();
const dialogueCardActionAbortController = new AbortController();
let pendingConfirmations = {
  count: 0,
  items: [],
  expanded: false,
};

// Messages overlay state
let overlayOpen = false;
let notifications = [];
let delightMsgs = [];
const pendingProbeActions = new Map();

function pendingProbeAction(type, domain) {
  return pendingProbeActions.get(probeNotificationKey(type, domain)) || null;
}

function setProbeCardBusy(card, busy) {
  if (!card) return;
  card.classList.toggle("is-processing", busy);
  card.setAttribute("aria-busy", busy ? "true" : "false");
  for (const actionBtn of card.querySelectorAll("[data-probe]")) {
    actionBtn.disabled = busy;
  }
}

// Placeholder carousel
const PLACEHOLDERS = [
  "\u6700\u8FD1\u6709\u4EC0\u4E48\u60F3\u804A\u7684\uFF1F",
  "\u5BF9\u54EA\u6761\u63A8\u8350\u6709\u60F3\u6CD5\uFF1F",
  "\u60F3\u63A2\u7D22\u4EC0\u4E48\u65B0\u9886\u57DF\uFF1F",
  "\u89C9\u5F97\u753B\u50CF\u51C6\u4E0D\u51C6\uFF1F",
  "\u6709\u4EC0\u4E48\u4E0D\u60F3\u518D\u770B\u5230\u7684\uFF1F",
];
let placeholderIdx = 0;
let placeholderTimer = null;
let inputFocused = false;

function chatSession(scope = "chat") {
  return getMobileChatSession(scope);
}

function contextStorage() {
  try { return globalThis.localStorage; } catch { return null; }
}

function storeDialogueContext(selection) {
  dialogueContextSelection = writeContextSelection(contextStorage(), "mobile-web", selection);
  return dialogueContextSelection;
}

async function validateDialogueContext({ announce = false } = {}) {
  const current = normalizeContextPreview(dialogueContextSelection);
  if (!current) return null;
  const contextTarget = turns.find((turn) => turn?.turn_id === current.reply_to_turn_id);
  if (isTerminalCardTurn(contextTarget)) {
    storeDialogueContext(clearContextSelection());
    return null;
  }
  try {
    const preview = normalizeContextPreview(await fetchChatContext(current.reply_to_turn_id));
    if (!preview) throw new Error("invalid_context_preview");
    return storeDialogueContext(preview);
  } catch (error) {
    const code = contextErrorCode(error);
    if (["reply_target_not_found", "reply_target_inactive", "invalid_reply_target"].includes(code)) {
      storeDialogueContext(clearContextSelection());
      if (announce) setDialogueStatus(contextErrorMessage(error), "error");
    } else if (announce && code === "reply_target_processing") {
      setDialogueStatus(contextErrorMessage(error), "info");
    }
    return code === "reply_target_processing" ? current : null;
  }
}

async function selectDialogueContext(turnId, preview = null) {
  const turn = turns.find((item) => item?.turn_id === turnId) || { turn_id: turnId };
  const candidate = contextSelectionFromTurn(turn, preview);
  if (candidate) {
    storeDialogueContext(candidate);
    render();
    return candidate;
  }
  try {
    const fetched = normalizeContextPreview(await fetchChatContext(turnId));
    const fetchedCandidate = contextSelectionFromTurn(turn, fetched);
    if (!fetchedCandidate) throw new Error("invalid_context_preview");
    storeDialogueContext(fetchedCandidate);
    render();
    return fetchedCandidate;
  } catch (error) {
    setDialogueStatus(contextErrorMessage(error), "error");
    return null;
  }
}

// ── Escape helper ────────────────────────────────────────────
function esc(s) {
  const el = document.createElement("span");
  el.textContent = s;
  return el.innerHTML;
}

function isChallengeProbe(item) {
  const mode = String(item?.probe_mode || "").toLowerCase();
  return Boolean(item?.challenge) || mode === "lateral" || mode === "bridge" || mode === "wildcard";
}

// ── Render Chat ──────────────────────────────────────────────
function isNearChatBottom(element) {
  if (!element) return true;
  return element.scrollHeight - element.clientHeight - element.scrollTop <= 40;
}

function openEvidenceTurnIds(element) {
  if (!element) return new Set();
  return new Set(
    Array.from(element.querySelectorAll(".dialogue-evidence[open]"))
      .map((details) => details.closest("[data-dialogue-turn-id]")?.dataset.dialogueTurnId || "")
      .filter(Boolean),
  );
}

function setDialogueStatus(message = "", tone = "info") {
  dialogueStatus = { message, tone };
  const status = $root?.querySelector(".chat-status");
  if (status instanceof HTMLElement) {
    status.textContent = message;
    status.hidden = !message;
    status.dataset.tone = tone;
  }
}

function createPendingPanel(previousScrollTop = 0) {
  const panel = document.createElement("section");
  panel.className = "chat-pending";
  panel.setAttribute("aria-label", "待聊确认");

  const toggle = document.createElement("button");
  toggle.className = `chat-pending-toggle${pendingConfirmations.expanded ? " is-expanded" : ""}`;
  toggle.type = "button";
  toggle.setAttribute("aria-expanded", String(pendingConfirmations.expanded));
  toggle.setAttribute("aria-controls", "mobile-chat-pending-list");
  const countText = pendingConfirmations.count > 99 ? "99+" : String(pendingConfirmations.count);
  toggle.innerHTML = `<span>待聊确认 <span class="chat-pending-count">${countText}</span></span>`;
  toggle.addEventListener("click", () => {
    pendingConfirmations.expanded = !pendingConfirmations.expanded;
    render();
    if (pendingConfirmations.expanded) void refreshPendingConfirmations();
  });

  const list = document.createElement("div");
  list.id = "mobile-chat-pending-list";
  list.className = "chat-pending-list";
  list.hidden = !pendingConfirmations.expanded;
  list.setAttribute("aria-label", "待聊确认列表");
  list.innerHTML = renderPendingListMarkup(pendingConfirmations.items);
  list.addEventListener("click", (event) => {
    const button = event.target instanceof Element
      ? event.target.closest("[data-confirmation-ref]")
      : null;
    if (button instanceof HTMLButtonElement) void handlePendingConfirmationOpen(button);
  });

  panel.append(toggle, list);
  requestAnimationFrame(() => {
    list.scrollTop = Math.min(previousScrollTop, Math.max(0, list.scrollHeight - list.clientHeight));
  });
  return panel;
}

function render() {
  if (!$root) return;
  const previousMessages = $root.querySelector("#chat-messages");
  const previousPendingList = $root.querySelector("#mobile-chat-pending-list");
  const previousInput = $root.querySelector("#chat-input");
  const previousScrollTop = previousMessages?.scrollTop || 0;
  const previousPendingScrollTop = previousPendingList?.scrollTop || 0;
  const shouldStickToBottom = !previousMessages || isNearChatBottom(previousMessages);
  const openEvidence = openEvidenceTurnIds(previousMessages);
  const previousDraft = previousInput instanceof HTMLTextAreaElement
    ? previousInput.value || retainedDraft
    : retainedDraft;
  const restoreInputFocus = document.activeElement === previousInput;
  $root.innerHTML = "";

  const shell = document.createElement("div");
  shell.className = "chat-shell";

  shell.appendChild(createPendingPanel(previousPendingScrollTop));

  // Messages area
  const messages = document.createElement("div");
  messages.className = "chat-messages";
  messages.id = "chat-messages";
  messages.tabIndex = 0;
  messages.setAttribute("role", "region");
  messages.setAttribute("aria-label", "口味对话记录");

  const dialogueTurns = selectDialogueTurns(turns);
  dialogueTurnsById.clear();
  if (dialogueTurns.length === 0 && !sending) {
    messages.innerHTML = `<div class="empty-state"><div class="empty-state-icon">\u{1F4AC}</div><div class="empty-state-text">\u548C AI \u804A\u804A\u4F60\u7684\u5174\u8DA3\u548C\u60F3\u6CD5</div></div>`;
  }

  for (const turn of dialogueTurns) {
    if (turn?.turn_id) dialogueTurnsById.set(turn.turn_id, turn);
    const container = document.createElement("div");
    container.className = "dialogue-turn";
    container.dataset.dialogueTurnContainer = turn?.turn_id || "";
    container.innerHTML = `${replyQuoteMarkup(turn, dialogueTurns)}${renderTurnMarkup(turn, { surface: "desktop" })}`;
    if (
      !isCardTurn(turn) &&
      !isQuestionTurn(turn) &&
      !turn.response &&
      (turn.status === "pending" || turn.status === "processing")
    ) {
      const thinking = document.createElement("div");
      thinking.className = "chat-bubble thinking";
      thinking.innerHTML = `<div class="spinner" style="width:16px;height:16px;display:inline-block;vertical-align:middle;margin-right:6px"></div>\u601D\u8003\u4E2D\u2026`;
      container.appendChild(thinking);
    }
    if (turn.status === "error" || turn.status === "failed") {
      const errBubble = container.querySelector('[data-part="assistant"]');
      if (errBubble) {
        errBubble.textContent = turn.error || "\u56DE\u590D\u5931\u8D25";
      }
      const retryBtn = document.createElement("button");
      retryBtn.className = "chat-retry-btn";
      retryBtn.type = "button";
      retryBtn.textContent = "\u91CD\u8BD5";
      retryBtn.addEventListener("click", () => retryTurn(turn));
      container.appendChild(retryBtn);
    }
    messages.appendChild(container);
  }

  // Scroll tracking
  messages.addEventListener("scroll", () => {
    userScrolledUp = !isNearChatBottom(messages);
  });
  messages.addEventListener("click", (event) => {
    activateReplyQuote(event, messages);
    const button = event.target instanceof Element
      ? event.target.closest("[data-card-action]")
      : null;
    if (button instanceof HTMLButtonElement) void handleDialogueCardAction(button);
  });

  shell.appendChild(messages);

  const contextMarkup = contextBarMarkup(dialogueContextSelection);
  if (contextMarkup) {
    const contextBar = document.createElement("div");
    contextBar.innerHTML = contextMarkup;
    const clearButton = contextBar.querySelector("[data-context-clear]");
    clearButton?.addEventListener("click", () => {
      storeDialogueContext(clearContextSelection());
      setDialogueStatus("已清除这条消息的对话上下文。", "info");
      render();
    });
    shell.appendChild(contextBar.firstElementChild || contextBar);
  }

  // Input row
  const inputRow = document.createElement("div");
  inputRow.className = "chat-input-row";

  const textarea = document.createElement("textarea");
  textarea.className = "chat-input";
  textarea.id = "chat-input";
  textarea.placeholder = PLACEHOLDERS[placeholderIdx];
  textarea.rows = 2;
  textarea.value = previousDraft;
  textarea.addEventListener("input", autoGrow);
  textarea.addEventListener("focus", () => { inputFocused = true; });
  textarea.addEventListener("blur", () => { inputFocused = false; });
  textarea.addEventListener("keydown", (e) => {
    if (e.key === "Enter" && !e.shiftKey && !e.isComposing) {
      e.preventDefault();
      handleSend();
    }
  });

  // Pre-fill from contextual chat context
  if (state.pendingChatContext && !sending) {
    const ctx = state.pendingChatContext;
    textarea.value = `\u5173\u4E8E\u300C${ctx.subjectTitle || ctx.subjectId}\u300D\uFF0C\u6211\u60F3\u804A\u804A`;
    patchState({ pendingChatContext: null });
  }

  const sendBtn = document.createElement("button");
  sendBtn.className = "chat-send-btn";
  sendBtn.id = "chat-send";
  sendBtn.type = "button";
  sendBtn.setAttribute("aria-label", "发送消息");
  sendBtn.innerHTML = "\u{1F4E8}";
  sendBtn.disabled = sending;
  sendBtn.addEventListener("click", handleSend);

  inputRow.appendChild(textarea);
  inputRow.appendChild(sendBtn);
  shell.appendChild(inputRow);

  const status = document.createElement("p");
  status.className = "chat-status";
  status.setAttribute("aria-live", "polite");
  status.dataset.tone = dialogueStatus.tone;
  status.textContent = dialogueStatus.message;
  status.hidden = !dialogueStatus.message;
  shell.appendChild(status);

  $root.appendChild(shell);

  for (const details of messages.querySelectorAll(".dialogue-evidence")) {
    const turnId = details.closest("[data-dialogue-turn-id]")?.dataset.dialogueTurnId || "";
    if (openEvidence.has(turnId)) details.open = true;
  }

  // Auto-scroll only while the reader is already following the newest turn.
  if (!userScrolledUp || shouldStickToBottom) {
    requestAnimationFrame(() => {
      messages.scrollTop = messages.scrollHeight;
    });
  } else {
    messages.scrollTop = Math.min(
      previousScrollTop,
      Math.max(0, messages.scrollHeight - messages.clientHeight),
    );
  }
  if (restoreInputFocus) {
    requestAnimationFrame(() => textarea.focus({ preventScroll: true }));
  }

  // Start placeholder carousel
  startPlaceholderCarousel();

  // Render overlay if open
  renderOverlay();
}

function autoGrow(e) {
  const el = e.target;
  el.style.height = "auto";
  el.style.height = Math.min(Math.max(el.scrollHeight, 60), 112) + "px";
}

function startPlaceholderCarousel() {
  if (placeholderTimer) clearInterval(placeholderTimer);
  placeholderTimer = setInterval(() => {
    if (inputFocused) return;
    placeholderIdx = (placeholderIdx + 1) % PLACEHOLDERS.length;
    const input = document.getElementById("chat-input");
    if (input && !input.value) {
      input.placeholder = PLACEHOLDERS[placeholderIdx];
    }
  }, 4000);
}

function isChatMessagesNearBottom(messages) {
  return messages.scrollHeight - messages.scrollTop - messages.clientHeight <= 48;
}

function chatHistorySignature(nextTurns) {
  return JSON.stringify(nextTurns);
}

function trackPendingHistoryTurn(nextTurns) {
  const last = [...nextTurns].reverse().find((turn) => turn.scope === "chat");
  if (!last || (last.status !== "pending" && last.status !== "processing")) return;
  if (pendingTurnId === last.turn_id) return;
  pendingTurnId = last.turn_id;
  sending = true;
  pollForResponse();
}

// ── Send ─────────────────────────────────────────────────────
async function handleSend() {
  const input = document.getElementById("chat-input");
  const text = input?.value?.trim();
  if (!text || sending) return;

  sending = true;
  const turnId = `m-${Date.now()}-${Math.random().toString(36).slice(2, 8)}`;
  const replyToTurnId = dialogueContextSelection?.reply_to_turn_id || "";

  retainedDraft = "";
  input.value = "";
  turns.push({
    turn_id: turnId,
    message: text,
    response: null,
    status: "pending",
    reply_to_turn_id: replyToTurnId,
  });
  userScrolledUp = false;
  setDialogueStatus("阿B 正在整理这句话…", "info");
  render();

  try {
    await startChatTurn({ turnId, ...chatSession(), replyToTurnId, message: text });
    pendingTurnId = turnId;
    pollForResponse();
  } catch (error) {
    const t = turns.find((t) => t.turn_id === turnId);
    if (t) { t.status = "error"; t.error = "\u53D1\u9001\u5931\u8D25"; }
    retainedDraft = text;
    sending = false;
    setDialogueStatus(contextErrorMessage(error), "error");
    render();
  }
}

async function retryTurn(failedTurn) {
  if (sending) return;
  failedTurn.status = "pending";
  failedTurn.error = "";
  retainedDraft = "";
  sending = true;
  render();

  try {
    await startChatTurn({
      turnId: failedTurn.turn_id,
      ...chatSession(failedTurn.scope || "chat"),
      message: failedTurn.message,
      subjectId: failedTurn.subject_id || "",
      subjectTitle: failedTurn.subject_title || "",
      replyToTurnId: failedTurn.reply_to_turn_id || "",
    });
    pendingTurnId = failedTurn.turn_id;
    pollForResponse();
  } catch (error) {
    failedTurn.status = "error";
    failedTurn.error = "\u91CD\u8BD5\u5931\u8D25";
    sending = false;
    retainedDraft = failedTurn.message || "";
    setDialogueStatus(contextErrorMessage(error), "error");
    render();
  }
}

function updateDialogueTurn(turn) {
  if (!turn?.turn_id) return;
  const normalized = normalizeChatTurn(turn);
  const index = turns.findIndex((item) => item?.turn_id === normalized.turn_id);
  if (index >= 0) turns[index] = normalized;
  else turns.push(normalized);
  render();
}

export async function refreshPendingConfirmations({ renderNow = true } = {}) {
  if (!state.online) {
    if (state.pendingConfirmationCount !== 0) patchState({ pendingConfirmationCount: 0 });
    return;
  }
  try {
    const payload = await fetchPendingConfirmations({ session: "popup" });
    const count = Math.max(0, Number(payload?.count) || 0);
    pendingConfirmations = {
      ...pendingConfirmations,
      count,
      items: Array.isArray(payload?.items) ? payload.items : [],
    };
    if (state.pendingConfirmationCount !== count) patchState({ pendingConfirmationCount: count });
    if (renderNow) render();
  } catch {
    // Preserve the last successful list while the backend reconnects.
  }
}

async function handleDialogueCardAction(button) {
  const card = button.closest(".dialogue-card");
  const turnId = card?.dataset.dialogueTurnId || "";
  const action = button.dataset.cardAction || "";
  const turn = dialogueTurnsById.get(turnId);
  if (!turn || !action || button.disabled) return;
  button.disabled = true;
  try {
    const { response } = await executeCardAction(turn, action, {
      request(_path, body) {
        return actOnChatCard(turnId, body.action, {
          signal: dialogueCardActionAbortController.signal,
        });
      },
      fetchTurn(id, options) {
        return fetchChatTurn(id, options);
      },
      signal: dialogueCardActionAbortController.signal,
      onUpdate: updateDialogueTurn,
    });
    if (response?.outcome === "retryable_error") {
      const reason = String(response?.reason || "").toLowerCase();
      setDialogueStatus(
        reason === "stale_anchor" || reason === "anchor_dependency_failed"
          ? "你正在聊另一条，先结束那条再结算这张卡。"
          : "后端结果暂未同步，可以刷新或直接重试。",
        "error",
      );
      return;
    }
    if (action === "discuss") {
      await selectDialogueContext(turnId, response?.context_preview || null);
    } else if (dialogueContextSelection?.reply_to_turn_id === turnId) {
      storeDialogueContext(clearContextSelection());
    }
    await loadHistory();
    setDialogueStatus(
      action === "discuss"
        ? "好，沿着这条猜测继续聊。"
        : action === "defer"
          ? "先放一放，之后再聊。"
          : response?.state === "revised"
            ? "已按你的修正记下。"
            : action === "confirm"
              ? "已确认这条猜测。"
              : "已记下这条猜测不准。",
      "success",
    );
  } catch (error) {
    setDialogueStatus(contextErrorMessage(error), "error");
    render();
  }
}

async function handlePendingConfirmationOpen(button) {
  const ref = button.dataset.confirmationRef || "";
  if (!ref || button.disabled) return;
  button.disabled = true;
  button.textContent = "打开中…";
  try {
    const turn = await executePendingConfirmationOpen(ref, {
      session: "popup",
      signal: dialogueCardActionAbortController.signal,
      request(_path, body, { signal } = {}) {
        return openPendingConfirmation(ref, { session: body.session, signal });
      },
      onWaiting({ message }) {
        button.textContent = "等待中…";
        setDialogueStatus(`${message}，空闲后会自动打开。`, "info");
      },
    });
    if (turn?.turn_id) {
      updateDialogueTurn(turn);
      await selectDialogueContext(turn.turn_id);
    }
    await loadHistory();
    userScrolledUp = false;
    setDialogueStatus(
      isQuestionTurn(turn) ? "这条疑惑已经放进对话里。" : "这张确认卡已经放进对话里。",
      "success",
    );
    render();
    $root?.querySelector("#chat-input")?.focus({ preventScroll: true });
  } catch (error) {
    button.disabled = false;
    button.textContent = "打开";
    if (Number(error?.status) === 409) {
      await refreshPendingConfirmations();
      setDialogueStatus("另一条疑惑正在聊，待聊列表已经同步。", "error");
    } else if (error?.name !== "AbortError") {
      const detail = String(error?.details?.detail?.message || "").trim();
      setDialogueStatus(detail || "这条待聊内容暂时打不开，请稍后重试。", "error");
    }
  }
}

function pollForResponse() {
  if (!pendingTurnId) return;
  clearTimeout(pollTimer);
  pollTimer = setTimeout(async () => {
    try {
      const turn = normalizeChatTurn(await fetchChatTurn(pendingTurnId));
      const idx = turns.findIndex((t) => t.turn_id === pendingTurnId);
      if (idx >= 0) turns[idx] = turn;

      if (turn.status === "done" || turn.status === "completed" || turn.response) {
        pendingTurnId = null;
        sending = false;
        userScrolledUp = false;
        setDialogueStatus("这句已经记下了。", "success");
        render();
        void Promise.allSettled([refreshAfterChatTurn(), refreshPendingConfirmations()]);
      } else if (turn.status === "error" || turn.status === "failed") {
        pendingTurnId = null;
        sending = false;
        setDialogueStatus("这句处理失败了，可以重试。", "error");
        render();
      } else {
        render();
        pollForResponse();
      }
    } catch {
      pollForResponse();
    }
  }, 1500);
}

// ── Messages Overlay ─────────────────────────────────────────
function renderOverlay() {
  let overlay = document.querySelector(".messages-overlay");
  if (!overlay) {
    overlay = document.createElement("div");
    overlay.className = "messages-overlay";
    overlay.addEventListener("click", (e) => {
      if (e.target === overlay) toggleMessages();
    });
    document.body.appendChild(overlay);
  }
  overlay.classList.toggle("open", overlayOpen);

  if (!overlayOpen) {
    overlay.innerHTML = "";
    return;
  }

  const panel = document.createElement("div");
  panel.className = "messages-panel";

  // Header
  const header = document.createElement("div");
  header.className = "messages-header";
  header.innerHTML = `<span class="messages-title">\u6D88\u606F</span>`;
  const closeBtn = document.createElement("button");
  closeBtn.className = "messages-close";
  closeBtn.textContent = "\u2715";
  closeBtn.addEventListener("click", (e) => {
    e.stopPropagation();
    overlayOpen = false;
    renderOverlay();
  });
  header.appendChild(closeBtn);
  panel.appendChild(header);

  // Probe notifications
  for (const n of notifications) {
    const domain = n.domain || n.title || "";
    const isAvoidance = (n.type || "") === "avoidance.probe";
    const isChallenge = !isAvoidance && isChallengeProbe(n);
    const actions = isAvoidance ? getAvoidanceProbeMessageActions() : getProbeMessageActions();
    const pending = pendingProbeAction(n.type, domain);
    const card = document.createElement("div");
    card.className = `message-card ${isAvoidance ? "is-avoidance-probe" : isChallenge ? "is-challenge-probe" : "is-interest-probe"}`;
    card.dataset.probeDomain = domain;
    card.setAttribute("aria-busy", pending ? "true" : "false");
    card.classList.toggle("is-processing", Boolean(pending));
    const prompt = isAvoidance
      ? "想少看这类，就确认这是雷点；如果阿B猜错了，点不是。"
      : isChallenge
        ? "这是挑战方向，会把口味往侧边推一点；想继续试探就点喜欢，不准就点不喜欢。"
      : "想继续探索这个方向，就点喜欢；不准就点不喜欢。";
    card.innerHTML = `
      <div class="message-card-type"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><circle cx="11" cy="11" r="8"/><path d="m21 21-4.3-4.3"/></svg>${isAvoidance ? "避雷确认" : isChallenge ? "挑战探针" : "兴趣探测"}</div>
      <div class="message-card-prompt">${esc(prompt)}</div>
      <div class="message-card-title">${esc(domain)}</div>
      <div class="message-card-body">${esc(n.description || n.reason || n.message || "")}</div>
      <div class="message-card-actions">
        ${actions.map((item) => `
          <button type="button" class="message-action-btn ${item.primary ? "primary" : "secondary"}" data-probe="${esc(item.action)}" data-probe-kind="${isAvoidance ? "avoidance" : "interest"}" data-domain="${esc(n.domain || "")}">${esc(item.label)}</button>
        `).join("")}
      </div>`;
    for (const button of card.querySelectorAll("button")) {
      button.disabled = Boolean(pending);
    }
    panel.appendChild(card);
  }

  if (notifications.length === 0) {
    const emptyState = document.createElement("div");
    emptyState.className = "messages-empty-state";
    emptyState.innerHTML = `
      <svg width="40" height="40" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round">
        <path d="M6 8a6 6 0 0 1 12 0c0 7 3 9 3 9H3s3-2 3-9"/>
        <path d="M10.3 21a1.94 1.94 0 0 0 3.4 0"/>
      </svg>
      <span class="messages-empty-title">暂时没有新消息</span>
      <span class="messages-empty-subtitle">兴趣探测会在这里出现</span>`;
    panel.appendChild(emptyState);
  }

  overlay.innerHTML = "";
  overlay.appendChild(panel);

  // Bind probe actions
  for (const btn of panel.querySelectorAll("[data-probe]")) {
    btn.addEventListener("click", async () => {
      const domain = btn.dataset.domain;
      const action = btn.dataset.probe;
      const isAvoidance = btn.dataset.probeKind === "avoidance";
      const probeType = isAvoidance ? "avoidance.probe" : "interest.probe";
      const card = btn.closest(".message-card");
      if (action === "chat") {
        expandInlineChatOnCard(card, {
          scope: isAvoidance ? "avoidance_probe" : "probe",
          subjectId: domain,
          subjectTitle: domain,
          placeholder: isAvoidance
            ? `聊聊你为什么想避开「${domain}」…`
            : `聊聊你对「${domain}」的想法…`,
        });
        return;
      }
      const key = probeNotificationKey(probeType, domain);
      if (!key || pendingProbeActions.has(key)) return;
      pendingProbeActions.set(key, { response: action });
      setProbeCardBusy(card, true);
      renderOverlay();
      try {
        await (isAvoidance
          ? respondToAvoidanceProbe(domain, action)
          : respondToProbe(domain, action));
        pendingProbeActions.delete(key);
        rememberHandledProbe(domain, probeType);
        notifications = removeProbeFromNotifications(notifications, domain, probeType);
        updateBadgeCount();
        renderOverlay();
      } catch {
        pendingProbeActions.delete(key);
        setProbeCardBusy(card, false);
        renderOverlay();
      }
    });
  }

  // Bind delight actions
  for (const btn of panel.querySelectorAll("[data-delight]")) {
    btn.addEventListener("click", async () => {
      const bvid = btn.dataset.bvid;
      const action = btn.dataset.delight;
      const title = btn.dataset.title || "";

      if (action === "chat") {
        const card = btn.closest(".message-card");
        expandInlineChatOnCard(card, {
          scope: "delight",
          subjectId: bvid,
          subjectTitle: title,
          placeholder: `聊聊你对「${title}」的想法…`,
        });
        return;
      }

      const { apiResponse, permanent } = getDelightActionState(action);
      btn.disabled = true;

      if (apiResponse) {
        try { await respondToDelight(bvid, apiResponse, title); } catch { /* best-effort */ }
      }
      if (permanent) {
        markDelightSent(bvid).catch(() => {});
        delightMsgs = delightMsgs.filter((d) => d.bvid !== bvid);
        updateBadgeCount();
        renderOverlay();
      } else {
        delightMsgs = delightMsgs.map((d) =>
          d.bvid === bvid
            ? {
                ...d,
                state: action === "like" ? "liked" : action === "view" ? "viewed" : d.state,
                response_message: action === "like"
                  ? "好，这类多来点。"
                  : action === "view"
                    ? "已打开，阿B 会把这次点击当成强信号。"
                    : d.response_message,
              }
            : d
        );
        updateBadgeCount();
        renderOverlay();
      }

      if (action === "view") {
        const item = normalizeDelightCandidate({ bvid, title });
        const url = buildContentUrl(item);
        if (url) openContentUrl(url);
      }
    });
  }
}

function updateBadgeCount() {
  const msgs = { notifications: [...notifications], delights: [] };
  patchState({ messages: msgs });
  setUnreadCount(notifications.length);
}

// ── Load ─────────────────────────────────────────────────────
async function loadHistory() {
  if (!state.online || historyRefreshInFlight) return;
  historyRefreshInFlight = true;
  const existingMessages = document.getElementById("chat-messages");
  const shouldStickToBottom =
    !(existingMessages instanceof HTMLElement) || isChatMessagesNearBottom(existingMessages);
  const previousScrollTop = existingMessages instanceof HTMLElement ? existingMessages.scrollTop : 0;
  try {
    const [historyResult, pendingResult] = await Promise.allSettled([
      fetchChatTurns({ session: "popup", limit: 100 }),
      fetchPendingConfirmations({ session: "popup" }),
    ]);
    let changed = false;
    if (historyResult.status === "fulfilled") {
      const data = historyResult.value;
      const nextTurns = Array.isArray(data?.items || data?.turns)
        ? (data.items || data.turns).map(normalizeChatTurn)
        : [];
      trackPendingHistoryTurn(nextTurns);
      const signature = chatHistorySignature(nextTurns);
      if (signature !== lastHistorySignature) {
        lastHistorySignature = signature;
        turns = nextTurns;
        changed = true;
      }
    }
    if (pendingResult.status === "fulfilled") {
      const payload = pendingResult.value;
      const nextPending = {
        count: Math.max(0, Number(payload?.count) || 0),
        items: Array.isArray(payload?.items) ? payload.items : [],
      };
      if (
        nextPending.count !== pendingConfirmations.count ||
        JSON.stringify(nextPending.items) !== JSON.stringify(pendingConfirmations.items)
      ) {
        pendingConfirmations = { ...pendingConfirmations, ...nextPending };
        changed = true;
      }
      if (state.pendingConfirmationCount !== nextPending.count) {
        patchState({ pendingConfirmationCount: nextPending.count });
      }
    }
    const contextBefore = dialogueContextSelection?.reply_to_turn_id || "";
    await validateDialogueContext({ announce: true });
    if ((dialogueContextSelection?.reply_to_turn_id || "") !== contextBefore) {
      changed = true;
    }
    if (!changed) return;
    render();
    if (!shouldStickToBottom) {
      window.requestAnimationFrame(() => {
        const messages = document.getElementById("chat-messages");
        if (!(messages instanceof HTMLElement)) return;
        messages.scrollTop = Math.min(
          previousScrollTop,
          Math.max(0, messages.scrollHeight - messages.clientHeight),
        );
      });
    }
  } catch {
    // Keep the last durable snapshot while offline.
  } finally {
    historyRefreshInFlight = false;
  }
}

function startChatHistorySync() {
  if (historyRefreshTimer !== null) return;
  historyRefreshTimer = window.setInterval(() => {
    if (state.activeTab !== "chat" || document.hidden || !state.online) return;
    void loadHistory();
  }, CHAT_HISTORY_REFRESH_INTERVAL_MS);
}

async function refreshAfterChatTurn() {
  try {
    const [profileResult, activityResult] = await Promise.allSettled([
      fetchProfileSummary({ limit: 5 }),
      fetchActivityFeed({ limit: 5 }),
    ]);
    const next = {};
    if (profileResult.status === "fulfilled") {
      next.profile = normalizeProfileSummary(profileResult.value);
    }
    if (activityResult.status === "fulfilled") {
      next.activityFeed = normalizeActivityFeed(activityResult.value);
    }
    if (Object.keys(next).length > 0) patchState(next);
  } catch { /* best-effort */ }
}

export async function loadNotifications({ includeDelights = false } = {}) {
  try {
    const [notifData, probeData, avoidanceProbeData, delightData] = await Promise.all([
      fetchPendingNotifications().catch(() => ({})),
      fetchPendingProbes().catch(() => []),
      fetchPendingAvoidanceProbes().catch(() => []),
      includeDelights ? fetchDelightBatch().catch(() => []) : Promise.resolve(delightMsgs),
    ]);
    // Start with persisted probes from backend
    const probes = [
      ...(Array.isArray(probeData) ? probeData.map((p) => ({ ...p, type: "interest.probe" })) : []),
      ...(Array.isArray(avoidanceProbeData)
        ? avoidanceProbeData.map((p) => ({ ...p, type: "avoidance.probe" }))
        : []),
    ];
    notifications = mergeProbeNotifications(probes, notifications);
    if (includeDelights) {
      delightMsgs = delightData;
    }
    updateBadgeCount();
  } catch { /* ignore */ }
  // Re-render if overlay is visible so first-click shows real data
  if (overlayOpen) renderOverlay();
}

// ── Public API ───────────────────────────────────────────────
export function initChatView(root) {
  $root = root;
  startChatHistorySync();
  if (!loaded) {
    loaded = true;
    loadNotifications();
  }
  loadHistory();
}

export async function toggleMessages() {
  overlayOpen = !overlayOpen;
  if (overlayOpen) {
    renderOverlay();          // show panel immediately (loading state)
    await loadNotifications({ includeDelights: true });
    renderOverlay();          // re-render with actual data
  } else {
    renderOverlay();
  }
}

export function updateBadge() {
  updateBadgeCount();
}

export function onStreamEvent(payload) {
  if (pendingConfirmationRefreshTimer !== null) {
    window.clearTimeout(pendingConfirmationRefreshTimer);
  }
  pendingConfirmationRefreshTimer = window.setTimeout(() => {
    pendingConfirmationRefreshTimer = null;
    void refreshPendingConfirmations();
  }, 300);
  const type = payload?.type || payload?.event;
  if (type === "interest.probe" || type === "avoidance.probe") {
    const item = payload.data || payload;
    if (shouldDisplayProbeFromWebSocket(item, type)) {
      notifications = mergeProbeNotifications(notifications, [{ ...item, type }]);
      updateBadgeCount();
    }
  } else if (type === "delight.liked") {
    const data = payload.data || payload;
    const bvid = data?.bvid || data?.domain;
    if (bvid) {
      delightMsgs = delightMsgs.map((d) =>
        d.bvid === bvid
          ? { ...d, state: "liked", response_message: data?.message || "好，这类多来点。" }
          : d
      );
      if (overlayOpen) renderOverlay();
    }
  } else if (type === "delight.disliked") {
    // Negative feedback from another client removes the message locally.
    const bvid = (payload.data || payload)?.bvid || (payload.data || payload)?.domain;
    if (bvid) {
      const before = delightMsgs.length;
      delightMsgs = delightMsgs.filter((d) => d.bvid !== bvid);
      if (delightMsgs.length !== before) {
        if (overlayOpen) renderOverlay();
      }
    }
  }
}

/**
 * Start a contextual chat from delight "聊一聊" or probe "多聊聊".
 * Legacy: navigates to chat tab. Prefer expandInlineChatOnCard for overlay cards.
 */
export async function startContextualChat({ scope, subjectId, subjectTitle, message }) {
  patchState({ pendingChatContext: { scope, subjectId, subjectTitle } });
  navigateToTab("chat");

  if (!message) return; // render() will pre-fill composer text

  const turnId = `m-${Date.now()}-${Math.random().toString(36).slice(2, 8)}`;
  turns.push({
    turn_id: turnId, message, response: null, status: "pending",
    scope, subject_id: subjectId, subject_title: subjectTitle,
  });
  sending = true;
  userScrolledUp = false;
  render();

  try {
    await startChatTurn({ turnId, ...chatSession(scope), subjectId, subjectTitle, message });
    pendingTurnId = turnId;
    pollForResponse();
  } catch {
    const t = turns.find((t) => t.turn_id === turnId);
    if (t) { t.status = "error"; t.error = "\u53D1\u9001\u5931\u8D25"; }
    sending = false;
    render();
  }
}

/**
 * Expand inline chat within a message card (probe or delight).
 * Replaces action buttons with a textarea + send button, sends the turn
 * to the backend, shows the reply inline, then removes the card.
 */
function expandInlineChatOnCard(card, { scope, subjectId, subjectTitle, placeholder }) {
  if (!card || card.querySelector(".inline-chat-area")) return;

  // Hide action buttons
  const actions = card.querySelector(".message-card-actions");
  if (actions) actions.style.display = "none";

  const chatArea = document.createElement("div");
  chatArea.className = "inline-chat-area";

  const input = document.createElement("textarea");
  input.className = "inline-chat-input";
  input.rows = 2;
  input.placeholder = placeholder || "聊聊你的想法…";

  const sendBtn = document.createElement("button");
  sendBtn.className = "inline-chat-send";
  sendBtn.textContent = "发送";

  async function doSend() {
    const message = input.value.trim();
    if (!message) return;
    sendBtn.disabled = true;
    input.disabled = true;

    // Show thinking indicator
    const thinking = document.createElement("div");
    thinking.className = "inline-chat-thinking";
    thinking.textContent = "阿B 正在思考…";
    chatArea.appendChild(thinking);

    const turnId = `m-${Date.now()}-${Math.random().toString(36).slice(2, 8)}`;
    const isProbeScope = scope === "probe" || scope === "avoidance_probe";
    const probeType = scope === "avoidance_probe" ? "avoidance.probe" : "interest.probe";
    if (isProbeScope) {
      rememberHandledProbe(subjectId, probeType);
    }
    try {
      const turn = await startChatTurn({
        turnId,
        ...chatSession(scope),
        subjectId,
        subjectTitle,
        message,
      });

      // Only a completed turn consumes a probe notification. Failed turns
      // keep the composer/card available so the user can retry.
      const showReply = (t) => {
        thinking.remove();
        input.remove();
        sendBtn.remove();
        const replyEl = document.createElement("div");
        replyEl.className = "inline-chat-reply chat-markdown";
        replyEl.innerHTML = renderMarkdown(t.reply || t.response || "收到了，我会结合这个方向继续观察。");
        chatArea.appendChild(replyEl);
        setTimeout(() => {
          const domain = subjectId;
          if (isProbeScope) {
            notifications = removeProbeFromNotifications(notifications, domain, probeType);
          }
          updateBadgeCount();
          renderOverlay();
        }, 3500);
      };

      const settleTurn = (t) => {
        if (t.status === "failed") {
          thinking.remove();
          sendBtn.disabled = false;
          input.disabled = false;
          if (isProbeScope) {
            forgetHandledProbe(subjectId, probeType);
          }
          const errEl = document.createElement("div");
          errEl.className = "inline-chat-error";
          errEl.textContent = t.error || "刚刚没发出去，换个说法再试试。";
          chatArea.appendChild(errEl);
          return;
        }
        if (t.status === "completed") showReply(t);
      };

      if (turn.status === "completed" || turn.status === "failed") {
        settleTurn(turn);
      } else {
        // Poll until settled
        const poll = async () => {
          try {
            const t = await fetchChatTurn(turnId);
            if (t.status === "completed" || t.status === "failed") {
              settleTurn(t);
            } else {
              setTimeout(poll, 1500);
            }
          } catch {
            setTimeout(poll, 2000);
          }
        };
        setTimeout(poll, 1500);
      }
    } catch {
      thinking.remove();
      sendBtn.disabled = false;
      input.disabled = false;
      if (isProbeScope) {
        forgetHandledProbe(subjectId, probeType);
      }
      const errEl = document.createElement("div");
      errEl.className = "inline-chat-error";
      errEl.textContent = "后台正忙，等一下再聊。";
      chatArea.appendChild(errEl);
      setTimeout(() => errEl.remove(), 3000);
    }
  }

  sendBtn.addEventListener("click", doSend);
  input.addEventListener("keydown", (e) => {
    if (e.key === "Enter" && !e.shiftKey) {
      e.preventDefault();
      doSend();
    }
  });

  chatArea.append(input, sendBtn);
  card.appendChild(chatArea);
  input.focus();
}
