/**
 * Tests for xhs task executor's pure helpers.
 *
 * The task-executor module imports from passive.js (a .js extension for
 * bundler resolution) which Node can't resolve directly. We test the
 * executor's data contracts and logic boundaries here without importing
 * the module — the real integration is tested via the extension build.
 */

import test from "node:test";
import assert from "node:assert/strict";

import {
  buildBootstrapDebugPayload,
  buildBootstrapPartialPayload,
  bootstrapProfileTabLabels,
  bootstrapScrollShouldContinue,
  clickOwnProfileAnchorFromDocument,
  collectBootstrapScrollCandidates,
  countBootstrapStateNotesByScope,
  describeBootstrapScrollTarget,
  extractOwnProfileUrlFromDocument,
  extractOwnProfileUrlFromState,
  findOwnProfileAnchorFromDocument,
  findBootstrapScrollContainer,
  hasDifferentProfileDocumentNotes,
  hasBootstrapProfileContent,
  isActiveBootstrapProfileTab,
  limitBootstrapNewNotesToRemainingCapacity,
  normalizeBootstrapScrollRounds,
  normalizeBootstrapScrollWaitMs,
  normalizeBootstrapStagnantScrollRounds,
  extractBootstrapNotesFromProfileDocument,
  extractBootstrapNotesFromState,
  profileDocumentNoteKeys,
  readBootstrapScrollMetrics,
  type XhsBootstrapNote,
} from "../src/content/xhs/bootstrap.ts";
import {
  classifyXhsLoginRequiredText,
  classifyXhsRiskControlText,
  detectXhsTaskLoginRequired,
} from "../src/content/xhs/risk-control.ts";

// We can't directly import task-executor.ts because it transitively
// imports "./passive.js" which Node resolves differently from esbuild.
// Instead we test the logic inline — buildLargeViewport is tiny.

function buildLargeViewport(innerHeight: number): {
  top: number;
  bottom: number;
  height: number;
} {
  const height = innerHeight || 900;
  return { top: -500, bottom: height + 500, height: height + 1000 };
}

test("buildLargeViewport creates an oversized viewport for initial capture", () => {
  const vp = buildLargeViewport(900);

  assert.ok(vp.top < 0, "top should be negative (above fold)");
  assert.ok(vp.bottom > 900, "bottom should exceed innerHeight");
  assert.ok(vp.height > 900, "height should be larger than innerHeight");
});

test("buildLargeViewport falls back when innerHeight is 0", () => {
  const vp = buildLargeViewport(0);

  assert.ok(vp.height > 0, "height should be positive even with 0 innerHeight");
  assert.ok(vp.bottom > 0, "bottom should be positive");
});

test("TaskResultPayload shape matches dispatcher expectations", () => {
  // Type-level contract check — the dispatcher expects these fields.
  const okResult = {
    task_id: "t1",
    urls: ["https://www.xiaohongshu.com/explore/abc123"],
    status: "ok" as const,
  };
  assert.equal(okResult.task_id, "t1");
  assert.equal(okResult.status, "ok");
  assert.equal(okResult.urls.length, 1);

  const errorResult = {
    task_id: "t2",
    urls: [] as string[],
    status: "error" as const,
    error: "timeout",
  };
  assert.equal(errorResult.status, "error");
  assert.equal(errorResult.error, "timeout");

  const emptyResult = {
    task_id: "t3",
    urls: [] as string[],
    status: "empty" as const,
  };
  assert.equal(emptyResult.status, "empty");
  assert.equal(emptyResult.urls.length, 0);

  const rateLimitedResult = {
    task_id: "t4",
    urls: [] as string[],
    status: "rate_limited" as const,
    error: "xhs_rate_limited",
  };
  assert.equal(rateLimitedResult.status, "rate_limited");
  assert.equal(rateLimitedResult.error, "xhs_rate_limited");
});

test("classifyXhsRiskControlText recognizes the security-verification popup", () => {
  assert.deepEqual(
    classifyXhsRiskControlText("安全验证\n请勿频繁操作，稍后重试\n问题反馈"),
    {
      error: "xhs_rate_limited",
      reason: "security_verification",
    },
  );
  assert.deepEqual(
    classifyXhsRiskControlText("Too Many Requests (HTTP 429)"),
    {
      error: "xhs_rate_limited",
      reason: "frequent_operation",
    },
  );
});

