import assert from "node:assert/strict";
import test from "node:test";

import {
  createXiaohongshuBrowserEnvironment,
  saveXiaohongshu,
  verifyXiaohongshu,
  type XiaohongshuFavoriteRequestResult,
  type XiaohongshuNativeSaveEnvironment,
  type XiaohongshuSaveControl,
} from "../src/content/native-save/xiaohongshu.ts";
import type { NativeSaveTask } from "../src/shared/native-save.ts";

const CONTENT_ID = "66aabbcc000000001e00dead";
const task: NativeSaveTask = {
  id: "123e4567-e89b-42d3-a456-426614174011",
  type: "native_save",
  platform: "xiaohongshu",
  platform_slug: "xhs",
  item_key: `xiaohongshu:${CONTENT_ID}`,
  content_id: CONTENT_ID,
  content_url: `https://www.xiaohongshu.com/explore/${CONTENT_ID}`,
  content_type: "note",
  requested_action: "favorite",
  resolved_action: "favorite",
  target_label: "小红书收藏",
};

function fixture(options: {
  currentUrl?: string;
  loggedIn?: boolean;
  unavailable?: boolean;
  initialSelected?: boolean;
  controls?: number;
  controlsAfterSleeps?: number;
  contentReady?: boolean;
  requestResult?: XiaohongshuFavoriteRequestResult;
  rejectRequest?: boolean;
  confirmAfterRequest?: boolean;
  confirmAfterClick?: boolean;
  rateBefore?: string;
  rateAfterMutation?: string;
} = {}): XiaohongshuNativeSaveEnvironment & { clicks: number; requests: number; sleeps: number } {
  let selected = options.initialSelected ?? false;
  let mutated = false;
  const control: XiaohongshuSaveControl = {
    isSelected: () => selected,
    click() {
      env.clicks += 1;
      mutated = true;
      if (options.confirmAfterClick ?? true) selected = true;
    },
  };
  const env = {
    clicks: 0,
    requests: 0,
    sleeps: 0,
    currentUrl: options.currentUrl ?? task.content_url,
    isLoggedIn: () => options.loggedIn ?? true,
    isUnavailable: () => options.unavailable ?? false,
    isContentReady: () => options.contentReady ??
      env.sleeps >= (options.controlsAfterSleeps ?? 0),
    rateLimitFingerprint: () => mutated ? (options.rateAfterMutation ?? options.rateBefore ?? "") : (options.rateBefore ?? ""),
    async requestFavorite() {
      env.requests += 1;
      if (options.rejectRequest) throw new Error("network outcome unknown");
      if (options.requestResult !== null && options.requestResult !== undefined) mutated = true;
      if (options.confirmAfterRequest) selected = true;
      return options.requestResult ?? null;
    },
    findFavoriteControls: () => Array.from({
      length: env.sleeps >= (options.controlsAfterSleeps ?? 0) ? (options.controls ?? 1) : 0,
    }, () => control),
    sleep: async () => { env.sleeps += 1; },
  } satisfies XiaohongshuNativeSaveEnvironment & { clicks: number; requests: number; sleeps: number };
  return env;
}

test("XHS native save accepts only canonical correlated note/video identities", async () => {
  for (const candidate of [
    task,
    { ...task, content_type: "video", content_url: `https://www.xiaohongshu.com/discovery/item/${CONTENT_ID}` },
    { ...task, content_url: `https://www.xiaohongshu.com/search_result/${CONTENT_ID}` },
  ]) {
    const env = fixture({ currentUrl: candidate.content_url });
    assert.deepEqual(await saveXiaohongshu(candidate, env), { status: "synced" });
  }
  for (const candidate of [
    { ...task, platform: "douyin" as const, platform_slug: "dy" as const },
    { ...task, item_key: "xiaohongshu:other" },
    { ...task, content_type: "profile", content_url: "https://www.xiaohongshu.com/user/profile/alice" },
    { ...task, content_type: "note", content_url: `https://www.xiaohongshu.com/explore/${CONTENT_ID}/extra` },
    { ...task, content_id: "other", item_key: "xiaohongshu:other" },
  ]) {
    const env = fixture({ currentUrl: candidate.content_url });
    assert.deepEqual(await saveXiaohongshu(candidate, env), {
      status: "unsupported",
      error_code: "unsupported_content_type",
    });
    assert.equal(env.clicks + env.requests, 0);
  }
  const wrongPage = fixture({ currentUrl: "https://www.xiaohongshu.com/explore/other" });
  assert.deepEqual(await saveXiaohongshu(task, wrongPage), {
    status: "unsupported",
    error_code: "unsupported_content_type",
  });
});

