/**
 * Douyin DOM-driven bootstrap extractor.
 *
 * Why this exists: Douyin's user-tab routes (作品 / 喜欢 / 收藏 /
 * 关注) are React-Router driven and frequently re-render WITHOUT
 * issuing a fresh /aweme/v1/web/aweme/<scope>/ XHR — verified
 * empirically (2026-05-08 e2e + url_probe). The MAIN-world XHR/fetch
 * tap captures the few requests that *do* fire (mostly the initial
 * landing) but misses everything served from React state. DOM
 * extraction harvests whatever is rendered RIGHT NOW, mirroring the
 * approach the XHS bootstrap takes.
 *
 * Used in two ways:
 *   1. Standalone for scopes 喜欢/收藏 where XHR rarely fires.
 *   2. As a safety net at the end of every runScope pass — items
 *      collected here merge with XHR items, deduped by aweme_id /
 *      creator_sec_uid.
 *
 * Selectors are deliberately tolerant: video identity may live on a
 * normal anchor, a div[href], data-aweme-id, or a video_<id> class token.
 * Per-card metadata pickers then fall through semantic text heuristics.
 * Missing fields default to empty string — downstream consumers tolerate
 * empty fields via the existing BootstrapItemSink filter.
 */

import type {
  DouyinBootstrapItem,
  DouyinScope,
  DouyinSearchItem,
} from "../../main/dy-fetch-tap.ts";
import { pickMetricCount } from "../metric-count.ts";

// ---------------------------------------------------------------------------
// href shape guards
// ---------------------------------------------------------------------------

/**
 * Extract an aweme_id from a /video/<id> URL. Returns "" when the
 * href doesn't point at a video page.
 */
export function extractAwemeIdFromHref(href: string): string {
  if (!href) return "";
  const match = href.match(/\/video\/(\d+)/);
  return match ? (match[1] ?? "") : "";
}

/**
 * Extract a sec_uid from a /user/<sec_uid> URL. Returns "" when the
 * href doesn't point at a user page or points at /user/self (which
 * is the viewing user, not a followed creator).
 */
export function extractSecUidFromHref(href: string): string {
  if (!href) return "";
  // /user/self isn't a meaningful follow target.
  if (/\/user\/self(\?|$)/.test(href)) return "";
  // Real sec_uid starts with MS4w (douyin's base64-like prefix).
  const match = href.match(/\/user\/(MS4w[\w-]+)/);
  return match ? (match[1] ?? "") : "";
}

// ---------------------------------------------------------------------------
// Pickers — per-card text/image extraction with selector fallbacks
// ---------------------------------------------------------------------------

/**
 * Find the closest "card" container for an anchor. Tries common
 * Douyin class-name fragments first, falls back to the anchor itself
 * if no parent matches (which still gives the pickers something to
 * search inside).
 */
const VIDEO_CARD_TARGET_SELECTOR = [
  'a[href*="/video/"]',
  '[href*="/video/"]',
  "[data-aweme-id]",
  "[data-video-id]",
  "[data-content-id]",
  '[class*="video_"]',
].join(",");

function findCardContainer(target: HTMLElement): HTMLElement {
  const card = target.closest<HTMLElement>(
    [
      "[data-aweme-id]",
      "[data-video-id]",
      "[data-content-id]",
      'div[class*="waterfall-videoCard"]',
      'div[class*="jingxuanVideoCard"]',
      'li[class*="ec-card"]',
      'li[class*="card"]',
      'div[class*="ec-card"]',
      'div[class*="card-wrap"]',
      'div[class*="aweme-card"]',
      'div[class*="user-card"]',
      'div[class*="follow-card"]',
      'div[class*="cover-wrap"]',
      "li",
      "article",
      "section",
    ].join(","),
  );
  return card ?? target;
}