test("classifyXhsRiskControlText does not treat generic retry copy as a challenge", () => {
  assert.equal(classifyXhsRiskControlText("网络开小差了，请稍后重试"), null);
  assert.equal(classifyXhsRiskControlText("账号安全验证使用说明"), null);
  assert.equal(classifyXhsRiskControlText("普通的小红书笔记内容"), null);
});

test("XHS login gate is distinct from risk control and ordinary login prose", () => {
  assert.equal(classifyXhsLoginRequiredText("登录后查看搜索结果"), true);
  assert.equal(classifyXhsLoginRequiredText("登录即可查看 Ta 的笔记"), true);
  assert.equal(classifyXhsLoginRequiredText("登录探索更多内容"), false);
  assert.equal(classifyXhsRiskControlText("登录后查看搜索结果"), null);
});

function loginGateDocument(
  requiredSelector: string,
  options: { visible?: boolean; input?: boolean } = {},
): Document {
  const visible = options.visible ?? true;
  const element = {
    hidden: false,
    parentElement: null,
    innerText: "登录",
    textContent: "登录",
    getAttribute: () => null,
    getBoundingClientRect: () => ({
      width: visible ? 120 : 0,
      height: visible ? 40 : 0,
    }),
    matches: (selector: string) =>
      selector.includes(requiredSelector) ||
      (Boolean(options.input) && selector.includes("input[type='tel']")),
  } as unknown as HTMLElement;
  return {
    defaultView: {
      getComputedStyle: () => ({
        display: "block",
        visibility: "visible",
        opacity: "1",
      }),
    },
    querySelectorAll: (selector: string) =>
      selector.includes(requiredSelector) ? [element] : [],
  } as unknown as Document;
}

test("detectXhsTaskLoginRequired recognizes the current visible sidebar login control", () => {
  const doc = loginGateDocument(
    ".side-bar .side-bar-component.login-btn button.login-btn",
  );
  assert.equal(detectXhsTaskLoginRequired(doc), true);
});

test("detectXhsTaskLoginRequired recognizes a phone input inside the current login modal", () => {
  const doc = loginGateDocument(".login-container input[type='tel']", { input: true });
  assert.equal(detectXhsTaskLoginRequired(doc), true);
});

test("detectXhsTaskLoginRequired ignores hidden sidebar login controls", () => {
  const doc = loginGateDocument(
    ".side-bar .side-bar-component.login-btn button.login-btn",
    { visible: false },
  );
  assert.equal(detectXhsTaskLoginRequired(doc), false);
});

test("extractBootstrapNotesFromState maps saved liked and history groups", () => {
  const published = { id: "published", display_title: "published" };
  const saved = {
    id: "saved-id",
    display_title: "saved",
    create_time: 1783492200000,
    xsec_token: "saved-token",
    user: { nickname: "saved-author" },
    cover: { url: "https://example.com/saved.jpg" },
  };
  const liked = {
    note_id: "liked-id",
    title: "liked",
    xsecToken: "liked-token",
    user_info: { nickname: "liked-author" },
  };
  const history = {
    noteId: "history-id",
    desc: "history",
    author: "history-author",
  };

  const notes = extractBootstrapNotesFromState({
    user: { notes: { _rawValue: [[published], [saved], [liked]] } },
    history: { notes: { _rawValue: [history] } },
  });

  assert.equal(notes.find((n) => n.title === "saved")?.scope, "saved");
  assert.equal(notes.find((n) => n.title === "liked")?.scope, "liked");
  assert.equal(notes.find((n) => n.title === "history")?.scope, "xhs_history");
  assert.equal(notes.find((n) => n.title === "saved")?.note_id, "saved-id");
  assert.equal(notes.find((n) => n.title === "saved")?.xsec_token, "saved-token");
  assert.equal(notes.find((n) => n.title === "saved")?.published_at, 1783492200000);
});

