/** Read-only V2EX bootstrap scraper. Cookie values never leave the browser. */

export type V2EXScope =
  | "public_topics"
  | "public_replies"
  | "favorite_topics"
  | "favorite_nodes";

export interface V2EXBootstrapItem {
  scope: V2EXScope;
  topic_id?: string;
  title?: string;
  author_name?: string;
  url?: string;
  node_name?: string;
  node_title?: string;
  reply_id?: string;
  reply_text?: string;
  reply_excerpts?: string[];
  published_at?: string;
}

export interface V2EXScopeExecuteMessage {
  task_id: string;
  scope: V2EXScope;
  username?: string;
  page?: number;
  max_items?: number;
}

export interface V2EXScopeResult {
  task_id: string;
  scope: V2EXScope;
  status:
    | "ok"
    | "empty"
    | "hidden"
    | "login_required"
    | "rate_limited"
    | "parse_error"
    | "failed";
  items: V2EXBootstrapItem[];
  scope_count: number;
  error?: string;
  debug?: Record<string, unknown>;
}

const KNOWN_SCOPES: readonly V2EXScope[] = [
  "public_topics",
  "public_replies",
  "favorite_topics",
  "favorite_nodes",
];

function isScope(value: unknown): value is V2EXScope {
  return KNOWN_SCOPES.includes(value as V2EXScope);
}

export function v2exScopeRouteMatches(
  scope: V2EXScope,
  pageUrl: string,
  username = "",
): boolean {
  try {
    const url = new URL(pageUrl);
    if (url.protocol !== "https:" || (url.hostname !== "v2ex.com" && !url.hostname.endsWith(".v2ex.com"))) {
      return false;
    }
    const path = (decodeURIComponent(url.pathname).replace(/\/+$/, "") || "/").toLowerCase();
    const memberPath = username ? `/member/${username.toLowerCase()}` : "/";
    if (scope === "public_topics") return path === memberPath;
    if (scope === "public_replies") return path === (username ? `${memberPath}/replies` : "/");
    if (scope === "favorite_topics") return path === "/my/topics";
    return path === "/my/nodes";
  } catch {
    return false;
  }
}

function text(value: unknown, limit = 6000): string {
  return typeof value === "string" ? value.trim().slice(0, limit) : "";
}

function absoluteUrl(value: string): string {
  try {
    return new URL(value, window.location.origin).href.split("#", 1)[0];
  } catch {
    return "";
  }
}

function href(element: Element | null): string {
  if (!element) return "";
  const raw = element.getAttribute("href") || (element as HTMLAnchorElement).href || "";
  return absoluteUrl(raw);
}

function topicIdFromUrl(value: string): string {
  const match = value.match(/\/t\/(\d+)/);
  return match?.[1] || "";
}