function pickCardTitle(card: HTMLElement, target: HTMLElement): string {
  // Target's own aria-label / title is often the cleanest source.
  const aria = target.getAttribute("aria-label")?.trim() ?? "";
  if (aria) return aria;
  const title = target.getAttribute("title")?.trim() ?? "";
  if (title) return title;

  const candidates = [
    'p[class*="title"]',
    'div[class*="title"]',
    'span[class*="title"]',
    'p[class*="desc"]',
    'div[class*="desc"]',
    'span[class*="desc"]',
    "p",
  ];
  for (const sel of candidates) {
    const el = card.querySelector<HTMLElement>(sel);
    const text = el?.textContent?.trim() ?? "";
    if (text) return text;
  }
  const semanticText = pickSemanticCardDescription(card);
  if (semanticText) return semanticText;
  // Last resort: first non-empty text inside the target.
  return target.textContent?.trim() ?? "";
}

function pickAuthorName(card: HTMLElement): string {
  const candidates = [
    '[class*="author-name"]',
    '[class*="user-name"]',
    '[class*="nickname"]',
    '[class*="author"] [class*="name"]',
  ];
  for (const sel of candidates) {
    const el = card.querySelector<HTMLElement>(sel);
    const text = el?.textContent?.trim() ?? "";
    if (text) return text;
  }
  const semanticAuthors = Array.from(card.querySelectorAll<HTMLElement>("span"))
    .map((element) => element.textContent?.replace(/\s+/g, " ").trim() ?? "")
    .filter((text) => /^@\s*\S/.test(text))
    .map((text) => text.replace(/^@\s*/, "").split(/\s*·\s*/, 1)[0]?.trim() ?? "")
    .filter(Boolean)
    .sort((left, right) => left.length - right.length);
  if (semanticAuthors[0]) return semanticAuthors[0];
  return "";
}

function pickSemanticCardDescription(card: HTMLElement): string {
  const ignoredExact = new Set([
    "重播",
    "点击按住可拖动视频",
    "暂停",
    "进入全屏",
    "截图",
    "画中画",
    "直播",
    "/",
  ]);
  const values: string[] = [];
  for (const element of Array.from(card.querySelectorAll<HTMLElement>("span,div,p"))) {
    const childNodes = Array.from(element.childNodes ?? []);
    const text = childNodes
      .filter((node) => node.nodeType === 3)
      .map((node) => node.textContent ?? "")
      .join(" ")
      .replace(/\s+/g, " ")
      .trim();
    if (!text || ignoredExact.has(text)) continue;
    if (/^@/.test(text) || /^·/.test(text)) continue;
    if (/^\d{1,2}:\d{2}$/.test(text)) continue;
    if (/^\d+(?:\.\d+)?(?:万|亿)?$/.test(text)) continue;
    values.push(text);
  }
  values.sort((left, right) => right.length - left.length);
  return values[0] ?? "";
}

function extractAwemeIdFromTarget(target: HTMLElement): string {
  const ownHref = target.getAttribute("href") ?? "";
  const hrefId = extractAwemeIdFromHref(ownHref);
  if (hrefId) return hrefId;
  for (const name of ["data-aweme-id", "data-video-id", "data-content-id"]) {
    const value = target.getAttribute(name)?.trim() ?? "";
    if (/^\d{10,}$/.test(value)) return value;
  }
  const className = typeof target.className === "string" ? target.className : "";
  for (const token of className.split(/\s+/)) {
    const match = token.match(/^video_(\d{10,})$/);
    if (match?.[1]) return match[1];
  }
  const nestedHref = target.querySelector<HTMLElement>('[href*="/video/"]');
  return extractAwemeIdFromHref(nestedHref?.getAttribute("href") ?? "");
}

function extractVideoHrefFromTarget(target: HTMLElement, awemeId: string): string {
  const ownHref = target.getAttribute("href") ?? "";
  if (extractAwemeIdFromHref(ownHref)) return ownHref;
  const nestedHref = target
    .querySelector<HTMLElement>('[href*="/video/"]')
    ?.getAttribute("href") ?? "";
  return extractAwemeIdFromHref(nestedHref) ? nestedHref : `/video/${awemeId}`;
}

