/**
 * Xiaohongshu (小红书) platform adapter. MVP scope: capture snapshot,
 * click, scroll, and search events. Like/collect/comment DOM is
 * unstable on xhs and left out of this phase.
 */

import type { ActionHint, PageType, PlatformAdapter } from "../types.js";
import { queryParam } from "./search-query.ts";

// 24-char hex note ids (e.g. "69dea966000000001a0280ad").
const NOTE_ID_PATTERN = /\/(?:explore|discovery\/item|search_result)\/([0-9a-f]{24})/i;

const CARD_SELECTOR = [
  'a[href*="/explore/"]',
  'a[href*="/discovery/item/"]',
  'a[href*="/search_result/"]',
  ".note-item",
  ".feeds-page .note-item",
].join(",");

const SEARCH_INPUT_SELECTOR =
  'input[placeholder*="搜索"], input[type="search"], .search-input input';

export function detectXiaohongshuPageType(url: string): PageType {
  if (/\/search_result\/[0-9a-f]{24}(?:[/?#]|$)/i.test(url)) return "note";
  if (url.includes("/search_result")) return "search";
  if (url.includes("/explore/") || url.includes("/discovery/item/")) return "note";
  if (url.includes("/user/profile/")) return "user";
  if (url.includes("/explore")) return "home";
  return "home";
}

export function extractNoteId(url: string): string | null {
  const match = url.match(NOTE_ID_PATTERN);
  return match ? match[1] : null;
}

function normalizeText(value: string | null | undefined): string {
  return (value ?? "").trim();
}

function inferXiaohongshuActionType(hint: ActionHint): string | null {
  // xhs shares the Chinese action vocabulary with bilibili — we match on
  // text/aria-label first and fall back to className tokens for icon-only
  // buttons. There is no "投币" on xhs, so coin is intentionally absent.
  const text = `${normalizeText(hint.text)} ${normalizeText(hint.ariaLabel)} ${hint.className}`
    .toLowerCase();
  if (!text) return null;
  if (text.includes("点赞") || text.includes("like")) return "like";
  if (text.includes("收藏") || text.includes("collect") || text.includes("favorite")) {
    return "favorite";
  }
  if (text.includes("评论") || text.includes("comment")) return "comment";
  if (text.includes("分享") || text.includes("share")) return "share";
  return null;
}

export const xiaohongshuAdapter: PlatformAdapter = {
  sourcePlatform: "xiaohongshu",
  // The MAIN-world action tap (`main/xhs-action-tap.ts`) emits the
  // authoritative like / favorite signal from the write endpoints and their
  // withdrawals as retractions. The DOM click path must suppress all three so
  // it never double-counts the tap nor records icon-button misfires. comment /
  // share have no tap and keep flowing through the DOM.
  tapAuthoritativeActions: new Set(["like", "favorite", "retraction"]),
  detectPageType: detectXiaohongshuPageType,
  extractContentId: extractNoteId,
  extractSearchQuery: (url) => queryParam(url, "keyword"),
  cardSelector: CARD_SELECTOR,
  searchInputSelector: SEARCH_INPUT_SELECTOR,
  videoSelector: null,
  dwellPageTypes: ["note"],
  inferActionType: inferXiaohongshuActionType,
  buildEventMetadata(url: string): Record<string, unknown> {
    return { note_id: extractNoteId(url) };
  },
};
