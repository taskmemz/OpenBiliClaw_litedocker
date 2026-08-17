import assert from "node:assert/strict";
import test from "node:test";

import {
  dispatcherMutexHolder,
  releaseDispatcherMutex,
  tryAcquireDispatcherMutex,
} from "../src/background/dispatcher-mutex.ts";
import {
  ensureNativeSaveTaskRecovery,
  handleNativeSaveContentResult,
  recoverRecordedNativeSaveTaskTab,
  resetNativeSaveTaskRecoveryForTest,
  runNativeSaveTask,
} from "../src/background/native-save-task-runner.ts";
import type { NativeSaveResult, NativeSaveTask } from "../src/shared/native-save.ts";
import { installChromeMock } from "./helpers/chrome-mock.ts";

const task: NativeSaveTask = {
  id: "123e4567-e89b-12d3-a456-426614174000",
  type: "native_save",
  platform: "reddit",
  platform_slug: "reddit",
  item_key: "reddit:t3_abc",
  content_id: "t3_abc",
  content_url: "https://www.reddit.com/r/test/comments/abc/demo/",
  content_type: "post",
  requested_action: "favorite",
  resolved_action: "favorite",
  target_label: "Reddit Saved",
};

const redditExecutionUrl = "https://old.reddit.com/r/test/comments/abc/demo/";

const tokenizedXhsTask: NativeSaveTask = {
  ...task,
  id: "123e4567-e89b-12d3-a456-426614174001",
  platform: "xiaohongshu",
  platform_slug: "xhs",
  item_key: "xiaohongshu:note-123",
  content_id: "note-123",
  content_url:
    "https://www.xiaohongshu.com/explore/note-123?xsec_token=public-note-token&xsec_source=pc_feed",
  content_type: "note",
  target_label: "小红书收藏",
};

const douyinTask: NativeSaveTask = {
  ...task,
  id: "123e4567-e89b-12d3-a456-426614174002",
  platform: "douyin",
  platform_slug: "dy",
  item_key: "douyin:7300000000000000000",
  content_id: "7300000000000000000",
  content_url: "https://www.douyin.com/video/7300000000000000000",
  content_type: "video",
  target_label: "抖音收藏",
};

function tick(): Promise<void> {
  return new Promise((resolve) => setTimeout(resolve, 0));
}

test("native save runner opens an active allow-listed URL and posts one correlated result", async () => {
  const state = installChromeMock();
  const posted: NativeSaveResult[] = [];
  state.sendMessageImpl = async () => ({ ready: true });
  try {
    const running = runNativeSaveTask(task, "reddit", async (result) => { posted.push(result); }, { timeoutMs: 100 });
    await tick();
    assert.deepEqual(state.createdTabs, [{ active: true, url: redditExecutionUrl }]);
    state.emitRuntimeMessage({ type: "NATIVE_SAVE_RESULT", platform: "reddit", task_id: task.id, item_key: task.item_key, status: "synced" }, { tab: { id: 42, url: task.content_url } });
    state.emitRuntimeMessage({ type: "NATIVE_SAVE_RESULT", platform: "reddit", task_id: task.id, item_key: task.item_key, status: "synced" }, { tab: { id: 42, url: task.content_url } });
    await running;
    assert.deepEqual(posted, [{ task_id: task.id, item_key: task.item_key, status: "synced", error_code: "", error_message: "" }]);
    assert.deepEqual(state.removedTabs, [42]);
    assert.equal(dispatcherMutexHolder(), null);
  } finally {
    state.restore();
  }
});

test("native save runner opens the exact tokenized Xiaohongshu public-note URL", async () => {
  const state = installChromeMock();
  const posted: NativeSaveResult[] = [];
  state.nextCreatedTabStatus = "loading";
  state.sendMessageImpl = async () => ({ ready: true });
  try {
    const running = runNativeSaveTask(
      tokenizedXhsTask,
      "xhs",
      async (result) => { posted.push(result); },
      { timeoutMs: 100 },
    );
    await tick();
    assert.deepEqual(state.createdTabs, [{ active: true, url: tokenizedXhsTask.content_url }]);
    assert.equal(state.sentMessages.length, 1);
    state.emitRuntimeMessage(
      {
        type: "NATIVE_SAVE_RESULT",
        platform: "xiaohongshu",
        task_id: tokenizedXhsTask.id,
        item_key: tokenizedXhsTask.item_key,
        status: "synced",
      },
      { tab: { id: 42, url: tokenizedXhsTask.content_url } },
    );
    await running;
    assert.equal(posted[0]?.status, "synced");
  } finally {
    state.restore();
  }
});