test("XHS native save waits for the correlated favorite control before mutation", async () => {
  const env = fixture({ controlsAfterSleeps: 2 });
  assert.deepEqual(await saveXiaohongshu(task, env), { status: "synced" });
  assert.ok(env.sleeps >= 2);
  assert.equal(env.clicks, 1);
});

test("XHS native save distinguishes content readiness from a missing exact control", async () => {
  const notReady = fixture({ contentReady: false });
  assert.deepEqual(await saveXiaohongshu(task, notReady), {
    status: "failed",
    error_code: "native_content_not_ready",
  });
  assert.equal(notReady.clicks + notReady.requests, 0);

  const missingControl = fixture({ contentReady: true, controls: 0 });
  assert.deepEqual(await saveXiaohongshu(task, missingControl), {
    status: "failed",
    error_code: "native_control_not_found",
  });
  assert.equal(missingControl.clicks + missingControl.requests, 0);
});

test("XHS native save checks login and deleted content before mutation", async () => {
  const loggedOut = fixture({ loggedIn: false, requestResult: "success", confirmAfterRequest: true });
  assert.deepEqual(await saveXiaohongshu(task, loggedOut), { status: "login_required" });
  assert.equal(loggedOut.clicks + loggedOut.requests, 0);
  const deleted = fixture({ unavailable: true, requestResult: "success", confirmAfterRequest: true });
  assert.deepEqual(await saveXiaohongshu(task, deleted), {
    status: "unsupported",
    error_code: "unsupported_content_type",
  });
  assert.equal(deleted.clicks + deleted.requests, 0);
});

test("XHS native save validates favorite and watch-later fallback target exactly", async () => {
  const watchLater = fixture();
  assert.deepEqual(await saveXiaohongshu({ ...task, requested_action: "watch_later" }, watchLater), {
    status: "synced",
  });
  for (const mismatch of [
    { ...task, target_label: "小红书点赞" },
    { ...task, requested_action: "watch_later" as const, resolved_action: "watch_later" as const },
    { ...task, requested_action: "favorite" as const, resolved_action: "watch_later" as const },
  ]) {
    const env = fixture();
    assert.deepEqual(await saveXiaohongshu(mismatch, env), {
      status: "failed",
      error_code: "native_save_failed",
    });
    assert.equal(env.clicks + env.requests, 0);
  }
});

test("XHS native save returns already_synced without a second mutation", async () => {
  const env = fixture({ initialSelected: true, requestResult: "success" });
  assert.deepEqual(await saveXiaohongshu(task, env), { status: "already_synced" });
  assert.equal(env.clicks + env.requests, 0);
});

test("XHS read-only verifier reports persisted selection without mutating", async () => {
  const selected = fixture({ initialSelected: true, requestResult: "success" });
  assert.deepEqual(await verifyXiaohongshu(task, selected), { status: "already_synced" });
  assert.equal(selected.clicks + selected.requests, 0);

  const unselected = fixture({ requestResult: "success" });
  assert.deepEqual(await verifyXiaohongshu(task, unselected), {
    status: "failed",
    error_code: "native_confirmation_not_observed",
  });
  assert.equal(unselected.clicks + unselected.requests, 0);
});

