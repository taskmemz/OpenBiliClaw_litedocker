import test from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { resolve } from "node:path";

test("Weibo bootstrap task is same-origin and credential-free", () => {
  const executor = readFileSync(
    resolve(import.meta.dirname, "../src/content/weibo/task-executor.ts"),
    "utf8",
  );
  const dispatcher = readFileSync(
    resolve(import.meta.dirname, "../src/background/weibo-task-dispatcher.ts"),
    "utf8",
  );
  const contentEntry = readFileSync(
    resolve(import.meta.dirname, "../src/content/weibo.ts"),
    "utf8",
  );

  assert.match(executor, /credentials:\s*["']include["']/);
  assert.match(executor, /weibo_login_required/);
  assert.match(executor, /weibo_identity_required/);
  assert.match(executor, /explicitLogin === false/);
  assert.match(executor, /upstream_error/);
  assert.match(executor, /ok !== 1/);
  assert.match(executor, /comments\/to_me/);
  assert.match(executor, /containerid=230259/);
  assert.match(executor, /message\/mentionsAt/);
  assert.match(executor, /message\/mentionsCmt/);
  assert.match(executor, /weibo_favorites/);
  assert.match(executor, /weibo_following/);
  assert.match(executor, /weibo_mentions/);
  assert.match(executor, /claim_token/);
  assert.doesNotMatch(executor, /chrome\.cookies/);
  assert.doesNotMatch(executor, /document\.cookie/);
  assert.match(dispatcher, /\/sources\/weibo\/next-task/);
  assert.match(dispatcher, /\/sources\/weibo\/task-result/);
  assert.match(dispatcher, /task_claim_conflict|claim_token/);
  assert.match(contentEntry, /isWeiboTaskTabLocation/);
});
