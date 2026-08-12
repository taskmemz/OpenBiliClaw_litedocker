/** Linux.do content script entry point. */

import { startCollector } from "./kernel.js";
import { installLinuxdoMessageListener } from "./linuxdo/task-executor.ts";
import { isLinuxdoTaskTabLocation } from "./linuxdo/task-mode.ts";
import { linuxdoAdapter } from "../shared/platforms/linuxdo.ts";

if (!isLinuxdoTaskTabLocation()) {
  startCollector(linuxdoAdapter);
}
installLinuxdoMessageListener();