test("XHS native save handles stable request success, rejection fallback, 429, and uncertainty", async () => {
  const confirmed = fixture({ requestResult: "success", confirmAfterRequest: true });
  assert.deepEqual(await saveXiaohongshu(task, confirmed), { status: "synced" });
  assert.equal(confirmed.requests, 1);
  assert.equal(confirmed.clicks, 0);

  const rejected = fixture({ requestResult: "rejected", confirmAfterClick: true });
  assert.deepEqual(await saveXiaohongshu(task, rejected), { status: "synced" });
  assert.equal(rejected.requests, 1);
  assert.equal(rejected.clicks, 1);

  const concurrentlySelected = fixture({ requestResult: "rejected", confirmAfterRequest: true });
  assert.deepEqual(await saveXiaohongshu(task, concurrentlySelected), { status: "synced" });
  assert.equal(concurrentlySelected.requests, 1);
  assert.equal(concurrentlySelected.clicks, 0);

  const limited = fixture({ requestResult: "rate_limited" });
  assert.deepEqual(await saveXiaohongshu(task, limited), { status: "rate_limited" });
  assert.equal(limited.clicks, 0);

  const uncertain = fixture({ rejectRequest: true, confirmAfterClick: true });
  assert.deepEqual(await saveXiaohongshu(task, uncertain), {
    status: "failed",
    error_code: "native_save_failed",
  });
  assert.equal(uncertain.clicks, 0);
});

test("XHS native save never falls back after accepted-but-unconfirmed request", async () => {
  const env = fixture({ requestResult: "success", confirmAfterClick: true });
  assert.deepEqual(await saveXiaohongshu(task, env), {
    status: "failed",
    error_code: "native_confirmation_not_observed",
  });
  assert.equal(env.requests, 1);
  assert.equal(env.clicks, 0);
});

test("XHS native save requires selected state and does not accept count-only change", async () => {
  const env = fixture({ confirmAfterClick: false });
  assert.deepEqual(await saveXiaohongshu(task, env), {
    status: "failed",
    error_code: "native_confirmation_not_observed",
  });
  assert.equal(env.clicks, 1);
});

test("XHS native save ignores stale rate UI but correlates a new post-action risk control", async () => {
  const stale = fixture({ rateBefore: "old-toast", confirmAfterClick: true });
  assert.deepEqual(await saveXiaohongshu(task, stale), { status: "synced" });
  const fresh = fixture({ rateBefore: "", rateAfterMutation: "new-toast", confirmAfterClick: false });
  assert.deepEqual(await saveXiaohongshu(task, fresh), { status: "rate_limited" });
  for (const env of [
    fixture({ rateBefore: "old-toast", rateAfterMutation: "", confirmAfterClick: false }),
    fixture({ rateBefore: "1:first\n2:second", rateAfterMutation: "2:second\n1:first", confirmAfterClick: false }),
  ]) {
    assert.deepEqual(await saveXiaohongshu(task, env), {
      status: "failed",
      error_code: "native_confirmation_not_observed",
    });
  }
});

interface FakeElement {
  hidden: boolean;
  style: { display: string; visibility: string };
  title: string;
  textContent: string;
  parentElement: FakeElement | null;
  attributes: Map<string, string>;
  clicks: number;
  hasAttribute(name: string): boolean;
  getAttribute(name: string): string | null;
  querySelectorAll(selector: string): FakeElement[];
  closest(selector: string): FakeElement | null;
  click(): void;
}

function domElement(attributes: Record<string, string> = {}): FakeElement {
  const values = new Map(Object.entries(attributes));
  return {
    hidden: false,
    style: { display: "", visibility: "" },
    title: "",
    textContent: "",
    parentElement: null,
    attributes: values,
    clicks: 0,
    hasAttribute: (name) => values.has(name),
    getAttribute: (name) => values.get(name) ?? null,
    querySelectorAll: () => [],
    closest(selector) {
      let current: FakeElement | null = this;
      while (current) {
        if (
          selector.includes("data-note-id") && current.attributes.has("data-note-id") ||
          selector.includes("data-item-id") && current.attributes.has("data-item-id") ||
          selector.includes("data-content-id") && current.attributes.has("data-content-id")
        ) return current;
        current = current.parentElement;
      }
      return null;
    },
    click() { this.clicks += 1; values.set("aria-pressed", "true"); },
  };
}

