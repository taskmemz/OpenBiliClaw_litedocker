import test from "node:test";
import assert from "node:assert/strict";

import {
  buildLinuxdoTaskUrl,
  collectLinuxdoTopicItems,
  executeLinuxdoTask,
  installLinuxdoMessageListener,
  linuxdoTopicIdFromUrl,
  normalizeLinuxdoTopic,
} from "../src/content/linuxdo/task-executor.ts";

function jsonResponse(value: unknown, status = 200): Response {
  return new Response(JSON.stringify(value), {
    status,
    headers: { "content-type": "application/json" },
  });
}

test("normalizeLinuxdoTopic maps topic identity, author, category, tags and engagement", () => {
  const raw = {
    users: [{ id: 7, username: "alice" }],
    categories: [{ id: 4, name: "开发调优" }],
    topic_list: {
      topics: [{
        id: 42,
        slug: "local-first-agent",
        title: "<b>Local-first</b> Agent",
        excerpt: "<p>Practical &amp; private.</p>",
        posters: [{ user_id: 7, description: "Original Poster" }],
        category_id: 4,
        tags: ["AI", { name: "agent" }, { nested: "ignored" }],
        views: 120,
        like_count: 9,
        posts_count: 6,
        created_at: "2026-08-09T01:02:03Z",
      }],
    },
  };

  const items = collectLinuxdoTopicItems(raw, {
    scope: "linuxdo_feed",
    strategy: "linuxdo-feed",
  });
  assert.deepEqual(items, [{
    scope: "linuxdo_feed",
    content_type: "post",
    topic_id: "42",
    content_id: "topic:42",
    title: "Local-first Agent",
    url: "https://linux.do/t/local-first-agent/42",
    author: "alice",
    author_url: "https://linux.do/u/alice/activity/topics",
    summary: "Practical & private.",
    category: "开发调优",
    tags: ["AI", "agent"],
    engagement_available: ["view", "like", "comment"],
    views: 120,
    like_count: 9,
    reply_count: 5,
    published_at: "2026-08-09T01:02:03Z",
    source_strategy: "linuxdo-feed",
  }]);
});

test("normalizer rejects unstable identity and never stringifies nested fields", () => {
  assert.equal(
    normalizeLinuxdoTopic(
      { id: { nested: 42 }, title: ["not", "a", "title"], author: { username: "bad" } },
      { scope: "linuxdo_feed", strategy: "linuxdo-feed" },
    ),
    null,
  );
  const item = normalizeLinuxdoTopic(
    { id: 43, title: "合法短名", username: "!", tags: "not-an-array" },
    { scope: "linuxdo_feed", strategy: "linuxdo-feed" },
  );
  assert.equal(item?.author, "!");
  assert.deepEqual(item?.tags, []);
  assert.equal(
    normalizeLinuxdoTopic(
      {
        id: 999,
        bookmarkable_type: "Post",
        bookmarkable_id: 123,
        title: "post bookmark without topic identity",
      },
      {
        scope: "linuxdo_bookmarks",
        strategy: "linuxdo-bootstrap-bookmarks",
        interactionAction: "favorite",
      },
    ),
    null,
  );
});

test("200 wrong-shape envelopes fail instead of becoming true empty", async () => {
  const originalFetch = globalThis.fetch;
  try {
    for (const payload of [{}, [], { error: "upstream changed" }]) {
      globalThis.fetch = (async () => jsonResponse(payload)) as typeof fetch;
      const result = await executeLinuxdoTask({
        task_id: "bad-envelope",
        type: "feed",
        max_items: 1,
      });
      assert.equal(result.status, "failed");
      assert.equal(result.error, "linuxdo_invalid_envelope");
      assert.notEqual(result.response_observed, true);
    }

    globalThis.fetch = (async () =>
      jsonResponse({ topic_list: { topics: [] } })) as typeof fetch;
    const empty = await executeLinuxdoTask({
      task_id: "affirmative-empty",
      type: "feed",
      max_items: 1,
    });
    assert.equal(empty.status, "empty");
    assert.equal(empty.response_observed, true);
    assert.deepEqual(empty.complete_scopes, ["linuxdo_feed"]);

    globalThis.fetch = (async () => new Response(
      JSON.stringify({ topic_list: { topics: [] } }),
      { status: 200 },
    )) as typeof fetch;
    const missingContentType = await executeLinuxdoTask({
      task_id: "missing-content-type",
      type: "feed",
      max_items: 1,
    });
    assert.equal(missingContentType.status, "failed");
    assert.equal(missingContentType.error, "linuxdo_invalid_response");
  } finally {
    globalThis.fetch = originalFetch;
  }
});

