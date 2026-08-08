import type { NativeSaveTask } from "../../shared/native-save.ts";
import { waitForNativeSaveReadiness } from "./readiness.ts";

export interface XiaohongshuSaveControl {
  isSelected(): boolean;
  click(): void;
}

export type XiaohongshuFavoriteRequestResult =
  | "success"
  | "rejected"
  | "rate_limited"
  | null;

export interface XiaohongshuNativeSaveEnvironment {
  currentUrl: string;
  isLoggedIn(): boolean;
  isUnavailable(): boolean;
  isContentReady(): boolean;
  rateLimitFingerprint(): string;
  requestFavorite(): Promise<XiaohongshuFavoriteRequestResult>;
  findFavoriteControls(contentId: string): XiaohongshuSaveControl[];
  sleep(ms: number): Promise<void>;
}

const CONFIRM_ATTEMPTS = 20;
const CONFIRM_INTERVAL_MS = 100;
const XHS_CONTENT_IDENTITY_SELECTOR = "[data-note-id], [data-item-id], [data-content-id]";

function xiaohongshuRouteId(value: string): string | null {
  try {
    const url = new URL(value);
    if (url.protocol !== "https:" || url.username || url.password || url.port || url.hash) return null;
    if (url.hostname !== "xiaohongshu.com" && !url.hostname.endsWith(".xiaohongshu.com")) {
      return null;
    }
    return /^\/(?:explore|discovery\/item|search_result)\/([A-Za-z0-9_-]+)\/?$/
      .exec(url.pathname)?.[1] ?? null;
  } catch {
    return null;
  }
}

function hasNewRateLimit(before: string, after: string): boolean {
  const baseline = new Set(before.split("\n").filter(Boolean));
  return after.split("\n").filter(Boolean).some((entry) => !baseline.has(entry));
}

async function confirmSelected(
  task: NativeSaveTask,
  env: XiaohongshuNativeSaveEnvironment,
): Promise<boolean> {
  for (let attempt = 0; attempt < CONFIRM_ATTEMPTS; attempt += 1) {
    const controls = env.findFavoriteControls(task.content_id);
    if (controls.length === 1 && controls[0].isSelected()) return true;
    if (attempt + 1 < CONFIRM_ATTEMPTS) await env.sleep(CONFIRM_INTERVAL_MS);
  }
  return false;
}

function isSupported(task: NativeSaveTask, currentUrl: string): boolean {
  if (
    task.platform !== "xiaohongshu" ||
    task.platform_slug !== "xhs" ||
    task.item_key !== `xiaohongshu:${task.content_id}`
  ) return false;
  if (!["note", "video"].includes(task.content_type)) return false;
  if (!/^[A-Za-z0-9_-]+$/.test(task.content_id)) return false;
  return xiaohongshuRouteId(task.content_url) === task.content_id &&
    xiaohongshuRouteId(currentUrl) === task.content_id;
}

function hasTargetContract(task: NativeSaveTask): boolean {
  return task.target_label === "小红书收藏" && task.resolved_action === "favorite" &&
    (task.requested_action === "favorite" || task.requested_action === "watch_later");
}

export async function saveXiaohongshu(
  task: NativeSaveTask,
  env: XiaohongshuNativeSaveEnvironment = createXiaohongshuBrowserEnvironment(),
): Promise<unknown> {
  if (!isSupported(task, env.currentUrl) || env.isUnavailable()) {
    return { status: "unsupported", error_code: "unsupported_content_type" };
  }
  if (!hasTargetContract(task)) return { status: "failed", error_code: "native_save_failed" };
  const contentReady = await waitForNativeSaveReadiness(
    () => !env.isLoggedIn() || env.isUnavailable() || (
      env.isContentReady() && env.findFavoriteControls(task.content_id).length > 0
    ),
    env.sleep,
  );
  if (!contentReady) {
    return env.isContentReady()
      ? { status: "failed", error_code: "native_control_not_found" }
      : { status: "failed", error_code: "native_content_not_ready" };
  }
  if (!env.isLoggedIn()) return { status: "login_required" };
  if (env.isUnavailable()) return { status: "unsupported", error_code: "unsupported_content_type" };
  const initial = env.findFavoriteControls(task.content_id);
  if (initial.length !== 1) return { status: "failed", error_code: "native_control_not_found" };
  if (initial[0].isSelected()) return { status: "already_synced" };
  const rateLimitBefore = env.rateLimitFingerprint();

  let requestResult: XiaohongshuFavoriteRequestResult;
  try {
    requestResult = await env.requestFavorite();
  } catch {
    return { status: "failed", error_code: "native_save_failed" };
  }
  if (requestResult === "rate_limited") return { status: "rate_limited" };
  if (requestResult === "success") {
    if (await confirmSelected(task, env)) return { status: "synced" };
    return hasNewRateLimit(rateLimitBefore, env.rateLimitFingerprint())
      ? { status: "rate_limited" }
      : { status: "failed", error_code: "native_confirmation_not_observed" };
  }

  const controls = env.findFavoriteControls(task.content_id);
  if (controls.length !== 1) return { status: "failed", error_code: "native_save_failed" };
  if (controls[0].isSelected()) return { status: "synced" };
  try {
    controls[0].click();
  } catch {
    return { status: "failed", error_code: "native_save_failed" };
  }
  if (await confirmSelected(task, env)) return { status: "synced" };
  return hasNewRateLimit(rateLimitBefore, env.rateLimitFingerprint())
    ? { status: "rate_limited" }
    : { status: "failed", error_code: "native_confirmation_not_observed" };
}