function browserDocument(controls: FakeElement[], options: {
  login?: boolean;
  hiddenLogin?: boolean;
  unavailable?: boolean;
  hiddenUnavailable?: boolean;
  staleUnrelatedError?: boolean;
  unrelatedControls?: FakeElement[];
  nestedUnrelatedControls?: FakeElement[];
  targetRiskElements?: () => FakeElement[];
  nestedUnrelatedRiskElements?: () => FakeElement[];
  unrelatedRiskElements?: () => FakeElement[];
} = {}): Document {
  const login = domElement();
  if (options.hiddenLogin) login.style.display = "none";
  const unavailable = domElement();
  if (options.hiddenUnavailable) unavailable.hidden = true;
  const unrelated = domElement();
  unrelated.textContent = "操作频繁";
  const target = domElement({ "data-note-id": CONTENT_ID });
  const nestedUnrelatedContainer = domElement({ "data-note-id": "nested-unrelated-note" });
  nestedUnrelatedContainer.parentElement = target;
  for (const control of options.nestedUnrelatedControls ?? []) {
    control.parentElement ??= nestedUnrelatedContainer;
  }
  target.querySelectorAll = (selector) => {
    if (selector.includes("collect-button")) return [...controls, ...(options.nestedUnrelatedControls ?? [])];
    if (selector.includes("role='alert'")) {
      const targetRisk = options.targetRiskElements?.() ?? [];
      for (const element of targetRisk) element.parentElement ??= target;
      const nestedRisk = options.nestedUnrelatedRiskElements?.() ?? [];
      for (const element of nestedRisk) element.parentElement ??= nestedUnrelatedContainer;
      return [...targetRisk, ...nestedRisk];
    }
    return [];
  };
  for (const control of controls) control.parentElement ??= target;
  const unrelatedContainer = domElement({ "data-note-id": "unrelated-note" });
  unrelatedContainer.querySelectorAll = (selector) => selector.includes("collect-button")
    ? (options.unrelatedControls ?? [])
    : [];
  for (const control of options.unrelatedControls ?? []) control.parentElement ??= unrelatedContainer;
  return {
    defaultView: {
      getComputedStyle(element: FakeElement) {
        return element.style;
      },
    },
    querySelector(selector: string) {
      if ((options.login || options.hiddenLogin) && selector.includes("login-container")) return login;
      if ((options.unavailable || options.hiddenUnavailable) && selector.includes("not-found")) return unavailable;
      return null;
    },
    querySelectorAll(selector: string) {
      if (selector.includes("data-note-id")) return [target, unrelatedContainer];
      if (selector.includes("collect-button")) return [...controls, ...(options.unrelatedControls ?? [])];
      if (selector.includes("login-container")) return options.login || options.hiddenLogin ? [login] : [];
      if (selector.includes("not-found")) return options.unavailable || options.hiddenUnavailable ? [unavailable] : [];
      if (selector.includes("role='alert'")) {
        return [...(options.targetRiskElements?.() ?? []), ...(options.unrelatedRiskElements?.() ?? [])];
      }
      if (options.staleUnrelatedError && selector === "main p") return [unrelated];
      return [];
    },
  } as unknown as Document;
}