test("native save runner opens Douyin's exact modal route instead of the anti-bot video shell", async () => {
  const state = installChromeMock();
  state.sendMessageImpl = async () => ({ ready: true });
  try {
    const running = runNativeSaveTask(douyinTask, "dy", async () => {}, { timeoutMs: 100 });
    await tick();
    const executionUrl = "https://www.douyin.com/jingxuan?modal_id=7300000000000000000";
    assert.deepEqual(state.createdTabs, [{ active: true, url: executionUrl }]);
    state.emitRuntimeMessage(
      {
        type: "NATIVE_SAVE_RESULT",
        platform: "douyin",
        task_id: douyinTask.id,
        item_key: douyinTask.item_key,
        status: "synced",
      },
      { tab: { id: 42, url: executionUrl } },
    );
    await running;
  } finally {
    state.restore();
  }
});

test("native save runner reloads Douyin once for read-only persisted confirmation", async () => {
  const state = installChromeMock();
  const posted: NativeSaveResult[] = [];
  state.sendMessageImpl = async () => ({ ready: true });
  try {
    const running = runNativeSaveTask(
      douyinTask,
      "dy",
      async (result) => { posted.push(result); },
      { timeoutMs: 100 },
    );
    await tick();
    const executionUrl = "https://www.douyin.com/jingxuan?modal_id=7300000000000000000";
    state.emitRuntimeMessage(
      {
        type: "NATIVE_SAVE_RESULT",
        platform: "douyin",
        task_id: douyinTask.id,
        item_key: douyinTask.item_key,
        status: "failed",
        error_code: "native_confirmation_not_observed",
      },
      { tab: { id: 42, url: executionUrl } },
    );
    await tick();
    assert.deepEqual(state.updatedTabs, [
      { tabId: 42, muted: true },
      { tabId: 42, active: true, url: executionUrl },
    ]);
    assert.equal(
      (state.sentMessages.at(-1)?.message as { verification_only?: unknown }).verification_only,
      true,
    );
    state.emitRuntimeMessage(
      {
        type: "NATIVE_SAVE_RESULT",
        platform: "douyin",
        task_id: douyinTask.id,
        item_key: douyinTask.item_key,
        status: "already_synced",
      },
      { tab: { id: 42, url: executionUrl } },
    );
    await running;
    assert.equal(posted[0]?.status, "already_synced");
    assert.deepEqual(state.removedTabs, [42]);
  } finally {
    state.restore();
  }
});

test("native save runner reloads Xiaohongshu once for read-only persisted confirmation", async () => {
  const state = installChromeMock();
  const posted: NativeSaveResult[] = [];
  state.sendMessageImpl = async () => ({ ready: true });
  try {
    const running = runNativeSaveTask(
      tokenizedXhsTask,
      "xhs",
      async (result) => { posted.push(result); },
      { timeoutMs: 100 },
    );
    await tick();
    state.emitRuntimeMessage(
      {
        type: "NATIVE_SAVE_RESULT",
        platform: "xiaohongshu",
        task_id: tokenizedXhsTask.id,
        item_key: tokenizedXhsTask.item_key,
        status: "failed",
        error_code: "native_confirmation_not_observed",
      },
      { tab: { id: 42, url: tokenizedXhsTask.content_url } },
    );
    await tick();
    assert.deepEqual(state.updatedTabs, [
      { tabId: 42, muted: true },
      {
        tabId: 42,
        active: true,
        url: tokenizedXhsTask.content_url,
      },
    ]);
    assert.equal(
      (state.sentMessages.at(-1)?.message as { verification_only?: unknown }).verification_only,
      true,
    );
    state.emitRuntimeMessage(
      {
        type: "NATIVE_SAVE_RESULT",
        platform: "xiaohongshu",
        task_id: tokenizedXhsTask.id,
        item_key: tokenizedXhsTask.item_key,
        status: "already_synced",
      },
      { tab: { id: 42, url: tokenizedXhsTask.content_url } },
    );
    await running;
    assert.equal(posted[0]?.status, "already_synced");
  } finally {
    state.restore();
  }
});

