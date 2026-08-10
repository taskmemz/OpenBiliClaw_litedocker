import test from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { resolve } from "node:path";

test("mobile content library keeps canonical and legacy routes without duplicate first apply", () => {
  const app = readFileSync(resolve("../src/openbiliclaw/web/js/app.js"), "utf8");
  const library = readFileSync(resolve("../src/openbiliclaw/web/js/views/library.js"), "utf8");
  const css = readFileSync(resolve("../src/openbiliclaw/web/css/app.css"), "utf8");
  const topTabs = app.match(/const TABS = \[[\s\S]*?\n\];/)?.[0] ?? "";

  assert.match(topTabs, /id: "recommend"[\s\S]*id: "library"[\s\S]*id: "profile"[\s\S]*id: "chat"/);
  assert.equal((topTabs.match(/\{ id: "(?:recommend|library|profile|chat)"/g) || []).length, 4);
  assert.match(app, /let appliedRouteKey = ""/);
  assert.match(app, /appliedRouteKey === nextHash/);
  assert.match(app, /\["watchLater", "watch-later", "watch_later", "favorites", "favorite", "history"\]/);
  assert.match(app, /replaceState\(null, "",[\s\S]*nextHash/);
  assert.match(app, /#\/library\/\$\{contentLibrarySlug\(nextLibraryTab\)\}/);
  assert.match(app, /onSelect: \(libraryTab\)[\s\S]*appliedRouteKey = hash;[\s\S]*window\.location\.hash = hash/);
  assert.match(app, /e\.key === "Home"/);
  assert.match(app, /e\.key === "End"/);

  assert.match(library, /role="tablist"/);
  assert.match(library, /role="tabpanel"/);
  assert.match(library, /aria-selected/);
  assert.match(library, /event\.key === "ArrowRight"/);
  assert.match(library, /event\.key === "Home"/);
  assert.match(library, /leaveContentLibraryView/);
  assert.match(library, /scrollPositions\.set\(activeTab, scroller\.scrollTop\)/);
  assert.match(css, /\.content-library-tabs\s*\{[\s\S]*grid-template-columns:\s*repeat\(3, minmax\(0, 1fr\)\)[\s\S]*gap:\s*8px/);
  assert.match(css, /\.content-library-tab\s*\{[\s\S]*min-height:\s*44px/);
});

test("desktop content library supports canonical hash history and legacy aliases", () => {
  const html = readFileSync(resolve("../src/openbiliclaw/web/desktop/index.html"), "utf8");
  const app = readFileSync(resolve("../src/openbiliclaw/web/desktop/assets/js/app.js"), "utf8");

  assert.match(html, /id="contentLibraryBtn"/);
  assert.match(html, /class="content-library-tabs"[^>]*role="tablist"/);
  assert.match(html, /id="contentLibraryWatchLaterTab"[^>]*role="tab"/);
  assert.match(html, /id="contentLibraryFavoritesTab"[^>]*role="tab"/);
  assert.match(html, /id="contentLibraryHistoryTab"[^>]*role="tab"/);
  assert.match(app, /#library\/\$\{desktopContentLibrarySlug\(desktopContentLibraryTab\)\}/);
  assert.match(app, /\["watchLater", "watch-later", "watch_later", "favorites", "favorite", "history"\]/);
  assert.match(app, /window\.addEventListener\("hashchange"/);
  assert.match(app, /else if \(desktopContentLibraryVisible\)[\s\S]*showMainPage\("homePage"\)/);
  assert.match(app, /window\.history\.replaceState[\s\S]*canonicalHash/);
  assert.match(app, /event\.key === "Home"/);
  assert.match(app, /event\.key === "End"/);
});

test("popup maps old query tabs into one keyboard-operable content library", () => {
  const html = readFileSync(resolve("popup", "popup.html"), "utf8");
  const app = readFileSync(resolve("popup", "popup.js"), "utf8");
  const topBar = html.match(/<div class="tab-bar"[\s\S]*?<\/div>\s*<\/div>/)?.[0] ?? "";

  assert.equal((topBar.match(/class="tab-button/g) || []).length, 4);
  assert.match(app, /POPUP_LIBRARY_TABS = \["watchLater", "favorites", "history"\]/);
  assert.match(app, /\["recommend", "library", "watchLater", "favorites", "history", "profile", "chat"\]\.includes\(requestedTab\)/);
  assert.match(app, /params\.get\("section"\) \|\| params\.get\("library"\)/);
  assert.match(app, /legacyChild \? "library" : requestedTab/);
  assert.match(app, /event\.key === "ArrowRight"/);
  assert.match(app, /event\.key === "ArrowLeft"/);
  assert.match(app, /event\.key === "Home"/);
  assert.match(app, /event\.key === "End"/);
});