test("XHS browser environment excludes hidden controls and fails closed on ambiguous visible controls", async () => {
  const hidden = domElement({ "aria-label": "收藏" });
  hidden.style.display = "none";
  const exact = domElement({ "aria-label": "收藏" });
  const countOnly = domElement({ "aria-label": "收藏数 123" });
  const env = createXiaohongshuBrowserEnvironment(browserDocument([hidden, countOnly, exact]), task.content_url);
  env.sleep = async () => {};
  assert.deepEqual(await saveXiaohongshu(task, env), { status: "synced" });
  assert.equal(hidden.clicks, 0);
  assert.equal(countOnly.clicks, 0);
  assert.equal(exact.clicks, 1);

  const ambiguous = createXiaohongshuBrowserEnvironment(
    browserDocument([domElement({ "aria-label": "收藏" }), domElement({ "aria-label": "收藏" })]),
    task.content_url,
  );
  assert.deepEqual(await saveXiaohongshu(task, ambiguous), {
    status: "failed",
    error_code: "native_control_not_found",
  });
});

test("XHS browser environment supports the current noteContainer collect-wrapper markup", async () => {
  const container = domElement({ id: "noteContainer", class: "note-container" });
  const control = domElement({
    id: "note-page-collect-board-guide",
    class: "collect-wrapper",
  });
  const use = domElement({ href: "#collect" });
  control.parentElement = container;
  use.parentElement = control;
  control.querySelectorAll = (selector) => selector.includes("use") ? [use] : [];
  control.click = function click() {
    this.clicks += 1;
    use.attributes.set("href", "#collected");
  };
  container.querySelectorAll = (selector) =>
    selector.includes("note-page-collect-board-guide") ? [control] : [];
  const root = {
    defaultView: { getComputedStyle: (element: FakeElement) => element.style },
    querySelectorAll(selector: string) {
      if (selector.includes("data-note-id")) return [];
      if (selector.includes("#noteContainer.note-container")) return [container];
      if (selector.includes("login-container") || selector.includes("not-found")) return [];
      return [];
    },
  } as unknown as Document;
  const env = createXiaohongshuBrowserEnvironment(root, task.content_url);
  env.sleep = async () => {};
  assert.deepEqual(await saveXiaohongshu(task, env), { status: "synced" });
  assert.equal(control.clicks, 1);
});

test("XHS browser environment scopes mutation to the exact note and rejects hidden ancestors", async () => {
  const exact = domElement({ "aria-label": "收藏" });
  const hiddenAncestor = domElement();
  hiddenAncestor.hidden = true;
  const hiddenDescendant = domElement({ "aria-label": "收藏" });
  hiddenDescendant.parentElement = hiddenAncestor;
  const unrelated = domElement({ "aria-label": "收藏" });
  const env = createXiaohongshuBrowserEnvironment(
    browserDocument([hiddenDescendant, exact], { unrelatedControls: [unrelated] }),
    task.content_url,
  );
  env.sleep = async () => {};
  assert.deepEqual(await saveXiaohongshu(task, env), { status: "synced" });
  assert.equal(exact.clicks, 1);
  assert.equal(hiddenDescendant.clicks, 0);
  assert.equal(unrelated.clicks, 0);

  const soleUnrelated = domElement({ "aria-label": "收藏" });
  const failClosed = createXiaohongshuBrowserEnvironment(
    browserDocument([], { unrelatedControls: [soleUnrelated] }),
    task.content_url,
  );
  failClosed.sleep = async () => {};
  assert.deepEqual(await saveXiaohongshu(task, failClosed), {
    status: "failed",
    error_code: "native_control_not_found",
  });
  assert.equal(soleUnrelated.clicks, 0);

  const nestedUnrelated = domElement({ "aria-label": "收藏" });
  const nestedFailClosed = createXiaohongshuBrowserEnvironment(
    browserDocument([], { nestedUnrelatedControls: [nestedUnrelated] }),
    task.content_url,
  );
  nestedFailClosed.sleep = async () => {};
  assert.deepEqual(await saveXiaohongshu(task, nestedFailClosed), {
    status: "failed",
    error_code: "native_control_not_found",
  });
  assert.equal(nestedUnrelated.clicks, 0);
});

