import { readFileSync } from "node:fs";
import { resolve } from "node:path";
import test from "node:test";
import assert from "node:assert/strict";

test("runtime stream connection gates concurrent async health probes", () => {
  const source = readFileSync(resolve("src", "background", "service-worker.ts"), "utf8");
  const connectStart = source.indexOf("async function connectRuntimeStream");
  const connectEnd = source.indexOf("function scheduleWsReconnect", connectStart);
  const connectBlock = source.slice(connectStart, connectEnd);

  assert.match(source, /let runtimeConnectInFlight = false;/);
  assert.match(connectBlock, /runtimeSocket !== null \|\| runtimeConnectInFlight/);

  const markInFlight = connectBlock.indexOf("runtimeConnectInFlight = true");
  const healthProbe = connectBlock.indexOf("await isBackendAlive");
  assert.ok(markInFlight >= 0, "connectRuntimeStream should mark connection as in-flight");
  assert.ok(
    markInFlight < healthProbe,
    "connectRuntimeStream should mark in-flight before awaiting the health probe",
  );

  assert.match(connectBlock, /finally \{\s*runtimeConnectInFlight = false;\s*\}/);
});

test("service worker starts platform task polling during hot reload bootstrap", () => {
  const source = readFileSync(resolve("src", "background", "service-worker.ts"), "utf8");
  const bootstrapStart = source.indexOf("ensureFlushAlarm();", source.indexOf("chrome.notifications"));
  const bootstrapEnd = source.indexOf("onBackendEndpointChange", bootstrapStart);
  const bootstrapBlock = source.slice(bootstrapStart, bootstrapEnd);

  assert.match(source, /function startPlatformTaskPolling\(\): void \{/);
  assert.match(source, /async function startServiceWorkerAfterRecovery/);
  assert.match(bootstrapBlock, /startServiceWorkerAfterRecovery\(\);/);
  const initializeStart = source.indexOf("async function startServiceWorkerAfterRecovery");
  const initializeEnd = source.indexOf("chrome.runtime.onInstalled", initializeStart);
  const initializeBlock = source.slice(initializeStart, initializeEnd);
  assert.ok(
    initializeBlock.indexOf("const runtimeStreamReady = connectRuntimeStream()") <
      initializeBlock.indexOf("await ensureLinuxdoTaskRecovery()"),
  );
  assert.ok(initializeBlock.indexOf("await ensureNativeSaveTaskRecovery()") < initializeBlock.indexOf("startPlatformTaskPolling()"));
  assert.match(initializeBlock, /startCookieSync\(\);/);
});

test("background runtime stream reconnect uses a fixed high-frequency interval", () => {
  const source = readFileSync(resolve("src", "background", "service-worker.ts"), "utf8");
  const scheduleStart = source.indexOf("function scheduleWsReconnect");
  const scheduleEnd = source.indexOf("// ---------------------------------------------------------------------------", scheduleStart);
  const scheduleBlock = source.slice(scheduleStart, scheduleEnd);

  assert.match(source, /const WS_RECONNECT_DELAY = 1_000;/);
  assert.doesNotMatch(source, /WS_RECONNECT_MAX_DELAY/);
  assert.doesNotMatch(source, /wsReconnectDelay/);
  assert.doesNotMatch(source, /Math\.min\(wsReconnectDelay \* 2/);
  assert.match(scheduleBlock, /}, WS_RECONNECT_DELAY\);/);
});

test("background runtime stream passes an explicit short session", () => {
  const source = readFileSync(resolve("src", "background", "service-worker.ts"), "utf8");
  assert.match(
    source,
    /wsUrl\("\/runtime-stream\?client=background", await ensureSession\(\)\)/,
  );
  assert.match(source, /clearSession\(\)\.then\(\(\) => connectRuntimeStream\(\)\)/);
});

test("service worker wires X polling, alarm, and immediate task wake", () => {
  const source = readFileSync(resolve("src", "background", "service-worker.ts"), "utf8");
  assert.match(source, /startXTaskPolling/);
  assert.match(source, /void handleXTaskAlarm\(alarm\.name\)/);
  assert.match(source, /eventType === "x_task_available"/);
  assert.match(source, /await pollXTaskNow\(\)/);
});

test("service worker recovers only the recorded native runner orphan on evaluation and lifecycle", () => {
  const source = readFileSync(resolve("src", "background", "service-worker.ts"), "utf8");
  assert.match(source, /ensureNativeSaveTaskRecovery/);
  assert.match(source, /await ensureNativeSaveTaskRecovery\(\);/);
  const installed = source.slice(source.indexOf("chrome.runtime.onInstalled"), source.indexOf("chrome.runtime.onStartup"));
  const startup = source.slice(source.indexOf("chrome.runtime.onStartup"), source.indexOf("chrome.action.onClicked"));
  assert.match(installed, /startServiceWorkerAfterRecovery/);
  assert.match(startup, /startServiceWorkerAfterRecovery/);
  assert.doesNotMatch(source, /tabs\.query\([^)]*(?:reddit|twitter|x\.com)/s);
});
