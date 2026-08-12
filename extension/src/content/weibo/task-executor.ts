/**
 * Logged-in Weibo bootstrap executor.
 *
 * All requests are same-origin from the task tab and use the browser's
 * credentials.  The result contains normalized public rows only; no Cookie,
 * Authorization header, or raw HTML is ever sent to the backend.
 */

export type WeiboScope = "weibo_favorites" | "weibo_following" | "weibo_mentions";
export type WeiboTaskType = "bootstrap_events";

export interface WeiboBootstrapItem {
  scope: WeiboScope;
  content_type: "post" | "user";
  content_id?: string;
  status_id?: string;
  uid?: string;
  user_id?: string;
  title?: string;
  summary?: string;
  author?: string;
  screen_name?: string;
  url?: string;
  interaction_time?: string;
  published_at?: string | number;
  like_count?: number;
  comment_count?: number;
  share_count?: number;
}

export interface WeiboExecuteMessage {
  task_id: string;
  claim_token: string;
  type?: WeiboTaskType;
  scopes?: WeiboScope[];
  max_items_per_scope?: number;
}

export interface WeiboTaskResult {
  task_id: string;
  claim_token: string;
  status: "ok" | "empty" | "partial" | "failed";
  items: WeiboBootstrapItem[];
  scope_counts: Record<string, number>;
  error?: string;
  debug?: Record<string, unknown>;
}

class WeiboHttpError extends Error {
  readonly status: number;
  readonly requestUrl: string;