test("bootstrap action time is not mislabeled as topic publication time", () => {
  const item = normalizeLinuxdoTopic(
    {
      bookmarkable_type: "Topic",
      bookmarkable_id: 44,
      title: "Saved",
      created_at: "2026-08-09T10:00:00Z",
    },
    {
      scope: "linuxdo_bookmarks",
      strategy: "linuxdo-bootstrap-bookmarks",
      interactionAction: "favorite",
    },
  );
  assert.equal(item?.interaction_time, "2026-08-09T10:00:00Z");
  assert.equal(item?.published_at, undefined);
  const liked = normalizeLinuxdoTopic(
    {
      topic_id: 45,
      title: "Liked reply",
      post_created_at: "2026-08-09T11:00:00Z",
      created_at: "2026-08-09T12:00:00Z",
    },
    {
      scope: "linuxdo_likes",
      strategy: "linuxdo-bootstrap-likes",
      interactionAction: "like",
    },
  );
  assert.equal(liked?.published_at, undefined);
  assert.equal(liked?.interaction_time, "2026-08-09T12:00:00Z");
});

test("Linux.do URL builders only produce same-origin read endpoints", () => {
  assert.equal(
    buildLinuxdoTaskUrl("search", "local agent", 2),
    "https://linux.do/search.json?q=local+agent&page=2",
  );
  assert.equal(buildLinuxdoTaskUrl("feed", "", 1), "https://linux.do/latest.json?page=1");
  assert.equal(
    buildLinuxdoTaskUrl("creator", "https://linux.do/u/alice/activity/topics", 0),
    "https://linux.do/topics/created-by/alice.json?page=0",
  );
  assert.equal(
    buildLinuxdoTaskUrl("related", "https://linux.do/t/local-first/42", 0),
    "https://linux.do/t/42.json",
  );
  assert.equal(linuxdoTopicIdFromUrl("https://evil.example/t/topic/42"), "");
  assert.equal(linuxdoTopicIdFromUrl("https://linux.do/t/topic/0"), "");
  assert.equal(linuxdoTopicIdFromUrl("https://linux.do/t/42/7"), "42");
  assert.throws(() => buildLinuxdoTaskUrl("related", "https://evil.example/t/topic/42"));
});

test("related task uses similar_to and reads the official nested topic author shape", async () => {
  const originalFetch = globalThis.fetch;
  const calls: string[] = [];
  globalThis.fetch = (async (input: string | URL | Request) => {
    const url = String(input);
    calls.push(url);
    if (url.includes("/t/42.json")) {
      return jsonResponse({
        id: 42,
        title: "How to tune a local inference runtime",
        post_stream: { posts: [{ cooked: "<p>KV cache and quantization details.</p>" }] },
      });
    }
    return jsonResponse({
      similar_topics: [
        { id: 42, title: "Seed must be removed" },
        {
          id: 77,
          title: "Similar topic",
          slug: "similar-topic",
          posters: [{
            description: "Original Poster",
            user: { username: "nested-author" },
          }],
        },
      ],
    });
  }) as typeof fetch;
  try {
    const result = await executeLinuxdoTask({
      task_id: "related-1",
      type: "related",
      related_urls: ["https://linux.do/t/seed/42"],
      max_items_per_seed: 3,
    });
    assert.equal(result.status, "ok");
    assert.equal(result.items[0]?.author, "nested-author");
    assert.equal(
      result.items[0]?.author_url,
      "https://linux.do/u/nested-author/activity/topics",
    );
    assert.deepEqual(result.items.map((item) => item.topic_id), ["77"]);
    assert.equal(result.items[0]?.source_input, "https://linux.do/t/seed/42");
    assert.ok(calls[1]?.includes("/topics/similar_to.json?"));
    assert.ok(calls[1]?.includes("title=How+to+tune+a+local+inference+runtime"));
    assert.ok(calls[1]?.includes("raw=KV+cache+and+quantization+details."));
  } finally {
    globalThis.fetch = originalFetch;
  }
});