/** Verify persisted favorite state after a reload without ever clicking the control. */
export async function verifyXiaohongshu(
  task: NativeSaveTask,
  env: XiaohongshuNativeSaveEnvironment = createXiaohongshuBrowserEnvironment(),
): Promise<unknown> {
  if (!isSupported(task, env.currentUrl) || env.isUnavailable()) {
    return { status: "unsupported", error_code: "unsupported_content_type" };
  }
  if (!hasTargetContract(task)) return { status: "failed", error_code: "native_save_failed" };
  const contentReady = await waitForNativeSaveReadiness(
    () => !env.isLoggedIn() || env.isUnavailable() || (
      env.isContentReady() && env.findFavoriteControls(task.content_id).length > 0
    ),
    env.sleep,
  );
  if (!contentReady) {
    return env.isContentReady()
      ? { status: "failed", error_code: "native_control_not_found" }
      : { status: "failed", error_code: "native_content_not_ready" };
  }
  if (!env.isLoggedIn()) return { status: "login_required" };
  if (env.isUnavailable()) return { status: "unsupported", error_code: "unsupported_content_type" };
  const controls = env.findFavoriteControls(task.content_id);
  if (controls.length !== 1) return { status: "failed", error_code: "native_control_not_found" };
  return controls[0].isSelected()
    ? { status: "already_synced" }
    : { status: "failed", error_code: "native_confirmation_not_observed" };
}

function isEffectivelyVisible(element: HTMLElement, root: Document): boolean {
  const view = root.defaultView ?? element.ownerDocument?.defaultView;
  let current: HTMLElement | null = element;
  while (current) {
    if (
      current.hidden || current.hasAttribute("hidden") || current.hasAttribute("inert") ||
      current.getAttribute("aria-hidden") === "true" || current.style.display === "none" ||
      current.style.visibility === "hidden"
    ) return false;
    if (view) {
      const style = view.getComputedStyle(current);
      if (style.display === "none" || style.visibility === "hidden") return false;
    }
    current = current.parentElement;
  }
  return true;
}