test("native save runner reuses one exact Xiaohongshu note tab without closing it", async () => {
  const state = installChromeMock();
  const posted: NativeSaveResult[] = [];
  const exactUrl = "https://www.xiaohongshu.com/explore/note-123";
  state.queryResult = [
    { id: 70, status: "complete", url: exactUrl },
    { id: 71, status: "complete", url: "https://www.xiaohongshu.com/explore" },
  ];
  state.tabById.set(70, state.queryResult[0]);
  state.tabById.set(71, state.queryResult[1]);
  state.sendMessageImpl = async () => ({ ready: true });
  assert.equal(tryAcquireDispatcherMutex("legacy-discovery"), true);
  try {
    const running = runNativeSaveTask(
      tokenizedXhsTask,
      "xhs",
      async (result) => { posted.push(result); },
      { timeoutMs: 100 },
    );
    await tick();
    assert.deepEqual(state.createdTabs, []);
    assert.deepEqual(state.updatedTabs, [{ tabId: 70, active: true }]);
    assert.equal(state.sentMessages.length, 1);
    assert.deepEqual(state.sessionStorage, {});
    state.emitRuntimeMessage(
      {
        type: "NATIVE_SAVE_RESULT",
        platform: "xiaohongshu",
        task_id: tokenizedXhsTask.id,
        item_key: tokenizedXhsTask.item_key,
        status: "already_synced",
      },
      { tab: { id: 70, url: exactUrl } },
    );
    await running;
    assert.equal(posted[0]?.status, "already_synced");
    assert.deepEqual(state.removedTabs, []);
  } finally {
    releaseDispatcherMutex("legacy-discovery");
    state.restore();
  }
});

test("native save runner opens Xiaohongshu immediately while legacy discovery holds the mutex", async () => {
  const state = installChromeMock();
  const posted: NativeSaveResult[] = [];
  state.sendMessageImpl = async () => ({ ready: true });
  assert.equal(tryAcquireDispatcherMutex("legacy-discovery"), true);
  try {
    const running = runNativeSaveTask(
      tokenizedXhsTask,
      "xhs",
      async (result) => { posted.push(result); },
      { timeoutMs: 100, mutexRetryMs: 1 },
    );
    await tick();
    await tick();
    assert.deepEqual(state.createdTabs, [{
      active: true,
      url: tokenizedXhsTask.content_url,
    }]);
    state.emitRuntimeMessage(
      {
        type: "NATIVE_SAVE_RESULT",
        platform: "xiaohongshu",
        task_id: tokenizedXhsTask.id,
        item_key: tokenizedXhsTask.item_key,
        status: "already_synced",
      },
      { tab: { id: 42, url: tokenizedXhsTask.content_url } },
    );
    await running;
    assert.equal(posted[0]?.status, "already_synced");
    assert.equal(dispatcherMutexHolder(), "legacy-discovery");
  } finally {
    releaseDispatcherMutex("legacy-discovery");
    state.restore();
  }
});

test("native save runner executes two platforms concurrently with independent correlation", async () => {
  const state = installChromeMock();
  const posted: NativeSaveResult[] = [];
  state.sendMessageImpl = async () => ({ ready: true });
  try {
    const redditRun = runNativeSaveTask(
      task,
      "reddit",
      async (result) => { posted.push(result); },
      { timeoutMs: 100, mutexRetryMs: 1 },
    );
    const xhsRun = runNativeSaveTask(
      tokenizedXhsTask,
      "xhs",
      async (result) => { posted.push(result); },
      { timeoutMs: 100, mutexRetryMs: 1 },
    );
    await tick();
    await tick();
    assert.deepEqual(state.createdTabs, [
      { active: true, url: redditExecutionUrl },
      { active: true, url: tokenizedXhsTask.content_url },
    ]);
    assert.deepEqual(state.sessionStorage, {
      openbiliclaw_native_save_task_tab_id: [42, 43],
    });
    state.emitRuntimeMessage(
      {
        type: "NATIVE_SAVE_RESULT",
        platform: "reddit",
        task_id: task.id,
        item_key: task.item_key,
        status: "synced",
      },
      { tab: { id: 42, url: task.content_url } },
    );
    state.emitRuntimeMessage(
      {
        type: "NATIVE_SAVE_RESULT",
        platform: "xiaohongshu",
        task_id: tokenizedXhsTask.id,
        item_key: tokenizedXhsTask.item_key,
        status: "already_synced",
      },
      { tab: { id: 43, url: tokenizedXhsTask.content_url } },
    );
    await Promise.all([redditRun, xhsRun]);
    assert.equal(posted.length, 2);
    assert.deepEqual(state.removedTabs.sort((a, b) => a - b), [42, 43]);
  } finally {
    state.restore();
  }
});