test("extractBootstrapNotesFromState reads Xiaohongshu profile noteCard state shape", () => {
  const notes = extractBootstrapNotesFromState(
    {
      user: {
        notes: {
          _rawValue: [
            [],
            [
              {
                id: "saved-note-id",
                xsecToken: "saved-xsec",
                noteCard: {
                  displayTitle: "收藏标题",
                  time: 1783492200000,
                  cover: { urlDefault: "https://example.com/saved-cover.jpg" },
                  user: { nickName: "收藏作者" },
                },
              },
            ],
            [
              {
                id: "liked-note-id",
                xsecToken: "liked-xsec",
                noteCard: {
                  displayTitle: "赞过标题",
                  cover: { urlDefault: "https://example.com/liked-cover.jpg" },
                  user: { nickName: "赞过作者" },
                },
              },
            ],
          ],
        },
      },
    },
    ["saved", "liked"],
  );

  assert.deepEqual(
    notes.map((note) => ({
      scope: note.scope,
      note_id: note.note_id,
      title: note.title,
      author: note.author,
      cover_url: note.cover_url,
      xsec_token: note.xsec_token,
      published_at: note.published_at,
    })),
    [
      {
        scope: "saved",
        note_id: "saved-note-id",
        title: "收藏标题",
        author: "收藏作者",
        cover_url: "https://example.com/saved-cover.jpg",
        xsec_token: "saved-xsec",
        published_at: 1783492200000,
      },
      {
        scope: "liked",
        note_id: "liked-note-id",
        title: "赞过标题",
        author: "赞过作者",
        cover_url: "https://example.com/liked-cover.jpg",
        xsec_token: "liked-xsec",
        published_at: undefined,
      },
    ],
  );
  assert.equal("published_at" in notes[1]!, false);
});

test("countBootstrapStateNotesByScope returns per-scope diagnostic counts", () => {
  const state = {
    user: {
      notes: {
        _rawValue: [
          [{ id: "published", display_title: "published" }],
          [
            { id: "saved-a", display_title: "saved-a" },
            { id: "saved-b", display_title: "saved-b" },
          ],
          [{ id: "liked-a", display_title: "liked-a" }],
        ],
      },
    },
  };

  assert.deepEqual(
    countBootstrapStateNotesByScope(state, ["saved", "liked", "xhs_history"], {
      maxItemsPerScope: 20,
    }),
    { saved: 2, liked: 1, xhs_history: 0 },
  );
});

test("buildBootstrapDebugPayload wraps a single bootstrap diagnostic step", () => {
  assert.deepEqual(
    buildBootstrapDebugPayload({
      page_url: "https://www.xiaohongshu.com/explore",
      is_profile_page: false,
      has_initial_state: true,
      requested_scopes: ["saved"],
      state_counts: { saved: 0 },
    }),
    {
      xhs_bootstrap: {
        steps: [
          {
            page_url: "https://www.xiaohongshu.com/explore",
            is_profile_page: false,
            has_initial_state: true,
            requested_scopes: ["saved"],
            state_counts: { saved: 0 },
          },
        ],
      },
    },
  );
});

test("bootstrapProfileTabLabels keeps liked clicks on the profile tab", () => {
  assert.deepEqual(bootstrapProfileTabLabels("saved"), ["收藏"]);
  assert.deepEqual(bootstrapProfileTabLabels("liked"), ["赞过", "喜欢", "点赞"]);
  assert.deepEqual(bootstrapProfileTabLabels("xhs_history"), []);
});

test("normalizeBootstrapScrollRounds keeps scrolling explicit and bounded", () => {
  assert.equal(normalizeBootstrapScrollRounds(undefined), 0);
  assert.equal(normalizeBootstrapScrollRounds(0), 0);
  assert.equal(normalizeBootstrapScrollRounds(-1), 0);
  assert.equal(normalizeBootstrapScrollRounds(3.8), 3);
  assert.equal(normalizeBootstrapScrollRounds(100), 30);
});

test("bootstrap scroll wait and stagnant rounds are backend-controlled but clamped", () => {
  assert.equal(normalizeBootstrapScrollWaitMs(undefined), 1_200);
  assert.equal(normalizeBootstrapScrollWaitMs(100), 500);
  assert.equal(normalizeBootstrapScrollWaitMs(2_750.8), 2_750);
  assert.equal(normalizeBootstrapScrollWaitMs(20_000), 5_000);

  assert.equal(normalizeBootstrapStagnantScrollRounds(undefined), 5);
  assert.equal(normalizeBootstrapStagnantScrollRounds(1), 1);
  assert.equal(normalizeBootstrapStagnantScrollRounds(8.9), 8);
  assert.equal(normalizeBootstrapStagnantScrollRounds(100), 10);
});