test("XHS browser environment detects login overlay and ignores unrelated prose errors", async () => {
  const control = domElement({ "aria-label": "收藏" });
  const loggedOut = createXiaohongshuBrowserEnvironment(browserDocument([control], { login: true }), task.content_url);
  assert.deepEqual(await saveXiaohongshu(task, loggedOut), { status: "login_required" });
  assert.equal(control.clicks, 0);

  const unrelatedControl = domElement({ "aria-label": "收藏" });
  const unrelated = createXiaohongshuBrowserEnvironment(
    browserDocument([unrelatedControl], { staleUnrelatedError: true }),
    task.content_url,
  );
  unrelated.sleep = async () => {};
  assert.deepEqual(await saveXiaohongshu(task, unrelated), { status: "synced" });
});

test("XHS browser environment ignores hidden login and unavailable SPA templates", async () => {
  for (const options of [{ hiddenLogin: true }, { hiddenUnavailable: true }]) {
    const control = domElement({ "aria-label": "收藏" });
    const env = createXiaohongshuBrowserEnvironment(browserDocument([control], options), task.content_url);
    env.sleep = async () => {};
    assert.deepEqual(await saveXiaohongshu(task, env), { status: "synced" });
    assert.equal(control.clicks, 1);
  }

  const unavailableControl = domElement({ "aria-label": "收藏" });
  const unavailable = createXiaohongshuBrowserEnvironment(
    browserDocument([unavailableControl], { unavailable: true }),
    task.content_url,
  );
  assert.deepEqual(await saveXiaohongshu(task, unavailable), {
    status: "unsupported",
    error_code: "unsupported_content_type",
  });
  assert.equal(unavailableControl.clicks, 0);
});

test("XHS browser environment fingerprints only new target-local risk events", async () => {
  let unrelatedAppeared = false;
  const unrelatedRisk = domElement();
  unrelatedRisk.textContent = "操作频繁";
  const unconfirmed = domElement({ "aria-label": "收藏" });
  unconfirmed.click = function click() { this.clicks += 1; unrelatedAppeared = true; };
  const unrelatedEnv = createXiaohongshuBrowserEnvironment(browserDocument([unconfirmed], {
    unrelatedRiskElements: () => unrelatedAppeared ? [unrelatedRisk] : [],
  }), task.content_url);
  unrelatedEnv.sleep = async () => {};
  assert.deepEqual(await saveXiaohongshu(task, unrelatedEnv), {
    status: "failed",
    error_code: "native_confirmation_not_observed",
  });

  let nestedAppeared = false;
  const nestedRisk = domElement();
  nestedRisk.textContent = "操作频繁";
  const nestedUnconfirmed = domElement({ "aria-label": "收藏" });
  nestedUnconfirmed.click = function click() { this.clicks += 1; nestedAppeared = true; };
  const nestedEnv = createXiaohongshuBrowserEnvironment(browserDocument([nestedUnconfirmed], {
    nestedUnrelatedRiskElements: () => nestedAppeared ? [nestedRisk] : [],
  }), task.content_url);
  nestedEnv.sleep = async () => {};
  assert.deepEqual(await saveXiaohongshu(task, nestedEnv), {
    status: "failed",
    error_code: "native_confirmation_not_observed",
  });

  let replaced = false;
  const staleRisk = domElement();
  staleRisk.textContent = "操作频繁";
  const freshRisk = domElement();
  freshRisk.textContent = "操作频繁";
  const targetUnconfirmed = domElement({ "aria-label": "收藏" });
  targetUnconfirmed.click = function click() { this.clicks += 1; replaced = true; };
  const targetEnv = createXiaohongshuBrowserEnvironment(browserDocument([targetUnconfirmed], {
    targetRiskElements: () => replaced ? [freshRisk] : [staleRisk],
  }), task.content_url);
  targetEnv.sleep = async () => {};
  assert.deepEqual(await saveXiaohongshu(task, targetEnv), { status: "rate_limited" });
});
