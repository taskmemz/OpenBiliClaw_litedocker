import test from "node:test";
import assert from "node:assert/strict";

import {
  extractDouyinSearchItemsFromDocument,
  pickSearchScrollTarget,
} from "../src/content/dy/dom-extractor.ts";

class FakeElement {
  readonly textContent: string;
  readonly href: string;
  readonly src: string;
  readonly className: string;
  readonly childNodes: Array<{ nodeType: number; textContent: string }>;
  private readonly attrs: Record<string, string>;
  private readonly selectorMap: Record<string, FakeElement[]>;
  private readonly closestElement?: FakeElement;

  constructor(opts: {
    textContent?: string;
    href?: string;
    src?: string;
    className?: string;
    ownText?: string;
    attrs?: Record<string, string>;
    selectorMap?: Record<string, FakeElement[]>;
    closestElement?: FakeElement;
  } = {}) {
    this.textContent = opts.textContent ?? "";
    this.href = opts.href ?? "";
    this.src = opts.src ?? "";
    this.className = opts.className ?? "";
    const ownText = opts.ownText ?? opts.textContent ?? "";
    this.childNodes = ownText ? [{ nodeType: 3, textContent: ownText }] : [];
    this.attrs = opts.attrs ?? {};
    this.selectorMap = opts.selectorMap ?? {};
    this.closestElement = opts.closestElement;
  }

  closest(): FakeElement {
    return this.closestElement ?? this;
  }

  querySelector(selector: string): FakeElement | null {
    return this.querySelectorAll(selector)[0] ?? null;
  }

  querySelectorAll(selector: string): FakeElement[] {
    if (selector.includes("[aria-label]")) {
      return this.selectorMap.metrics ?? [];
    }
    if (selector === "span,div,p") return this.selectorMap.semantic ?? [];
    if (selector === "span") return this.selectorMap.spans ?? [];
    return this.selectorMap[selector] ?? [];
  }

  getAttribute(name: string): string | null {
    if (name === "href") return this.href || this.attrs[name] || null;
    if (name === "src") return this.src || this.attrs[name] || null;
    if (name === "class") return this.className || this.attrs[name] || null;
    return this.attrs[name] ?? null;
  }
}

class FakeDocument {
  private readonly anchors: FakeElement[];

  constructor(anchors: FakeElement[]) {
    this.anchors = anchors;
  }

  querySelectorAll(selector: string): FakeElement[] {
    if (selector.includes('[href*="/video/"]') || selector.includes("data-aweme-id")) {
      return this.anchors;
    }
    return [];
  }
}

test("extractDouyinSearchItemsFromDocument reads visible metric chips", () => {
  const title = new FakeElement({ textContent: "猫咪搜索结果" });
  const author = new FakeElement({ textContent: "作者A" });
  const image = new FakeElement({ attrs: { src: "https://p3.douyinpic.com/cover.jpg" } });
  const card = new FakeElement({
    selectorMap: {
      'p[class*="title"]': [title],
      '[class*="author-name"]': [author],
      "img": [image],
      metrics: [
        new FakeElement({ textContent: "播放 1.2万" }),
        new FakeElement({ textContent: "点赞 42" }),
        new FakeElement({ textContent: "收藏 1,234" }),
        new FakeElement({ textContent: "评论 3k" }),
        new FakeElement({ textContent: "分享 9" }),
      ],
    },
  });
  const anchor = new FakeElement({
    href: "/video/1234567890",
    closestElement: card,
  });

  const items = extractDouyinSearchItemsFromDocument(
    new FakeDocument([anchor]) as unknown as Document,
    "https://www.douyin.com/",
    5,
  );

  assert.deepEqual(items, [
    {
      scope: "dy_search",
      aweme_id: "1234567890",
      url: "https://www.douyin.com/video/1234567890",
      title: "猫咪搜索结果",
      author: "作者A",
      author_sec_uid: "",
      cover_url: "https://p3.douyinpic.com/cover.jpg",
      view_count: 12_000,
      like_count: 42,
      collect_count: 1_234,
      comment_count: 3_000,
      share_count: 9,
    },
  ]);
});

