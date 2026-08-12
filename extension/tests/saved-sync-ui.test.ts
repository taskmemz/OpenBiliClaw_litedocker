import test from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { resolve } from "node:path";

import {
  fetchSavedItems,
  pollSavedSyncTask,
  removeSavedItem,
  saveItem,
  syncSavedItems,
} from "../popup/popup-api.js";
import { __resetBackendEndpointForTests } from "../popup/popup-backend-config.js";
import {
  getSavedSyncPresentation,
  sanitizeSavedSyncTask,
  summarizeSavedSyncResults,
} from "../popup/popup-saved-sync.js";

test("saved sync API helpers send canonical identity and manual sync keys", async () => {
  __resetBackendEndpointForTests();
  const calls: Array<{ url: string; options: RequestInit }> = [];
  globalThis.fetch = (async (url: string, options: RequestInit = {}) => {
    calls.push({ url, options });
    return {
      ok: true,
      status: 200,
      async json() {
        return { task_id: "task-1", items: [] };
      },
    };
  }) as unknown as typeof fetch;

  await saveItem("watch_later", {
    item_key: "bilibili:BV1",
    source_platform: "bilibili",
    content_id: "BV1",
    content_url: "https://www.bilibili.com/video/BV1",
    content_type: "video",
    title: "一条视频",
    up_name: "测试 UP",
  });
  await removeSavedItem("watch_later", "bilibili:BV1");
  await syncSavedItems("watch_later", ["bilibili:BV1"]);
  await fetchSavedItems("watch_later", 20, 40);
  await pollSavedSyncTask("123e4567-e89b-12d3-a456-426614174000");

  assert.equal(calls[0].url, "http://127.0.0.1:8420/api/saved/watch_later");
  assert.deepEqual(JSON.parse(String(calls[0].options.body)), {
    source_platform: "bilibili",
    content_id: "BV1",
    content_url: "https://www.bilibili.com/video/BV1",
    content_type: "video",
    title: "一条视频",
    author_name: "测试 UP",
    cover_url: "",
    note: "",
  });
  assert.equal(calls[1].url, "http://127.0.0.1:8420/api/saved/watch_later/remove");
  assert.deepEqual(JSON.parse(String(calls[1].options.body)), { item_key: "bilibili:BV1" });
  assert.equal(calls[2].url, "http://127.0.0.1:8420/api/saved/watch_later/sync");
  assert.deepEqual(JSON.parse(String(calls[2].options.body)), {
    item_keys: ["bilibili:BV1"],
  });
  assert.equal(
    calls[3].url,
    "http://127.0.0.1:8420/api/saved/watch_later?limit=20&offset=40",
  );
  assert.equal(
    calls[4].url,
    "http://127.0.0.1:8420/api/saved-sync/tasks/123e4567-e89b-12d3-a456-426614174000",
  );
});

test("saved sync UI exposes consentful config, live status, and manual controls", () => {
  const popupHtml = readFileSync(resolve("popup", "popup.html"), "utf8");
  const popupJs = readFileSync(resolve("popup", "popup.js"), "utf8");
  const warning =
    "开启后，在 OpenBiliClaw 点击收藏或稍后再看会修改对应平台账号中的收藏、书签、Saved、播放列表或稍后观看。";

  assert.match(popupHtml, /id="cfgSavedAutoSync"[^>]*type="checkbox"/);
  assert.doesNotMatch(popupHtml, /id="cfgSavedAutoSync"[^>]*checked/);
  assert.match(popupHtml, /保存时自动同步到对应平台/);
  assert.match(popupHtml, /id="watchLaterSyncAll"/);
  assert.match(popupHtml, /id="favoritesSyncAll"/);
  assert.match(popupHtml, /aria-live="polite"/);
  assert.match(popupJs, new RegExp(warning));
  assert.match(popupJs, /Promise\.allSettled/);
  assert.match(popupJs, /本地保存.*同步中.*失败/);
  assert.match(popupJs, /removeSavedItem/);
  assert.doesNotMatch(popupJs, /switch\s*\(\s*[^)]*source_platform/);
});

test("saved sync view model sanitizes task snapshots and groups platform results", () => {
  const task = sanitizeSavedSyncTask({
    task_id: " task-1\u0000 ",
    items: [
      {
        item_key: "bilibili:BV1\u0000",
        status: "synced",
        resolved_target: "B站稍后再看<script>",
        error_message: "ignored\u0000",
      },
      {
        item_key: "youtube:abc",
        status: "extension_required",
        resolved_target: "YouTube Watch Later",
        error_message: "connect\u2028extension",
      },
    ],
  });

  assert.equal(task.task_id, "task-1");
  assert.equal(task.items[0].item_key, "bilibili:BV1");
  assert.equal(task.items[0].resolved_target, "B站稍后再看<script>");
  assert.equal(task.items[1].error_message, "connectextension");
  assert.deepEqual(getSavedSyncPresentation("login_required"), {
    label: "需要登录",
    tone: "warning",
    retryable: true,
    busy: false,
    localOnly: false,
    actionable: true,
    actionLabel: "重试同步",
    detail: "请登录对应平台后重试。",
  });
  assert.deepEqual(getSavedSyncPresentation("already_synced"), {
    label: "已同步",
    tone: "success",
    retryable: false,
    busy: false,
    localOnly: false,
    actionable: false,
    actionLabel: "同步",
    detail: "平台已确认同步完成。",
  });
  assert.deepEqual(getSavedSyncPresentation("unsupported", "unsupported_content_type"), {
    label: "仅本地保存",
    tone: "neutral",
    retryable: false,
    busy: false,
    localOnly: true,
    actionable: false,
    actionLabel: "同步",
    detail: "此内容类型暂不支持平台同步，仅保存在本地。",
  });
  assert.equal(summarizeSavedSyncResults(task.items), "B站 1/1 · YouTube 0/1");
  assert.equal(
    summarizeSavedSyncResults([
      { item_key: "linuxdo:topic:42", status: "unsupported" },
    ]),
    "Linux.do 0/1",
  );
});
