/**
 * Cross-source dispatcher mutex.
 *
 * Bootstrap import tasks can open foreground tabs. Discovery tasks
 * should run in background tabs, but all task bridges still share
 * the same service-worker lifecycle and long-running browser slots.
 * Without coordination, daemon's continuous producers can start at
 * the same moment the user runs a manual fetch, resulting in task
 * tabs racing each other and occasionally grabbing browser focus.
 *
 * This module owns the single source of truth: at any moment, **at
 * most one** dispatcher's task may hold the shared task slot.
 * Each dispatcher acquires before opening its tab and releases when
 * the task completes / fails / times out. If acquire fails (someone
 * else holds the slot), the dispatcher should bail early —
 * the alarm-driven poll will retry the task in 60s.
 *
 * Lives in its own module so both background dispatchers can share
 * one global variable inside the same service-worker process. No
 * persistence — the mutex resets when the service worker restarts. MV3 task
 * tabs can outlive that restart, so later service-worker wiring must reconcile
 * orphan task tabs before claiming fresh work.
 */

interface DispatcherMutexGlobals {
  __OBC_DISPATCHER_MUTEX_HOLDER__?: string;
  __OBC_DISPATCHER_MUTEX_HELD_SINCE__?: number;
}

function mutexGlobals(): DispatcherMutexGlobals {
  return globalThis as DispatcherMutexGlobals;
}

const STALE_HOLD_TIMEOUT_MS = 36 * 60 * 1000;
// Linux.do can legitimately hold the browser slot for up to ~29 minutes when
// three personal scopes are paced conservatively. Keep stale eviction beyond
// that task budget and its result grace; the durable source-task lease remains
// the authority for replay after an actual crash.

/**
 * Try to acquire the cross-source mutex for ``ownerLabel`` (e.g.
 * "xhs" or "dy"). Returns true if acquired (caller should proceed),
 * false if another dispatcher is currently holding (caller should
 * bail; their next alarm tick will retry).
 *
 * Stale holds (older than STALE_HOLD_TIMEOUT_MS) are auto-released
 * to recover from crashed dispatchers.
 */
export function tryAcquireDispatcherMutex(ownerLabel: string): boolean {
  const globals = mutexGlobals();
  const holder = globals.__OBC_DISPATCHER_MUTEX_HOLDER__;
  if (holder) {
    const heldSince = globals.__OBC_DISPATCHER_MUTEX_HELD_SINCE__ ?? 0;
    if (Date.now() - heldSince > STALE_HOLD_TIMEOUT_MS) {
      // Stale hold — previous owner crashed without releasing.
      // eslint-disable-next-line no-console
      console.warn(
        `[OpenBiliClaw] dispatcher-mutex: forcibly evicting stale holder ${holder} (${
          (Date.now() - heldSince) / 1000
        }s old)`,
      );
      globals.__OBC_DISPATCHER_MUTEX_HOLDER__ = undefined;
      globals.__OBC_DISPATCHER_MUTEX_HELD_SINCE__ = undefined;
    } else {
      return false;
    }
  }
  globals.__OBC_DISPATCHER_MUTEX_HOLDER__ = ownerLabel;
  globals.__OBC_DISPATCHER_MUTEX_HELD_SINCE__ = Date.now();
  return true;
}

/**
 * Release the mutex. Idempotent. Releasing a mutex held by someone
 * else is a no-op (logs a warning) — this prevents a buggy dispatcher
 * from yanking the slot from under a healthy peer.
 */
export function releaseDispatcherMutex(ownerLabel: string): void {
  const globals = mutexGlobals();
  const holder = globals.__OBC_DISPATCHER_MUTEX_HOLDER__;
  if (!holder) return;
  if (holder !== ownerLabel) {
    // eslint-disable-next-line no-console
    console.warn(
      `[OpenBiliClaw] dispatcher-mutex: ${ownerLabel} tried to release a slot held by ${holder} — ignoring`,
    );
    return;
  }
  globals.__OBC_DISPATCHER_MUTEX_HOLDER__ = undefined;
  globals.__OBC_DISPATCHER_MUTEX_HELD_SINCE__ = undefined;
}

/** Diagnostic: who currently holds the slot, or null. */
export function dispatcherMutexHolder(): string | null {
  return mutexGlobals().__OBC_DISPATCHER_MUTEX_HOLDER__ ?? null;
}
