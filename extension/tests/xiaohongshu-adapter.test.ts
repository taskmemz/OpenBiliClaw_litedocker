/**
 * Tests for the Xiaohongshu platform adapter.
 *
 * Mirrors twitter-adapter.test.ts: URL → note id, page-type detection,
 * action inference (incl. the deliberate icon-only → null degradation the
 * MAIN-world action tap now backstops), tap-authoritative declaration, and
 * event metadata shape. Closes the last remaining adapter-test gap called out
 * in the event-capture-completion spec (§D4).
 */

import test from "node:test";
import assert from "node:assert/strict";

import {
  xiaohongshuAdapter,
  detectXiaohongshuPageType,
  extractNoteId,
} from "../src/shared/platforms/xiaohongshu.ts";

const NOTE_ID = "69dea966000000001a0280ad";

test("xiaohongshuAdapter exposes the correct source identity", () => {
  assert.equal(xiaohongshuAdapter.sourcePlatform, "xiaohongshu");
});

test("xiaohongshuAdapter declares the action tap authoritative for like/favorite/retraction", () => {
  // The MAIN-world xhs-action-tap emits the real like/favorite and their
  // withdrawals (retraction); the DOM click path must suppress each so it
  // never double-counts nor misfires on icon buttons. comment / share have no
  // tap on xhs and stay DOM-sourced.
  const actions = xiaohongshuAdapter.tapAuthoritativeActions;
  assert.ok(actions instanceof Set);
  for (const action of ["like", "favorite", "retraction"]) {
    assert.ok(actions?.has(action), action);
  }
  for (const action of ["comment", "share"]) {
    assert.equal(actions?.has(action), false, action);
  }
  // The old coarse flag never existed on xhs; make sure none crept in.
  assert.equal(
    (xiaohongshuAdapter as { strongSignalSource?: unknown }).strongSignalSource,
    undefined,
  );
});

test("extractContentId / extractNoteId pull the 24-hex note id from all three URL shapes", () => {
  assert.equal(
    xiaohongshuAdapter.extractContentId(`https://www.xiaohongshu.com/explore/${NOTE_ID}`),
    NOTE_ID,
  );
  assert.equal(
    extractNoteId(`https://www.xiaohongshu.com/discovery/item/${NOTE_ID}?xsec_token=Z`),
    NOTE_ID,
  );
  assert.equal(
    extractNoteId(`https://www.xiaohongshu.com/search_result/${NOTE_ID}`),
    NOTE_ID,
  );
  // Case-insensitive on the hex.
  assert.equal(
    extractNoteId(`https://www.xiaohongshu.com/explore/${NOTE_ID.toUpperCase()}`),
    NOTE_ID.toUpperCase(),
  );
});

test("extractNoteId returns null for non-note / non-24-hex URLs (id-shape boundary)", () => {
  assert.equal(extractNoteId("https://www.xiaohongshu.com/explore"), null);
  assert.equal(extractNoteId("https://www.xiaohongshu.com/user/profile/abc"), null);
  // 23 hex chars — one short of a note id.
  assert.equal(extractNoteId("https://www.xiaohongshu.com/explore/69dea966000000001a0280a"), null);
  // 24 chars but not all hex.
  assert.equal(extractNoteId("https://www.xiaohongshu.com/explore/zzdea966000000001a0280ad"), null);
});

test("detectXiaohongshuPageType classifies search / note / user / home", () => {
  assert.equal(detectXiaohongshuPageType("https://www.xiaohongshu.com/search_result?keyword=x"), "search");
  assert.equal(
    detectXiaohongshuPageType(`https://www.xiaohongshu.com/search_result/${NOTE_ID}`),
    "note",
  );
  assert.equal(detectXiaohongshuPageType(`https://www.xiaohongshu.com/explore/${NOTE_ID}`), "note");
  assert.equal(
    detectXiaohongshuPageType(`https://www.xiaohongshu.com/discovery/item/${NOTE_ID}`),
    "note",
  );
  assert.equal(detectXiaohongshuPageType("https://www.xiaohongshu.com/user/profile/123"), "user");
  assert.equal(detectXiaohongshuPageType("https://www.xiaohongshu.com/explore"), "home");
  assert.equal(detectXiaohongshuPageType("https://www.xiaohongshu.com/"), "home");
});

test("inferActionType maps zh/en action vocabulary from text/aria/className", () => {
  assert.equal(
    xiaohongshuAdapter.inferActionType({ text: "点赞", ariaLabel: null, className: "" }),
    "like",
  );
  assert.equal(
    xiaohongshuAdapter.inferActionType({ text: "", ariaLabel: "Like", className: "" }),
    "like",
  );
  assert.equal(
    xiaohongshuAdapter.inferActionType({ text: "收藏", ariaLabel: null, className: "" }),
    "favorite",
  );
  assert.equal(
    xiaohongshuAdapter.inferActionType({ text: "", ariaLabel: "collect", className: "" }),
    "favorite",
  );
  assert.equal(
    xiaohongshuAdapter.inferActionType({ text: "评论", ariaLabel: null, className: "" }),
    "comment",
  );
  assert.equal(
    xiaohongshuAdapter.inferActionType({ text: "分享", ariaLabel: null, className: "" }),
    "share",
  );
});

test("inferActionType returns null for icon-only buttons with no text/aria (why the tap exists)", () => {
  // This is the exact gap the MAIN-world action tap backstops: xhs like/collect
  // are frequently text-less icon buttons, so DOM keyword matching returns null.
  // Locking the current behavior documents why like/favorite must come from the
  // network tap, not this fallback.
  // A generic icon button whose class tokens carry no action keyword: the
  // fallback has nothing to match on → null (so the network tap is the only
  // reliable source for this like/collect click).
  assert.equal(
    xiaohongshuAdapter.inferActionType({ text: "", ariaLabel: null, className: "reds-icon interact-btn" }),
    null,
  );
  assert.equal(
    xiaohongshuAdapter.inferActionType({ text: "  ", ariaLabel: "", className: "" }),
    null,
  );
});

test("buildEventMetadata returns the note_id (null off a note URL)", () => {
  assert.deepEqual(
    xiaohongshuAdapter.buildEventMetadata(`https://www.xiaohongshu.com/explore/${NOTE_ID}`),
    { note_id: NOTE_ID },
  );
  assert.deepEqual(xiaohongshuAdapter.buildEventMetadata("https://www.xiaohongshu.com/"), {
    note_id: null,
  });
});

test("adapter exposes card + search-input selectors and no video selector", () => {
  assert.equal(typeof xiaohongshuAdapter.cardSelector, "string");
  assert.ok(xiaohongshuAdapter.cardSelector.length > 0);
  assert.equal(typeof xiaohongshuAdapter.searchInputSelector, "string");
  assert.ok(xiaohongshuAdapter.searchInputSelector.length > 0);
  assert.equal(xiaohongshuAdapter.videoSelector, null);
  assert.deepEqual(xiaohongshuAdapter.dwellPageTypes, ["note"]);
});