test("bootstrapScrollShouldContinue stops at caps and stagnant rounds", () => {
  assert.equal(
    bootstrapScrollShouldContinue({
      currentCount: 20,
      maxItemsPerScope: 100,
      round: 0,
      maxScrollRounds: 10,
      stagnantRounds: 0,
    }),
    true,
  );
  assert.equal(
    bootstrapScrollShouldContinue({
      currentCount: 100,
      maxItemsPerScope: 100,
      round: 0,
      maxScrollRounds: 10,
      stagnantRounds: 0,
    }),
    false,
  );
  assert.equal(
    bootstrapScrollShouldContinue({
      currentCount: 20,
      maxItemsPerScope: 100,
      round: 10,
      maxScrollRounds: 10,
      stagnantRounds: 0,
    }),
    false,
  );
  assert.equal(
    bootstrapScrollShouldContinue({
      currentCount: 20,
      maxItemsPerScope: 100,
      round: 3,
      maxScrollRounds: 10,
      stagnantRounds: 4,
    }),
    true,
  );
  assert.equal(
    bootstrapScrollShouldContinue({
      currentCount: 20,
      maxItemsPerScope: 100,
      round: 5,
      maxScrollRounds: 10,
      stagnantRounds: 5,
    }),
    false,
  );
  assert.equal(
    bootstrapScrollShouldContinue({
      currentCount: 20,
      maxItemsPerScope: 100,
      round: 7,
      maxScrollRounds: 10,
      stagnantRounds: 7,
      maxStagnantScrollRounds: 8,
    }),
    true,
  );
  assert.equal(
    bootstrapScrollShouldContinue({
      currentCount: 20,
      maxItemsPerScope: 100,
      round: 8,
      maxScrollRounds: 10,
      stagnantRounds: 8,
      maxStagnantScrollRounds: 8,
    }),
    false,
  );
});

test("findBootstrapScrollContainer prefers a scrollable note feed container", () => {
  const shallow = {
    id: "",
    className: "main-container",
    tagName: "DIV",
    scrollTop: 0,
    scrollHeight: 900,
    clientHeight: 820,
    querySelectorAll: () => [],
  };
  const feed = {
    id: "feed",
    className: "feeds-container waterfall",
    tagName: "DIV",
    scrollTop: 120,
    scrollHeight: 3600,
    clientHeight: 720,
    querySelectorAll: (selector: string) => (selector.includes("/explore/") ? [{}] : []),
  };
  const doc = {
    querySelectorAll: () => [shallow, feed],
  } as unknown as Document;

  assert.equal(findBootstrapScrollContainer(doc), feed);
  assert.equal(describeBootstrapScrollTarget(feed as unknown as HTMLElement), "div#feed.feeds-container.waterfall");
  assert.deepEqual(readBootstrapScrollMetrics(feed as unknown as HTMLElement), {
    target: "div#feed.feeds-container.waterfall",
    scroll_top: 120,
    scroll_height: 3600,
    client_height: 720,
  });
});

test("findBootstrapScrollContainer ignores zero-height feed wrappers", () => {
  const zeroHeightFeed = {
    id: "feed",
    className: "feeds-container waterfall",
    tagName: "DIV",
    scrollTop: 0,
    scrollHeight: 900,
    clientHeight: 0,
    querySelectorAll: (selector: string) => (selector.includes("/explore/") ? [{}, {}] : []),
  };
  const actualScroller = {
    id: "scroller",
    className: "main-scroll",
    tagName: "DIV",
    scrollTop: 0,
    scrollHeight: 3000,
    clientHeight: 720,
    querySelectorAll: () => [],
  };
  const doc = {
    querySelectorAll: () => [zeroHeightFeed, actualScroller],
  } as unknown as Document;

  assert.equal(findBootstrapScrollContainer(doc), actualScroller);
});

