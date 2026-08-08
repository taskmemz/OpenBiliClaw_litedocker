import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { resolve } from "node:path";
import test from "node:test";

function readProjectFile(path: string): string {
  return readFileSync(resolve("..", path), "utf8");
}

test("runtime stream refresh handlers coalesce expensive frontend reloads", () => {
  const popupJs = readFileSync(resolve("popup", "popup.js"), "utf8");
  const desktopJs = readProjectFile("src/openbiliclaw/web/desktop/assets/js/app.js");
  const mobileRecommendJs = readProjectFile("src/openbiliclaw/web/js/views/recommend.js");
  const mobileProfileJs = readProjectFile("src/openbiliclaw/web/js/views/profile.js");

  assert.match(popupJs, /function scheduleRecommendationsRefresh/);
  assert.match(popupJs, /function scheduleActivityFeedRefresh/);
  assert.match(popupJs, /recommendationsRefreshInFlight/);
  assert.match(popupJs, /activityFeedRefreshInFlight/);
  assert.doesNotMatch(
    popupJs,
    /if \(event\.type === "activity\.added"\) \{\s*void loadActivityFeed\(\);\s*\}/,
  );
  assert.doesNotMatch(
    popupJs,
    /if \(event\.type === "refresh\.pool_updated"\) \{\s*void initializeRecommendations\(\);\s*\}/,
  );
  assert.doesNotMatch(
    popupJs,
    /if \(event\.type === "refresh\.pool_updated"\) \{[\s\S]*?scheduleRecommendationsRefresh\(/,
  );
  assert.match(popupJs, /function renderReadyRecommendationHint\(\)/);
  assert.match(
    popupJs,
    /renderPoolStatus\(state\.runtimeStatus\);\s*if \(runtimeEventCarriesPoolCounts\(event\)\) \{\s*renderReadyRecommendationHint\(\);\s*\}/,
  );
  const initializeBlock =
    popupJs.match(/async function initializeRecommendations\(\) \{[\s\S]*?\n\}/)?.[0] ?? "";
  assert.notEqual(initializeBlock, "", "popup should initialize recommendations through one function");
  assert.match(
    initializeBlock,
    /state\.runtimeStatus = await fetchRuntimeStatus\(\)\.catch\(\(\) => state\.runtimeStatus\);[\s\S]*?renderPoolStatus\(state\.runtimeStatus\);[\s\S]*?renderRecommendationState\(/,
  );

  assert.match(desktopJs, /function scheduleBackendHydration/);
  assert.match(desktopJs, /function scheduleActivityPageRefresh/);
  assert.match(desktopJs, /backendHydrationInFlight/);
  assert.match(desktopJs, /activityPageRefreshInFlight/);
  assert.doesNotMatch(
    desktopJs,
    /includes\(event\.type\)\) void hydrateFromBackend\(\);/,
  );
  assert.doesNotMatch(
    desktopJs,
    /if \(event\.type === "activity\.added"\) void loadActivityPage\(\{ reset: true \}\);/,
  );

  // 库存与推荐重排事件只更新状态，不能整表覆盖用户已加载的卡片。
  // 配置终态统一通过安全调度入口再水合，并在执行前后都保护未保存草稿。
  const desktopHydrationTrigger =
    desktopJs.match(
      /function scheduleSettingsHydrationIfSafe\(\) \{[\s\S]*?scheduleBackendHydration\(\);[\s\S]*?\n    \}/,
    )?.[0] ?? "";
  assert.match(desktopHydrationTrigger, /settingsDirtyFields/, "存在草稿时必须跳过再水合");
  assert.match(desktopHydrationTrigger, /settingsFormHasActiveEditor/, "编辑配置时必须跳过再水合");
  assert.notEqual(desktopHydrationTrigger, "", "配置终态仍需触发安全再水合");
  assert.doesNotMatch(desktopHydrationTrigger, /refresh\.pool_updated/);
  assert.doesNotMatch(desktopHydrationTrigger, /recommendation\.reshuffled/);
  assert.match(desktopJs, /if \(reachedTerminal\) \{/);
  assert.match(
    desktopJs,
    /settingsSavePhase === "failed" && settingsDirtyFields\.size > 0/,
    "失败终态保留新草稿时必须走 canonical 快照刷新",
  );
  assert.match(desktopJs, /void refreshConfigSnapshotOnly\(\);/);
  assert.match(
    desktopJs,
    /scheduleSettingsHydrationIfSafe\(\);/,
    "没有新草稿时配置终态必须安全再水合",
  );
  const desktopInitTrigger =
    desktopJs.match(/if \(\["init_progress", "init_failed", "init_completed"\]\.includes\(event\.type\)\) \{[\s\S]*?\n      \}/)?.[0] ?? "";
  assert.notEqual(desktopInitTrigger, "", "desktop should route init events through init status refresh");
  assert.match(desktopInitTrigger, /refreshInitStatus/);

  const desktopRuntimeHandler =
    desktopJs.match(/function handleRuntimeEvent\(event\) \{[\s\S]*?\n    \}\n\n    function connectRuntimeStream/)?.[0] ?? "";
  assert.notEqual(desktopRuntimeHandler, "", "desktop runtime handler should be inspectable");
  assert.match(desktopRuntimeHandler, /state\.videos\.length === 0/);
  assert.match(desktopRuntimeHandler, /desktopRecommendationLoadState === "failed"/);
  assert.match(desktopRuntimeHandler, /desktopRecommendationLoadState === "failed-exhausted"/);
  assert.match(desktopRuntimeHandler, /scheduleDesktopRecommendationRecovery\(\);/);
  assert.match(desktopRuntimeHandler, /desktopRuntimeGeneration \+= 1;/);
  assert.doesNotMatch(desktopRuntimeHandler, /state\.videos\s*=\s*normalizeRecommendationList/);

  const poolUpdatedBlock =
    mobileRecommendJs.match(/if \(type === "refresh\.pool_updated"\) \{[\s\S]*?\} else if/)?.[0] ?? "";
  assert.notEqual(poolUpdatedBlock, "", "mobile recommend stream handler should handle pool updates");
  assert.match(poolUpdatedBlock, /mergeRuntimeStatusEvent/);
  assert.match(poolUpdatedBlock, /runtimeStatusGeneration \+= 1;/);
  assert.match(poolUpdatedBlock, /rerenderRuntimeDependentChrome\(\);/);
  assert.match(poolUpdatedBlock, /state\.recommendations\.length === 0/);
  assert.match(poolUpdatedBlock, /recommendationLoadState === "failed"/);
  assert.match(poolUpdatedBlock, /recommendationLoadState === "failed-exhausted"/);
  assert.match(poolUpdatedBlock, /scheduleRecommendationRecovery\(\);/);
  assert.doesNotMatch(poolUpdatedBlock, /scheduleRecommendationItemsRefresh/);
  assert.doesNotMatch(poolUpdatedBlock, /fetchRecommendations|loadData|patchState\(\{ recommendations:/);
  assert.doesNotMatch(poolUpdatedBlock, /loadData\(/);

  assert.match(mobileProfileJs, /function scheduleProfileRefresh/);
  assert.match(mobileProfileJs, /profileRefreshInFlight/);
  assert.doesNotMatch(
    mobileProfileJs,
    /if \(type === "profile_updated"\) \{\s*loadData\(\);\s*\}/,
  );
});