test("extractDouyinSearchItemsFromDocument reads current jingxuan data-aweme cards", () => {
  const hrefTarget = new FakeElement({
    attrs: { href: "//www.douyin.com/video/7647302522265659834" },
  });
  const semanticTitle = new FakeElement({
    textContent: "真实精选标题 #推荐",
    ownText: "真实精选标题 #推荐",
  });
  const duration = new FakeElement({ textContent: "04:52", ownText: "04:52" });
  const author = new FakeElement({ textContent: "@ 真实作者", ownText: "@" });
  const card = new FakeElement({
    selectorMap: {
      '[href*="/video/"]': [hrefTarget],
      semantic: [duration, semanticTitle],
      spans: [author],
    },
  });
  const target = new FakeElement({
    attrs: { "data-aweme-id": "7647302522265659834" },
    selectorMap: { '[href*="/video/"]': [hrefTarget] },
    closestElement: card,
  });
  const dataOnlyDocument = {
    querySelectorAll: (selector: string) =>
      selector === 'a[href*="/video/"]' ? [] : [target],
  } as unknown as Document;

  assert.deepEqual(
    extractDouyinSearchItemsFromDocument(
      dataOnlyDocument,
      "https://www.douyin.com/",
      3,
    ),
    [],
  );

  const items = extractDouyinSearchItemsFromDocument(
    dataOnlyDocument,
    "https://www.douyin.com/",
    3,
    true,
  );

  assert.deepEqual(items, [
    {
      scope: "dy_search",
      aweme_id: "7647302522265659834",
      url: "https://www.douyin.com/video/7647302522265659834",
      title: "真实精选标题 #推荐",
      author: "真实作者",
      author_sec_uid: "",
      cover_url: "",
    },
  ]);
});

// ── pickSearchScrollTarget (inner scrollable container discovery) ────────

interface FakeScrollNode {
  overflowY: string;
  scrollHeight: number;
  clientHeight: number;
  parentElement: FakeScrollNode | null;
}

function makeScrollNode(opts: {
  overflowY?: string;
  scrollHeight?: number;
  clientHeight?: number;
  parent?: FakeScrollNode | null;
}): FakeScrollNode {
  return {
    overflowY: opts.overflowY ?? "visible",
    scrollHeight: opts.scrollHeight ?? 0,
    clientHeight: opts.clientHeight ?? 0,
    parentElement: opts.parent ?? null,
  };
}

function makeScrollDoc(anchor: FakeScrollNode | null): Document {
  return {
    querySelector: () => anchor,
    defaultView: {
      getComputedStyle: (el: FakeScrollNode) => ({ overflowY: el.overflowY }),
    },
  } as unknown as Document;
}

test("pickSearchScrollTarget finds the nearest scrollable ancestor", () => {
  const scrollable = makeScrollNode({
    overflowY: "auto",
    scrollHeight: 2_000,
    clientHeight: 800,
  });
  const wrapper = makeScrollNode({ overflowY: "visible", parent: scrollable });
  const anchor = makeScrollNode({ parent: wrapper });

  const target = pickSearchScrollTarget(makeScrollDoc(anchor));
  assert.equal(target, scrollable as unknown as Element);
});

test("pickSearchScrollTarget skips overflow containers with no real overflow", () => {
  // overflow-y auto but scrollHeight ~= clientHeight → not scrollable.
  const flat = makeScrollNode({
    overflowY: "auto",
    scrollHeight: 802,
    clientHeight: 800,
  });
  const anchor = makeScrollNode({ parent: flat });

  assert.equal(pickSearchScrollTarget(makeScrollDoc(anchor)), null);
});

test("pickSearchScrollTarget returns null without a video anchor", () => {
  assert.equal(pickSearchScrollTarget(makeScrollDoc(null)), null);
});