test("related task preserves a valid seed when an earlier seed is 404", async () => {
  const originalFetch = globalThis.fetch;
  globalThis.fetch = (async (input: string | URL | Request) => {
    const url = String(input);
    if (url.includes("/t/404.json")) return jsonResponse({ error: "missing" }, 404);
    if (url.includes("/t/42.json")) {
      return jsonResponse({ title: "Local vector database design" });
    }
    return jsonResponse({
      similar_topics: [{ id: 78, title: "Vector index tradeoffs" }],
    });
  }) as typeof fetch;
  try {
    const result = await executeLinuxdoTask({
      task_id: "related-partial",
      type: "related",
      related_urls: ["https://linux.do/t/missing/404", "https://linux.do/t/valid/42"],
      max_items_per_seed: 3,
    });

    assert.equal(result.status, "degraded");
    assert.deepEqual(result.items.map((item) => item.topic_id), ["78"]);
    assert.equal(result.items[0]?.source_input, "https://linux.do/t/valid/42");
    assert.deepEqual(result.debug, {
      input_errors: {
        "related:https://linux.do/t/missing/404": "linuxdo_http_error",
      },
    });
  } finally {
    globalThis.fetch = originalFetch;
  }
});

test("search task uses browser credentials, paginates and preserves keyword ownership", async () => {
  const originalFetch = globalThis.fetch;
  const calls: Array<{ url: string; credentials?: RequestCredentials }> = [];
  globalThis.fetch = (async (input: string | URL | Request, init?: RequestInit) => {
    const url = String(input);
    calls.push({ url, credentials: init?.credentials });
    if (url.includes("page=1")) {
      return jsonResponse({
        users: [
          { id: 2, username: "op-bob" },
          { id: 22, username: "reply-bob" },
        ],
        topics: [{
          id: 2,
          title: "Second",
          slug: "second",
          posters: [{ user_id: 2, description: "Original Poster" }],
          like_count: 8,
          created_at: "2026-08-02T00:00:00Z",
        }],
        posts: [{
          id: 22,
          topic_id: 2,
          username: "reply-bob",
          blurb: "second excerpt",
          like_count: 3,
          created_at: "2026-08-08T00:00:00Z",
        }],
      });
    }
    return jsonResponse({
      users: [
        { id: 1, username: "op-alice" },
        { id: 11, username: "reply-alice" },
      ],
      topics: [{
        id: 1,
        title: "First",
        slug: "first",
        posters: [{ user_id: 1, description: "Original Poster" }],
        like_count: 9,
        created_at: "2026-08-01T00:00:00Z",
      }],
      posts: [{
        id: 11,
        topic_id: 1,
        username: "reply-alice",
        blurb: "first excerpt",
        like_count: 2,
        created_at: "2026-08-09T00:00:00Z",
      }],
      grouped_search_result: { more_full_page_results: true },
    });
  }) as typeof fetch;
  try {
    const result = await executeLinuxdoTask({
      task_id: "search-1",
      type: "search",
      keywords: ["agent"],
      source_keyword_ids: { agent: 77 },
      max_items_per_keyword: 2,
      max_pages: 2,
    });
    assert.equal(result.status, "ok");
    assert.deepEqual(result.items.map((item) => item.topic_id), ["1", "2"]);
    assert.deepEqual(result.items.map((item) => item.author), ["op-alice", "op-bob"]);
    assert.deepEqual(result.items.map((item) => item.summary), ["first excerpt", "second excerpt"]);
    assert.deepEqual(result.items.map((item) => item.like_count), [9, 8]);
    assert.deepEqual(result.items.map((item) => item.published_at), [
      "2026-08-01T00:00:00Z",
      "2026-08-02T00:00:00Z",
    ]);
    assert.ok(result.items.every((item) => item.source_keyword_id === 77));
    assert.ok(calls.every((call) => call.url.startsWith("https://linux.do/")));
    assert.ok(calls.every((call) => call.credentials === "include"));
  } finally {
    globalThis.fetch = originalFetch;
  }
});