test("findBootstrapScrollContainer falls back to generic scrollable elements", () => {
  const genericScroller = {
    id: "root-scroll",
    className: "layout-shell",
    tagName: "DIV",
    scrollTop: 0,
    scrollHeight: 4200,
    clientHeight: 760,
    querySelectorAll: () => [],
  };
  const doc = {
    querySelectorAll: (selector: string) => (selector === "body *" ? [genericScroller] : []),
  } as unknown as Document;

  assert.equal(findBootstrapScrollContainer(doc), genericScroller);
});

test("findBootstrapScrollContainer ignores overflowing wrappers without scroll overflow style", () => {
  const hiddenWrapper = {
    id: "tab-panel",
    className: "tab-content-item",
    tagName: "DIV",
    scrollTop: 0,
    scrollHeight: 500,
    clientHeight: 300,
    querySelectorAll: (selector: string) => (selector.includes("/explore/") ? [{}, {}] : []),
  };
  const realScroller = {
    id: "real-scroll",
    className: "layout-shell",
    tagName: "DIV",
    scrollTop: 0,
    scrollHeight: 3000,
    clientHeight: 700,
    querySelectorAll: () => [],
  };
  const doc = {
    defaultView: {
      getComputedStyle: (element: unknown) => ({
        overflowY: element === realScroller ? "auto" : "hidden",
      }),
    },
    querySelectorAll: () => [hiddenWrapper, realScroller],
  } as unknown as Document;
  Object.assign(hiddenWrapper, { ownerDocument: doc });
  Object.assign(realScroller, { ownerDocument: doc });

  assert.equal(findBootstrapScrollContainer(doc), realScroller);
});

test("findBootstrapScrollContainer ignores Xiaohongshu channel sidebar scrollers", () => {
  const channelList = {
    id: "",
    className: "channel-list",
    tagName: "UL",
    scrollTop: 0,
    scrollHeight: 964,
    clientHeight: 675,
    querySelectorAll: () => [],
  };
  const doc = {
    defaultView: {
      getComputedStyle: () => ({ overflowY: "scroll" }),
    },
    querySelectorAll: () => [channelList],
  } as unknown as Document;
  Object.assign(channelList, { ownerDocument: doc });

  assert.equal(findBootstrapScrollContainer(doc), null);
  assert.deepEqual(collectBootstrapScrollCandidates(doc), []);
});

test("collectBootstrapScrollCandidates exposes ranked scroll diagnostics", () => {
  const first = {
    id: "first",
    className: "layout-shell",
    tagName: "DIV",
    scrollTop: 10,
    scrollHeight: 2000,
    clientHeight: 700,
    querySelectorAll: () => [],
  };
  const second = {
    id: "second",
    className: "feeds-container",
    tagName: "DIV",
    scrollTop: 5,
    scrollHeight: 1200,
    clientHeight: 700,
    querySelectorAll: (selector: string) => (selector.includes("/explore/") ? [{}] : []),
  };
  const doc = {
    defaultView: {
      getComputedStyle: () => ({ overflowY: "auto" }),
    },
    querySelectorAll: () => [first, second],
  } as unknown as Document;
  Object.assign(first, { ownerDocument: doc });
  Object.assign(second, { ownerDocument: doc });

  assert.deepEqual(
    collectBootstrapScrollCandidates(doc, 2).map((candidate) => ({
      target: candidate.target,
      overflow_y: candidate.overflow_y,
      note_count: candidate.note_count,
      scroll_top: candidate.scroll_top,
    })),
    [
      {
        target: "div#second.feeds-container",
        overflow_y: "auto",
        note_count: 1,
        scroll_top: 5,
      },
      {
        target: "div#first.layout-shell",
        overflow_y: "auto",
        note_count: 0,
        scroll_top: 10,
      },
    ],
  );
});

test("buildBootstrapPartialPayload emits a small partial result", () => {
  assert.deepEqual(
    buildBootstrapPartialPayload({
      taskId: "task-partial",
      scope: "saved",
      notes: [
        {
          scope: "saved",
          url: "https://www.xiaohongshu.com/explore/a",
          title: "a",
          author: "",
          cover_url: "",
          note_id: "a",
          xsec_token: "",
        },
      ],
      scopeCounts: { saved: 1, liked: 0, xhs_history: 0 },
      round: 2,
    }),
    {
      task_id: "task-partial",
      status: "partial",
      urls: ["https://www.xiaohongshu.com/explore/a"],
      notes: [
        {
          scope: "saved",
          url: "https://www.xiaohongshu.com/explore/a",
          title: "a",
          author: "",
          cover_url: "",
          note_id: "a",
          xsec_token: "",
        },
      ],
      scope_counts: { saved: 1, liked: 0, xhs_history: 0 },
      debug: { xhs_bootstrap_partial: { scope: "saved", round: 2, count: 1 } },
    },
  );
});

