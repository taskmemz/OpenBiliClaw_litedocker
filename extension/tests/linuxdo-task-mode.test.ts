import test from "node:test";
import assert from "node:assert/strict";

import {
  isLinuxdoTaskTabLocation,
  LINUXDO_TASK_TAB_URL,
} from "../src/content/linuxdo/task-mode.ts";

test("Linux.do task tab uses a stable query marker and recognizes legacy hash markers", () => {
  assert.equal(LINUXDO_TASK_TAB_URL, "https://linux.do/?openbiliclaw_linuxdo_task=1");
  assert.equal(isLinuxdoTaskTabLocation({ hash: "#openbiliclaw_linuxdo_task=1" }), true);
  assert.equal(isLinuxdoTaskTabLocation({ search: "?openbiliclaw_linuxdo_task=1" }), true);
  assert.equal(isLinuxdoTaskTabLocation({ href: "https://linux.do/latest" }), false);
});
