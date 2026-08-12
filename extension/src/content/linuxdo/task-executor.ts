/**
 * Linux.do content-script executor for read-only Discourse tasks.
 *
 * Every upstream request runs in a real linux.do tab with the browser's own
 * session. Only normalized topic rows leave the tab; cookies and raw response
 * bodies are never sent to the OpenBiliClaw backend.
 */

export type LinuxdoDiscoveryTaskType = "search" | "hot" | "feed" | "creator" | "related";
export type LinuxdoTaskType = LinuxdoDiscoveryTaskType | "bootstrap_events";
export type LinuxdoScope =
  | "linuxdo_search"
  | "linuxdo_hot"
  | "linuxdo_feed"
  | "linuxdo_creator"
  | "linuxdo_related"
  | "linuxdo_bookmarks"
  | "linuxdo_likes"
  | "linuxdo_read_history";

export interface LinuxdoCursorPosition {
  page: number;
  offset: number;
}

export interface LinuxdoTaskItem {
  scope: LinuxdoScope;
  content_type: "post";
  topic_id: string;
  content_id: string;
  title: string;
  url: string;
  author?: string;
  author_url?: string;
  summary?: string;
  category?: string;
  tags: string[];
  views?: number;
  like_count?: number;
  reply_count?: number;
  engagement_available: Array<"view" | "like" | "comment">;
  published_at?: string | number;
  interaction_action?: "favorite" | "like" | "view";
  interaction_time?: string | number;
  search_keyword?: string;
  source_input?: string;
  source_strategy: string;
  source_keyword_id?: number;
}

export interface LinuxdoExecuteMessage {
  task_id: string;
  claim_token?: string;
  type?: LinuxdoTaskType;
  scopes?: LinuxdoScope[];
  keywords?: string[];
  max_items_per_keyword?: number;
  source_keyword_ids?: Record<string, number>;
  max_items?: number;
  creator_urls?: string[];
  max_items_per_creator?: number;
  related_urls?: string[];
  max_items_per_seed?: number;
  max_items_per_scope?: number;
  max_pages?: number;
  fetch_timeout_ms?: number;
  request_interval_seconds?: number;
  cursor_contract?: "page-offset-v1";
  start_cursors?: Record<string, LinuxdoCursorPosition>;
  hydrate_topic_details?: boolean;
}

export interface LinuxdoTaskResult {
  task_id: string;
  claim_token?: string;
  status: "ok" | "empty" | "degraded" | "failed";
  items: LinuxdoTaskItem[];
  scope_counts: Record<string, number>;
  account_key?: string;
  response_observed?: boolean;
  complete_scopes?: LinuxdoScope[];
  next_cursors?: Record<string, LinuxdoCursorPosition>;
  error?: string;
  debug?: Record<string, unknown>;
}

export interface LinuxdoNormalizeContext {
  scope: LinuxdoScope;
  strategy: string;
  users?: Map<string, string>;
  categories?: Map<string, string>;
  searchKeyword?: string;
  sourceInput?: string;
  sourceKeywordId?: number;
  interactionAction?: "favorite" | "like" | "view";
  topicIdOverride?: string;
}

interface LinuxdoCurrentUser {
  id: string;
  username: string;
}

const LINUXDO_ORIGIN = "https://linux.do";
const DEFAULT_FETCH_TIMEOUT_MS = 30_000;
const DEFAULT_MAX_PAGES = 5;
const ABSOLUTE_MAX_PAGES = 50;
const ABSOLUTE_MAX_ITEMS = 300;
const ABSOLUTE_MAX_INPUTS = 20;
const MAX_RESPONSE_BYTES = 2 * 1024 * 1024;
const MAX_REQUEST_INTERVAL_MS = 30_000;
const TASK_LISTENER_SENTINEL = "__OPENBILICLAW_LINUXDO_TASK_LISTENER__";
const TASK_EXECUTION_SENTINEL = "__OPENBILICLAW_LINUXDO_TASK_EXECUTION__";
const TASK_TYPES = new Set<LinuxdoTaskType>([
  "bootstrap_events",
  "search",
  "hot",
  "feed",
  "creator",
  "related",
]);
const BOOTSTRAP_SCOPES: readonly LinuxdoScope[] = [
  "linuxdo_bookmarks",
  "linuxdo_likes",
  "linuxdo_read_history",
];
let activeRequestIntervalMs = 0;
let nextRequestAtMs = 0;

class LinuxdoHttpError extends Error {
  readonly status: number;
  readonly code: string;
  readonly requestPath: string;

  constructor(status: number, code: string, requestPath: string) {
    super(code);
    this.name = "LinuxdoHttpError";
    this.status = status;
    this.code = code;
    this.requestPath = requestPath;
  }
}

class LinuxdoPartialFetchError extends Error {
  readonly items: LinuxdoTaskItem[];
  readonly causeError: unknown;