test("formal search hydrates missing topic-owned author and engagement", async () => {
  const originalFetch = globalThis.fetch;
  const calls: string[] = [];
  globalThis.fetch = (async (input: string | URL | Request) => {
    const url = String(input);
    calls.push(url);
    if (url.includes("/t/91.json")) {
      return jsonResponse({
        id: 91,
        title: "Hydrated topic",
        details: { created_by: { username: "topic-owner" } },
        views: 456,
        like_count: 23,
        reply_count: 7,
        created_at: "2026-08-01T00:00:00Z",
      });
    }
    return jsonResponse({
      topics: [{ id: 91, title: "Hydrated topic", created_at: "2026-08-01T00:00:00Z" }],
      posts: [{ topic_id: 91, username: "matching-reply", blurb: "match", like_count: 99 }],
    });
  }) as typeof fetch;
  try {
    const result = await executeLinuxdoTask({
      task_id: "search-hydrate",
      type: "search",
      keywords: ["topic"],
      max_items_per_keyword: 5,
      max_items: 1,
      hydrate_topic_details: true,
    });
    assert.equal(result.status, "ok");
    assert.deepEqual(
      {
        author: result.items[0]?.author,
        views: result.items[0]?.views,
        likes: result.items[0]?.like_count,
        replies: result.items[0]?.reply_count,
      },
      { author: "topic-owner", views: 456, likes: 23, replies: 7 },
    );
    assert.equal(result.items[0]?.summary, "match");
    assert.equal(calls.filter((url) => url.includes("/t/91.json")).length, 1);
  } finally {
    globalThis.fetch = originalFetch;
  }
});

test("formal related hydrates each retained candidate from topic detail", async () => {
  const originalFetch = globalThis.fetch;
  globalThis.fetch = (async (input: string | URL | Request) => {
    const url = String(input);
    if (url.includes("/t/42.json")) return jsonResponse({ title: "Seed topic" });
    if (url.includes("/topics/similar_to.json")) {
      return jsonResponse({ similar_topics: [{ id: 92, title: "Related candidate" }] });
    }
    return jsonResponse({
      id: 92,
      title: "Related candidate",
      post_stream: { posts: [{ username: "related-owner" }] },
      views: 789,
      like_count: 31,
      posts_count: 10,
    });
  }) as typeof fetch;
  try {
    const result = await executeLinuxdoTask({
      task_id: "related-hydrate",
      type: "related",
      related_urls: ["https://linux.do/t/seed/42"],
      max_items_per_seed: 5,
      max_items: 1,
      hydrate_topic_details: true,
    });
    assert.equal(result.status, "ok");
    assert.deepEqual(
      {
        author: result.items[0]?.author,
        views: result.items[0]?.views,
        likes: result.items[0]?.like_count,
        replies: result.items[0]?.reply_count,
      },
      { author: "related-owner", views: 789, likes: 31, replies: 9 },
    );
  } finally {
    globalThis.fetch = originalFetch;
  }
});

test("formal feed cursor resumes inside a page and returns the next offset", async () => {
  const originalFetch = globalThis.fetch;
  const calls: string[] = [];
  globalThis.fetch = (async (input: string | URL | Request) => {
    calls.push(String(input));
    return jsonResponse({
      topic_list: {
        topics: [
          { id: 1, title: "Already consumed" },
          { id: 2, title: "Resume here" },
          { id: 3, title: "Next run" },
        ],
      },
    });
  }) as typeof fetch;
  try {
    const result = await executeLinuxdoTask({
      task_id: "feed-cursor-resume",
      type: "feed",
      max_items: 1,
      cursor_contract: "page-offset-v1",
      start_cursors: { default: { page: 0, offset: 1 } },
    });

    assert.equal(result.status, "ok");
    assert.deepEqual(result.items.map((item) => item.topic_id), ["2"]);
    assert.deepEqual(result.next_cursors, { default: { page: 0, offset: 2 } });
    assert.equal(calls.length, 1);
  } finally {
    globalThis.fetch = originalFetch;
  }
});

