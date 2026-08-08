/**
 * Xiaohongshu task-page risk-control detection.
 *
 * Task tabs are allowed to inspect only the already-rendered, same-origin DOM.
 * We deliberately return a small stable reason instead of forwarding page text:
 * the latter may contain note content or account-adjacent data and is not needed
 * by the backend circuit breaker.
 */

export type XhsRiskControlReason =
  | "security_verification"
  | "frequent_operation";

export interface XhsRiskControlDetection {
  error: "xhs_rate_limited";
  reason: XhsRiskControlReason;
}

const LOGIN_REQUIRED_PATTERN = /登录后查看搜索结果|登录即可查看\s*(?:Ta 的)?笔记/i;
const LOGIN_REQUIRED_CONTROL_SELECTOR = [
  "[role='dialog']",
  "[role='dialog'] input[type='tel']",
  ".login-container",
  ".login-container input[type='tel']",
  ".login-modal",
  ".login-modal input[type='tel']",
  ".side-bar .side-bar-component.login-btn button.login-btn",
].join(", ");
const DIRECT_LOGIN_REQUIRED_CONTROL_SELECTOR = [
  "[role='dialog'] input[type='tel']",
  ".login-container input[type='tel']",
  ".login-modal input[type='tel']",
  ".side-bar .side-bar-component.login-btn button.login-btn",
].join(", ");

/** Detect the stable login-gate copy without returning any surrounding text. */
export function classifyXhsLoginRequiredText(value: unknown): boolean {
  const text = normalizeRiskText(value);
  return text ? LOGIN_REQUIRED_PATTERN.test(text) : false;
}

const STRONG_FREQUENCY_PATTERNS = [
  /请(?:勿|不要)频繁(?:操作|访问|请求)/i,
  /(?:操作|请求|访问)(?:过于|太)?频繁/i,
  /too many requests/i,
  /\b(?:http\s*)?429\b/i,
  /risk control/i,
];

const SECURITY_VERIFICATION_PATTERN =
  /安全验证|人机验证|异常访问|security verification/i;
const RETRY_PATTERN = /稍后(?:再试|重试)|请稍后|try again later|retry later/i;

function normalizeRiskText(value: unknown): string {
  return typeof value === "string"
    ? value.replace(/\s+/g, " ").trim().slice(0, 50_000)
    : "";
}

/** Classify text from a task-page dialog or page-level empty state. */
export function classifyXhsRiskControlText(
  value: unknown,
): XhsRiskControlDetection | null {
  const text = normalizeRiskText(value);
  if (!text) return null;

  if (STRONG_FREQUENCY_PATTERNS.some((pattern) => pattern.test(text))) {
    return {
      error: "xhs_rate_limited",
      reason: SECURITY_VERIFICATION_PATTERN.test(text)
        ? "security_verification"
        : "frequent_operation",
    };
  }
  if (
    SECURITY_VERIFICATION_PATTERN.test(text) &&
    RETRY_PATTERN.test(text)
  ) {
    return {
      error: "xhs_rate_limited",
      reason: "security_verification",
    };
  }
  return null;
}

const RISK_DIALOG_SELECTOR = [
  "[role='dialog']",
  ".reds-modal",
  ".modal",
  "[class*='security']",
  "[class*='verify']",
  "[class*='captcha']",
].join(", ");

function isEffectivelyVisible(element: HTMLElement, root: Document): boolean {
  let current: HTMLElement | null = element;
  while (current) {
    if (current.hidden || current.getAttribute("aria-hidden") === "true") {
      return false;
    }
    const style = root.defaultView?.getComputedStyle(current);
    if (
      style?.display === "none" ||
      style?.visibility === "hidden" ||
      style?.visibility === "collapse" ||
      style?.opacity === "0"
    ) {
      return false;
    }
    current = current.parentElement;
  }
  const rect = element.getBoundingClientRect();
  return rect.width > 0 && rect.height > 0;
}

/** Detect the visible XHS login gate that can coexist with a stale web_session cookie. */
export function detectXhsTaskLoginRequired(root: Document): boolean {
  const overlays = root.querySelectorAll<HTMLElement>(LOGIN_REQUIRED_CONTROL_SELECTOR);
  for (let index = 0; index < overlays.length; index += 1) {
    const overlay = overlays[index];
    if (!overlay || !isEffectivelyVisible(overlay, root)) continue;
    // Current XHS logged-out pages expose either the phone field inside the
    // login modal or one exact sidebar login control. Both are stronger than
    // a stale web_session cookie and avoid inspecting unrelated page text.
    if (overlay.matches(DIRECT_LOGIN_REQUIRED_CONTROL_SELECTOR)) return true;
    if (classifyXhsLoginRequiredText(overlay.innerText ?? overlay.textContent)) return true;
  }
  return false;
}

/**
 * Detect a visible task-page challenge.
 *
 * Dialog-like nodes are safe to inspect immediately. Whole-page text is only
 * inspected when the caller has already established that no note cards or
 * bootstrap content rendered, avoiding false positives from ordinary notes
 * discussing account verification.
 */
export function detectXhsTaskRiskControl(
  root: Document,
  options: { includePageText?: boolean } = {},
): XhsRiskControlDetection | null {
  const dialogs = root.querySelectorAll<HTMLElement>(RISK_DIALOG_SELECTOR);
  for (let index = 0; index < dialogs.length; index += 1) {
    const dialog = dialogs[index];
    if (!dialog) continue;
    if (!isEffectivelyVisible(dialog, root)) continue;
    const detection = classifyXhsRiskControlText(
      dialog.innerText ?? dialog.textContent,
    );
    if (detection) return detection;
  }

  if (!options.includePageText) return null;
  const pageText =
    (root.body
      ? (root.body.innerText ?? root.body.textContent)
      : root.documentElement?.textContent) ?? "";
  return classifyXhsRiskControlText(pageText);
}
