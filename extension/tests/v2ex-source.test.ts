import test from "node:test";
import assert from "node:assert/strict";

import {
  v2exScopeUrl,
  isValidV2EXTask,
  pollV2EXTaskOnce,
  postV2EXTaskResult,
  v2exScopePageDecision,
  v2exTaskMessageSenderMatches,
  type V2EXTask,
} from "../src/background/v2ex-task-dispatcher.ts";
import {
  detectV2EXPageType,
  extractV2EXTopicNodeMetadata,
  extractV2EXContentId,
  v2exAdapter,
} from "../src/shared/platforms/v2ex.ts";
import {
  classifyV2EXScopePage,
  resolveV2EXReplyContext,
  v2exPagerHasNext,
  v2exScopeRouteMatches,
} from "../src/content/v2ex/task-executor.ts";
import { isV2EXTaskTabLocation } from "../src/content/v2ex/task-mode.ts";

test("V2EX task validation accepts the four read-only bootstrap scopes", () => {
  const task: V2EXTask = {
    id: "v2ex-init",
    type: "bootstrap_profile",
    scopes: ["public_topics", "public_replies", "favorite_topics", "favorite_nodes"],
    username: "alice",
    max_pages_per_scope: 4,
  };

  assert.equal(isValidV2EXTask(task), true);
  assert.equal(isValidV2EXTask({ id: "v2ex", type: "search" }), false);
  assert.equal(
    isValidV2EXTask({ id: "v2ex", type: "bootstrap_profile", scopes: ["writes"] }),
    false,
  );
  assert.equal(isValidV2EXTask({ id: "", type: "bootstrap_profile" }), false);
  assert.equal(
    isValidV2EXTask({
      id: "duplicate",
      type: "bootstrap_profile",
      scopes: ["public_topics", "public_topics"],
    }),
    false,
  );
});

test("V2EX result delivery retries one byte-identical payload until a 2xx ACK", async () => {
  const bodies: string[] = [];
  const delays: number[] = [];
  let attempt = 0;
  const payload = {
    task_id: "v2ex-result",
    status: "ok" as const,
    items: [{ scope: "public_topics" as const, topic_id: "42", title: "Agent" }],
    scope_counts: { public_topics: 1 },
  };

  await postV2EXTaskResult(payload, {
    resolveUrl: async (path) => `http://127.0.0.1:8420/api${path}`,
    fetch: async (_url, init) => {
      bodies.push(String(init.body));
      attempt += 1;
      return { ok: attempt === 3, status: attempt === 3 ? 200 : 503 };
    },
    sleep: async (delayMs) => {
      delays.push(delayMs);
    },
  }, { maxAttempts: 3, baseDelayMs: 10 });

  assert.equal(bodies.length, 3);
  assert.equal(new Set(bodies).size, 1);
  assert.equal(bodies[0], JSON.stringify(payload));
  assert.deepEqual(delays, [10, 20]);
});

test("V2EX polling checks recovery and the shared mutex before backend claim", async () => {
  const state = globalThis as unknown as {
    __OBC_DISPATCHER_MUTEX_HOLDER__?: string;
    __OBC_DISPATCHER_MUTEX_HELD_SINCE__?: number;
  };
  state.__OBC_DISPATCHER_MUTEX_HOLDER__ = "other-source";
  state.__OBC_DISPATCHER_MUTEX_HELD_SINCE__ = Date.now();
  const calls: string[] = [];
  try {
    await pollV2EXTaskOnce({
      ensureRecovery: async () => {
        calls.push("recovery");
      },
      canExecute: () => {
        calls.push("capability");
        return true;
      },
      fetchTask: async () => {
        calls.push("claim");
        return null;
      },
      execute: async () => "accepted",
      reportDeclined: async () => {},
    });
  } finally {
    state.__OBC_DISPATCHER_MUTEX_HOLDER__ = undefined;
    state.__OBC_DISPATCHER_MUTEX_HELD_SINCE__ = undefined;
  }
  assert.deepEqual(calls, ["recovery", "capability"]);
});

test("V2EX task URLs stay on public or login-gated read-only pages", () => {
  assert.equal(
    v2exScopeUrl("public_topics", "alice", 1),
    "https://www.v2ex.com/member/alice?p=1#openbiliclaw_v2ex_task=1",
  );
  assert.equal(
    v2exScopeUrl("public_replies", "alice", 2),
    "https://www.v2ex.com/member/alice/replies?p=2#openbiliclaw_v2ex_task=1",
  );
  assert.equal(
    v2exScopeUrl("favorite_topics", "alice", 3),
    "https://www.v2ex.com/my/topics?p=3#openbiliclaw_v2ex_task=1",
  );
  assert.equal(
    v2exScopeUrl("favorite_nodes", "alice", 1),
    "https://www.v2ex.com/my/nodes?p=1#openbiliclaw_v2ex_task=1",
  );
});