test("formal feed cursor performs one bounded reset when its tail offset is invalid", async () => {
  const originalFetch = globalThis.fetch;
  const calls: string[] = [];
  globalThis.fetch = (async (input: string | URL | Request) => {
    calls.push(String(input));
    return jsonResponse({
      topic_list: {
        topics: [
          { id: 10, title: "New cycle first" },
          { id: 11, title: "New cycle second" },
        ],
      },
    });
  }) as typeof fetch;
  try {
    const result = await executeLinuxdoTask({
      task_id: "feed-cursor-reset",
      type: "feed",
      max_items: 1,
      cursor_contract: "page-offset-v1",
      start_cursors: { default: { page: 0, offset: 2 } },
    });

    assert.equal(result.status, "ok");
    assert.deepEqual(result.items.map((item) => item.topic_id), ["10"]);
    assert.deepEqual(result.next_cursors, { default: { page: 0, offset: 1 } });
    assert.equal(calls.length, 2);
  } finally {
    globalThis.fetch = originalFetch;
  }
});

test("message listener coalesces a recovered replay of the same task", async () => {
  const originalChrome = (globalThis as { chrome?: unknown }).chrome;
  const originalFetch = globalThis.fetch;
  const globalState = globalThis as unknown as Record<string, unknown>;
  delete globalState.__OPENBILICLAW_LINUXDO_TASK_LISTENER__;
  delete globalState.__OPENBILICLAW_LINUXDO_TASK_EXECUTION__;
  let listener:
    | ((message: unknown, sender: unknown, sendResponse: (response: unknown) => void) => boolean)
    | null = null;
  const sentMessages: unknown[] = [];
  let upstreamRequests = 0;
  (globalThis as { chrome?: unknown }).chrome = {
    runtime: {
      onMessage: {
        addListener(callback: typeof listener) {
          listener = callback;
        },
      },
      async sendMessage(message: unknown) {
        sentMessages.push(message);
        return { ok: true };
      },
    },
  };
  globalThis.fetch = (async () => {
    upstreamRequests += 1;
    await new Promise((resolve) => setTimeout(resolve, 5));
    return jsonResponse({ topic_list: { topics: [{ id: 88, title: "Recovered" }] } });
  }) as typeof fetch;
  try {
    installLinuxdoMessageListener();
    assert.ok(listener);
    const responses: unknown[] = [];
    const message = {
      action: "LINUXDO_TASK_EXECUTE",
      data: {
        task_id: "recovered-task",
        claim_token: "recovered-claim",
        type: "feed",
        max_items: 1,
      },
    };
    assert.equal(listener(message, {}, (value) => responses.push(value)), true);
    assert.equal(listener(message, {}, (value) => responses.push(value)), true);
    await new Promise((resolve) => setTimeout(resolve, 30));
    assert.equal(upstreamRequests, 1);
    assert.equal(sentMessages.length, 1);
    assert.deepEqual(responses, [{ ok: true }, { ok: true }]);
  } finally {
    (globalThis as { chrome?: unknown }).chrome = originalChrome;
    globalThis.fetch = originalFetch;
    delete globalState.__OPENBILICLAW_LINUXDO_TASK_LISTENER__;
    delete globalState.__OPENBILICLAW_LINUXDO_TASK_EXECUTION__;
  }
});

test("hot task falls back to weekly top only when hot endpoint is absent", async () => {
  const originalFetch = globalThis.fetch;
  const calls: string[] = [];
  globalThis.fetch = (async (input: string | URL | Request) => {
    const url = String(input);
    calls.push(url);
    if (url.includes("/hot.json")) return jsonResponse({ error: "missing" }, 404);
    return jsonResponse({ topic_list: { topics: [{ id: 8, title: "Top topic", slug: "top" }] } });
  }) as typeof fetch;
  try {
    const result = await executeLinuxdoTask({ task_id: "hot-1", type: "hot", max_items: 1 });
    assert.equal(result.status, "ok");
    assert.equal(result.items[0]?.source_strategy, "linuxdo-hot");
    assert.ok(calls.some((url) => url.includes("/top.json?period=weekly")));
  } finally {
    globalThis.fetch = originalFetch;
  }
});