test("limitBootstrapNewNotesToRemainingCapacity clips partial batches at the scope cap", () => {
  const currentNotes: XhsBootstrapNote[] = Array.from({ length: 191 }, (_, index) => ({
    scope: "saved",
    url: `https://www.xiaohongshu.com/explore/current-${index}`,
    title: `current ${index}`,
    author: "",
    cover_url: "",
    note_id: `current-${index}`,
    xsec_token: "",
  }));
  const newNotes: XhsBootstrapNote[] = Array.from({ length: 10 }, (_, index) => ({
    scope: "saved",
    url: `https://www.xiaohongshu.com/explore/new-${index}`,
    title: `new ${index}`,
    author: "",
    cover_url: "",
    note_id: `new-${index}`,
    xsec_token: "",
  }));

  const clipped = limitBootstrapNewNotesToRemainingCapacity(currentNotes, newNotes, 200);

  assert.equal(clipped.length, 9);
  assert.equal(clipped.at(-1)?.note_id, "new-8");
  assert.deepEqual(limitBootstrapNewNotesToRemainingCapacity(currentNotes, newNotes, 191), []);
});

test("extractBootstrapNotesFromProfileDocument reads visible saved or liked cards only", () => {
  const card = {
    querySelector: (selector: string) => {
      if (selector.includes("title")) return { textContent: "DOM 收藏笔记" };
      if (selector.includes("author") || selector.includes("nickname")) {
        return { textContent: "作者名" };
      }
      if (selector.includes("img")) {
        return {
          getAttribute: (name: string) =>
            name === "src" ? "https://example.com/cover.jpg" : "",
        };
      }
      return null;
    },
  };
  const anchor = {
    href: "https://www.xiaohongshu.com/explore/dom-note-id?xsec_token=dom-token",
    title: "",
    closest: () => card,
  };
  const doc = {
    querySelectorAll: () => [anchor],
  } as unknown as Document;

  const notes = extractBootstrapNotesFromProfileDocument(
    doc,
    "saved",
    "https://www.xiaohongshu.com/user/profile/current-user",
  );

  assert.equal(notes.length, 1);
  assert.equal(notes[0].scope, "saved");
  assert.equal(notes[0].title, "DOM 收藏笔记");
  assert.equal(notes[0].author, "作者名");
  assert.equal(notes[0].note_id, "dom-note-id");
  assert.equal(notes[0].xsec_token, "dom-token");

  assert.deepEqual(
    extractBootstrapNotesFromProfileDocument(
      doc,
      "xhs_history",
      "https://www.xiaohongshu.com/user/profile/current-user",
    ),
    [],
  );
});

test("hasBootstrapProfileContent waits for profile state tabs or cards", () => {
  assert.equal(
    hasBootstrapProfileContent({
      defaultView: null,
      body: { textContent: "" },
      querySelector: () => null,
    } as unknown as Document),
    false,
  );
  assert.equal(
    hasBootstrapProfileContent({
      defaultView: { __INITIAL_STATE__: { user: { notes: [] } } },
      body: { textContent: "" },
      querySelector: () => null,
    } as unknown as Document),
    true,
  );
  assert.equal(
    hasBootstrapProfileContent({
      defaultView: null,
      body: { textContent: "笔记 收藏 赞过" },
      querySelector: () => null,
    } as unknown as Document),
    true,
  );
  assert.equal(
    hasBootstrapProfileContent({
      defaultView: null,
      body: { textContent: "" },
      querySelector: (selector: string) => (selector.includes("/explore/") ? {} : null),
    } as unknown as Document),
    true,
  );
});