function pickAuthorSecUid(card: HTMLElement): string {
  // Look for any /user/MS4w... anchor inside the card (typically the
  // author chip). Avoid matching the card's own primary anchor when
  // it IS a user link (handled by caller for dy_follow).
  const anchors = Array.from(
    card.querySelectorAll<HTMLAnchorElement>('a[href*="/user/MS4w"]'),
  );
  for (const a of anchors) {
    const secUid = extractSecUidFromHref(a.getAttribute("href") ?? a.href ?? "");
    if (secUid) return secUid;
  }
  return "";
}

function pickCoverUrl(card: HTMLElement): string {
  const img = card.querySelector<HTMLImageElement>("img");
  if (!img) return "";
  return (
    img.getAttribute("src") ||
    img.getAttribute("data-src") ||
    img.getAttribute("data-original") ||
    ""
  );
}

function pickCardMetrics(card: HTMLElement): Pick<
  DouyinSearchItem,
  "view_count" | "like_count" | "collect_count" | "comment_count" | "share_count"
> {
  const view_count = pickMetricCount(card, ["播放", "观看", "浏览", "view", "play"]);
  const like_count = pickMetricCount(card, ["点赞", "获赞", "赞", "like"]);
  const collect_count = pickMetricCount(card, ["收藏", "collect", "save"]);
  const comment_count = pickMetricCount(card, ["评论", "comment"]);
  const share_count = pickMetricCount(card, ["分享", "share"]);
  return {
    ...(view_count > 0 ? { view_count } : {}),
    ...(like_count > 0 ? { like_count } : {}),
    ...(collect_count > 0 ? { collect_count } : {}),
    ...(comment_count > 0 ? { comment_count } : {}),
    ...(share_count > 0 ? { share_count } : {}),
  };
}

// ---------------------------------------------------------------------------
// Public extractor
// ---------------------------------------------------------------------------

/**
 * Walk the document for cards matching the requested scope and
 * return normalized DouyinBootstrapItem[]. Caps results at maxItems
 * to keep the merge pass cheap.
 *
 * For dy_post / dy_like / dy_collect: anchors with href containing
 * /video/<digits>. The aweme_id is the digit run.
 *
 * For dy_follow: anchors with href starting at /user/MS4w (real
 * follow targets — /user/self is filtered out).
 */
export function extractDouyinItemsFromDocument(
  doc: Document,
  scope: DouyinScope,
  baseUrl: string,
  maxItems: number,
): DouyinBootstrapItem[] {
  const cap = Math.max(0, Math.floor(maxItems));
  if (cap === 0) return [];

  if (scope === "dy_follow") {
    return extractFollowItems(doc, baseUrl, cap);
  }
  return extractVideoItems(doc, scope, baseUrl, cap);
}

export function extractDouyinSearchItemsFromDocument(
  doc: Document,
  baseUrl: string,
  maxItems: number,
  includeRenderedFeedCards: boolean = false,
): DouyinSearchItem[] {
  const cap = Math.max(0, Math.floor(maxItems));
  if (cap === 0) return [];

  const items: DouyinSearchItem[] = [];
  const seen = new Set<string>();
  const selector = includeRenderedFeedCards
    ? VIDEO_CARD_TARGET_SELECTOR
    : 'a[href*="/video/"]';
  const targets = Array.from(doc.querySelectorAll<HTMLElement>(selector));
  for (const target of targets) {
    if (items.length >= cap) break;
    const awemeId = extractAwemeIdFromTarget(target);
    if (!awemeId || seen.has(awemeId)) continue;
    seen.add(awemeId);

    const href = extractVideoHrefFromTarget(target, awemeId);
    const card = findCardContainer(target);
    items.push({
      scope: "dy_search",
      aweme_id: awemeId,
      url: absolutize(href, baseUrl),
      title: pickCardTitle(card, target),
      author: pickAuthorName(card),
      author_sec_uid: pickAuthorSecUid(card),
      cover_url: pickCoverUrl(card),
      ...pickCardMetrics(card),
    });
  }
  return items;
}

