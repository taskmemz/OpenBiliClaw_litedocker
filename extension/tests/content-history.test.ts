import test from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { resolve } from "node:path";
import {
  CONTENT_HISTORY_READ_TIMEOUT_MS,
  fetchContentHistory,
  reconcileContentHistoryPage,
} from "../popup/popup-api.js";

test("popup nests bounded lazy history in the four-item content library", () => {
  const html = readFileSync(resolve("popup", "popup.html"), "utf8");
  const app = readFileSync(resolve("popup", "popup.js"), "utf8");
  const api = readFileSync(resolve("popup", "popup-api.js"), "utf8");
  const outerTabs = html.match(/<div class="tab-bar"[\s\S]*?<\/div>\s*<\/div>/)?.[0] ?? "";

  assert.equal((outerTabs.match(/class="tab-button/g) || []).length, 4);
  assert.match(html, /id="tabLibrary"[^>]*role="tab"[^>]*aria-controls="viewLibrary"/);
  assert.match(html, /id="viewLibrary"[^>]*role="tabpanel"/);
  assert.match(html, /class="library-tabs"[^>]*role="tablist"/);
  assert.match(html, /id="tabHistory"[^>]*role="tab"[^>]*aria-controls="viewHistory"/);
  assert.match(html, /id="viewHistory"[^>]*role="tabpanel"/);
  assert.match(html, /id="historySections"/);
  assert.match(api, /export async function fetchContentHistory/);
  assert.match(api, /`\/content-history\?\$\{params\}`/);
  assert.match(api, /timeoutMs: CONTENT_HISTORY_READ_TIMEOUT_MS/);
  assert.match(app, /CONTENT_HISTORY_PAGE_SIZE = 12/);
  assert.match(app, /image\.setAttribute\("loading", "lazy"\)/);
  assert.match(app, /image\.setAttribute\("fetchpriority", "low"\)/);
  assert.match(app, /append \? page\.nextCursor : ""/);
  assert.match(app, /Array\.isArray\(item\?\.contexts\)/);
  assert.match(app, /await saveItem\(context\.context, item\)/);
  assert.match(app, /restoreContentHistoryFocus\(focusToken/);
  assert.match(app, /近 30 天还没有这类记录/);
});

test("content history omits an empty initial cursor and sends an opaque continuation cursor", async () => {
  const originalFetch = globalThis.fetch;
  const calls: Array<{ url: string; options: RequestInit }> = [];
  globalThis.fetch = (async (url, options = {}) => {
    calls.push({ url: String(url), options });
    return {
      ok: true,
      status: 200,
      async json() {
        return { items: [], total: 0, has_more: false, next_cursor: "" };
      },
    } as Response;
  }) as typeof fetch;

  try {
    const first = await fetchContentHistory("clicked", 12, "");
    await fetchContentHistory("clicked", 12, "opaque-page-2");
    assert.deepEqual(first.items, []);
    assert.equal(first.total, 0);
    assert.equal(CONTENT_HISTORY_READ_TIMEOUT_MS, 12_000);
    assert.equal(calls.length, 2);
    assert.match(calls[0].url, /\/api\/content-history\?category=clicked&limit=12$/);
    assert.doesNotMatch(calls[0].url, /(?:cursor|offset)=/);
    assert.match(calls[1].url, /\/api\/content-history\?category=clicked&limit=12&cursor=opaque-page-2$/);
    assert.equal(calls[0].options.method, "GET");
    assert.ok(calls[0].options.signal instanceof AbortSignal);
  } finally {
    globalThis.fetch = originalFetch;
  }
});

test("cursor pagination appends a stable page and follows server has_more", () => {
  const first = reconcileContentHistoryPage({
    incomingItems: [
      { item_key: "bilibili:a" },
      { item_key: "bilibili:b" },
    ],
    incomingTotal: 4,
    nextCursor: "page-2",
    hasMore: true,
  });
  const second = reconcileContentHistoryPage({
    items: first.items,
    incomingItems: [
      { item_key: "bilibili:c" },
      { item_key: "bilibili:d" },
    ],
    incomingTotal: 4,
    nextCursor: "ignored-when-complete",
    hasMore: false,
    append: true,
  });

  assert.equal(first.nextCursor, "page-2");
  assert.equal(first.hasMore, true);
  assert.equal(second.nextCursor, "");
  assert.equal(second.hasMore, false);
  assert.deepEqual(second.items.map((item) => item.item_key), [
    "bilibili:a",
    "bilibili:b",
    "bilibili:c",
    "bilibili:d",
  ]);
});

test("cursor pagination de-duplicates canonical item keys without discarding the page", () => {
  const result = reconcileContentHistoryPage({
    items: [
      { item_key: "bilibili:a" },
      { item_key: "bilibili:b" },
    ],
    incomingItems: [
      { item_key: "bilibili:b" },
      { item_key: "bilibili:c" },
    ],
    incomingTotal: 3,
    nextCursor: "",
    hasMore: false,
    append: true,
  });

  assert.deepEqual(result.reasons, ["duplicate_item_key"]);
  assert.deepEqual(result.items.map((item) => item.item_key), [
    "bilibili:a",
    "bilibili:b",
    "bilibili:c",
  ]);
});

test("cursor pagination ignores malformed rows and multiple removed contexts restore independently", () => {
  const result = reconcileContentHistoryPage({
    items: [{ item_key: "bilibili:a" }],
    incomingItems: [{ title: "missing canonical identity" }],
    incomingTotal: 1,
    append: true,
  });
  const app = readFileSync(resolve("popup", "popup.js"), "utf8");

  assert.deepEqual(result.reasons, ["missing_item_key"]);
  assert.deepEqual(result.items.map((item) => item.item_key), ["bilibili:a"]);
  assert.match(app, /for \(const context of contexts\)/);
  assert.match(app, /\["watch_later", "favorite"\]\.includes\(context\.context\)/);
  assert.match(app, /context\.restored = true/);
  assert.match(app, /restore\.dataset\.historyContext = context\.context/);
});