test("V2EX scope executor rejects redirects and cross-scope pages", () => {
  assert.equal(
    v2exScopeRouteMatches("public_topics", "https://www.v2ex.com/member/alice?p=2", "alice"),
    true,
  );
  assert.equal(
    v2exScopeRouteMatches("public_replies", "https://www.v2ex.com/member/alice/replies?p=2", "alice"),
    true,
  );
  assert.equal(
    v2exScopeRouteMatches("public_replies", "https://www.v2ex.com/", ""),
    true,
  );
  assert.equal(
    v2exScopeRouteMatches("favorite_topics", "https://www.v2ex.com/signin", "alice"),
    false,
  );
  assert.equal(
    v2exScopeRouteMatches("favorite_nodes", "https://example.com/my/nodes", "alice"),
    false,
  );
});

test("V2EX passive adapter maps identities without inferring upstream actions", () => {
  assert.equal(detectV2EXPageType("https://www.v2ex.com/t/123"), "topic");
  assert.equal(detectV2EXPageType("https://www.v2ex.com/go/programmer"), "node");
  assert.equal(detectV2EXPageType("https://www.v2ex.com/member/alice"), "profile");
  assert.equal(extractV2EXContentId("https://www.v2ex.com/t/123"), "123");
  assert.equal(extractV2EXContentId("https://www.v2ex.com/go/programmer"), null);
  assert.equal(
    v2exAdapter.inferActionType?.({ text: "收藏", ariaLabel: "", className: "", pressed: null }),
    null,
  );
  assert.equal(
    v2exAdapter.inferActionType?.({ text: "回复", ariaLabel: "", className: "", pressed: null }),
    null,
  );
  assert.deepEqual(
    v2exAdapter.buildEventMetadata?.("https://www.v2ex.com/t/123"),
    { topic_id: "123", content_id: "123", content_type: "topic" },
  );
});

test("V2EX topic Node metadata comes only from the matching rendered header", () => {
  const root = {
    querySelector: (_selector: string) => ({
      getAttribute: (name: string) => name === "href" ? "/go/Programmer" : null,
      textContent: "  程序员  ",
    }),
  };
  assert.deepEqual(
    extractV2EXTopicNodeMetadata(
      "https://www.v2ex.com/t/123",
      root,
      "https://www.v2ex.com/t/123",
    ),
    { node_name: "programmer", node_title: "程序员" },
  );
  assert.deepEqual(
    extractV2EXTopicNodeMetadata(
      "https://www.v2ex.com/t/123",
      root,
      "https://www.v2ex.com/t/999",
    ),
    {},
  );
  assert.deepEqual(
    extractV2EXTopicNodeMetadata(
      "https://www.v2ex.com/t/123",
      {
        querySelector: () => ({
          getAttribute: () => "https://example.com/go/programmer",
          textContent: "程序员",
        }),
      },
      "https://www.v2ex.com/t/123",
    ),
    {},
  );
});

test("V2EX task tabs are isolated from passive page collection", () => {
  assert.equal(isV2EXTaskTabLocation({ hash: "#openbiliclaw_v2ex_task=1" }), true);
  assert.equal(isV2EXTaskTabLocation({ search: "?openbiliclaw_v2ex_task=1" }), true);
  assert.equal(isV2EXTaskTabLocation({ hash: "#topic=1" }), false);
});

test("V2EX scope results require the exact marked task tab sender", () => {
  const taskUrl = "https://www.v2ex.com/member/alice#openbiliclaw_v2ex_task=1";
  assert.equal(
    v2exTaskMessageSenderMatches(42, { tab: { id: 42, url: taskUrl }, url: taskUrl }),
    true,
  );
  assert.equal(v2exTaskMessageSenderMatches(42, undefined), false);
  assert.equal(
    v2exTaskMessageSenderMatches(42, {
      tab: { id: 7, url: taskUrl },
      url: taskUrl,
    }),
    false,
  );
  assert.equal(
    v2exTaskMessageSenderMatches(42, {
      tab: { id: 42, url: "https://www.v2ex.com/member/alice" },
      url: "https://www.v2ex.com/member/alice",
    }),
    false,
  );
});

