/** V2EX adapter for passive, read-only behavior collection. */

import type { PageType, PlatformAdapter } from "../types.js";

const TOPIC_PATTERN = /v2ex\.com\/t\/(\d+)/;
const NODE_PATTERN = /v2ex\.com\/go\/([^/?#]+)/;
const NODE_SLUG_PATTERN = /^[a-z0-9][a-z0-9_-]{0,127}$/i;

interface V2EXQueryRoot {
  querySelector(selector: string): {
    getAttribute(name: string): string | null;
    textContent?: string | null;
  } | null;
}

export function detectV2EXPageType(url: string): PageType {
  if (TOPIC_PATTERN.test(url)) return "topic";
  if (NODE_PATTERN.test(url)) return "node";
  if (url.includes("/member/") || url.includes("/my/")) return "profile";
  return "home";
}

export function extractV2EXContentId(url: string): string | null {
  return url.match(TOPIC_PATTERN)?.[1] || null;
}

function nodeNameFromHref(href: string): string {
  try {
    const parsed = new URL(href, "https://www.v2ex.com");
    const hostname = parsed.hostname.toLowerCase();
    if (hostname !== "v2ex.com" && !hostname.endsWith(".v2ex.com")) return "";
    const encoded = parsed.pathname.match(/^\/go\/([^/]+)\/?$/)?.[1] || "";
    const decoded = decodeURIComponent(encoded);
    return NODE_SLUG_PATTERN.test(decoded) ? decoded.toLowerCase() : "";
  } catch {
    return "";
  }
}

/** Read the current Topic's Node from V2EX's rendered topic header only. */
export function extractV2EXTopicNodeMetadata(
  url: string,
  root?: V2EXQueryRoot | null,
  currentUrl?: string,
): Record<string, string> {
  const topicId = extractV2EXContentId(url);
  if (!topicId) return {};
  const activeUrl = currentUrl ??
    (typeof window !== "undefined" ? window.location.href : url);
  if (extractV2EXContentId(activeUrl) !== topicId) return {};
  const queryRoot = root ?? (typeof document !== "undefined" ? document : null);
  if (!queryRoot) return {};
  const nodeLink = queryRoot.querySelector(
    "#Main .box .header a[href^='/go/'], #Main .box .header a[href*='v2ex.com/go/']",
  );
  if (!nodeLink) return {};
  const nodeName = nodeNameFromHref(nodeLink.getAttribute("href") || "");
  if (!nodeName) return {};
  const nodeTitle = String(nodeLink.textContent || "").replace(/\s+/g, " ").trim().slice(0, 200);
  return {
    node_name: nodeName,
    ...(nodeTitle ? { node_title: nodeTitle } : {}),
  };
}

export const v2exAdapter: PlatformAdapter = {
  sourcePlatform: "v2ex",
  detectPageType: detectV2EXPageType,
  extractContentId: extractV2EXContentId,
  cardSelector: ".cell.item, .topic-link, a[href*='/t/']",
  searchInputSelector: "input[type='search'], input[name='q']",
  videoSelector: null,
  dwellPageTypes: ["topic"],
  // A click on “回复” is not proof that a reply was submitted, and a
  // “收藏/取消收藏” control cannot be distinguished from its label alone.
  // Strong V2EX actions therefore come only from authoritative bootstrap
  // snapshots (or local OpenBiliClaw feedback), never passive click inference.
  inferActionType: () => null,
  buildEventMetadata(url: string): Record<string, unknown> {
    const topicId = extractV2EXContentId(url);
    const node = url.match(NODE_PATTERN)?.[1] || "";
    return {
      ...(topicId ? { topic_id: topicId, content_id: topicId, content_type: "topic" } : {}),
      ...(topicId ? extractV2EXTopicNodeMetadata(url) : {}),
      ...(node ? { node_name: node, content_type: "node" } : {}),
    };
  },
};