test("native save runner retries readiness but never retries the mutation", async () => {
  const state = installChromeMock();
  let attempts = 0;
  state.sendMessageImpl = async () => {
    attempts += 1;
    if (attempts < 3) throw new Error("Receiving end does not exist");
    return { ready: true };
  };
  try {
    const running = runNativeSaveTask(task, "reddit", async () => {}, { timeoutMs: 150, readinessRetryMs: 1 });
    await tick();
    await tick();
    await tick();
    assert.equal(state.sentMessages.length, 3);
    state.emitRuntimeMessage({ type: "NATIVE_SAVE_RESULT", platform: "reddit", task_id: task.id, item_key: task.item_key, status: "already_synced" }, { tab: { id: 42, url: task.content_url } });
    await running;
    assert.equal(state.sentMessages.length, 3);
  } finally {
    state.restore();
  }
});

test("native save runner ignores mismatched tab, platform, task ID, and item key", async () => {
  const state = installChromeMock();
  const posted: NativeSaveResult[] = [];
  try {
    const running = runNativeSaveTask(task, "reddit", async (result) => { posted.push(result); }, { timeoutMs: 100 });
    await tick();
    const base = { type: "NATIVE_SAVE_RESULT", platform: "reddit", task_id: task.id, item_key: task.item_key, status: "synced" };
    state.emitRuntimeMessage(base, { tab: { id: 99, url: task.content_url } });
    state.emitRuntimeMessage({ ...base, platform: "twitter" }, { tab: { id: 42, url: task.content_url } });
    state.emitRuntimeMessage({ ...base, task_id: "wrong" }, { tab: { id: 42, url: task.content_url } });
    state.emitRuntimeMessage({ ...base, item_key: "reddit:t3_wrong" }, { tab: { id: 42, url: task.content_url } });
    state.emitRuntimeMessage(base, { tab: { id: 42, url: "https://evil.example/redirect" } });
    state.emitRuntimeMessage(base, { url: "https://evil.example/frame", tab: { id: 42, url: task.content_url } });
    assert.equal(handleNativeSaveContentResult(base), false);
    assert.equal(posted.length, 0);
    assert.equal(
      handleNativeSaveContentResult(base, { tab: { id: 42, url: task.content_url } } as chrome.runtime.MessageSender),
      true,
    );
    await running;
  } finally {
    state.restore();
  }
});

test("native save timeout posts the fixed safe failure and releases all resources", async () => {
  const state = installChromeMock();
  const posted: NativeSaveResult[] = [];
  state.sendMessageImpl = async () => { throw new Error("no receiver"); };
  try {
    await runNativeSaveTask(task, "reddit", async (result) => { posted.push(result); }, { timeoutMs: 5, readinessRetryMs: 1 });
    assert.deepEqual(posted, [{ task_id: task.id, item_key: task.item_key, status: "failed", error_code: "native_save_timeout", error_message: "Platform native-save task timed out" }]);
    assert.deepEqual(state.removedTabs, [42]);
    assert.equal(dispatcherMutexHolder(), null);
    assert.ok(state.sentMessages.length >= 1);
    assert.equal(state.runtimeListenerCount(), 0);
    assert.equal(state.tabUpdatedListenerCount(), 0);
    assert.equal(handleNativeSaveContentResult(
      { type: "NATIVE_SAVE_RESULT", platform: "reddit", task_id: task.id, item_key: task.item_key, status: "synced" },
      { url: task.content_url, tab: { id: 42, url: task.content_url } } as chrome.runtime.MessageSender,
    ), false);
    assert.equal(posted.length, 1);
  } finally {
    state.restore();
  }
});