function nodeFromHref(value: string): string {
  try {
    const pathname = new URL(value, window.location.origin).pathname;
    const match = pathname.match(/^\/go\/([^/?#]+)/);
    return match?.[1] || "";
  } catch {
    return "";
  }
}

function currentUsername(): string {
  // Only inspect the top navigation. Arbitrary member links in a topic body
  // are not identity evidence.
  const candidates = [
    "#Top .tools a[href^='/member/']",
    "#Top a[href^='/member/']",
    "header a[href^='/member/']",
  ];
  for (const selector of candidates) {
    const link = document.querySelector<HTMLAnchorElement>(selector);
    const match = link?.getAttribute("href")?.match(/^\/member\/([^/?#]+)/);
    if (match?.[1]) return decodeURIComponent(match[1]);
  }
  return "";
}

export type V2EXLoginState = "logged_in" | "logged_out" | "unknown";

function observedLoginState(): V2EXLoginState {
  if (currentUsername()) return "logged_in";
  if (
    document.querySelector(
      "#Top a[href^='/signin'], header a[href^='/signin'], form[action^='/signin'], input[type='password']",
    )
  ) return "logged_out";
  return "unknown";
}

function nodeMeta(container: Element): { node_name: string; node_title: string } {
  const node = container.querySelector<HTMLAnchorElement>(
    "a.node, a[href^='/go/'], a[href*='v2ex.com/go/']",
  );
  const nodeHref = href(node);
  return {
    node_name: nodeFromHref(nodeHref),
    node_title: text(node?.textContent, 200),
  };
}

function authorFromTopic(container: Element): string {
  const link = container.querySelector<HTMLAnchorElement>(
    ".topic_info a[href^='/member/'], a[href^='/member/']",
  );
  const match = link?.getAttribute("href")?.match(/^\/member\/([^/?#]+)/);
  return match?.[1] ? decodeURIComponent(match[1]) : text(link?.textContent, 128);
}

function topicItem(scope: V2EXScope, container: Element): V2EXBootstrapItem | null {
  const link = container.querySelector<HTMLAnchorElement>(
    "a.topic-link[href*='/t/'], .item_title a[href*='/t/'], a[href*='/t/']",
  );
  const url = href(link);
  const topic_id = topicIdFromUrl(url);
  const title = text(link?.textContent, 300);
  if (!topic_id || (!title && !url)) return null;
  const meta = nodeMeta(container);
  return {
    scope,
    topic_id,
    title,
    url,
    author_name: authorFromTopic(container),
    ...meta,
  };
}

function extractTopics(scope: "public_topics" | "favorite_topics", maxItems: number): V2EXBootstrapItem[] {
  const rows = Array.from(document.querySelectorAll<HTMLElement>(
    "#Main .cell.item, #Main .item_title, #Main .topic-link",
  ));
  const result: V2EXBootstrapItem[] = [];
  const seen = new Set<string>();
  for (const row of rows) {
    const item = topicItem(scope, row.closest(".cell.item") || row);
    if (!item || seen.has(item.topic_id || "")) continue;
    seen.add(item.topic_id || "");
    result.push(item);
    if (result.length >= maxItems) break;
  }
  return result;
}

/** Resolve the Topic metadata adjacent to one reply without crossing replies.
 *
 * V2EX currently renders the member replies page as alternating
 * ``.dock_area`` metadata + ``.inner`` reply rows inside one shared ``.box``.
 * Older fixtures used one ``.cell`` per reply. Keep both shapes, but never
 * fall back to the shared box because that would attribute every reply to the
 * first Topic on the page.
 */
export function resolveV2EXReplyContext(reply: Element): Element | null {
  const cell = reply.closest(".cell");
  if (cell?.querySelector("a[href*='/t/']")) return cell;
  const inner = reply.closest(".inner");
  const adjacentMetadata = inner?.previousElementSibling;
  if (
    adjacentMetadata?.matches(".dock_area") &&
    adjacentMetadata.querySelector("a[href*='/t/']")
  ) {
    return adjacentMetadata;
  }
  return null;
}

function extractReplies(maxItems: number, targetUsername = ""): V2EXBootstrapItem[] {
  const grouped = new Map<string, V2EXBootstrapItem>();
  const replies = Array.from(document.querySelectorAll<HTMLElement>(
    "#Main .reply_content, #Main .cell .reply_content",
  ));
  for (const reply of replies) {
    const container = resolveV2EXReplyContext(reply);
    if (!container) continue;
    const topicLink = container.querySelector<HTMLAnchorElement>("a[href*='/t/']");
    const rawUrl = topicLink?.getAttribute("href") || topicLink?.href || "";
    const url = href(topicLink);
    const topic_id = topicIdFromUrl(url);
    const replyMatch = rawUrl.match(/#reply(\d+)/);
    const replyText = text(reply.textContent, 200);
    if (!topic_id || !replyText) continue;
    const existing = grouped.get(topic_id);
    if (existing) {
      if (existing.reply_excerpts && existing.reply_excerpts.length < 3 &&
          !existing.reply_excerpts.includes(replyText)) {
        existing.reply_excerpts.push(replyText);
      }
      continue;
    }
    grouped.set(topic_id, {
      scope: "public_replies",
      topic_id,
      title: text(topicLink?.textContent, 300),
      url,
      author_name: text(targetUsername, 128) || currentUsername() || authorFromTopic(container),
      ...nodeMeta(container),
      reply_id: replyMatch?.[1] || "",
      reply_text: replyText,
      reply_excerpts: [replyText],
    });
    if (grouped.size >= maxItems) break;
  }
  return [...grouped.values()];
}

function extractNodes(maxItems: number): V2EXBootstrapItem[] {
  const result: V2EXBootstrapItem[] = [];
  const seen = new Set<string>();
  for (const link of Array.from(document.querySelectorAll<HTMLAnchorElement>(
    "#Main a.node[href^='/go/'], #Main a[href^='/go/']",
  ))) {
    const url = href(link);
    const node_name = nodeFromHref(url);
    if (!node_name || seen.has(node_name)) continue;
    seen.add(node_name);
    result.push({
      scope: "favorite_nodes",
      node_name,
      node_title: text(link.textContent, 200),
      url,
    });
    if (result.length >= maxItems) break;
  }
  return result;
}

export interface V2EXPagerEntry {
  page: number;
  current: boolean;
  next: boolean;
}

/** Resolve V2EX's numbered pager, including its out-of-range clamp behavior.
 *
 * A request such as ``?p=3`` can render the final page while keeping ``p=3``
 * in the address bar. The rendered ``.page_current`` entry is authoritative;
 * using only the requested page would repeatedly scrape the final page until
 * the task's hard page cap.
 */
export function v2exPagerHasNext(
  requestedPage: number,
  entries: readonly V2EXPagerEntry[],
): boolean {
  if (entries.some((entry) => entry.next)) return true;
  const current = entries.find((entry) => entry.current)?.page || requestedPage;
  return entries.some((entry) => Number.isFinite(entry.page) && entry.page > current);
}

function pageHasNext(requestedPage: number): boolean {
  const entries: V2EXPagerEntry[] = [];
  for (const element of Array.from(document.querySelectorAll<HTMLAnchorElement>(
    "a.page_current, a.page_normal, a[rel='next']",
  ))) {
    const rawHref = element.getAttribute("href") || "";
    let page = Number.parseInt(text(element.textContent, 16), 10);
    try {
      const queryPage = Number.parseInt(new URL(rawHref, window.location.origin).searchParams.get("p") || "", 10);
      if (Number.isFinite(queryPage)) page = queryPage;
    } catch {
      // Keep the visible numeric label when an upstream href is malformed.
    }
    const label = text(element.textContent, 32).toLowerCase();
    entries.push({
      page,
      current: element.classList.contains("page_current"),
      next: element.rel === "next" || /^(下一页|next|›|»|→)$/.test(label),
    });
  }
  return v2exPagerHasNext(requestedPage, entries);
}

export interface V2EXScopePageEvidence {
  routeMatches: boolean;
  mainPresent: boolean;
  loginState: V2EXLoginState;
  challengePresent: boolean;
  hiddenPresent: boolean;
  scopeLayoutPresent: boolean;
  explicitEmptyPresent: boolean;
  itemCount: number;
}

export interface V2EXScopePageClassification {
  status: V2EXScopeResult["status"];
  error?: string;
  pageRecognized: boolean;
  affirmativeEmpty: boolean;
}

/** Classify a rendered page without ever treating a generic shell as empty. */
export function classifyV2EXScopePage(
  scope: V2EXScope,
  evidence: V2EXScopePageEvidence,
): V2EXScopePageClassification {
  if (evidence.challengePresent) {
    return {
      status: "rate_limited",
      error: "challenge_page",
      pageRecognized: false,
      affirmativeEmpty: false,
    };
  }
  const privateScope = scope === "favorite_topics" || scope === "favorite_nodes";
  if (privateScope && evidence.loginState === "logged_out") {
    return {
      status: "login_required",
      error: "login_required",
      pageRecognized: false,
      affirmativeEmpty: false,
    };
  }
  if (!evidence.routeMatches) {
    return {
      status: "failed",
      error: "unexpected_page",
      pageRecognized: false,
      affirmativeEmpty: false,
    };
  }
  if (!evidence.mainPresent) {
    return {
      status: "parse_error",
      error: "main_container_missing",
      pageRecognized: false,
      affirmativeEmpty: false,
    };
  }
  if (privateScope && evidence.loginState !== "logged_in") {
    return {
      status: "parse_error",
      error: "login_state_unknown",
      pageRecognized: false,
      affirmativeEmpty: false,
    };
  }
  if (evidence.hiddenPresent) {
    return {
      status: "hidden",
      error: "scope_hidden",
      pageRecognized: true,
      affirmativeEmpty: false,
    };
  }
  if (evidence.itemCount > 0) {
    return {
      status: "ok",
      pageRecognized: true,
      affirmativeEmpty: false,
    };
  }
  if (evidence.explicitEmptyPresent) {
    return {
      status: "empty",
      pageRecognized: true,
      affirmativeEmpty: true,
    };
  }
  return {
    status: "parse_error",
    error: evidence.scopeLayoutPresent ? "empty_state_unproven" : "scope_layout_missing",
    pageRecognized: false,
    affirmativeEmpty: false,
  };
}

const EMPTY_PATTERNS: Record<V2EXScope, readonly RegExp[]> = {
  public_topics: [
    /(?:还|尚|暂时)?没有(?:发布|发表)过?(?:任何)?主题/,
    /no topics? (?:yet|found)/i,
  ],
  public_replies: [
    /(?:还|尚|暂时)?没有(?:发表|发布)过?(?:任何)?回复/,
    /no replies? (?:yet|found)/i,
  ],
  favorite_topics: [
    /(?:还|尚|目前|暂时)?没有收藏(?:过)?(?:任何)?主题/,
    /no favou?rite topics?/i,
  ],
  favorite_nodes: [
    /(?:还|尚|目前|暂时)?没有收藏(?:过)?(?:任何)?节点/,
    /no favou?rite nodes?/i,
  ],
};

const HIDDEN_PATTERNS = [
  /(?:主题|回复)(?:列表|记录)?(?:已被|已|被)?隐藏/,
  /(?:用户|该用户).*(?:不公开|不可见|没有权限查看)/,
  /(?:topics|replies).*(?:hidden|private)/i,
];

const CHALLENGE_PATTERNS = [
  /请求过于频繁/,
  /请完成(?:安全)?验证/,
  /访问验证/,
  /too many requests/i,
  /rate limit/i,
  /just a moment/i,
  /security check/i,
];

function bodyText(): string {
  return text(document.body?.innerText || document.body?.textContent, 20_000);
}

function anyPattern(value: string, patterns: readonly RegExp[]): boolean {
  return patterns.some((pattern) => pattern.test(value));
}

function scopeLayoutPresent(scope: V2EXScope): boolean {
  if (scope === "public_topics" || scope === "favorite_topics") {
    return Boolean(document.querySelector("#Main .box, #Main .cell, #Main .item_title"));
  }
  if (scope === "public_replies") {
    return Boolean(document.querySelector("#Main .box, #Main .dock_area, #Main .reply_content"));
  }
  return Boolean(document.querySelector("#Main .box, #Main .grid, #Main a.node"));
}

export function executeV2EXScope(message: V2EXScopeExecuteMessage): V2EXScopeResult {
  const scope = message.scope;
  const maxItems = Math.max(1, Math.min(1000, Math.floor(Number(message.max_items) || 300)));
  const pageUrl = window.location.href.split("#", 1)[0];
  const routeMatches = isScope(scope) && v2exScopeRouteMatches(scope, pageUrl, text(message.username, 128));
  const loginState = observedLoginState();
  const debug: Record<string, unknown> = {
    username: currentUsername(),
    login_state: loginState,
    page_url: pageUrl,
  };
  if (loginState !== "unknown") debug.logged_in = loginState === "logged_in";
  if (!isScope(scope)) {
    return {
      task_id: message.task_id,
      scope,
      status: "failed",
      items: [],
      scope_count: 0,
      error: "unknown_scope",
      debug,
    };
  }
  try {
    const extractedItems = scope === "public_topics"
      ? extractTopics(scope, maxItems + 1)
      : scope === "public_replies"
        ? extractReplies(maxItems + 1, text(message.username, 128))
        : scope === "favorite_topics"
          ? extractTopics(scope, maxItems + 1)
          : extractNodes(maxItems + 1);
    const items = extractedItems.slice(0, maxItems);
    const pageBodyText = bodyText();
    const classification = classifyV2EXScopePage(scope, {
      routeMatches,
      mainPresent: Boolean(document.querySelector("#Main")),
      loginState,
      challengePresent:
        Boolean(document.querySelector("#cf-challenge-running, .cf-challenge, [data-sitekey], iframe[src*='captcha']"))
        || anyPattern(pageBodyText, CHALLENGE_PATTERNS),
      hiddenPresent: anyPattern(pageBodyText, HIDDEN_PATTERNS),
      scopeLayoutPresent: scopeLayoutPresent(scope),
      explicitEmptyPresent: anyPattern(pageBodyText, EMPTY_PATTERNS[scope]),
      itemCount: items.length,
    });
    debug.page_recognized = classification.pageRecognized;
    debug.affirmative_empty = classification.affirmativeEmpty;
    debug.page_truncated = extractedItems.length > maxItems;
    debug.has_next_page = scope === "favorite_nodes"
      ? false
      : pageHasNext(Math.max(1, Math.floor(Number(message.page) || 1)));
    return {
      task_id: message.task_id,
      scope,
      status: classification.status,
      items,
      scope_count: items.length,
      ...(classification.error ? { error: classification.error } : {}),
      debug,
    };
  } catch (error) {
    return {
      task_id: message.task_id,
      scope,
      status: "failed",
      items: [],
      scope_count: 0,
      error: error instanceof Error ? error.message : String(error),
      debug,
    };
  }
}

export function installV2EXMessageListener(): void {
  chrome.runtime.onMessage.addListener(
    (message: { action?: string; data?: V2EXScopeExecuteMessage }, _sender, sendResponse) => {
      if (message.action !== "V2EX_SCOPE_EXECUTE") return false;
      const result = executeV2EXScope(message.data as V2EXScopeExecuteMessage);
      void chrome.runtime.sendMessage({ action: "V2EX_SCOPE_RESULT", data: result });
      sendResponse({ ok: true });
      return true;
    },
  );
}