test("V2EX complete-snapshot evidence is conservative at pagination boundaries", () => {
  assert.deepEqual(
    v2exScopePageDecision("favorite_topics", 1, 20, "ok", 20),
    { continuePaging: true, complete: false, truncated: false },
  );
  assert.deepEqual(
    v2exScopePageDecision("favorite_topics", 2, 20, "empty", 0),
    { continuePaging: false, complete: true, truncated: false },
  );
  assert.deepEqual(
    v2exScopePageDecision("favorite_topics", 20, 20, "ok", 20),
    { continuePaging: false, complete: false, truncated: true },
  );
  assert.deepEqual(
    v2exScopePageDecision("favorite_nodes", 1, 20, "ok", 16),
    { continuePaging: false, complete: true, truncated: false },
  );
  assert.equal(
    v2exScopePageDecision("favorite_nodes", 1, 20, "failed", 0).complete,
    false,
  );
  assert.deepEqual(
    v2exScopePageDecision("favorite_nodes", 1, 20, "ok", 1000, true),
    { continuePaging: false, complete: false, truncated: true },
  );
  assert.deepEqual(
    v2exScopePageDecision("public_topics", 1, 20, "ok", 4, false, false),
    { continuePaging: false, complete: true, truncated: false },
  );
  assert.deepEqual(
    v2exScopePageDecision("public_replies", 1, 20, "ok", 19, false, true),
    { continuePaging: true, complete: false, truncated: false },
  );
});

test("V2EX DOM evidence never turns a generic #Main shell into valid empty", () => {
  const genericShell = {
    routeMatches: true,
    mainPresent: true,
    loginState: "logged_in" as const,
    challengePresent: false,
    hiddenPresent: false,
    scopeLayoutPresent: true,
    explicitEmptyPresent: false,
    itemCount: 0,
  };

  assert.deepEqual(classifyV2EXScopePage("favorite_topics", genericShell), {
    status: "parse_error",
    error: "empty_state_unproven",
    pageRecognized: false,
    affirmativeEmpty: false,
  });
  assert.deepEqual(classifyV2EXScopePage("favorite_topics", {
    ...genericShell,
    explicitEmptyPresent: true,
  }), {
    status: "empty",
    pageRecognized: true,
    affirmativeEmpty: true,
  });
});

test("V2EX DOM evidence keeps hidden, login, challenge, and parse failures distinct", () => {
  const base = {
    routeMatches: true,
    mainPresent: true,
    loginState: "logged_in" as const,
    challengePresent: false,
    hiddenPresent: false,
    scopeLayoutPresent: true,
    explicitEmptyPresent: false,
    itemCount: 0,
  };
  assert.equal(classifyV2EXScopePage("public_replies", {
    ...base,
    hiddenPresent: true,
  }).status, "hidden");
  assert.equal(classifyV2EXScopePage("favorite_nodes", {
    ...base,
    routeMatches: false,
    loginState: "logged_out",
  }).status, "login_required");
  assert.equal(classifyV2EXScopePage("favorite_topics", {
    ...base,
    loginState: "unknown",
  }).error, "login_state_unknown");
  assert.equal(classifyV2EXScopePage("public_topics", {
    ...base,
    challengePresent: true,
  }).status, "rate_limited");
  assert.equal(classifyV2EXScopePage("public_topics", {
    ...base,
    itemCount: 2,
  }).status, "ok");
});

test("V2EX pager stops when an out-of-range URL is clamped to the final rendered page", () => {
  assert.equal(v2exPagerHasNext(1, []), false);
  assert.equal(v2exPagerHasNext(1, [
    { page: 1, current: true, next: false },
    { page: 2, current: false, next: false },
  ]), true);
  assert.equal(v2exPagerHasNext(3, [
    { page: 1, current: false, next: false },
    { page: 2, current: true, next: false },
  ]), false);
});

test("V2EX replies bind to the adjacent dock metadata on the current member page", () => {
  const topicAnchor = {};
  const dockArea = {
    matches: (selector: string) => selector === ".dock_area",
    querySelector: (selector: string) => selector === "a[href*='/t/']" ? topicAnchor : null,
  };
  const inner = { previousElementSibling: dockArea };
  const currentReply = {
    closest: (selector: string) => {
      if (selector === ".cell") return null;
      if (selector === ".inner") return inner;
      return null;
    },
  };

  assert.equal(
    resolveV2EXReplyContext(currentReply as unknown as Element),
    dockArea,
  );

  const oldCell = {
    querySelector: (selector: string) => selector === "a[href*='/t/']" ? topicAnchor : null,
  };
  const legacyReply = {
    closest: (selector: string) => selector === ".cell" ? oldCell : null,
  };
  assert.equal(
    resolveV2EXReplyContext(legacyReply as unknown as Element),
    oldCell,
  );
});