test("bootstrap positively resolves session identity before three personal scopes", async () => {
  const originalFetch = globalThis.fetch;
  const calls: string[] = [];
  globalThis.fetch = (async (input: string | URL | Request) => {
    const url = String(input);
    calls.push(url);
    if (url.includes("/session/current.json")) {
      return jsonResponse({ current_user: { username: "alice" } });
    }
    if (url.includes("/bookmarks.json")) {
      return jsonResponse({
        user_bookmark_list: {
          bookmarks: [{
            bookmarkable_type: "Topic",
            bookmarkable_id: 11,
            title: "Saved topic",
            slug: "saved-topic",
            user: { username: "saved-author" },
            created_at: "2026-08-08T00:00:00Z",
          }],
        },
      });
    }
    if (url.includes("/user_actions.json")) {
      return jsonResponse({
        user_actions: [{ topic_id: 12, title: "Liked topic", slug: "liked-topic" }],
      });
    }
    return jsonResponse({
      topic_list: { topics: [{ id: 13, title: "Read topic", slug: "read-topic" }] },
    });
  }) as typeof fetch;
  try {
    const result = await executeLinuxdoTask({
      task_id: "bootstrap-1",
      type: "bootstrap_events",
      max_items_per_scope: 3,
    });
    assert.equal(result.status, "ok");
    assert.deepEqual(result.scope_counts, {
      linuxdo_bookmarks: 1,
      linuxdo_likes: 1,
      linuxdo_read_history: 1,
    });
    assert.match(result.account_key ?? "", /^sha256:[0-9a-f]{64}$/);
    assert.equal(JSON.stringify(result).includes("alice"), false);
    assert.equal(result.response_observed, true);
    assert.deepEqual(result.complete_scopes, [
      "linuxdo_bookmarks",
      "linuxdo_likes",
      "linuxdo_read_history",
    ]);
    assert.deepEqual(result.items.map((item) => item.interaction_action), ["favorite", "like", "view"]);
    assert.equal(result.items[0]?.author, "saved-author");
    assert.ok(calls[0]?.includes("/session/current.json"));
    assert.ok(calls.some((url) => url.includes("/u/alice/bookmarks.json")));
  } finally {
    globalThis.fetch = originalFetch;
  }
});

test("bootstrap backfills missing engagement only from the same task and topic", async () => {
  const originalFetch = globalThis.fetch;
  globalThis.fetch = (async (input: string | URL | Request) => {
    const url = String(input);
    if (url.includes("/session/current.json")) {
      return jsonResponse({ current_user: { username: "alice" } });
    }
    if (url.includes("/bookmarks.json")) {
      return jsonResponse({
        user_bookmark_list: {
          bookmarks: [
            {
              bookmarkable_type: "Topic",
              bookmarkable_id: 71,
              title: "Shared topic",
              like_count: 2,
            },
            {
              bookmarkable_type: "Topic",
              bookmarkable_id: 72,
              title: "Bookmark only",
            },
          ],
        },
      });
    }
    if (url.includes("/user_actions.json")) {
      return jsonResponse({
        user_actions: [{ topic_id: 71, title: "Shared topic" }],
      });
    }
    return jsonResponse({
      topic_list: {
        topics: [{
          id: 71,
          title: "Shared topic",
          views: 321,
          like_count: 17,
          posts_count: 6,
        }],
      },
    });
  }) as typeof fetch;
  try {
    const result = await executeLinuxdoTask({
      task_id: "bootstrap-engagement-backfill",
      type: "bootstrap_events",
      max_items_per_scope: 3,
      max_pages: 1,
    });
    const sharedByScope = new Map(
      result.items
        .filter((item) => item.topic_id === "71")
        .map((item) => [item.scope, item]),
    );

    assert.equal(result.status, "ok");
    assert.deepEqual(sharedByScope.get("linuxdo_bookmarks"), {
      scope: "linuxdo_bookmarks",
      content_type: "post",
      topic_id: "71",
      content_id: "topic:71",
      title: "Shared topic",
      url: "https://linux.do/t/71",
      tags: [],
      engagement_available: ["like", "view", "comment"],
      like_count: 2,
      interaction_action: "favorite",
      source_strategy: "linuxdo-bootstrap-bookmarks",
      views: 321,
      reply_count: 5,
    });
    assert.deepEqual(
      {
        views: sharedByScope.get("linuxdo_likes")?.views,
        likes: sharedByScope.get("linuxdo_likes")?.like_count,
        replies: sharedByScope.get("linuxdo_likes")?.reply_count,
      },
      { views: 321, likes: 17, replies: 5 },
    );
    assert.deepEqual(
      {
        views: sharedByScope.get("linuxdo_read_history")?.views,
        likes: sharedByScope.get("linuxdo_read_history")?.like_count,
        replies: sharedByScope.get("linuxdo_read_history")?.reply_count,
      },
      { views: 321, likes: 17, replies: 5 },
    );
    const unrelated = result.items.find((item) => item.topic_id === "72");
    assert.equal(unrelated?.views, undefined);
    assert.equal(unrelated?.like_count, undefined);
    assert.equal(unrelated?.reply_count, undefined);
  } finally {
    globalThis.fetch = originalFetch;
  }
});

