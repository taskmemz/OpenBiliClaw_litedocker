import test from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { resolve } from "node:path";

import {
  makeExtensionArchiveName,
  makeFirefoxSignedXpiName,
  normalizeReleaseVersion,
} from "../scripts/release-utils.mjs";

test("normalizeReleaseVersion strips extension channel prefix", () => {
  assert.equal(normalizeReleaseVersion("extension-v0.1.3"), "v0.1.3");
});

test("normalizeReleaseVersion preserves plain manifest versions", () => {
  assert.equal(normalizeReleaseVersion("0.1.3"), "v0.1.3");
});

test("makeExtensionArchiveName keeps only the user-facing version", () => {
  assert.equal(
    makeExtensionArchiveName("extension-v0.1.3"),
    "openbiliclaw-extension-v0.1.3.zip",
  );
});

test("makeFirefoxSignedXpiName names the installable Firefox package", () => {
  assert.equal(
    makeFirefoxSignedXpiName("extension-v0.1.3"),
    "openbiliclaw-extension-v0.1.3-firefox.xpi",
  );
});

test("package scripts remove stale archive before zipping", () => {
  const chromeScript = readFileSync(resolve("scripts", "package.mjs"), "utf8");
  const firefoxScript = readFileSync(resolve("scripts", "package-firefox.mjs"), "utf8");

  for (const script of [chromeScript, firefoxScript]) {
    assert.match(script, /rm\(outPath,\s*\{\s*force:\s*true\s*\}\)/);
    assert.match(script, /zip -r -9/);
  }
});

test("Firefox build target matches manifest minimum version", () => {
  const script = readFileSync(resolve("scripts", "build.mjs"), "utf8");

  assert.match(script, /firefox140/);
});

test("Firefox signing script uses AMO unlisted signing and emits XPI", () => {
  const script = readFileSync(resolve("scripts", "sign-firefox.mjs"), "utf8");

  assert.match(script, /AMO_JWT_ISSUER/);
  assert.match(script, /AMO_JWT_SECRET/);
  assert.match(script, /web-ext/);
  assert.match(script, /sign/);
  assert.match(script, /--channel=unlisted/);
  assert.match(script, /makeFirefoxSignedXpiName/);
  assert.doesNotMatch(script, /zip -r -9/);
});

test("extension release workflow publishes signed Firefox XPI when enabled", () => {
  const workflow = readFileSync(
    resolve("..", ".github", "workflows", "release-extension.yml"),
    "utf8",
  );

  assert.match(workflow, /FIREFOX_SIGNING_ENABLED/);
  assert.match(workflow, /AMO_JWT_ISSUER/);
  assert.match(workflow, /AMO_JWT_SECRET/);
  assert.match(workflow, /npm run sign:firefox/);
  assert.match(workflow, /openbiliclaw-extension-v\$\{expected\}-firefox\.xpi/);
  assert.match(workflow, /release-artifacts\/openbiliclaw-extension-v\*\.(zip|xpi)/);
});

test("Firefox AMO workflow submits a listed build with metadata and reviewer source", () => {
  const workflow = readFileSync(
    resolve("..", ".github", "workflows", "publish-firefox-amo.yml"),
    "utf8",
  );
  const metadata = JSON.parse(readFileSync(resolve("amo-metadata.json"), "utf8")) as {
    categories?: Record<string, string[]>;
    name?: Record<string, string>;
    summary?: Record<string, string>;
    version?: { approval_notes?: string; license?: string };
  };

  assert.match(workflow, /--channel=listed/);
  assert.match(workflow, /--amo-metadata=amo-metadata\.json/);
  assert.match(workflow, /--upload-source-code=/);
  assert.match(workflow, /firefox-amo-privacy\.mjs/);
  assert.match(workflow, /firefox-amo-status\.mjs/);
  assert.match(workflow, /id: privacy\n\s+continue-on-error: true/);
  assert.ok(
    workflow.indexOf("Submit listed package to AMO") <
      workflow.indexOf("Synchronize AMO privacy policy"),
  );
  assert.deepEqual(metadata.categories?.firefox, ["photos-music-videos"]);
  assert.deepEqual(metadata.categories?.android, ["photos-music-videos"]);
  assert.equal(metadata.version?.license, "MIT");
  assert.ok(metadata.name?.["zh-CN"]);
  assert.ok(metadata.summary?.["en-US"]);
  assert.match(metadata.version?.approval_notes ?? "", /npm run build:firefox/);
  assert.match(metadata.version?.approval_notes ?? "", /\*\.v2ex\.com/);
  assert.match(metadata.version?.approval_notes ?? "", /performs no V2EX write actions/);
});

test("Firefox AMO source instructions reproduce the reviewed directory", () => {
  const instructions = readFileSync(
    resolve("..", "docs", "firefox-amo-source-build.md"),
    "utf8",
  );

  assert.match(instructions, /Node\.js 22/);
  assert.match(instructions, /npm ci/);
  assert.match(instructions, /npm run build:firefox/);
  assert.match(instructions, /extension\/dist-firefox/);
  assert.match(instructions, /A2/);
  assert.match(instructions, /never accesses, stores, or\s+sends the cookie value/);
});

test("Firefox AMO API requests use explicit JSON content negotiation", () => {
  const api = readFileSync(resolve("scripts", "amo-api.mjs"), "utf8");

  assert.match(api, /Accept: "application\/json"/);
  assert.match(api, /"Content-Type": "application\/json"/);
  assert.match(api, /"User-Agent": "OpenBiliClaw Firefox AMO publisher"/);
});

test("Firefox AMO privacy sync resolves the numeric add-on ID before its action route", () => {
  const privacy = readFileSync(
    resolve("scripts", "firefox-amo-privacy.mjs"),
    "utf8",
  );

  assert.match(privacy, /addons\/addon\/\$\{encodeURIComponent\(geckoId\)\}\//);
  assert.match(privacy, /Number\.isInteger\(addon\?\.id\)/);
  assert.match(privacy, /addons\/addon\/\$\{addon\.id\}\/eula_policy\//);
  assert.doesNotMatch(
    privacy,
    /addons\/addon\/\$\{encodeURIComponent\(geckoId\)\}\/eula_policy\//,
  );
});

test("aggregate release sync treats signed Firefox XPI as a package asset", () => {
  const script = readFileSync(
    resolve("..", ".github", "scripts", "sync-aggregate-release.sh"),
    "utf8",
  );

  assert.match(script, /firefox_signed_asset_line/);
  assert.match(script, /openbiliclaw-extension-v\$\{extension_version\}-firefox\.xpi/);
  assert.match(script, /download_release_assets "\$extension_tag" "openbiliclaw-extension-v\*\.zip" "openbiliclaw-extension-v\*\.xpi"/);
  assert.match(script, /openbiliclaw-extension-v\*\.xpi/);
});