function selected(element: HTMLElement): boolean {
  if (element.getAttribute("aria-pressed") === "true" ||
    element.getAttribute("aria-checked") === "true" ||
    element.getAttribute("data-selected") === "true" ||
    /(?:已收藏|取消收藏)/.test(
      element.getAttribute("aria-label") ?? element.title ?? element.textContent ?? "",
    )) return true;
  const use = element.querySelectorAll<HTMLElement>("use")[0];
  const icon = use?.getAttribute("href") ?? use?.getAttribute("xlink:href") ?? "";
  return /(?:#|\/)(?:collected|collect(?:select|selected|active))$/i.test(icon) ||
    /(?:select|selected|active).*collect/i.test(icon);
}

function isExactFavoriteControl(element: HTMLElement): boolean {
  if (element.getAttribute("data-testid") === "collect-button") return true;
  if (
    element.getAttribute("id") === "note-page-collect-board-guide" &&
    /(?:^|\s)collect-wrapper(?:\s|$)/.test(element.getAttribute("class") ?? "")
  ) return true;
  const label = (
    element.getAttribute("aria-label") ?? element.title ?? element.textContent ?? ""
  ).trim();
  return ["收藏", "已收藏", "取消收藏"].includes(label);
}

function xiaohongshuContentContainer(
  root: Document,
  contentId: string,
  currentUrl: string,
): HTMLElement | null {
  const candidates = Array.from(root.querySelectorAll<HTMLElement>(
    XHS_CONTENT_IDENTITY_SELECTOR,
  )).filter((element) => {
    const ids = ["data-note-id", "data-item-id", "data-content-id"]
      .map((name) => element.getAttribute(name))
      .filter((value): value is string => value !== null);
    return ids.includes(contentId) && isEffectivelyVisible(element, root);
  });
  if (candidates.length > 0) return candidates.length === 1 ? candidates[0] : null;
  if (xiaohongshuRouteId(currentUrl) !== contentId) return null;
  const current = Array.from(root.querySelectorAll<HTMLElement>(
    "#noteContainer.note-container",
  )).filter((element) => isEffectivelyVisible(element, root));
  return current.length === 1 ? current[0] : null;
}

function isWithinContainer(element: HTMLElement, container: HTMLElement): boolean {
  let current: HTMLElement | null = element;
  while (current) {
    if (current === container) return true;
    current = current.parentElement;
  }
  return false;
}

function isBoundToContainer(element: HTMLElement, container: HTMLElement): boolean {
  const closestIdentity = element.closest<HTMLElement>(XHS_CONTENT_IDENTITY_SELECTOR);
  return closestIdentity !== null
    ? closestIdentity === container
    : isWithinContainer(element, container);
}

export function createXiaohongshuBrowserEnvironment(
  root: Document = document,
  currentUrl: string = location.href,
): XiaohongshuNativeSaveEnvironment {
  const rateElementIds = new WeakMap<Element, number>();
  let nextRateElementId = 1;
  return {
    currentUrl,
    isLoggedIn() {
      const overlays = root.querySelectorAll<HTMLElement>(
        "[role='dialog'] input[type='tel'], .login-container, .login-modal",
      );
      return !Array.from(overlays).some((element) => isEffectivelyVisible(element, root));
    },
    isUnavailable() {
      const errors = root.querySelectorAll<HTMLElement>(
        ".not-found, .error-page, [data-testid='not-found']",
      );
      return Array.from(errors).some((element) => isEffectivelyVisible(element, root));
    },
    isContentReady() {
      const contentId = xiaohongshuRouteId(currentUrl);
      return contentId !== null &&
        xiaohongshuContentContainer(root, contentId, currentUrl) !== null;
    },
    rateLimitFingerprint() {
      const contentId = xiaohongshuRouteId(currentUrl);
      if (!contentId) return "";
      const container = xiaohongshuContentContainer(root, contentId, currentUrl);
      if (!container) return "";
      return Array.from(container.querySelectorAll<HTMLElement>("[role='alert'], .reds-toast, .toast"))
        .filter((element) =>
          isBoundToContainer(element, container) &&
          isEffectivelyVisible(element, root)
        )
        .map((element) => {
          const text = element.textContent?.trim() ?? "";
          if (!/(?:操作频繁|请求频繁|稍后再试|risk control|too many requests|429)/i.test(text)) {
            return "";
          }
          let id = rateElementIds.get(element);
          if (id === undefined) {
            id = nextRateElementId;
            nextRateElementId += 1;
            rateElementIds.set(element, id);
          }
          return `${id}:${text}`;
        })
        .filter(Boolean)
        .join("\n");
    },
    async requestFavorite() {
      return null;
    },
    findFavoriteControls(contentId) {
      const pageId = xiaohongshuRouteId(currentUrl);
      if (pageId !== contentId) return [];
      const container = xiaohongshuContentContainer(root, contentId, currentUrl);
      if (!container) return [];
      return Array.from(container.querySelectorAll<HTMLElement>(
        "button[aria-label*='收藏'], [role='button'][aria-label*='收藏'], [data-testid='collect-button'], #note-page-collect-board-guide.collect-wrapper",
      )).filter((element) =>
        isBoundToContainer(element, container) &&
        isEffectivelyVisible(element, root) && isExactFavoriteControl(element)
      ).map((element) => ({
        isSelected: () => selected(element),
        click: () => element.click(),
      }));
    },
    sleep(ms) {
      return new Promise((resolve) => setTimeout(resolve, ms));
    },
  };
}
