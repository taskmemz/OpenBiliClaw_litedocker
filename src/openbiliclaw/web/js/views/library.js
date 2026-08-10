/** Compact content library shell for saved lists and the 30-day history view. */

import { initWatchLaterView, initFavoritesView } from "./saved.js";
import { initHistoryView } from "./history.js";

const STORAGE_KEY = "openbiliclaw.mobile.contentLibraryTab";
const TABS = [
  { id: "watchLater", slug: "watch-later", label: "稍后再看", panel: "mobileLibraryWatchLaterPanel" },
  { id: "favorites", slug: "favorites", label: "收藏", panel: "mobileLibraryFavoritesPanel" },
  { id: "history", slug: "history", label: "历史记录", panel: "mobileLibraryHistoryPanel" },
];
const LEGACY_ALIASES = new Map([
  ["watchlater", "watchLater"],
  ["watch-later", "watchLater"],
  ["watch_later", "watchLater"],
  ["favorites", "favorites"],
  ["favorite", "favorites"],
  ["history", "history"],
]);

let $root = null;
let activeTab = "watchLater";
let onSelectTab = null;
const scrollPositions = new Map();
let isVisible = false;

export function normalizeContentLibraryTab(value, fallback = "watchLater") {
  const normalized = String(value || "").trim().toLowerCase();
  return LEGACY_ALIASES.get(normalized) || fallback;
}

export function contentLibrarySlug(value) {
  const id = normalizeContentLibraryTab(value);
  return TABS.find((tab) => tab.id === id)?.slug || "watch-later";
}

export function storedContentLibraryTab() {
  try {
    return normalizeContentLibraryTab(localStorage.getItem(STORAGE_KEY));
  } catch {
    return "watchLater";
  }
}

function persistContentLibraryTab(tab) {
  try { localStorage.setItem(STORAGE_KEY, tab); } catch { /* storage unavailable */ }
}

function initializeChild(tab) {
  const panel = document.getElementById(TABS.find((entry) => entry.id === tab)?.panel || "");
  if (!(panel instanceof HTMLElement)) return;
  if (tab === "watchLater") initWatchLaterView(panel);
  else if (tab === "favorites") initFavoritesView(panel);
  else initHistoryView(panel);
}

function activateContentLibraryTab(value, {
  focus = false,
  notify = false,
  forceInit = false,
  restoreScroll = false,
} = {}) {
  const nextTab = normalizeContentLibraryTab(value, activeTab);
  const scroller = document.getElementById("app");
  const changed = activeTab !== nextTab;
  if (scroller instanceof HTMLElement && changed) {
    scrollPositions.set(activeTab, scroller.scrollTop);
  }
  activeTab = nextTab;
  persistContentLibraryTab(nextTab);

  for (const tab of TABS) {
    const button = document.getElementById(`mobileLibraryTab${tab.id}`);
    const panel = document.getElementById(tab.panel);
    const selected = tab.id === nextTab;
    if (button instanceof HTMLButtonElement) {
      button.classList.toggle("is-active", selected);
      button.setAttribute("aria-selected", String(selected));
      button.tabIndex = selected ? 0 : -1;
    }
    if (panel instanceof HTMLElement) panel.hidden = !selected;
  }

  if (changed || forceInit) initializeChild(nextTab);
  if (changed || restoreScroll) requestAnimationFrame(() => {
    if (scroller instanceof HTMLElement) scroller.scrollTop = scrollPositions.get(nextTab) || 0;
    if (focus) document.getElementById(`mobileLibraryTab${nextTab}`)?.focus();
  });
  else if (focus) document.getElementById(`mobileLibraryTab${nextTab}`)?.focus();
  if (notify) onSelectTab?.(nextTab);
}

function wireLibraryTabs() {
  const buttons = TABS.map((tab) => document.getElementById(`mobileLibraryTab${tab.id}`))
    .filter((button) => button instanceof HTMLButtonElement);
  buttons.forEach((button, index) => {
    button.addEventListener("click", () => {
      activateContentLibraryTab(TABS[index].id, { notify: true, forceInit: true });
    });
    button.addEventListener("keydown", (event) => {
      let nextIndex = null;
      if (event.key === "ArrowRight") nextIndex = (index + 1) % buttons.length;
      else if (event.key === "ArrowLeft") nextIndex = (index - 1 + buttons.length) % buttons.length;
      else if (event.key === "Home") nextIndex = 0;
      else if (event.key === "End") nextIndex = buttons.length - 1;
      if (nextIndex === null) return;
      event.preventDefault();
      activateContentLibraryTab(TABS[nextIndex].id, { focus: true, notify: true });
    });
  });
}

function renderShell() {
  if (!$root) return;
  $root.innerHTML = `
    <section class="content-library-view" aria-labelledby="mobileContentLibraryTitle">
      <header class="content-library-head">
        <p class="eyebrow">Your library</p>
        <h1 id="mobileContentLibraryTitle">内容库</h1>
        <p>稍后再看、收藏和近 30 天历史，都收在这里。</p>
      </header>
      <div class="content-library-tabs" role="tablist" aria-label="内容库分类">
        ${TABS.map((tab) => `<button id="mobileLibraryTab${tab.id}" class="content-library-tab" type="button" role="tab" aria-selected="false" aria-controls="${tab.panel}" tabindex="-1">${tab.label}</button>`).join("")}
      </div>
      ${TABS.map((tab) => `<section id="${tab.panel}" class="content-library-panel" role="tabpanel" aria-labelledby="mobileLibraryTab${tab.id}" hidden></section>`).join("")}
    </section>`;
  wireLibraryTabs();
}

export function initContentLibraryView(root, { tab = storedContentLibraryTab(), onSelect } = {}) {
  const entering = !isVisible;
  if ($root !== root) {
    $root = root;
    scrollPositions.clear();
    onSelectTab = onSelect;
    renderShell();
  } else if (onSelect) {
    onSelectTab = onSelect;
  }
  isVisible = true;
  activateContentLibraryTab(tab, { forceInit: entering, restoreScroll: entering });
}

export function leaveContentLibraryView() {
  if (!isVisible) return;
  const scroller = document.getElementById("app");
  if (scroller instanceof HTMLElement) scrollPositions.set(activeTab, scroller.scrollTop);
  isVisible = false;
}
