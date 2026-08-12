/** V2EX content script entry point. */

import { startCollector } from "./kernel.js";
import { installV2EXMessageListener } from "./v2ex/task-executor.ts";
import { isV2EXTaskTabLocation } from "./v2ex/task-mode.ts";
import { v2exAdapter } from "../shared/platforms/v2ex.ts";

if (!isV2EXTaskTabLocation()) {
  startCollector(v2exAdapter);
}
installV2EXMessageListener();
