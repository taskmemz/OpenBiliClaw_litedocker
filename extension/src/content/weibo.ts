/** Weibo content entry: task listener only; normal browsing is not collected. */

import { installWeiboMessageListener } from "./weibo/task-executor.ts";
import { isWeiboTaskTabLocation } from "./weibo/task-mode.ts";

// Ordinary Weibo browsing gets no task listener at all.  The background
// dispatcher owns the explicit query-marked task tab, so a stray runtime
// message cannot turn a user's normal page into a data-collection surface.
if (isWeiboTaskTabLocation()) installWeiboMessageListener();
