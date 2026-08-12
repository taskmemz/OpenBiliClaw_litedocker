import test from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";

import {
  buildLinuxdoTargetMetadata,
  detectLinuxdoPageType,
  extractLinuxdoContentId,
  inferLinuxdoActionType,
  linuxdoAdapter,
} from "../src/shared/platforms/linuxdo.ts";

test("linuxdo adapter classifies Discourse pages and canonical topic ids", () => {
  assert.equal(detectLinuxdoPageType("https://linux.do/t/topic-title/2726297/3"), "post");
  assert.equal(detectLinuxdoPageType("https://linux.do/search?q=agent"), "search");
  assert.equal(detectLinuxdoPageType("https://linux.do/u/alice/activity/topics"), "profile");
  assert.equal(detectLinuxdoPageType("https://linux.do/c/develop/4"), "category");
  assert.equal(detectLinuxdoPageType("https://linux.do/latest"), "feed");
  assert.equal(extractLinuxdoContentId("https://linux.do/t/topic-title/2726297/3"), "topic:2726297");
  assert.equal(extractLinuxdoContentId("https://linux.do/t/2726297/3"), "topic:2726297");
  assert.equal(extractLinuxdoContentId("https://example.com/t/topic-title/2726297"), null);
});

test("linuxdo adapter maps read-only interaction observations", () => {
  assert.equal(inferLinuxdoActionType({ text: "点赞", ariaLabel: null, className: "" }), "like");
  assert.equal(inferLinuxdoActionType({ text: "收藏", ariaLabel: null, className: "" }), "favorite");
  assert.equal(inferLinuxdoActionType({ text: "回复", ariaLabel: null, className: "" }), "comment");
  assert.equal(inferLinuxdoActionType({ text: "Share", ariaLabel: null, className: "" }), "share");
  assert.equal(
    inferLinuxdoActionType({ text: "取消点赞", ariaLabel: null, className: "" }),
    null,
  );
});

test("linuxdo adapter stamps platform metadata", () => {
  assert.equal(linuxdoAdapter.sourcePlatform, "linuxdo");
  assert.equal(linuxdoAdapter.videoSelector, null);
  assert.deepEqual(
    linuxdoAdapter.buildEventMetadata("https://linux.do/t/local-first/42"),
    { content_type: "post", content_id: "topic:42", topic_id: "42" },
  );
});

test("linuxdo target metadata stays inside the clicked topic card", () => {
  const topicLink = {
    href: "https://linux.do/t/local-first/42",
    getAttribute(name: string) {
      return name === "href" ? this.href : null;
    },
  } as unknown as Element;
  const authorLink = {
    href: "https://linux.do/u/alice",
    getAttribute(name: string) {
      return name === "href" ? this.href : null;
    },
  } as unknown as Element;
  const card = {
    getAttribute(name: string) {
      return name === "data-topic-id" ? "42" : null;
    },
    querySelector(selector: string) {
      return selector.includes('/u/') ? authorLink : topicLink;
    },
  } as unknown as Element;
  const target = {
    getAttribute() {
      return null;
    },
    closest(selector: string) {
      return selector.includes("topic-list-item") ? card : null;
    },
  } as unknown as Element;

  assert.deepEqual(buildLinuxdoTargetMetadata(target, "https://linux.do/latest"), {
    target_url: "https://linux.do/t/local-first/42",
    content_id: "topic:42",
    topic_id: "42",
    content_type: "post",
    author_name: "alice",
    author_url: "https://linux.do/u/alice",
  });
});

test("Chrome and Firefox manifests register only the Linux.do origin and built content entry", () => {
  const chromeManifest = JSON.parse(readFileSync("manifest.json", "utf8")) as {
    host_permissions: string[];
    content_scripts: Array<{ matches: string[]; js: string[] }>;
  };
  const firefoxManifest = JSON.parse(readFileSync("manifest.firefox.json", "utf8")) as typeof chromeManifest;
  assert.ok(chromeManifest.host_permissions.includes("https://linux.do/*"));
  assert.ok(firefoxManifest.host_permissions.includes("https://linux.do/*"));
  assert.ok(chromeManifest.content_scripts.some((entry) => entry.js.includes("dist/content/linuxdo.js")));
  assert.ok(firefoxManifest.content_scripts.some((entry) => entry.js.includes("content/linuxdo.js")));
  assert.match(readFileSync("scripts/build.mjs", "utf8"), /src\/content\/linuxdo\.ts/);
});