test("bootstrap preserves earlier pages and reports degraded when a later page fails", async () => {
  const originalFetch = globalThis.fetch;
  globalThis.fetch = (async (input: string | URL | Request) => {
    const url = String(input);
    if (url.includes("/session/current.json")) {
      return jsonResponse({ current_user: { username: "alice" } });
    }
    if (url.includes("page=1")) return jsonResponse({ error: "slow down" }, 429);
    return jsonResponse({
      user_bookmark_list: {
        bookmarks: [{
          bookmarkable_type: "Topic",
          bookmarkable_id: 51,
          title: "Retained page",
          slug: "retained-page",
        }],
        more_bookmarks: true,
      },
    });
  }) as typeof fetch;
  try {
    const result = await executeLinuxdoTask({
      task_id: "bootstrap-partial",
      type: "bootstrap_events",
      scopes: ["linuxdo_bookmarks"],
      max_items_per_scope: 2,
      max_pages: 2,
    });
    assert.equal(result.status, "degraded");
    assert.deepEqual(result.items.map((item) => item.topic_id), ["51"]);
    assert.deepEqual(result.debug, {
      scope_errors: { linuxdo_bookmarks: "linuxdo_rate_limited" },
    });
  } finally {
    globalThis.fetch = originalFetch;
  }
});

test("search preserves completed keywords when a later input is rate limited", async () => {
  const originalFetch = globalThis.fetch;
  globalThis.fetch = (async (input: string | URL | Request) => {
    const url = String(input);
    if (url.includes("q=second")) return jsonResponse({ error: "slow down" }, 429);
    return jsonResponse({ topics: [{ id: 61, title: "First result", slug: "first-result" }] });
  }) as typeof fetch;
  try {
    const result = await executeLinuxdoTask({
      task_id: "search-partial",
      type: "search",
      keywords: ["first", "second"],
      max_items_per_keyword: 2,
      max_pages: 1,
    });
    assert.equal(result.status, "degraded");
    assert.deepEqual(result.items.map((item) => item.topic_id), ["61"]);
    assert.deepEqual(result.debug, {
      input_errors: { "search:second": "linuxdo_rate_limited" },
    });
  } finally {
    globalThis.fetch = originalFetch;
  }
});

test("likes bootstrap preserves earlier offsets when a later page is rate limited", async () => {
  const originalFetch = globalThis.fetch;
  globalThis.fetch = (async (input: string | URL | Request) => {
    const url = String(input);
    if (url.includes("/session/current.json")) {
      return jsonResponse({ current_user: { username: "alice" } });
    }
    if (url.includes("offset=1")) return jsonResponse({ error: "slow down" }, 429);
    return jsonResponse({
      user_actions: [{ topic_id: 71, title: "Retained like", slug: "retained-like" }],
    });
  }) as typeof fetch;
  try {
    const result = await executeLinuxdoTask({
      task_id: "likes-partial",
      type: "bootstrap_events",
      scopes: ["linuxdo_likes"],
      max_items_per_scope: 2,
      max_pages: 2,
    });
    assert.equal(result.status, "degraded");
    assert.deepEqual(result.items.map((item) => item.topic_id), ["71"]);
    assert.deepEqual(result.debug, {
      scope_errors: { linuxdo_likes: "linuxdo_rate_limited" },
    });
  } finally {
    globalThis.fetch = originalFetch;
  }
});