test("native save runner posts a safe failure when tab creation throws", async () => {
  const state = installChromeMock();
  const posted: NativeSaveResult[] = [];
  state.createImpl = async () => { throw new Error("create failed"); };
  try {
    await runNativeSaveTask(task, "reddit", async (result) => { posted.push(result); }, { timeoutMs: 20 });
    assert.equal(posted[0]?.error_code, "native_save_failed");
    assert.equal(state.runtimeListenerCount(), 0);
    assert.equal(dispatcherMutexHolder(), null);
  } finally {
    state.restore();
  }
});

test("native save runner records only its tab identity and clears it on normal cleanup", async () => {
  const state = installChromeMock();
  try {
    const running = runNativeSaveTask(task, "reddit", async () => {}, { timeoutMs: 50 });
    await tick();
    assert.deepEqual(state.sessionStorage, { openbiliclaw_native_save_task_tab_id: 42 });
    state.emitRuntimeMessage(
      { type: "NATIVE_SAVE_RESULT", platform: "reddit", task_id: task.id, item_key: task.item_key, status: "synced" },
      { url: task.content_url, tab: { id: 42, url: task.content_url } },
    );
    await running;
    assert.deepEqual(state.sessionStorage, {});
    assert.deepEqual(state.removedTabs, [42]);
  } finally {
    state.restore();
  }
});

test("native save restart recovery closes all and only recorded orphans", async () => {
  const state = installChromeMock();
  state.sessionStorage.openbiliclaw_native_save_task_tab_id = [77, 78];
  state.tabById.set(77, { id: 77, url: "https://x.com/i/status/123", status: "complete" });
  state.tabById.set(78, { id: 78, url: "https://www.youtube.com/watch?v=abc", status: "complete" });
  state.tabById.set(88, { id: 88, url: "https://www.reddit.com/r/test/", status: "complete" });
  try {
    await recoverRecordedNativeSaveTaskTab();
    assert.deepEqual(state.removedTabs, [77, 78]);
    assert.equal(state.tabById.has(88), true);
    assert.deepEqual(state.sessionStorage, {});
  } finally {
    state.restore();
  }
});

test("native save recovery shares one idempotent promise across concurrent startup calls", async () => {
  const state = installChromeMock();
  let resolveGet!: (value: Record<string, unknown>) => void;
  let getCalls = 0;
  state.sessionGetImpl = async () => {
    getCalls += 1;
    if (getCalls > 1) return { openbiliclaw_native_save_task_tab_id: 77 };
    return new Promise((resolve) => { resolveGet = resolve; });
  };
  resetNativeSaveTaskRecoveryForTest();
  try {
    const startup = ensureNativeSaveTaskRecovery();
    const installed = ensureNativeSaveTaskRecovery();
    assert.equal(startup, installed);
    assert.equal(getCalls, 1);
    resolveGet({ openbiliclaw_native_save_task_tab_id: 77 });
    await Promise.all([startup, installed]);
    assert.deepEqual(state.removedTabs, [77]);
  } finally {
    resetNativeSaveTaskRecoveryForTest();
    state.restore();
  }
});

test("native save runner continues when storage.session is absent", async () => {
  const state = installChromeMock();
  const posted: NativeSaveResult[] = [];
  delete (chrome.storage as { session?: chrome.storage.StorageArea }).session;
  resetNativeSaveTaskRecoveryForTest();
  try {
    const running = runNativeSaveTask(task, "reddit", async (result) => { posted.push(result); }, { timeoutMs: 50 });
    await tick();
    state.emitRuntimeMessage(
      { type: "NATIVE_SAVE_RESULT", platform: "reddit", task_id: task.id, item_key: task.item_key, status: "synced" },
      { url: task.content_url, tab: { id: 42, url: task.content_url } },
    );
    await running;
    assert.equal(posted[0]?.status, "synced");
  } finally {
    resetNativeSaveTaskRecoveryForTest();
    state.restore();
  }
});