  constructor(message: string, status: number, requestUrl: string) {
    super(message);
    this.name = "WeiboHttpError";
    this.status = status;
    this.requestUrl = requestUrl;
  }
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

function scalar(value: unknown): string {
  return typeof value === "string" || typeof value === "number"
    ? String(value).trim()
    : "";
}

function stripHtml(value: unknown): string {
  if (typeof value !== "string") return "";
  const text = value.replace(/<[^>]+>/g, " ").replace(/&nbsp;/g, " ");
  return text.replace(/\s+/g, " ").trim();
}

function asNumber(value: unknown): number | undefined {
  if (typeof value === "boolean" || value === null || value === undefined) return undefined;
  const parsed = Number(String(value).replace(/,/g, ""));
  return Number.isFinite(parsed) ? Math.max(0, Math.floor(parsed)) : undefined;
}

function absoluteUrl(value: unknown, fallback = ""): string {
  const raw = scalar(value);
  if (!raw) return fallback;
  try {
    return new URL(raw, "https://m.weibo.cn").toString();
  } catch {
    return fallback;
  }
}

function statusObject(raw: Record<string, unknown>): Record<string, unknown> {
  const nested = raw.status ?? raw.mblog ?? raw.weibo;
  return isRecord(nested) ? nested : raw;
}

function currentUserObject(raw: Record<string, unknown>): Record<string, unknown> | null {
  const data = isRecord(raw.data) ? raw.data : raw;
  const user = data.user ?? data.userInfo ?? data.userinfo ?? data.account;
  return isRecord(user) ? user : null;
}

function responseRows(raw: unknown): Record<string, unknown>[] {
  const rows: Record<string, unknown>[] = [];
  const visit = (value: unknown): void => {
    if (Array.isArray(value)) {
      for (const item of value) visit(item);
      return;
    }
    if (!isRecord(value)) return;
    const hasStatus = Boolean(value.mblog || value.status || value.weibo);
    const hasUser = Boolean(
      value.user || value.userInfo || value.uid || value.user_id ||
      (value.id && (value.screen_name || value.name || value.profile_url)),
    );
    if (hasStatus || hasUser) rows.push(value);
    for (const key of ["list", "data", "cards", "card_group", "items", "statuses", "users"]) {
      const nested: unknown = value[key];
      if (nested !== value) visit(nested);
    }
  };
  visit(raw);
  return rows;
}

function normalizeStatus(raw: Record<string, unknown>, scope: WeiboScope): WeiboBootstrapItem | null {
  const status = statusObject(raw);
  const user = isRecord(status.user) ? status.user : isRecord(raw.user) ? raw.user : {};
  const contentId = scalar(status.id ?? status.mid ?? status.idstr ?? raw.id);
  const uid = scalar(user.id ?? user.uid ?? status.uid ?? raw.uid);
  const author = stripHtml(user.screen_name ?? user.name ?? raw.screen_name ?? raw.name);
  const title = stripHtml(status.text_raw ?? status.raw_text ?? status.text ?? raw.text ?? raw.title);
  const url = absoluteUrl(
    status.scheme ?? status.url ?? raw.scheme ?? raw.url,
    contentId ? `https://m.weibo.cn/detail/${contentId}` : "",
  );
  if (!contentId && !title && !url) return null;
  return {
    scope,
    content_type: "post",
    content_id: contentId,
    status_id: contentId,
    uid,
    user_id: uid,
    title: title || url,
    summary: title,
    author,
    screen_name: author,
    url,
    interaction_time: scalar(raw.created_at ?? raw.favorited_time ?? raw.time),
    published_at: scalar(status.created_at ?? raw.created_at),
    like_count: asNumber(status.attitudes_count ?? status.like_counts ?? status.attitudes),
    comment_count: asNumber(status.comments_count ?? status.comment_count),
    share_count: asNumber(status.reposts_count ?? status.repost_count),
  };
}

function normalizeFollowing(raw: Record<string, unknown>): WeiboBootstrapItem | null {
  const user = isRecord(raw.user) ? raw.user : raw;
  const uid = scalar(user.id ?? user.uid ?? raw.user_id);
  const author = stripHtml(user.screen_name ?? user.name ?? raw.screen_name ?? raw.name);
  const url = absoluteUrl(user.profile_url ?? user.url ?? raw.url, uid ? `https://weibo.com/u/${uid}` : "");
  if (!uid && !author && !url) return null;
  return {
    scope: "weibo_following",
    content_type: "user",
    uid,
    user_id: uid,
    content_id: uid,
    title: author || url || uid,
    author,
    screen_name: author,
    url,
  };
}

async function fetchJson(url: string): Promise<Record<string, unknown>> {
  const response = await fetch(url, { credentials: "include", headers: { accept: "application/json" } });
  const text = await response.text();
  if (!response.ok) throw new WeiboHttpError(`http_${response.status}`, response.status, url);
  if (text.length > 2 * 1024 * 1024) {
    throw new WeiboHttpError("response_too_large", response.status, url);
  }
  let parsed: unknown;
  try {
    parsed = JSON.parse(text);
  } catch {
    throw new WeiboHttpError(
      response.headers.get("content-type")?.includes("text/html") ? "login_wall_or_challenge" : "invalid_json",
      response.status,
      url,
    );
  }
  if (!isRecord(parsed)) throw new WeiboHttpError("invalid_response", response.status, url);
  // A number of H5 account endpoints return HTTP 200 with an explicit
  // ``ok: 0`` business failure (login wall, expired session, or a challenge).
  // Treat that envelope as a failed scope instead of allowing an empty
  // ``data`` array to masquerade as a healthy empty import.
  const ok = parsed.ok;
  if (
    ok !== undefined &&
    ok !== 1 &&
    ok !== true &&
    ok !== "1"
  ) {
    const message = scalar(parsed.msg ?? parsed.message ?? parsed.error) || "upstream_error";
    throw new WeiboHttpError(message, response.status, url);
  }
  return parsed;
}

async function fetchWithCandidates(candidates: string[]): Promise<Record<string, unknown>> {
  let last: unknown = new Error("no_endpoint");
  for (const url of candidates) {
    try {
      return await fetchJson(url);
    } catch (error) {
      last = error;
      if (error instanceof WeiboHttpError && error.status === 401) throw error;
    }
  }
  throw last;
}

async function fetchCurrentConfig(): Promise<Record<string, unknown>> {
  return fetchJson("/api/config");
}

function configLogin(raw: Record<string, unknown>): { loggedIn: boolean; userId: string } {
  const data = isRecord(raw.data) ? raw.data : raw;
  const user = currentUserObject(raw);
  const userId = scalar(
    user?.id ?? user?.uid ?? data.uid ?? data.user_id ?? data.userId ?? raw.uid ?? raw.user_id,
  );
  const explicitLogin = typeof data.login === "boolean"
    ? data.login
    : typeof data.logged_in === "boolean"
      ? data.logged_in
      : undefined;
  // Some anonymous H5 responses include a guest-shaped ``userInfo`` object.
  // Never treat that object alone as an account; an explicit ``login:false``
  // also wins over any incidental guest uid in the payload.
  const loggedIn = explicitLogin === false
    ? false
    : explicitLogin === true || Boolean(userId);
  return { loggedIn, userId };
}

async function fetchCurrentIdentity(): Promise<{ loggedIn: boolean; userId: string }> {
  const configIdentity = configLogin(await fetchCurrentConfig());
  if (configIdentity.loggedIn && configIdentity.userId) return configIdentity;
  try {
    // ``/api/account/getuid`` is the account-specific signal on the H5 site;
    // anonymous sessions redirect to the visitor page and are rejected by
    // fetchJson rather than being treated as a valid empty identity.
    const accountIdentity = configLogin(await fetchJson("/api/account/getuid"));
    return {
      loggedIn: configIdentity.loggedIn || accountIdentity.loggedIn,
      userId: accountIdentity.userId || configIdentity.userId,
    };
  } catch {
    return configIdentity;
  }
}

async function fetchFavorites(limit: number): Promise<WeiboBootstrapItem[]> {
  const raw = await fetchWithCandidates([
    `/api/container/getIndex?containerid=230259&openApp=0&page=1`,
    `/api/favorites?count=${limit}&page=1`,
    `/api/fav?count=${limit}&page=1`,
    `/api/favorites?count=${limit}`,
  ]);
  return responseRows(raw)
    .map((row) => normalizeStatus(row, "weibo_favorites"))
    .filter((item): item is WeiboBootstrapItem => item !== null)
    .slice(0, limit);
}

async function fetchFollowing(limit: number): Promise<WeiboBootstrapItem[]> {
  const raw = await fetchWithCandidates([
    `/api/friendships/friends?count=${limit}&page=1`,
    `/api/friendships/friends?count=${limit}`,
  ]);
  return responseRows(raw)
    .map(normalizeFollowing)
    .filter((item): item is WeiboBootstrapItem => item !== null)
    .slice(0, limit);
}

async function fetchMentions(limit: number): Promise<WeiboBootstrapItem[]> {
  // H5 exposes @-mentions and comments as separate read-only feeds.  Keep
  // the legacy candidates as per-feed fallbacks for older deployments, but
  // merge both current feeds so a comment-only interaction is not lost.
  const raws = await Promise.all([
    fetchWithCandidates([
      `/message/mentionsAt?page=1`,
      `/api/statuses/mentions?count=${limit}&page=1`,
    ]),
    fetchWithCandidates([
      `/message/mentionsCmt?page=1`,
      `/api/comments/mentions?count=${limit}&page=1`,
      `/api/comments/to_me?count=${limit}&page=1`,
    ]),
  ]);
  return raws.flatMap(responseRows)
    .map((row) => normalizeStatus(row, "weibo_mentions"))
    .filter((item): item is WeiboBootstrapItem => item !== null)
    .slice(0, limit);
}

function dedupe(items: WeiboBootstrapItem[]): WeiboBootstrapItem[] {
  const seen = new Set<string>();
  return items.filter((item) => {
    const key = `${item.scope}:${item.content_id || item.url || item.title || ""}`;
    if (!key || seen.has(key)) return false;
    seen.add(key);
    return true;
  });
}

function countItems(items: WeiboBootstrapItem[]): Record<string, number> {
  const counts: Record<string, number> = {};
  for (const scope of ["weibo_favorites", "weibo_following", "weibo_mentions"]) counts[scope] = 0;
  for (const item of items) counts[item.scope] = (counts[item.scope] ?? 0) + 1;
  return counts;
}

export async function executeWeiboTask(msg: WeiboExecuteMessage): Promise<WeiboTaskResult> {
  const taskId = msg.task_id;
  const scopes = msg.scopes?.length ? msg.scopes : ["weibo_favorites", "weibo_following", "weibo_mentions"];
  const maxItems = Math.max(1, Math.min(500, Math.floor(msg.max_items_per_scope ?? 300)));
  const items: WeiboBootstrapItem[] = [];
  const debug: Record<string, unknown> = {};
  try {
    const identity = await fetchCurrentIdentity();
    debug.logged_in = identity.loggedIn;
    debug.user_id = identity.userId;
    if (!identity.loggedIn) {
      return { task_id: taskId, claim_token: msg.claim_token, status: "failed", items: [], scope_counts: countItems([]), error: "weibo_login_required", debug };
    }
    if (!identity.userId) {
      debug.identity_required = true;
      return {
        task_id: taskId,
        claim_token: msg.claim_token,
        status: "failed",
        items: [],
        scope_counts: countItems([]),
        error: "weibo_identity_required",
        debug,
      };
    }
    const scopeErrors: string[] = [];
    for (const scope of scopes) {
      try {
        const rows = scope === "weibo_favorites"
          ? await fetchFavorites(maxItems)
          : scope === "weibo_following"
            ? await fetchFollowing(maxItems)
            : await fetchMentions(maxItems);
        debug[scope] = rows.length;
        items.push(...rows);
      } catch (error) {
        const message = error instanceof Error ? error.message : String(error);
        debug[`${scope}_error`] = message;
        scopeErrors.push(`${scope}:${message}`);
      }
    }
    const normalized = dedupe(items);
    if (!normalized.length && scopeErrors.length) {
      return {
        task_id: taskId,
        claim_token: msg.claim_token,
        status: "failed",
        items: [],
        scope_counts: countItems([]),
        error: scopeErrors[0],
        debug,
      };
    }
    return {
      task_id: taskId,
      claim_token: msg.claim_token,
      status: scopeErrors.length ? "partial" : normalized.length ? "ok" : "empty",
      items: normalized,
      scope_counts: countItems(normalized),
      debug,
    };
  } catch (error) {
    if (error instanceof WeiboHttpError && error.status === 401) {
      debug.login_required = true;
      return { task_id: taskId, claim_token: msg.claim_token, status: "failed", items: [], scope_counts: countItems([]), error: "weibo_login_required", debug };
    }
    return {
      task_id: taskId,
      claim_token: msg.claim_token,
      status: "failed",
      items: [],
      scope_counts: countItems([]),
      error: error instanceof Error ? error.message : String(error),
      debug,
    };
  }
}

export function installWeiboMessageListener(): void {
  chrome.runtime.onMessage.addListener(
    (message: { action?: string; data?: WeiboExecuteMessage }, _sender, sendResponse) => {
      if (message.action !== "WEIBO_BOOTSTRAP_EXECUTE") return false;
      void executeWeiboTask(message.data as WeiboExecuteMessage).then((result) => {
        chrome.runtime.sendMessage({ action: "WEIBO_TASK_RESULT", data: result });
        sendResponse({ ok: true });
      });
      return true;
    },
  );
}
