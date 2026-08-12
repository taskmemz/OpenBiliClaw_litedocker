/**
 * Linux.do (Discourse) adapter for the generic behavior collector.
 */

import type { ActionHint, PageType, PlatformAdapter } from "../types.js";
import { queryParam } from "./search-query.ts";

const TOPIC_PATH_PATTERN =
  /^\/t\/(?:(?:([1-9]\d*))|(?:[^/?#]+\/([1-9]\d*)))(?:[/?#]|$)/i;
const USER_PATH_PATTERN = /^\/u\/([^/?#]+)/i;

const CARD_SELECTOR = [
  "tr.topic-list-item",
  "[data-topic-id]",
  ".search-result",
  ".topic-post",
  'a.raw-topic-link[href*="/t/"]',
  'a.title[href*="/t/"]',
].join(",");

const SEARCH_INPUT_SELECTOR = [
  "input#search-term",
  'input[name="q"]',
  'input[type="search"]',
  '.search-input input[type="text"]',
].join(",");

export function detectLinuxdoPageType(url: string): PageType {
  let pathname = "";
  try {
    pathname = new URL(url).pathname;
  } catch {
    pathname = url;
  }
  if (extractLinuxdoContentId(url)) return "post";
  if (pathname.startsWith("/search")) return "search";
  if (pathname.startsWith("/u/")) return "profile";
  if (pathname.startsWith("/c/")) return "category";
  if (pathname.startsWith("/tag/")) return "tag";
  if (pathname.startsWith("/top") || pathname.startsWith("/hot")) return "hot";
  if (pathname.startsWith("/latest")) return "feed";
  return "home";
}

export function extractLinuxdoContentId(url: string): string | null {
  let parsed: URL;
  try {
    parsed = new URL(url);
  } catch {
    return null;
  }
  if (parsed.protocol !== "https:" || parsed.hostname !== "linux.do") return null;
  const match = parsed.pathname.match(TOPIC_PATH_PATTERN);
  const topicId = match?.[1] ?? match?.[2] ?? "";
  return topicId ? `topic:${topicId}` : null;
}

export function extractLinuxdoUsername(url: string): string | null {
  let parsed: URL;
  try {
    parsed = new URL(url);
  } catch {
    return null;
  }
  if (parsed.protocol !== "https:" || parsed.hostname !== "linux.do") return null;
  const match = parsed.pathname.match(USER_PATH_PATTERN);
  if (!match?.[1]) return null;
  try {
    return decodeURIComponent(match[1]);
  } catch {
    return match[1];
  }
}

function normalizeText(value: string | null | undefined): string {
  return (value ?? "").trim();
}

export function inferLinuxdoActionType(hint: ActionHint): string | null {
  const text = `${normalizeText(hint.text)} ${normalizeText(hint.ariaLabel)} ${hint.className}`
    .toLowerCase();
  if (!text) return null;
  // Removing an existing like is a neutral retraction, never a negative vote.
  // The shared kernel handles explicit aria-pressed controls; labels such as
  // "unlike" without pressed-state evidence are ignored rather than invented
  // as a dislike signal.
  if (/unlike|取消点赞|撤销点赞|取消赞/.test(text)) return null;
  if (/bookmark|收藏/.test(text)) return "favorite";
  if (/\blike\b|点赞|赞/.test(text)) return "like";
  if (/\breply\b|comment|回复|评论/.test(text)) return "comment";
  if (/share|分享/.test(text)) return "share";
  if (/follow|关注|追踪/.test(text)) return "follow";
  return null;
}

function elementHref(element: Element | null): string {
  if (!element) return "";
  const href = (element as Element & { href?: unknown }).href;
  if (typeof href === "string" && href.trim()) return href.trim();
  return element.getAttribute("href")?.trim() ?? "";
}

function absoluteLinuxdoUrl(value: string, currentUrl: string): string {
  if (!value) return "";
  try {
    const url = new URL(value, currentUrl || "https://linux.do/");
    return url.hostname === "linux.do" && url.protocol === "https:" ? url.href : "";
  } catch {
    return "";
  }
}

export function buildLinuxdoTargetMetadata(
  target: Element,
  currentUrl: string,
): Record<string, unknown> {
  const card = target.closest(CARD_SELECTOR);
  const directLink = target.closest('a[href*="/t/"]');
  const cardLink = card?.querySelector('a[href*="/t/"]') ?? null;
  const href = absoluteLinuxdoUrl(elementHref(directLink) || elementHref(cardLink), currentUrl);
  const fromUrl = extractLinuxdoContentId(href);
  const attrTopicId =
    card?.getAttribute("data-topic-id")?.trim() ||
    target.getAttribute("data-topic-id")?.trim() ||
    "";
  const contentId = fromUrl || (/^[1-9]\d*$/.test(attrTopicId) ? `topic:${attrTopicId}` : "");
  const authorLink = card?.querySelector('a[href^="/u/"], a[href*="linux.do/u/"]') ?? null;
  const authorUrl = absoluteLinuxdoUrl(elementHref(authorLink), currentUrl);
  const author = extractLinuxdoUsername(authorUrl) ?? "";
  return {
    ...(href ? { target_url: href } : {}),
    ...(contentId
      ? {
          content_id: contentId,
          topic_id: contentId.replace(/^topic:/, ""),
          content_type: "post",
        }
      : {}),
    ...(author ? { author_name: author, author_url: authorUrl } : {}),
  };
}

export const linuxdoAdapter: PlatformAdapter = {
  sourcePlatform: "linuxdo",
  detectPageType: detectLinuxdoPageType,
  extractContentId: extractLinuxdoContentId,
  extractSearchQuery: (url) => queryParam(url, "q"),
  cardSelector: CARD_SELECTOR,
  searchInputSelector: SEARCH_INPUT_SELECTOR,
  videoSelector: null,
  dwellPageTypes: ["post"],
  inferActionType: inferLinuxdoActionType,
  buildEventMetadata(url: string): Record<string, unknown> {
    const contentId = extractLinuxdoContentId(url);
    if (!contentId) return {};
    return {
      content_type: "post",
      content_id: contentId,
      topic_id: contentId.replace(/^topic:/, ""),
    };
  },
  buildTargetMetadata: buildLinuxdoTargetMetadata,
};