test("native save runner continues when storage.session throws", async () => {
  const state = installChromeMock();
  const posted: NativeSaveResult[] = [];
  state.sessionGetImpl = async () => { throw new Error("session get unavailable"); };
  state.sessionSetImpl = async () => { throw new Error("session set unavailable"); };
  state.sessionRemoveImpl = async () => { throw new Error("session remove unavailable"); };
  resetNativeSaveTaskRecoveryForTest();
  try {
    const running = runNativeSaveTask(task, "reddit", async (result) => { posted.push(result); }, { timeoutMs: 50 });
    await tick();
    state.emitRuntimeMessage(
      { type: "NATIVE_SAVE_RESULT", platform: "reddit", task_id: task.id, item_key: task.item_key, status: "synced" },
      { url: task.content_url, tab: { id: 42, url: task.content_url } },
    );
    await running;
    assert.equal(posted[0]?.status, "synced");
  } finally {
    resetNativeSaveTaskRecoveryForTest();
    state.restore();
  }
});

test("native save runner closes a tab whose creation resolves after the deadline", async () => {
  const state = installChromeMock();
  const posted: NativeSaveResult[] = [];
  let resolveCreate!: (tab: { id: number; status: string; url: string }) => void;
  state.createImpl = async (opts) => {
    state.createdTabs.push(opts);
    return new Promise((resolve) => { resolveCreate = resolve; });
  };
  try {
    await runNativeSaveTask(task, "reddit", async (result) => { posted.push(result); }, {
      timeoutMs: 5,
    });
    assert.equal(posted[0]?.error_code, "native_save_timeout");
    assert.deepEqual(state.removedTabs, []);
    resolveCreate({ id: 77, status: "complete", url: task.content_url });
    await tick();
    await tick();
    assert.deepEqual(state.removedTabs, [77]);
    assert.equal(dispatcherMutexHolder(), null);
  } finally {
    state.restore();
  }
});

test("native save runner fences tab-update listener add and remove failures", async () => {
  const addState = installChromeMock();
  const addNormally = addState.tabUpdatedAddListenerImpl;
  addState.nextCreatedTabStatus = "loading";
  addState.tabUpdatedAddListenerImpl = (listener) => {
    addNormally(listener);
    throw new Error("tab update add failed");
  };
  try {
    await runNativeSaveTask(task, "reddit", async () => {}, { timeoutMs: 20 });
    assert.equal(addState.tabUpdatedListenerCount(), 0);
    assert.deepEqual(addState.removedTabs, [42]);
    assert.equal(dispatcherMutexHolder(), null);
  } finally {
    addState.restore();
  }

  const removeState = installChromeMock();
  const removeNormally = removeState.tabUpdatedRemoveListenerImpl;
  let removeAttempts = 0;
  removeState.nextCreatedTabStatus = "loading";
  removeState.tabUpdatedRemoveListenerImpl = (listener) => {
    removeAttempts += 1;
    if (removeAttempts === 1) throw new Error("tab update remove failed");
    removeNormally(listener);
  };
  try {
    const running = runNativeSaveTask(task, "reddit", async () => {}, { timeoutMs: 50 });
    await tick();
    removeState.emitTabUpdated(42, { status: "complete" });
    await tick();
    removeState.emitRuntimeMessage(
      { type: "NATIVE_SAVE_RESULT", platform: "reddit", task_id: task.id, item_key: task.item_key, status: "synced" },
      { url: task.content_url, tab: { id: 42, url: task.content_url } },
    );
    await running;
    assert.equal(removeState.tabUpdatedListenerCount(), 0);
    assert.deepEqual(removeState.removedTabs, [42]);
    assert.equal(dispatcherMutexHolder(), null);
  } finally {
    removeState.restore();
  }
});