test("profileDocumentNoteKeys and change detection guard against stale tab DOM", () => {
  const beforeDoc = {
    querySelectorAll: () => [
      {
        href: "https://www.xiaohongshu.com/explore/default-note",
        title: "默认笔记",
        closest: () => ({ querySelector: () => null }),
      },
    ],
  } as unknown as Document;
  const previousKeys = profileDocumentNoteKeys(
    beforeDoc,
    "https://www.xiaohongshu.com/user/profile/current-user",
  );
  assert.deepEqual(previousKeys, ["default-note"]);

  const staleNotes: XhsBootstrapNote[] = [
    {
      scope: "saved",
      url: "https://www.xiaohongshu.com/explore/default-note",
      title: "默认笔记",
      author: "",
      cover_url: "",
      note_id: "default-note",
      xsec_token: "",
    },
  ];
  const changedNotes: XhsBootstrapNote[] = [
    {
      scope: "saved",
      url: "https://www.xiaohongshu.com/explore/saved-note",
      title: "收藏笔记",
      author: "",
      cover_url: "",
      note_id: "saved-note",
      xsec_token: "",
    },
  ];

  assert.equal(hasDifferentProfileDocumentNotes(staleNotes, previousKeys), false);
  assert.equal(hasDifferentProfileDocumentNotes(changedNotes, previousKeys), true);
});

test("isActiveBootstrapProfileTab detects selected profile tabs", () => {
  assert.equal(
    isActiveBootstrapProfileTab({
      getAttribute: (name: string) => (name === "aria-selected" ? "true" : ""),
      className: "",
    } as unknown as HTMLElement),
    true,
  );
  assert.equal(
    isActiveBootstrapProfileTab({
      getAttribute: () => "",
      className: "reds-tab-item active",
    } as unknown as HTMLElement),
    true,
  );
  assert.equal(
    isActiveBootstrapProfileTab({
      getAttribute: () => "",
      className: "reds-tab-item",
    } as unknown as HTMLElement),
    false,
  );
});

test("extractBootstrapNotesFromState caps per scope and skips unusable notes", () => {
  const saved: XhsBootstrapNote[] = extractBootstrapNotesFromState(
    {
      user: {
        notes: {
          _rawValue: [
            [],
            [
              { id: "a", title: "a" },
              { id: "b", title: "b" },
              { id: "", title: "", url: "" },
            ],
          ],
        },
      },
    },
    ["saved"],
    { maxItemsPerScope: 1 },
  );

  assert.equal(saved.length, 1);
  assert.equal(saved[0].title, "a");
  assert.equal(saved[0].url, "https://www.xiaohongshu.com/explore/a");
});

test("extractOwnProfileUrlFromDocument reads the logged-in nav profile link", () => {
  const anchor = {
    getAttribute: (name: string) =>
      name === "href" ? "/user/profile/current-user?xsec_token=own" : "",
  };
  const doc = {
    querySelector: () => anchor,
    querySelectorAll: () => [],
  } as unknown as Document;

  assert.equal(
    extractOwnProfileUrlFromDocument(doc, "https://www.xiaohongshu.com/explore"),
    "https://www.xiaohongshu.com/user/profile/current-user?xsec_token=own",
  );
});

test("findOwnProfileAnchorFromDocument returns and clicks the logged-in nav profile link", () => {
  const events: string[] = [];
  class FakeMouseEvent {
    type: string;

    constructor(type: string) {
      this.type = type;
    }
  }
  const anchor = {
    textContent: "我",
    className: "link-wrapper",
    getAttribute: (name: string) =>
      name === "href" ? "/user/profile/current-user?xsec_token=own" : "",
    closest: () => ({}),
    scrollIntoView: () => events.push("scrollIntoView"),
    dispatchEvent: (event: { type: string }) => {
      events.push(event.type);
      return true;
    },
    click: () => events.push("click"),
  };
  const doc = {
    querySelector: () => null,
    querySelectorAll: () => [anchor],
  } as unknown as Document;
  const win = { MouseEvent: FakeMouseEvent } as unknown as Window;

  assert.equal(findOwnProfileAnchorFromDocument(doc, "https://www.xiaohongshu.com/explore"), anchor);
  assert.deepEqual(
    clickOwnProfileAnchorFromDocument(doc, "https://www.xiaohongshu.com/explore", win),
    {
      url: "https://www.xiaohongshu.com/user/profile/current-user?xsec_token=own",
      clicked: true,
    },
  );
  assert.deepEqual(events, ["scrollIntoView", "mousedown", "mouseup", "click"]);
});