  constructor(items: LinuxdoTaskItem[], causeError: unknown) {
    super("linuxdo_partial_fetch");
    this.items = items;
    this.causeError = causeError;
  }
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

function asRecord(value: unknown): Record<string, unknown> {
  return isRecord(value) ? value : {};
}

function str(value: unknown): string {
  return typeof value === "string" || typeof value === "number" ? String(value).trim() : "";
}

function nonNegativeNumber(value: unknown): number | undefined {
  if (typeof value === "number" && Number.isFinite(value)) return Math.max(0, value);
  if (typeof value === "string" && value.trim() && Number.isFinite(Number(value))) {
    return Math.max(0, Number(value));
  }
  return undefined;
}

function positiveInt(value: unknown, fallback: number, maximum: number): number {
  const parsed = Math.floor(Number(value));
  return Number.isFinite(parsed) && parsed > 0 ? Math.min(parsed, maximum) : fallback;
}

function cursorPosition(value: unknown): LinuxdoCursorPosition {
  const row = asRecord(value);
  const page = Math.floor(Number(row.page));
  const offset = Math.floor(Number(row.offset));
  return {
    page: Number.isFinite(page) && page >= 0 ? Math.min(page, 100_000) : 0,
    offset: Number.isFinite(offset) && offset >= 0 ? Math.min(offset, 10_000) : 0,
  };
}

function startCursor(message: LinuxdoExecuteMessage, key: string): LinuxdoCursorPosition {
  if (message.cursor_contract !== "page-offset-v1" || !isRecord(message.start_cursors)) {
    return { page: 0, offset: 0 };
  }
  return cursorPosition(message.start_cursors[key]);
}

function fetchTimeoutMs(message: LinuxdoExecuteMessage): number {
  return positiveInt(
    message.fetch_timeout_ms,
    DEFAULT_FETCH_TIMEOUT_MS,
    DEFAULT_FETCH_TIMEOUT_MS,
  );
}

function maxPages(message: LinuxdoExecuteMessage): number {
  return positiveInt(message.max_pages, DEFAULT_MAX_PAGES, ABSOLUTE_MAX_PAGES);
}

function requestIntervalMs(message: LinuxdoExecuteMessage): number {
  const seconds = Number(message.request_interval_seconds);
  if (!Number.isFinite(seconds) || seconds <= 0) return 0;
  return Math.min(MAX_REQUEST_INTERVAL_MS, Math.ceil(seconds * 1000));
}

async function waitForRequestSlot(): Promise<void> {
  const waitMs = Math.max(0, nextRequestAtMs - Date.now());
  if (waitMs > 0) {
    await new Promise<void>((resolve) => setTimeout(resolve, waitMs));
  }
  nextRequestAtMs = Date.now() + activeRequestIntervalMs;
}

function stripHtml(value: string): string {
  return value
    .replace(/<script\b[^>]*>[\s\S]*?<\/script>/gi, " ")
    .replace(/<style\b[^>]*>[\s\S]*?<\/style>/gi, " ")
    .replace(/<[^>]+>/g, " ")
    .replace(/&nbsp;|&#160;/gi, " ")
    .replace(/&amp;/gi, "&")
    .replace(/&lt;/gi, "<")
    .replace(/&gt;/gi, ">")
    .replace(/&quot;|&#34;/gi, '"')
    .replace(/&#39;|&apos;/gi, "'")
    .replace(/\s+/g, " ")
    .trim();
}

function safeLinuxdoUrl(value: string): string {
  if (!value) return "";
  try {
    const url = new URL(value, `${LINUXDO_ORIGIN}/`);
    if (url.protocol !== "https:" || url.hostname !== "linux.do") return "";
    url.hash = "";
    return url.href;
  } catch {
    return "";
  }
}

export function linuxdoTopicIdFromUrl(value: string): string {
  const url = safeLinuxdoUrl(value);
  const match = url.match(/\/t\/(?:(?:([1-9]\d*))|(?:[^/?#]+\/([1-9]\d*)))(?:[/?#]|$)/i);
  return match?.[1] ?? match?.[2] ?? "";
}

function topicUrl(topicId: string, slug: string, candidate = ""): string {
  const safeCandidate = safeLinuxdoUrl(candidate);
  if (safeCandidate && linuxdoTopicIdFromUrl(safeCandidate) === topicId) {
    const url = new URL(safeCandidate);
    url.search = "";
    return url.href;
  }
  return slug
    ? `${LINUXDO_ORIGIN}/t/${encodeURIComponent(slug)}/${topicId}`
    : `${LINUXDO_ORIGIN}/t/${topicId}`;
}

function usernameFromLinuxdoUrl(value: string): string {
  const url = safeLinuxdoUrl(value);
  const match = url.match(/\/u\/([^/?#]+)/i);
  if (!match?.[1]) return "";
  try {
    return decodeURIComponent(match[1]);
  } catch {
    return match[1];
  }
}

function creatorUsername(value: string): string {
  const trimmed = value.trim();
  const fromUrl = usernameFromLinuxdoUrl(trimmed);
  if (fromUrl) return fromUrl;
  return /^[A-Za-z0-9_.-]{1,100}$/.test(trimmed) ? trimmed : "";
}

function tagNames(value: unknown): string[] {
  if (!Array.isArray(value)) return [];
  const tags: string[] = [];
  for (const item of value) {
    const name = str(item) || str(asRecord(item).name) || str(asRecord(item).id);
    if (name && !tags.includes(name)) tags.push(name);
  }
  return tags.slice(0, 30);
}

function userMap(raw: unknown): Map<string, string> {
  const root = asRecord(raw);
  const users = Array.isArray(root.users) ? root.users : [];
  const result = new Map<string, string>();
  for (const value of users) {
    const row = asRecord(value);
    const id = str(row.id);
    const username = str(row.username);
    if (id && username) result.set(id, username);
  }
  return result;
}

function categoryMap(raw: unknown): Map<string, string> {
  const root = asRecord(raw);
  const categoryList = asRecord(root.category_list);
  const candidates = [root.categories, categoryList.categories];
  const result = new Map<string, string>();
  for (const candidate of candidates) {
    if (!Array.isArray(candidate)) continue;
    for (const value of candidate) {
      const row = asRecord(value);
      const id = str(row.id);
      const name = str(row.name) || str(row.slug);
      if (id && name) result.set(id, name);
    }
  }
  return result;
}

function authorFromTopic(row: Record<string, unknown>, users: Map<string, string>): string {
  const details = asRecord(row.details);
  const postStream = asRecord(row.post_stream);
  const firstPost = recordArray(postStream.posts)[0] ?? {};
  const direct =
    str(row.username) ||
    str(row.author) ||
    str(row.author_username) ||
    str(row.original_poster_username) ||
    str(asRecord(row.user).username) ||
    str(asRecord(details.created_by).username) ||
    str(firstPost.username);
  if (direct) return direct;
  const posters = Array.isArray(row.posters) ? row.posters : [];
  const original = posters.find((value) =>
    str(asRecord(value).description).toLowerCase().includes("original poster"),
  );
  const poster = asRecord(original ?? posters[0]);
  return (
    str(poster.username) ||
    str(asRecord(poster.user).username) ||
    users.get(str(poster.user_id)) ||
    ""
  );
}

function timestamp(value: unknown): string | number | undefined {
  if (typeof value === "number" && Number.isFinite(value)) return value;
  const text = str(value);
  return text || undefined;
}

export function normalizeLinuxdoTopic(
  raw: unknown,
  context: LinuxdoNormalizeContext,
): LinuxdoTaskItem | null {
  const wrapper = asRecord(raw);
  const nested = asRecord(wrapper.topic);
  const row = Object.keys(nested).length > 0 ? { ...wrapper, ...nested } : wrapper;
  const candidateUrl =
    str(row.url) ||
    str(row.link) ||
    str(row.bookmarkable_url) ||
    str(row.relative_url) ||
    str(row.permalink);
  const bookmarkableType = str(wrapper.bookmarkable_type).toLowerCase();
  const bookmarkableTopicId = bookmarkableType === "topic" ? str(wrapper.bookmarkable_id) : "";
  const rowCanOwnTopicIdentity =
    !bookmarkableType &&
    row.action_type === undefined &&
    row.post_id === undefined &&
    row.post_number === undefined;
  const topicId =
    str(context.topicIdOverride) ||
    str(row.topic_id) ||
    bookmarkableTopicId ||
    linuxdoTopicIdFromUrl(candidateUrl) ||
    (rowCanOwnTopicIdentity ? str(row.id) : "");
  if (!/^[1-9]\d*$/.test(topicId)) return null;

  const title = stripHtml(
    str(row.title) || str(row.topic_title) || str(row.fancy_title) || str(row.name),
  );
  const summary = stripHtml(
    str(row.excerpt) ||
      str(row.blurb) ||
      str(row.cooked) ||
      str(row.post_cooked) ||
      str(row.description),
  ).slice(0, 2_000);
  if (!title && !summary) return null;

  const slug = str(row.slug) || str(row.topic_slug);
  const users = context.users ?? new Map<string, string>();
  const categories = context.categories ?? new Map<string, string>();
  const author = authorFromTopic(row, users);
  const categoryId = str(row.category_id);
  const category =
    str(row.category_name) ||
    str(row.category) ||
    str(row.category_slug) ||
    categories.get(categoryId) ||
    "";
  const item: LinuxdoTaskItem = {
    scope: context.scope,
    content_type: "post",
    topic_id: topicId,
    content_id: `topic:${topicId}`,
    title: title || summary.slice(0, 100),
    url: topicUrl(topicId, slug, candidateUrl),
    tags: tagNames(row.tags ?? row.topic_tags),
    engagement_available: [],
    source_strategy: context.strategy,
  };
  if (author) {
    item.author = author;
    item.author_url = `${LINUXDO_ORIGIN}/u/${encodeURIComponent(author)}/activity/topics`;
  }
  if (summary) item.summary = summary;
  if (category) item.category = category;
  const views = nonNegativeNumber(row.views);
  if (views !== undefined) {
    item.views = views;
    item.engagement_available.push("view");
  }
  const likes = nonNegativeNumber(row.like_count ?? row.likes);
  if (likes !== undefined) {
    item.like_count = likes;
    item.engagement_available.push("like");
  }
  let replies = nonNegativeNumber(row.reply_count);
  const posts = nonNegativeNumber(row.posts_count);
  if (replies === undefined && posts !== undefined) replies = Math.max(0, posts - 1);
  if (replies !== undefined) {
    item.reply_count = replies;
    item.engagement_available.push("comment");
  }
  const publishedAt = timestamp(
    row.topic_created_at ??
      row.published_at ??
      (context.interactionAction === "favorite" || context.interactionAction === "like"
        ? undefined
        : row.created_at),
  );
  if (publishedAt !== undefined) item.published_at = publishedAt;
  if (context.interactionAction) {
    item.interaction_action = context.interactionAction;
    const interactionTime = context.interactionAction === "favorite"
      ? timestamp(wrapper.bookmarked_at ?? wrapper.created_at ?? wrapper.updated_at)
      : context.interactionAction === "like"
        ? timestamp(wrapper.acted_at ?? wrapper.created_at)
        : timestamp(wrapper.visited_at ?? wrapper.last_read_at ?? wrapper.read_at);
    if (interactionTime !== undefined) item.interaction_time = interactionTime;
  }
  if (context.searchKeyword) item.search_keyword = context.searchKeyword;
  if (context.sourceInput) item.source_input = context.sourceInput;
  if (context.sourceKeywordId !== undefined) item.source_keyword_id = context.sourceKeywordId;
  return item;
}

function responseContext(raw: unknown, context: LinuxdoNormalizeContext): LinuxdoNormalizeContext {
  return {
    ...context,
    users: userMap(raw),
    categories: categoryMap(raw),
  };
}

function recordArray(value: unknown): Record<string, unknown>[] {
  return Array.isArray(value) ? value.filter(isRecord) : [];
}

function requiredRecordArray(value: unknown, envelope: string): Record<string, unknown>[] {
  if (!Array.isArray(value) || value.some((item) => !isRecord(item))) {
    throw new LinuxdoHttpError(200, "linuxdo_invalid_envelope", envelope);
  }
  return value as Record<string, unknown>[];
}

export function collectLinuxdoTopicItems(
  raw: unknown,
  context: LinuxdoNormalizeContext,
  container: "listing" | "related" | "similar" | "bookmarks" | "actions" = "listing",
): LinuxdoTaskItem[] {
  const root = asRecord(raw);
  const ctx = responseContext(raw, context);
  let rows: Record<string, unknown>[] = [];
  if (container === "related") {
    rows = requiredRecordArray(root.suggested_topics, "related.suggested_topics");
  } else if (container === "similar") {
    const value = Array.isArray(root.similar_topics) ? root.similar_topics : root.topics;
    rows = requiredRecordArray(value, "similar.similar_topics");
  } else if (container === "bookmarks") {
    const nested = root.user_bookmark_list;
    const value = isRecord(nested) ? nested.bookmarks : root.bookmarks;
    rows = requiredRecordArray(value, "bookmarks.bookmarks");
  } else if (container === "actions") {
    rows = requiredRecordArray(root.user_actions, "actions.user_actions");
  } else {
    const topicList = asRecord(root.topic_list);
    const grouped = asRecord(root.grouped_search_result);
    const topicValue = Array.isArray(topicList.topics)
      ? topicList.topics
      : Array.isArray(root.topics)
      ? root.topics
      : grouped.topics;
    const postValue = Array.isArray(root.posts) ? root.posts : grouped.posts;
    const hasTopics = Array.isArray(topicValue);
    const hasPosts = Array.isArray(postValue);
    if (!hasTopics && !hasPosts) {
      throw new LinuxdoHttpError(200, "linuxdo_invalid_envelope", "listing.topics");
    }
    const topics = hasTopics ? requiredRecordArray(topicValue, "listing.topics") : [];
    const posts = hasPosts ? requiredRecordArray(postValue, "listing.posts") : [];
    if (posts.length > 0) {
      const topicsById = new Map(topics.map((topic) => [str(topic.id), topic]));
      rows = posts.map((post) => {
        const topic = topicsById.get(str(post.topic_id));
        if (!topic) return post;
        return {
          ...post,
          ...topic,
          topic_id: topic.id ?? post.topic_id,
          blurb: str(post.blurb) || str(topic.blurb),
          // Search posts identify the matching excerpt, not the topic owner or
          // topic-level engagement/publication. Keep those fields topic-owned
          // even when the search serializer omits them.
          username: topic.username,
          author: topic.author,
          author_username: topic.author_username,
          original_poster_username: topic.original_poster_username,
          like_count: topic.like_count,
          likes: topic.likes,
          views: topic.views,
          created_at: topic.created_at,
          topic_created_at: topic.created_at,
          post_created_at: undefined,
          published_at: topic.published_at,
        };
      });
    } else {
      rows = topics;
    }
  }
  return rows
    .map((row) => normalizeLinuxdoTopic(row, ctx))
    .filter((item): item is LinuxdoTaskItem => item !== null);
}

function buildSimilarTopicsUrl(raw: unknown): string {
  const root = asRecord(raw);
  const title = stripHtml(str(root.title) || str(root.fancy_title));
  if (!title) {
    throw new LinuxdoHttpError(200, "linuxdo_invalid_envelope", "related.topic.title");
  }
  const postStream = asRecord(root.post_stream);
  const firstPost = recordArray(postStream.posts)[0] ?? {};
  const body = stripHtml(
    str(firstPost.raw) || str(firstPost.cooked) || str(root.excerpt),
  ).slice(0, 500);
  const params = new URLSearchParams({ title, raw: body || title });
  return `${LINUXDO_ORIGIN}/topics/similar_to.json?${params.toString()}`;
}

function nextPagePath(raw: unknown): string {
  const root = asRecord(raw);
  const topicList = asRecord(root.topic_list);
  const grouped = asRecord(root.grouped_search_result);
  const bookmarks = asRecord(root.user_bookmark_list);
  const candidate =
    str(topicList.more_topics_url) ||
    str(grouped.more_results_url) ||
    str(grouped.more_full_page_results_url) ||
    str(bookmarks.more_bookmarks_url);
  const url = safeLinuxdoUrl(candidate);
  if (!url) return "";
  const parsed = new URL(url);
  return `${parsed.pathname}${parsed.search}`;
}

function pageNumberFromPath(path: string, fallback: number): number {
  if (!path) return Math.max(0, fallback);
  try {
    const parsed = new URL(path, `${LINUXDO_ORIGIN}/`);
    const page = Math.floor(Number(parsed.searchParams.get("page")));
    return Number.isFinite(page) && page >= 0 ? page : Math.max(0, fallback);
  } catch {
    return Math.max(0, fallback);
  }
}

function responseHasAnotherPage(raw: unknown): boolean {
  const root = asRecord(raw);
  const grouped = asRecord(root.grouped_search_result);
  const bookmarks = asRecord(root.user_bookmark_list);
  return (
    grouped.more_results === true ||
    grouped.more_posts === true ||
    grouped.more_full_page_results === true ||
    root.more_results === true ||
    bookmarks.more_bookmarks === true
  );
}

function requestPath(value: string): string {
  const url = safeLinuxdoUrl(value);
  if (!url) throw new LinuxdoHttpError(0, "invalid_request_url", "");
  const parsed = new URL(url);
  return `${parsed.pathname}${parsed.search}`;
}

function httpErrorCode(status: number): string {
  if (status === 401) return "linuxdo_login_required";
  if (status === 403) return "linuxdo_access_blocked";
  if (status === 429) return "linuxdo_rate_limited";
  if (status >= 500) return "linuxdo_upstream_unavailable";
  return "linuxdo_http_error";
}

async function fetchLinuxdoJson(
  path: string,
  timeoutMs: number = DEFAULT_FETCH_TIMEOUT_MS,
): Promise<unknown> {
  await waitForRequestSlot();
  const url = safeLinuxdoUrl(path);
  if (!url) throw new LinuxdoHttpError(0, "invalid_request_url", "");
  const safePath = requestPath(url);
  const controller = new AbortController();
  const timeoutId = setTimeout(() => controller.abort(), Math.max(1, timeoutMs));
  try {
    let response: Response;
    try {
      response = await fetch(url, {
        method: "GET",
        credentials: "include",
        headers: { accept: "application/json" },
        signal: controller.signal,
      });
    } catch (error) {
      if (controller.signal.aborted) {
        throw new LinuxdoHttpError(0, "linuxdo_request_timeout", safePath);
      }
      throw new LinuxdoHttpError(0, "linuxdo_network_error", safePath);
    }
    const declaredLength = Number(response.headers.get("content-length"));
    if (Number.isFinite(declaredLength) && declaredLength > MAX_RESPONSE_BYTES) {
      throw new LinuxdoHttpError(response.status, "linuxdo_response_too_large", safePath);
    }
    if (!response.ok) {
      throw new LinuxdoHttpError(response.status, httpErrorCode(response.status), safePath);
    }
    const text = await response.text();
    if (text.length > MAX_RESPONSE_BYTES) {
      throw new LinuxdoHttpError(response.status, "linuxdo_response_too_large", safePath);
    }
    const trimmed = text.trimStart();
    const contentType = response.headers.get("content-type")?.toLowerCase() ?? "";
    if (
      (!trimmed.startsWith("{") && !trimmed.startsWith("[")) ||
      !contentType.includes("json")
    ) {
      throw new LinuxdoHttpError(response.status, "linuxdo_invalid_response", safePath);
    }
    try {
      return JSON.parse(text) as unknown;
    } catch {
      throw new LinuxdoHttpError(response.status, "linuxdo_invalid_response", safePath);
    }
  } finally {
    clearTimeout(timeoutId);
  }
}

export function buildLinuxdoTaskUrl(
  type: LinuxdoDiscoveryTaskType,
  input: string,
  page = 0,
): string {
  const safePage = Math.max(0, Math.floor(Number(page) || 0));
  if (type === "search") {
    const params = new URLSearchParams({ q: input, page: String(safePage) });
    return `${LINUXDO_ORIGIN}/search.json?${params.toString()}`;
  }
  if (type === "hot") {
    return `${LINUXDO_ORIGIN}/hot.json?page=${safePage}`;
  }
  if (type === "feed") {
    return `${LINUXDO_ORIGIN}/latest.json?page=${safePage}`;
  }
  if (type === "creator") {
    const username = creatorUsername(input);
    if (!username) throw new LinuxdoHttpError(0, "invalid_creator", "");
    return `${LINUXDO_ORIGIN}/topics/created-by/${encodeURIComponent(username)}.json?page=${safePage}`;
  }
  const topicId =
    linuxdoTopicIdFromUrl(input) || (/^[1-9]\d*$/.test(input.trim()) ? input.trim() : "");
  if (!topicId) throw new LinuxdoHttpError(0, "invalid_related_seed", "");
  return `${LINUXDO_ORIGIN}/t/${topicId}.json`;
}

function buildTopFallbackUrl(page: number): string {
  const params = new URLSearchParams({ period: "weekly", page: String(page) });
  return `${LINUXDO_ORIGIN}/top.json?${params.toString()}`;
}

function mergeItem(previous: LinuxdoTaskItem, current: LinuxdoTaskItem): LinuxdoTaskItem {
  const merged = { ...previous };
  for (const [key, value] of Object.entries(current)) {
    if (value === undefined || value === "") continue;
    if (key === "views" || key === "like_count" || key === "reply_count") {
      const oldValue = nonNegativeNumber(merged[key as keyof LinuxdoTaskItem]);
      const newValue = nonNegativeNumber(value);
      if (newValue !== undefined && (oldValue === undefined || newValue > oldValue)) {
        (merged as unknown as Record<string, unknown>)[key] = newValue;
      }
      continue;
    }
    if (key === "tags" && Array.isArray(value)) {
      merged.tags = [
        ...new Set([
          ...merged.tags,
          ...value.filter((tag): tag is string => typeof tag === "string"),
        ]),
      ];
      continue;
    }
    if (key === "engagement_available" && Array.isArray(value)) {
      merged.engagement_available = [
        ...new Set([...merged.engagement_available, ...value]),
      ] as LinuxdoTaskItem["engagement_available"];
      continue;
    }
    if ((merged as unknown as Record<string, unknown>)[key] === undefined) {
      (merged as unknown as Record<string, unknown>)[key] = value;
    }
  }
  return merged;
}

function dedupeItems(items: LinuxdoTaskItem[]): LinuxdoTaskItem[] {
  const byKey = new Map<string, LinuxdoTaskItem>();
  for (const item of items) {
    const key = `${item.scope}:${item.topic_id}`;
    const previous = byKey.get(key);
    byKey.set(key, previous ? mergeItem(previous, item) : item);
  }
  return [...byKey.values()];
}

function detailContext(item: LinuxdoTaskItem): LinuxdoNormalizeContext {
  return {
    scope: item.scope,
    strategy: item.source_strategy,
    searchKeyword: item.search_keyword,
    sourceInput: item.source_input,
    sourceKeywordId: item.source_keyword_id,
    topicIdOverride: item.topic_id,
  };
}

function missingTopicDetailFields(item: LinuxdoTaskItem): string[] {
  const missing: string[] = [];
  if (!item.author) missing.push("author");
  if (item.views === undefined) missing.push("view");
  if (item.like_count === undefined) missing.push("like");
  if (item.reply_count === undefined) missing.push("comment");
  return missing;
}

async function hydrateTopicDetails(
  items: LinuxdoTaskItem[],
  timeoutMs: number,
): Promise<{ items: LinuxdoTaskItem[]; errors: Record<string, string> }> {
  const hydrated: LinuxdoTaskItem[] = [];
  const errors: Record<string, string> = {};
  for (const item of items) {
    if (missingTopicDetailFields(item).length === 0) {
      hydrated.push(item);
      continue;
    }
    try {
      const raw = await fetchLinuxdoJson(`${LINUXDO_ORIGIN}/t/${item.topic_id}.json`, timeoutMs);
      const detail = normalizeLinuxdoTopic(raw, detailContext(item));
      const merged = detail ? mergeItem(item, detail) : item;
      const missing = missingTopicDetailFields(merged);
      if (missing.length > 0) {
        errors[`detail:${item.topic_id}`] = `linuxdo_topic_detail_missing_${missing.join("_")}`;
      }
      hydrated.push(merged);
    } catch (error) {
      errors[`detail:${item.topic_id}`] = taskErrorCode(error);
      hydrated.push(item);
    }
  }
  return { items: hydrated, errors };
}

const BOOTSTRAP_ENGAGEMENT_FIELDS = ["views", "like_count", "reply_count"] as const;

function backfillBootstrapEngagement(items: LinuxdoTaskItem[]): LinuxdoTaskItem[] {
  const engagementByTopic = new Map<
    string,
    Partial<Record<(typeof BOOTSTRAP_ENGAGEMENT_FIELDS)[number], number>>
  >();

  for (const item of items) {
    const engagement = engagementByTopic.get(item.topic_id) ?? {};
    for (const field of BOOTSTRAP_ENGAGEMENT_FIELDS) {
      const value = nonNegativeNumber(item[field]);
      const known = engagement[field];
      if (value !== undefined && (known === undefined || value > known)) {
        engagement[field] = value;
      }
    }
    engagementByTopic.set(item.topic_id, engagement);
  }

  return items.map((item) => {
    const engagement = engagementByTopic.get(item.topic_id);
    if (!engagement) return item;
    let enriched = item;
    for (const field of BOOTSTRAP_ENGAGEMENT_FIELDS) {
      const value = engagement[field];
      if (item[field] === undefined && value !== undefined) {
        if (enriched === item) enriched = { ...item };
        enriched[field] = value;
        const metric = field === "views"
          ? "view"
          : field === "like_count"
          ? "like"
          : "comment";
        if (!enriched.engagement_available.includes(metric)) {
          enriched.engagement_available = [...enriched.engagement_available, metric];
        }
      }
    }
    return enriched;
  });
}

async function fetchPagedTopics(options: {
  buildUrl: (page: number) => string;
  context: LinuxdoNormalizeContext;
  limit: number;
  pages: number;
  timeoutMs: number;
  container?: "listing" | "bookmarks";
  hotFallback?: boolean;
  startCursor?: LinuxdoCursorPosition;
  onNextCursor?: (cursor: LinuxdoCursorPosition) => void;
}): Promise<LinuxdoTaskItem[]> {
  const results: LinuxdoTaskItem[] = [];
  const initial = cursorPosition(options.startCursor);
  let currentPage = initial.page;
  let currentOffset = initial.offset;
  let pagesFetched = 0;
  let nextPath = "";
  let lastSignature = "";
  let resetAttempted = false;
  let nextCursor: LinuxdoCursorPosition = { page: currentPage, offset: currentOffset };
  while (pagesFetched < options.pages && results.length < options.limit) {
    const primary = nextPath || options.buildUrl(currentPage);
    let raw: unknown;
    try {
      raw = await fetchLinuxdoJson(primary, options.timeoutMs);
    } catch (error) {
      if (
        options.hotFallback &&
        error instanceof LinuxdoHttpError &&
        (error.status === 400 || error.status === 404)
      ) {
        try {
          raw = await fetchLinuxdoJson(buildTopFallbackUrl(currentPage), options.timeoutMs);
        } catch (fallbackError) {
          if (results.length > 0) {
            throw new LinuxdoPartialFetchError(dedupeItems(results), fallbackError);
          }
          throw fallbackError;
        }
      } else {
        if (results.length > 0) {
          throw new LinuxdoPartialFetchError(dedupeItems(results), error);
        }
        throw error;
      }
    }
    const pageItems = collectLinuxdoTopicItems(raw, options.context, options.container ?? "listing");
    const signature = pageItems.map((item) => item.topic_id).join(",");
    const candidateNextPath = nextPagePath(raw);
    const hasMore = Boolean(candidateNextPath) || responseHasAnotherPage(raw);
    if (!pageItems.length || (signature && signature === lastSignature)) {
      if (
        results.length === 0 &&
        !resetAttempted &&
        (initial.page > 0 || initial.offset > 0)
      ) {
        // Live Discourse lists can shrink between runs. Reset once, in the
        // same task, rather than pinning a durable cursor past the new tail.
        resetAttempted = true;
        currentPage = 0;
        currentOffset = 0;
        pagesFetched = 0;
        nextPath = "";
        lastSignature = "";
        continue;
      }
      nextCursor = { page: 0, offset: 0 };
      break;
    }
    if (
      results.length === 0 &&
      !resetAttempted &&
      currentOffset > 0 &&
      (currentOffset > pageItems.length || (currentOffset === pageItems.length && !hasMore))
    ) {
      // The saved offset no longer identifies an unread row (the list may
      // have shrunk, or the prior run consumed the old tail). Reset once in
      // this same task so a valid cycle does not masquerade as true-empty.
      resetAttempted = true;
      currentPage = 0;
      currentOffset = 0;
      pagesFetched = 0;
      nextPath = "";
      lastSignature = "";
      continue;
    }
    lastSignature = signature;
    const offset = Math.min(currentOffset, pageItems.length);
    const available = pageItems.slice(offset);
    const remaining = Math.max(0, options.limit - results.length);
    const accepted = available.slice(0, remaining);
    results.push(...accepted);
    if (accepted.length < available.length) {
      nextCursor = { page: currentPage, offset: offset + accepted.length };
      break;
    }

    nextPath = candidateNextPath;
    nextCursor = hasMore
      ? { page: pageNumberFromPath(nextPath, currentPage + 1), offset: 0 }
      : { page: 0, offset: 0 };
    pagesFetched += 1;
    currentOffset = 0;
    if (!hasMore || results.length >= options.limit) break;
    currentPage = nextCursor.page;
  }
  options.onNextCursor?.(nextCursor);
  return dedupeItems(results).slice(0, options.limit);
}

function currentUser(raw: unknown): LinuxdoCurrentUser | null {
  const root = asRecord(raw);
  const row = asRecord(root.current_user);
  const username = str(row.username);
  const id = str(row.id);
  return username ? { id, username } : null;
}

async function linuxdoAccountKey(user: LinuxdoCurrentUser): Promise<string> {
  const identity = user.id
    ? `linuxdo:id:${user.id}`
    : `linuxdo:username:${user.username.trim().toLowerCase()}`;
  const digest = await crypto.subtle.digest("SHA-256", new TextEncoder().encode(identity));
  const hex = [...new Uint8Array(digest)]
    .map((value) => value.toString(16).padStart(2, "0"))
    .join("");
  return `sha256:${hex}`;
}

async function fetchCurrentUser(timeoutMs: number): Promise<LinuxdoCurrentUser> {
  const user = currentUser(await fetchLinuxdoJson("/session/current.json", timeoutMs));
  if (!user) {
    throw new LinuxdoHttpError(401, "linuxdo_login_required", "/session/current.json");
  }
  return user;
}

async function fetchLikes(
  username: string,
  context: LinuxdoNormalizeContext,
  limit: number,
  pages: number,
  timeoutMs: number,
): Promise<LinuxdoTaskItem[]> {
  const results: LinuxdoTaskItem[] = [];
  let offset = 0;
  let lastSignature = "";
  for (let page = 0; page < pages && results.length < limit; page += 1) {
    const params = new URLSearchParams({
      username,
      filter: "1",
      offset: String(offset),
    });
    let raw: unknown;
    try {
      raw = await fetchLinuxdoJson(`/user_actions.json?${params.toString()}`, timeoutMs);
    } catch (error) {
      if (results.length > 0) {
        throw new LinuxdoPartialFetchError(dedupeItems(results), error);
      }
      throw error;
    }
    const root = asRecord(raw);
    const rawActions = recordArray(root.user_actions);
    const pageItems = collectLinuxdoTopicItems(raw, context, "actions");
    const signature = pageItems.map((item) => item.topic_id).join(",");
    if (!rawActions.length || (signature && signature === lastSignature)) break;
    lastSignature = signature;
    results.push(...pageItems);
    offset += rawActions.length;
    if (rawActions.length === 0) break;
  }
  return dedupeItems(results).slice(0, limit);
}

async function runBootstrap(message: LinuxdoExecuteMessage): Promise<{
  items: LinuxdoTaskItem[];
  scopeErrors: Record<string, string>;
  accountKey: string;
  completeScopes: LinuxdoScope[];
}> {
  const timeoutMs = fetchTimeoutMs(message);
  const user = await fetchCurrentUser(timeoutMs);
  const accountKey = await linuxdoAccountKey(user);
  const limit = positiveInt(
    message.max_items_per_scope ?? message.max_items,
    300,
    ABSOLUTE_MAX_ITEMS,
  );
  const pages = message.max_pages === undefined
    ? Math.min(
      ABSOLUTE_MAX_PAGES,
      Math.max(DEFAULT_MAX_PAGES, Math.ceil(limit / 20)),
    )
    : maxPages(message);
  const requested = Array.isArray(message.scopes) && message.scopes.length > 0
    ? message.scopes.filter((scope): scope is LinuxdoScope => BOOTSTRAP_SCOPES.includes(scope))
    : [...BOOTSTRAP_SCOPES];
  const scopes = [...new Set(requested)];
  const items: LinuxdoTaskItem[] = [];
  const scopeErrors: Record<string, string> = {};
  const completeScopes: LinuxdoScope[] = [];

  for (const scope of scopes) {
    try {
      if (scope === "linuxdo_bookmarks") {
        items.push(
          ...(await fetchPagedTopics({
            buildUrl: (page) =>
              `${LINUXDO_ORIGIN}/u/${encodeURIComponent(user.username)}/bookmarks.json?page=${page}`,
            context: {
              scope,
              strategy: "linuxdo-bootstrap-bookmarks",
              interactionAction: "favorite",
            },
            limit,
            pages,
            timeoutMs,
            container: "bookmarks",
          })),
        );
      } else if (scope === "linuxdo_likes") {
        items.push(
          ...(await fetchLikes(
            user.username,
            { scope, strategy: "linuxdo-bootstrap-likes", interactionAction: "like" },
            limit,
            pages,
            timeoutMs,
          )),
        );
      } else if (scope === "linuxdo_read_history") {
        items.push(
          ...(await fetchPagedTopics({
            buildUrl: (page) => `${LINUXDO_ORIGIN}/read.json?page=${page}`,
            context: {
              scope,
              strategy: "linuxdo-bootstrap-read-history",
              interactionAction: "view",
            },
            limit,
            pages,
            timeoutMs,
          })),
        );
      }
      completeScopes.push(scope);
    } catch (error) {
      if (error instanceof LinuxdoPartialFetchError) items.push(...error.items);
      scopeErrors[scope] = taskErrorCode(error);
    }
  }
  return {
    items: backfillBootstrapEngagement(dedupeItems(items)),
    scopeErrors,
    accountKey,
    completeScopes,
  };
}

function discoveryScope(type: LinuxdoDiscoveryTaskType): LinuxdoScope {
  return `linuxdo_${type}` as LinuxdoScope;
}

async function runDiscovery(message: LinuxdoExecuteMessage): Promise<{
  items: LinuxdoTaskItem[];
  inputErrors: Record<string, string>;
  responseObserved: boolean;
  completeScopes: LinuxdoScope[];
  nextCursors: Record<string, LinuxdoCursorPosition>;
}> {
  const type = (message.type ?? "search") as LinuxdoDiscoveryTaskType;
  const timeoutMs = fetchTimeoutMs(message);
  const pages = maxPages(message);
  const scope = discoveryScope(type);
  const strategy = `linuxdo-${type}`;
  const rows: LinuxdoTaskItem[] = [];
  const inputErrors: Record<string, string> = {};
  const nextCursors: Record<string, LinuxdoCursorPosition> = {};
  const cursorEnabled = message.cursor_contract === "page-offset-v1";
  let observedInputs = 0;

  if (type === "search") {
    const keywords = (Array.isArray(message.keywords) ? message.keywords : [])
      .map(str)
      .filter(Boolean)
      .slice(0, ABSOLUTE_MAX_INPUTS);
    const ids = isRecord(message.source_keyword_ids) ? message.source_keyword_ids : {};
    const limit = positiveInt(
      message.max_items_per_keyword ?? message.max_items,
      10,
      ABSOLUTE_MAX_ITEMS,
    );
    const totalLimit = positiveInt(
      message.max_items,
      Math.min(ABSOLUTE_MAX_ITEMS, Math.max(1, limit * Math.max(1, keywords.length))),
      ABSOLUTE_MAX_ITEMS,
    );
    for (const keyword of keywords) {
      const remaining = totalLimit - dedupeItems(rows).length;
      if (remaining <= 0) break;
      try {
        rows.push(...(await fetchPagedTopics({
          buildUrl: (page) => buildLinuxdoTaskUrl("search", keyword, page),
          context: {
            scope,
            strategy,
            searchKeyword: keyword,
            sourceKeywordId: nonNegativeNumber(ids[keyword]),
          },
          limit: Math.min(limit, remaining),
          pages,
          timeoutMs,
          startCursor: startCursor(message, keyword),
          onNextCursor: cursorEnabled
            ? (cursor) => { nextCursors[keyword] = cursor; }
            : undefined,
        })));
        observedInputs += 1;
      } catch (error) {
        if (error instanceof LinuxdoPartialFetchError) rows.push(...error.items);
        inputErrors[`search:${keyword}`] = taskErrorCode(error);
        break;
      }
    }
    let retained = dedupeItems(rows).slice(0, totalLimit);
    if (message.hydrate_topic_details === true) {
      const detail = await hydrateTopicDetails(retained, timeoutMs);
      retained = detail.items;
      Object.assign(inputErrors, detail.errors);
    }
    return {
      items: retained,
      inputErrors,
      responseObserved: observedInputs > 0 || rows.length > 0,
      completeScopes: Object.keys(inputErrors).length === 0 ? [scope] : [],
      nextCursors,
    };
  }

  if (type === "hot" || type === "feed") {
    const limit = positiveInt(message.max_items, 20, ABSOLUTE_MAX_ITEMS);
    try {
      const items = await fetchPagedTopics({
        buildUrl: (page) => buildLinuxdoTaskUrl(type, "", page),
        context: { scope, strategy },
        limit,
        pages,
        timeoutMs,
        hotFallback: type === "hot",
        startCursor: startCursor(message, "default"),
        onNextCursor: cursorEnabled
          ? (cursor) => { nextCursors.default = cursor; }
          : undefined,
      });
      return {
        items,
        inputErrors,
        responseObserved: true,
        completeScopes: [scope],
        nextCursors,
      };
    } catch (error) {
      const items = error instanceof LinuxdoPartialFetchError ? error.items : [];
      inputErrors[type] = taskErrorCode(error);
      return {
        items,
        inputErrors,
        responseObserved: items.length > 0,
        completeScopes: [],
        nextCursors,
      };
    }
  }

  if (type === "creator") {
    const creators = (Array.isArray(message.creator_urls) ? message.creator_urls : [])
      .map(str)
      .filter(Boolean)
      .slice(0, ABSOLUTE_MAX_INPUTS);
    const limit = positiveInt(message.max_items_per_creator ?? message.max_items, 20, ABSOLUTE_MAX_ITEMS);
    for (const creator of creators) {
      try {
        rows.push(...(await fetchPagedTopics({
          buildUrl: (page) => buildLinuxdoTaskUrl("creator", creator, page),
          context: { scope, strategy, sourceInput: creator },
          limit,
          pages,
          timeoutMs,
          startCursor: startCursor(message, creator),
          onNextCursor: cursorEnabled
            ? (cursor) => { nextCursors[creator] = cursor; }
            : undefined,
        })));
        observedInputs += 1;
      } catch (error) {
        if (error instanceof LinuxdoPartialFetchError) rows.push(...error.items);
        inputErrors[`creator:${creator}`] = taskErrorCode(error);
        break;
      }
    }
    return {
      items: dedupeItems(rows),
      inputErrors,
      responseObserved: observedInputs > 0 || rows.length > 0,
      completeScopes: Object.keys(inputErrors).length === 0 ? [scope] : [],
      nextCursors,
    };
  }

  const seeds = (Array.isArray(message.related_urls) ? message.related_urls : [])
    .map(str)
    .filter(Boolean)
    .slice(0, ABSOLUTE_MAX_INPUTS);
  const limit = positiveInt(message.max_items_per_seed ?? message.max_items, 20, ABSOLUTE_MAX_ITEMS);
  const totalLimit = positiveInt(
    message.max_items,
    Math.min(ABSOLUTE_MAX_ITEMS, Math.max(1, limit * Math.max(1, seeds.length))),
    ABSOLUTE_MAX_ITEMS,
  );
  for (const seed of seeds) {
    const remaining = totalLimit - dedupeItems(rows).length;
    if (remaining <= 0) break;
    try {
      const seedId = linuxdoTopicIdFromUrl(seed) || str(seed);
      const detail = await fetchLinuxdoJson(buildLinuxdoTaskUrl("related", seed), timeoutMs);
      const similar = await fetchLinuxdoJson(buildSimilarTopicsUrl(detail), timeoutMs);
      rows.push(
        ...collectLinuxdoTopicItems(
          similar,
          { scope, strategy, sourceInput: seed },
          "similar",
        )
          .filter((item) => item.topic_id !== seedId)
          .slice(0, Math.min(limit, remaining)),
      );
      observedInputs += 1;
    } catch (error) {
      inputErrors[`related:${seed}`] = taskErrorCode(error);
      if (!(error instanceof LinuxdoHttpError) || error.status !== 404) break;
    }
  }
  let retained = dedupeItems(rows).slice(0, totalLimit);
  if (message.hydrate_topic_details === true) {
    const detail = await hydrateTopicDetails(retained, timeoutMs);
    retained = detail.items;
    Object.assign(inputErrors, detail.errors);
  }
  return {
    items: retained,
    inputErrors,
    responseObserved: observedInputs > 0,
    completeScopes: Object.keys(inputErrors).length === 0 ? [scope] : [],
    nextCursors,
  };
}

function taskErrorCode(error: unknown): string {
  if (error instanceof LinuxdoPartialFetchError) return taskErrorCode(error.causeError);
  if (error instanceof LinuxdoHttpError) return error.code;
  return "linuxdo_task_failed";
}

function taskErrorDebug(error: unknown): Record<string, unknown> {
  if (!(error instanceof LinuxdoHttpError)) return { code: "linuxdo_task_failed" };
  return {
    code: error.code,
    status: error.status,
    ...(error.requestPath ? { path: error.requestPath } : {}),
  };
}

function scopeCounts(items: LinuxdoTaskItem[]): Record<string, number> {
  const counts: Record<string, number> = {};
  for (const item of items) counts[item.scope] = (counts[item.scope] ?? 0) + 1;
  return counts;
}

export async function executeLinuxdoTask(message: LinuxdoExecuteMessage): Promise<LinuxdoTaskResult> {
  const taskId = str(message.task_id);
  if (!taskId) {
    return {
      task_id: "",
      status: "failed",
      items: [],
      scope_counts: {},
      error: "task_id_required",
    };
  }
  const type = message.type ?? "search";
  if (!TASK_TYPES.has(type)) {
    return {
      task_id: taskId,
      status: "failed",
      items: [],
      scope_counts: {},
      error: "invalid_task_type",
    };
  }
  activeRequestIntervalMs = requestIntervalMs(message);
  try {
    if (type === "bootstrap_events") {
      const { items, scopeErrors, accountKey, completeScopes } = await runBootstrap(message);
      if (items.length === 0 && Object.keys(scopeErrors).length > 0) {
        const firstError = Object.values(scopeErrors)[0] ?? "linuxdo_task_failed";
        return {
          task_id: taskId,
          status: "failed",
          items: [],
          scope_counts: {},
          account_key: accountKey,
          response_observed: completeScopes.length > 0,
          complete_scopes: completeScopes,
          error: firstError,
          debug: { scope_errors: scopeErrors },
        };
      }
      return {
        task_id: taskId,
        status: Object.keys(scopeErrors).length > 0
          ? "degraded"
          : items.length > 0
          ? "ok"
          : "empty",
        items,
        scope_counts: scopeCounts(items),
        account_key: accountKey,
        response_observed: completeScopes.length > 0,
        complete_scopes: completeScopes,
        ...(Object.keys(scopeErrors).length > 0
          ? { debug: { scope_errors: scopeErrors } }
          : {}),
      };
    }
    const { items, inputErrors, responseObserved, completeScopes, nextCursors } =
      await runDiscovery(message);
    if (items.length === 0 && Object.keys(inputErrors).length > 0) {
      return {
        task_id: taskId,
        status: "failed",
        items: [],
        scope_counts: {},
        response_observed: responseObserved,
        complete_scopes: completeScopes,
        ...(Object.keys(nextCursors).length > 0 ? { next_cursors: nextCursors } : {}),
        error: Object.values(inputErrors)[0] ?? "linuxdo_task_failed",
        debug: { input_errors: inputErrors },
      };
    }
    return {
      task_id: taskId,
      status: Object.keys(inputErrors).length > 0
        ? "degraded"
        : items.length > 0
        ? "ok"
        : "empty",
      items,
      scope_counts: scopeCounts(items),
      response_observed: responseObserved,
      complete_scopes: completeScopes,
      ...(Object.keys(nextCursors).length > 0 ? { next_cursors: nextCursors } : {}),
      ...(Object.keys(inputErrors).length > 0
        ? { debug: { input_errors: inputErrors } }
        : {}),
    };
  } catch (error) {
    return {
      task_id: taskId,
      status: "failed",
      items: [],
      scope_counts: {},
      error: taskErrorCode(error),
      debug: taskErrorDebug(error),
    };
  }
}

async function sendLinuxdoTaskResult(result: LinuxdoTaskResult): Promise<void> {
  let lastError: unknown;
  for (let attempt = 0; attempt < 3; attempt += 1) {
    try {
      const response = await chrome.runtime.sendMessage({
        action: "LINUXDO_TASK_RESULT",
        data: result,
      });
      if (asRecord(response).ok === true) return;
      lastError = new Error(str(asRecord(response).error) || "linuxdo_result_rejected");
    } catch (error) {
      lastError = error;
    }
    await new Promise<void>((resolve) => setTimeout(resolve, 500 * (attempt + 1)));
  }
  throw lastError instanceof Error ? lastError : new Error("linuxdo_result_transport_failed");
}

export function installLinuxdoMessageListener(): void {
  if (typeof chrome === "undefined" || !chrome.runtime?.onMessage) return;
  const globalState = globalThis as unknown as Record<string, unknown>;
  if (globalState[TASK_LISTENER_SENTINEL]) return;
  globalState[TASK_LISTENER_SENTINEL] = true;
  chrome.runtime.onMessage.addListener(
    (message: unknown, _sender, sendResponse: (response: unknown) => void) => {
      const payload = asRecord(message);
      if (payload.action !== "LINUXDO_TASK_EXECUTE") return false;
      const task = asRecord(payload.data) as unknown as LinuxdoExecuteMessage;
      const taskId = str(task.task_id);
      const existing = globalState[TASK_EXECUTION_SENTINEL] as
        | { taskId: string; delivery: Promise<void> }
        | undefined;
      if (existing && existing.taskId !== taskId) {
        sendResponse({ ok: false, error: "linuxdo_task_already_running" });
        return true;
      }
      const delivery = existing?.delivery ?? executeLinuxdoTask(task).then((result) =>
        sendLinuxdoTaskResult({ ...result, claim_token: str(task.claim_token) })
      );
      if (!existing) {
        globalState[TASK_EXECUTION_SENTINEL] = { taskId, delivery };
      }
      void delivery
        .then(() => sendResponse({ ok: true }))
        .catch(() => sendResponse({ ok: false, error: "linuxdo_result_transport_failed" }));
      return true;
    },
  );
}