test("native save runner contains listener registration and removal failures", async () => {
  const addState = installChromeMock();
  const addPosted: NativeSaveResult[] = [];
  const addNormally = addState.runtimeAddListenerImpl;
  addState.runtimeAddListenerImpl = (listener) => {
    addNormally(listener);
    throw new Error("add listener failed");
  };
  try {
    await runNativeSaveTask(task, "reddit", async (result) => { addPosted.push(result); }, { timeoutMs: 20 });
    assert.equal(addPosted[0]?.error_code, "native_save_failed");
    assert.deepEqual(addState.removedTabs, [42]);
    assert.equal(addState.runtimeListenerCount(), 0);
    assert.equal(dispatcherMutexHolder(), null);
  } finally {
    releaseNativeMutexForTest();
    addState.restore();
  }

  const removeState = installChromeMock();
  const removePosted: NativeSaveResult[] = [];
  const removeNormally = removeState.runtimeRemoveListenerImpl;
  let removeAttempts = 0;
  removeState.runtimeRemoveListenerImpl = (listener) => {
    removeAttempts += 1;
    if (removeAttempts === 1) throw new Error("remove listener failed");
    removeNormally(listener);
  };
  try {
    const running = runNativeSaveTask(task, "reddit", async (result) => { removePosted.push(result); }, { timeoutMs: 50 });
    await tick();
    removeState.emitRuntimeMessage(
      { type: "NATIVE_SAVE_RESULT", platform: "reddit", task_id: task.id, item_key: task.item_key, status: "synced" },
      { url: task.content_url, tab: { id: 42, url: task.content_url } },
    );
    await running;
    assert.equal(removePosted.length, 1);
    assert.deepEqual(removeState.removedTabs, [42]);
    assert.equal(removeState.runtimeListenerCount(), 0);
    assert.equal(dispatcherMutexHolder(), null);
  } finally {
    releaseNativeMutexForTest();
    removeState.restore();
  }
});

test("native save runner posts and cleans a safe failure when tab inspection throws", async () => {
  const state = installChromeMock();
  const posted: NativeSaveResult[] = [];
  state.getImpl = async () => { throw new Error("get failed"); };
  try {
    await runNativeSaveTask(task, "reddit", async (result) => { posted.push(result); }, { timeoutMs: 20 });
    assert.equal(posted[0]?.error_code, "native_save_failed");
    assert.deepEqual(state.removedTabs, [42]);
    assert.equal(state.tabUpdatedListenerCount(), 0);
    assert.equal(state.runtimeListenerCount(), 0);
    assert.equal(dispatcherMutexHolder(), null);
  } finally {
    state.restore();
  }
});

test("native save runner cleans independently when result posting or tab removal fails", async () => {
  const state = installChromeMock();
  state.removeImpl = async () => { throw new Error("remove tab failed"); };
  try {
    const running = runNativeSaveTask(task, "reddit", async () => { throw new Error("post failed"); }, { timeoutMs: 50 });
    await tick();
    state.emitRuntimeMessage(
      { type: "NATIVE_SAVE_RESULT", platform: "reddit", task_id: task.id, item_key: task.item_key, status: "synced" },
      { url: task.content_url, tab: { id: 42, url: task.content_url } },
    );
    await assert.rejects(running, /post failed/);
    assert.equal(state.runtimeListenerCount(), 0);
    assert.equal(state.tabUpdatedListenerCount(), 0);
    assert.equal(dispatcherMutexHolder(), null);
  } finally {
    releaseNativeMutexForTest();
    state.restore();
  }
});

function releaseNativeMutexForTest(): void {
  const globals = globalThis as typeof globalThis & {
    __OBC_DISPATCHER_MUTEX_HOLDER__?: string;
    __OBC_DISPATCHER_MUTEX_HELD_SINCE__?: number;
  };
  globals.__OBC_DISPATCHER_MUTEX_HOLDER__ = undefined;
  globals.__OBC_DISPATCHER_MUTEX_HELD_SINCE__ = undefined;
  releaseDispatcherMutex("native-save:reddit");
}