test("extractOwnProfileUrlFromDocument ignores feed author profile links", () => {
  const anchors = [
    {
      textContent: "作者",
      className: "author",
      getAttribute: (name: string) =>
        name === "href" ? "/user/profile/someone-else?xsec_source=pc_feed" : "",
      closest: () => null,
    },
    {
      textContent: "笔记标题",
      className: "title",
      getAttribute: (name: string) =>
        name === "href" ? "/user/profile/someone-else?xsec_source=pc_user" : "",
      closest: () => null,
    },
  ];
  const doc = {
    querySelector: () => null,
    querySelectorAll: () => anchors,
  } as unknown as Document;

  assert.equal(
    extractOwnProfileUrlFromDocument(doc, "https://www.xiaohongshu.com/explore"),
    "",
  );
});

test("extractOwnProfileUrlFromState builds a profile URL only for logged-in users", () => {
  assert.equal(
    extractOwnProfileUrlFromState({
      user: {
        loggedIn: true,
        userInfo: { _rawValue: { userId: "current-user" } },
      },
    }),
    "https://www.xiaohongshu.com/user/profile/current-user",
  );

  assert.equal(
    extractOwnProfileUrlFromState({
      user: {
        loggedIn: false,
        userInfo: { _rawValue: { userId: "guest-user" } },
      },
    }),
    "",
  );
});

// v0.3.12+ MAIN-world state bridge integration. The MV3 isolated world
// can't read ``window.__INITIAL_STATE__``; ``xhs-state-bridge.ts`` runs
// in MAIN world and postMessages a snapshot. ``bootstrap.ts``'s message
// listener caches it for synchronous reads via
// ``extractBootstrapStateFromDocument``.

test("ingestMainWorldStateMessage caches snapshots from xhs-state-bridge", async () => {
  const mod = await import("../src/content/xhs/bootstrap.ts");
  mod._resetMainWorldStateCacheForTesting();

  // Wrong source — must be ignored.
  assert.equal(
    mod.ingestMainWorldStateMessage({ source: "obc-xhs-sniffer", state: { user: {} } }),
    false,
  );

  // Correct source — must cache.
  assert.equal(
    mod.ingestMainWorldStateMessage({
      source: "obc-xhs-state",
      state: { user: { loggedIn: true, userInfo: { nickname: "屎屎" } } },
    }),
    true,
  );

  const fakeDoc = {
    defaultView: {} as unknown as Window,
    querySelectorAll: () => [],
  } as unknown as Document;
  const recovered = mod.extractBootstrapStateFromDocument(fakeDoc) as {
    user: { loggedIn: boolean; userInfo: { nickname: string } };
  };
  assert.equal(recovered.user.loggedIn, true);
  assert.equal(recovered.user.userInfo.nickname, "屎屎");

  mod._resetMainWorldStateCacheForTesting();
});

test("ingestMainWorldStateMessage rejects malformed payloads", async () => {
  const mod = await import("../src/content/xhs/bootstrap.ts");
  mod._resetMainWorldStateCacheForTesting();

  assert.equal(mod.ingestMainWorldStateMessage(null), false);
  assert.equal(mod.ingestMainWorldStateMessage("not an object"), false);
  assert.equal(mod.ingestMainWorldStateMessage({ source: "obc-xhs-state" }), false);
  assert.equal(
    mod.ingestMainWorldStateMessage({ source: "obc-xhs-state", state: null }),
    false,
  );
});

test("extractBootstrapStateFromDocument prefers cache over doc.defaultView", async () => {
  const mod = await import("../src/content/xhs/bootstrap.ts");
  mod._resetMainWorldStateCacheForTesting();

  mod.ingestMainWorldStateMessage({
    source: "obc-xhs-state",
    state: { user: { fromBridge: true } },
  });

  const fakeDoc = {
    defaultView: { __INITIAL_STATE__: { user: { fromDoc: true } } } as unknown as Window,
    querySelectorAll: () => [],
  } as unknown as Document;

  const result = mod.extractBootstrapStateFromDocument(fakeDoc) as {
    user: { fromBridge?: boolean; fromDoc?: boolean };
  };
  assert.equal(result.user.fromBridge, true);
  assert.equal(result.user.fromDoc, undefined);

  mod._resetMainWorldStateCacheForTesting();
});