// A container must overflow its viewport by more than this to count as
// scrollable — sub-pixel layout rounding can leave scrollHeight a hair
// above clientHeight on non-scrolling wrappers.
const SCROLLABLE_OVERFLOW_TOLERANCE_PX = 4;

/**
 * Find the inner scrollable container that hosts Douyin search results.
 *
 * Douyin's search results usually live in a virtualized inner list;
 * scrolling the window often triggers NO pagination. Walk up from a
 * /video/-href anchor (the same shape the extractors key on) to the
 * nearest ancestor that is actually scrollable (overflow-y auto/scroll
 * AND real overflow). Returns null when none is found — callers fall
 * back to window scrolling.
 */
export function pickSearchScrollTarget(doc: Document): Element | null {
  const target = doc.querySelector<HTMLElement>('a[href*="/video/"]');
  if (!target) return null;
  const view = doc.defaultView;
  let node: Element | null = target.parentElement;
  while (node) {
    if (isScrollableContainer(node, view)) return node;
    node = node.parentElement;
  }
  return null;
}

function isScrollableContainer(el: Element, view: Window | null): boolean {
  let overflowY = "";
  try {
    overflowY = view?.getComputedStyle?.(el)?.overflowY ?? "";
  } catch {
    return false;
  }
  if (overflowY !== "auto" && overflowY !== "scroll") return false;
  const scrollHeight = Number(el.scrollHeight ?? 0);
  const clientHeight = Number(el.clientHeight ?? 0);
  return scrollHeight > clientHeight + SCROLLABLE_OVERFLOW_TOLERANCE_PX;
}

function extractVideoItems(
  doc: Document,
  scope: DouyinScope,
  baseUrl: string,
  cap: number,
): DouyinBootstrapItem[] {
  const items: DouyinBootstrapItem[] = [];
  const seen = new Set<string>();
  const targets = Array.from(
    doc.querySelectorAll<HTMLElement>('a[href*="/video/"]'),
  );
  for (const target of targets) {
    if (items.length >= cap) break;
    const awemeId = extractAwemeIdFromTarget(target);
    if (!awemeId || seen.has(awemeId)) continue;
    seen.add(awemeId);

    const href = extractVideoHrefFromTarget(target, awemeId);
    const card = findCardContainer(target);
    const url = absolutize(href, baseUrl);
    items.push({
      scope,
      aweme_id: awemeId,
      creator_sec_uid: "",
      url,
      title: pickCardTitle(card, target),
      author: pickAuthorName(card),
      author_sec_uid: pickAuthorSecUid(card),
      cover_url: pickCoverUrl(card),
      ...pickCardMetrics(card),
    });
  }
  return items;
}

function extractFollowItems(
  doc: Document,
  baseUrl: string,
  cap: number,
): DouyinBootstrapItem[] {
  const items: DouyinBootstrapItem[] = [];
  const seen = new Set<string>();
  const anchors = Array.from(
    doc.querySelectorAll<HTMLAnchorElement>('a[href*="/user/MS4w"]'),
  );
  for (const anchor of anchors) {
    if (items.length >= cap) break;
    const href = anchor.getAttribute("href") ?? anchor.href ?? "";
    const secUid = extractSecUidFromHref(href);
    if (!secUid || seen.has(secUid)) continue;
    seen.add(secUid);

    const card = findCardContainer(anchor);
    const nickname = pickAuthorName(card) || anchor.textContent?.trim() || "";
    const cover = pickCoverUrl(card);
    items.push({
      scope: "dy_follow",
      aweme_id: "",
      creator_sec_uid: secUid,
      url: absolutize(href, baseUrl),
      title: nickname,
      author: nickname,
      author_sec_uid: secUid,
      cover_url: cover,
    });
  }
  return items;
}

function absolutize(href: string, baseUrl: string): string {
  if (!href) return "";
  if (/^https?:\/\//.test(href)) return href;
  try {
    return new URL(href, baseUrl).toString();
  } catch {
    return href;
  }
}