test("native save runner rejects a mismatched slug and times out once behind the legacy mutex", async () => {
  const state = installChromeMock();
  const posted: NativeSaveResult[] = [];
  const globals = globalThis as typeof globalThis & {
    __OBC_DISPATCHER_MUTEX_HOLDER__?: string;
    __OBC_DISPATCHER_MUTEX_HELD_SINCE__?: number;
  };
  try {
    await assert.rejects(runNativeSaveTask(task, "x", async () => {}), /platform slug/);
    globals.__OBC_DISPATCHER_MUTEX_HOLDER__ = "legacy-xhs";
    globals.__OBC_DISPATCHER_MUTEX_HELD_SINCE__ = Date.now();
    await runNativeSaveTask(task, "reddit", async (result) => { posted.push(result); }, {
      timeoutMs: 5,
      mutexRetryMs: 1,
    });
    assert.deepEqual(state.createdTabs, []);
    assert.equal(globals.__OBC_DISPATCHER_MUTEX_HOLDER__, "legacy-xhs");
    assert.deepEqual(posted, [{
      task_id: task.id,
      item_key: task.item_key,
      status: "failed",
      error_code: "native_save_timeout",
      error_message: "Platform native-save task timed out",
    }]);
  } finally {
    globals.__OBC_DISPATCHER_MUTEX_HOLDER__ = undefined;
    globals.__OBC_DISPATCHER_MUTEX_HELD_SINCE__ = undefined;
    state.restore();
  }
});

test("native save runner waits for the legacy mutex within the same deadline", async () => {
  const state = installChromeMock();
  const globals = globalThis as typeof globalThis & {
    __OBC_DISPATCHER_MUTEX_HOLDER__?: string;
    __OBC_DISPATCHER_MUTEX_HELD_SINCE__?: number;
  };
  globals.__OBC_DISPATCHER_MUTEX_HOLDER__ = "legacy-yt";
  globals.__OBC_DISPATCHER_MUTEX_HELD_SINCE__ = Date.now();
  try {
    const running = runNativeSaveTask(task, "reddit", async () => {}, {
      timeoutMs: 100,
      mutexRetryMs: 1,
    });
    await tick();
    assert.deepEqual(state.createdTabs, []);
    globals.__OBC_DISPATCHER_MUTEX_HOLDER__ = undefined;
    globals.__OBC_DISPATCHER_MUTEX_HELD_SINCE__ = undefined;
    await tick();
    await tick();
    state.emitRuntimeMessage(
      { type: "NATIVE_SAVE_RESULT", platform: "reddit", task_id: task.id, item_key: task.item_key, status: "synced" },
      { url: task.content_url, tab: { id: 42, url: task.content_url } },
    );
    await running;
    assert.equal(dispatcherMutexHolder(), null);
    assert.equal(state.createdTabs.length, 1);
  } finally {
    globals.__OBC_DISPATCHER_MUTEX_HOLDER__ = undefined;
    globals.__OBC_DISPATCHER_MUTEX_HELD_SINCE__ = undefined;
    state.restore();
  }
});

test("native save runner rejects a redirected final tab before execution", async () => {
  const state = installChromeMock();
  const posted: NativeSaveResult[] = [];
  state.getImpl = async (tabId) => ({ id: tabId, status: "complete", url: "https://evil.example/" });
  try {
    await runNativeSaveTask(task, "reddit", async (result) => { posted.push(result); }, { timeoutMs: 20 });
    assert.deepEqual(state.sentMessages, []);
    assert.equal(posted[0]?.status, "failed");
    assert.equal(posted[0]?.error_code, "native_save_failed");
  } finally {
    state.restore();
  }
});

test("native save runner closes the tab-load get/listener race", async () => {
  const state = installChromeMock();
  state.nextCreatedTabStatus = "loading";
  let gets = 0;
  state.getImpl = async (tabId) => {
    gets += 1;
    if (gets === 1) {
      state.emitTabUpdated(tabId, { status: "complete" });
      return { id: tabId, status: "loading", url: task.content_url };
    }
    return { id: tabId, status: "complete", url: task.content_url };
  };
  try {
    const running = runNativeSaveTask(task, "reddit", async () => {}, { timeoutMs: 100 });
    await tick();
    await tick();
    const sentBeforeRecovery = state.sentMessages.length;
    state.emitTabUpdated(42, { status: "complete" });
    await tick();
    state.emitRuntimeMessage(
      { type: "NATIVE_SAVE_RESULT", platform: "reddit", task_id: task.id, item_key: task.item_key, status: "synced" },
      { url: task.content_url, tab: { id: 42, url: task.content_url } },
    );
    await running;
    assert.equal(sentBeforeRecovery, 1);
    assert.equal(state.tabUpdatedListenerCount(), 0);
  } finally {
    state.restore();
  }
});