test("bootstrap default limit can page through 300 topic bookmarks", async () => {
  const originalFetch = globalThis.fetch;
  const bookmarkCalls: string[] = [];
  globalThis.fetch = (async (input: string | URL | Request) => {
    const url = String(input);
    if (url.includes("/session/current.json")) {
      return jsonResponse({ current_user: { username: "alice" } });
    }
    bookmarkCalls.push(url);
    const page = Number(new URL(url).searchParams.get("page") ?? 0);
    const start = page * 20 + 1;
    return jsonResponse({
      user_bookmark_list: {
        bookmarks: Array.from({ length: 20 }, (_value, index) => ({
          bookmarkable_type: "Topic",
          bookmarkable_id: start + index,
          title: `Saved topic ${start + index}`,
        })),
        ...(page < 14
          ? { more_bookmarks_url: `/u/alice/bookmarks.json?page=${page + 1}` }
          : {}),
      },
    });
  }) as typeof fetch;
  try {
    const result = await executeLinuxdoTask({
      task_id: "bootstrap-300",
      type: "bootstrap_events",
      scopes: ["linuxdo_bookmarks"],
      max_items_per_scope: 300,
    });
    assert.equal(result.status, "ok");
    assert.equal(result.items.length, 300);
    assert.equal(bookmarkCalls.length, 15);
  } finally {
    globalThis.fetch = originalFetch;
  }
});

test("bootstrap fails closed when session endpoint does not identify a user", async () => {
  const originalFetch = globalThis.fetch;
  globalThis.fetch = (async () => jsonResponse({ current_user: null })) as typeof fetch;
  try {
    const result = await executeLinuxdoTask({ task_id: "bootstrap-logged-out", type: "bootstrap_events" });
    assert.equal(result.status, "failed");
    assert.equal(result.error, "linuxdo_login_required");
    assert.deepEqual(result.items, []);
  } finally {
    globalThis.fetch = originalFetch;
  }
});

test("executor rejects a 200 HTML challenge without uploading its body", async () => {
  const originalFetch = globalThis.fetch;
  globalThis.fetch = (async () => new Response("<html>secret challenge body</html>", {
    status: 200,
    headers: { "content-type": "text/html" },
  })) as typeof fetch;
  try {
    const result = await executeLinuxdoTask({ task_id: "challenge", type: "feed" });
    assert.equal(result.status, "failed");
    assert.equal(result.error, "linuxdo_invalid_response");
    assert.equal(JSON.stringify(result).includes("secret challenge body"), false);
  } finally {
    globalThis.fetch = originalFetch;
  }
});

test("executor maps upstream rate limits without reading or returning the body", async () => {
  const originalFetch = globalThis.fetch;
  globalThis.fetch = (async () => new Response("private rate-limit page", { status: 429 })) as typeof fetch;
  try {
    const result = await executeLinuxdoTask({ task_id: "rate", type: "feed" });
    assert.equal(result.status, "failed");
    assert.equal(result.error, "linuxdo_rate_limited");
    assert.equal(JSON.stringify(result).includes("private rate-limit page"), false);
  } finally {
    globalThis.fetch = originalFetch;
  }
});

test("executor aborts a stalled request with a structured timeout", async () => {
  const originalFetch = globalThis.fetch;
  globalThis.fetch = (async (_input: string | URL | Request, init?: RequestInit) =>
    await new Promise<Response>((_resolve, reject) => {
      init?.signal?.addEventListener("abort", () => reject(new Error("aborted")), { once: true });
    })) as typeof fetch;
  try {
    const result = await executeLinuxdoTask({
      task_id: "timeout",
      type: "feed",
      fetch_timeout_ms: 5,
    });
    assert.equal(result.status, "failed");
    assert.equal(result.error, "linuxdo_request_timeout");
  } finally {
    globalThis.fetch = originalFetch;
  }
});
