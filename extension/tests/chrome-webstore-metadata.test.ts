import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import test from "node:test";

import {
  buildMetadataPayload,
  parseListingMarkdown,
  summarizeDraft,
  validateListingMetadata,
  verifyMetadataReadback,
} from "../scripts/chrome-webstore-metadata-lib.mjs";
import {
  findReviewState,
  parseArgs,
  requestJson,
  runMetadataCommand,
} from "../scripts/chrome-webstore-metadata.mjs";

const markdown = `# Chrome Web Store 商店页文案与素材

- 项目主页 / Website URL: <https://whiteguo233.github.io/OpenBiliClaw/>
- 支持 / Support URL: <https://github.com/whiteguo233/OpenBiliClaw/issues>

## Short Description

\`\`\`text
需本地后端的多平台内容发现 AI Agent：跨平台推荐、私有画像与可反馈侧边栏
\`\`\`

## Detailed Description

\`\`\`text
OpenBiliClaw 需要本地后端，平台数据默认保存在你的本机。
\`\`\`
`;

test("parses canonical copy and validates local-data claims", () => {
  const listing = parseListingMarkdown(markdown);

  assert.match(listing.summary, /需本地后端/);
  assert.match(listing.description, /保存在你的本机/);
  assert.equal(listing.homepageUrl, "https://whiteguo233.github.io/OpenBiliClaw/");
  assert.equal(listing.supportUrl, "https://github.com/whiteguo233/OpenBiliClaw/issues");
  assert.doesNotThrow(() => validateListingMetadata(listing));
});

test("the repository listing document is valid canonical input", async () => {
  const source = await readFile(
    new URL("../../docs/chrome-webstore-listing.md", import.meta.url),
    "utf8",
  );
  const listing = parseListingMarkdown(source);

  assert.doesNotThrow(() => validateListingMetadata(listing));
  assert.equal(listing.homepageUrl, "https://whiteguo233.github.io/OpenBiliClaw/");
  assert.equal(listing.supportUrl, "https://github.com/whiteguo233/OpenBiliClaw/issues");
});

test("rejects empty, overlong, or misleading canonical copy", () => {
  const listing = parseListingMarkdown(markdown);

  assert.throws(
    () => validateListingMetadata({ ...listing, summary: "字".repeat(133) }),
    /132/,
  );
  assert.throws(
    () => validateListingMetadata({ ...listing, summary: "普通插件", description: "云端服务" }),
    /本地后端/,
  );
  assert.throws(
    () => validateListingMetadata({ ...listing, description: "需要本地后端" }),
    /本机|本地数据/,
  );
});

test("summarizes drafts without exposing raw values", () => {
  const draft = {
    title: "OpenBiliClaw",
    summary: "old summary secret",
    description: "old description secret",
    screenshots: ["private-image-id"],
    promotionalImages: ["private-promo-id"],
  };

  const summary = summarizeDraft(draft);
  const serialized = JSON.stringify(summary);

  assert.deepEqual(summary.fieldNames, [
    "description",
    "promotionalImages",
    "screenshots",
    "summary",
    "title",
  ]);
  assert.deepEqual(summary.assetFieldNames, ["promotionalImages", "screenshots"]);
  assert.equal(summary.summary.present, true);
  assert.equal(summary.summary.length, 18);
  assert.equal(summary.summary.sha256.length, 64);
  assert.doesNotMatch(serialized, /old summary secret|old description secret|private-image-id/);
});

test("builds an allowlisted payload and replaces documented URLs only when present", () => {
  const listing = parseListingMarkdown(markdown);
  const draft = {
    title: "OpenBiliClaw",
    category: "PRODUCTIVITY",
    defaultLocale: "zh_CN",
    homepageUrl: "https://old.example/",
    supportUrl: "https://old.example/issues",
    summary: "old",
    description: "old private description",
    status: "PENDING_REVIEW",
    screenshots: ["secret"],
  };

  assert.deepEqual(buildMetadataPayload(draft, listing), {
    title: "OpenBiliClaw",
    category: "PRODUCTIVITY",
    defaultLocale: "zh_CN",
    homepageUrl: listing.homepageUrl,
    supportUrl: listing.supportUrl,
    summary: listing.summary,
    description: listing.description,
  });
  assert.deepEqual(
    buildMetadataPayload(
      {
        title: "OpenBiliClaw",
        defaultLocale: "zh_CN",
        summary: "old",
        description: "old",
      },
      listing,
    ),
    {
      title: "OpenBiliClaw",
      defaultLocale: "zh_CN",
      summary: listing.summary,
      description: listing.description,
    },
  );
});

test("requires enough draft identity and exact metadata read-back", () => {
  const listing = parseListingMarkdown(markdown);

  assert.throws(
    () => buildMetadataPayload({ summary: "old", description: "old" }, listing),
    /identity/,
  );
  assert.doesNotThrow(() => verifyMetadataReadback({ ...listing }, listing));
  assert.throws(
    () =>
      verifyMetadataReadback(
        { ...listing, description: `${listing.description} changed` },
        listing,
      ),
    /read-back/,
  );
});

const oauthEnv = {
  CHROME_WEBSTORE_CLIENT_ID: "client-id",
  CHROME_WEBSTORE_CLIENT_SECRET: "client-secret",
  CHROME_WEBSTORE_REFRESH_TOKEN: "refresh-token",
  CHROME_WEBSTORE_PUBLISHER_ID: "publisher-id",
  CHROME_WEBSTORE_EXTENSION_ID: "item-id",
};

const canonical = parseListingMarkdown(markdown);

function jsonResponse(payload: unknown, status = 200) {
  return new Response(JSON.stringify(payload), {
    status,
    headers: { "Content-Type": "application/json" },
  });
}

function fakeFetch(
  calls: Array<{ url: string; method: string; body: string }>,
  responses: Response[],
) {
  return async (input: string | URL | Request, init: RequestInit = {}) => {
    const response = responses.shift();
    if (!response) {
      throw new Error(`Unexpected request: ${String(input)}`);
    }
    calls.push({
      url: String(input),
      method: init.method ?? "GET",
      body: typeof init.body === "string" ? init.body : "",
    });
    return response;
  };
}

function draftResponse(overrides: Record<string, unknown> = {}) {
  return jsonResponse({
    title: "OpenBiliClaw",
    category: "PRODUCTIVITY",
    defaultLocale: "zh_CN",
    homepageUrl: "https://old.example/",
    supportUrl: "https://old.example/issues",
    summary: "old summary",
    description: "old description",
    ...overrides,
  });
}

function statusResponse(state: string) {
  return jsonResponse({ submittedItemRevisionStatus: { state } });
}

const readFileImpl = async () => markdown;

test("parses safe CLI modes and rejects dangerous flag combinations", () => {
  assert.deepEqual(parseArgs(["--listing", "listing.md", "--mode", "probe"]), {
    listing: "listing.md",
    mode: "probe",
    replacePending: false,
    publish: false,
  });
  assert.throws(
    () => parseArgs(["--listing", "listing.md", "--mode", "probe", "--publish"]),
    /apply/,
  );
  assert.throws(
    () => parseArgs(["--listing", "listing.md", "--mode", "apply", "--publish"]),
    /replace-pending/,
  );
});

test("probe performs only OAuth and one redacted v1.1 GET", async () => {
  const calls: Array<{ url: string; method: string; body: string }> = [];
  const logs: string[] = [];

  const result = await runMetadataCommand({
    options: {
      listing: "listing.md",
      mode: "probe",
      replacePending: false,
      publish: false,
    },
    env: oauthEnv,
    fetchImpl: fakeFetch(calls, [
      jsonResponse({ access_token: "access-token", scope: "https://www.googleapis.com/auth/chromewebstore" }),
      draftResponse(),
    ]),
    readFileImpl,
    log: (line: string) => logs.push(line),
  });

  assert.deepEqual(calls.map(({ method }) => method), ["POST", "GET"]);
  assert.match(calls[1].url, /chromewebstore\/v1\.1\/items\/item-id\?projection=DRAFT$/);
  assert.equal(result.operation, "probe");
  assert.equal(result.probe.summary.present, true);
  assert.doesNotMatch(logs.join("\n"), /access-token|refresh-token|old description/);
});

test("probe schema failure stops before status, cancellation, or writes", async () => {
  const calls: Array<{ url: string; method: string; body: string }> = [];
  const logs: string[] = [];

  await assert.rejects(
    runMetadataCommand({
      options: {
        listing: "listing.md",
        mode: "apply",
        replacePending: true,
        publish: true,
      },
      env: oauthEnv,
      fetchImpl: fakeFetch(calls, [
        jsonResponse({ access_token: "access-token" }),
        jsonResponse({ kind: "chromewebstore#item", id: "item-id", uploadState: "SUCCESS" }),
      ]),
      readFileImpl,
      log: (line: string) => logs.push(line),
    }),
    /writable listing metadata/,
  );

  assert.deepEqual(calls.map(({ method }) => method), ["POST", "GET"]);
  assert.deepEqual(JSON.parse(logs[0]), {
    operation: "probe",
    probe: {
      fieldNames: ["id", "kind", "uploadState"],
      summary: {
        present: false,
        length: 0,
        sha256: "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855",
      },
      description: {
        present: false,
        length: 0,
        sha256: "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855",
      },
      assetFieldNames: [],
    },
  });
});

test("apply refuses to cancel a pending review without explicit replacement", async () => {
  const calls: Array<{ url: string; method: string; body: string }> = [];

  await assert.rejects(
    runMetadataCommand({
      options: {
        listing: "listing.md",
        mode: "apply",
        replacePending: false,
        publish: false,
      },
      env: oauthEnv,
      fetchImpl: fakeFetch(calls, [
        jsonResponse({ access_token: "access-token" }),
        draftResponse(),
        statusResponse("PENDING_REVIEW"),
      ]),
      readFileImpl,
      log: () => {},
    }),
    /--replace-pending/,
  );

  assert.equal(calls.some(({ url }) => url.includes("cancelSubmission")), false);
  assert.equal(calls.some(({ method }) => method === "PUT"), false);
});

test("apply probes, cancels, writes, reads back, publishes, and verifies in order", async () => {
  const calls: Array<{ url: string; method: string; body: string }> = [];
  const logs: string[] = [];

  const result = await runMetadataCommand({
    options: {
      listing: "listing.md",
      mode: "apply",
      replacePending: true,
      publish: true,
    },
    env: oauthEnv,
    fetchImpl: fakeFetch(calls, [
      jsonResponse({ access_token: "access-token", scope: "https://www.googleapis.com/auth/chromewebstore" }),
      draftResponse(),
      statusResponse("PENDING_REVIEW"),
      jsonResponse({}),
      jsonResponse({ kind: "chromewebstore#item", id: "item-id" }),
      draftResponse({ summary: canonical.summary, description: canonical.description }),
      jsonResponse({}),
      statusResponse("PENDING_REVIEW"),
    ]),
    readFileImpl,
    log: (line: string) => logs.push(line),
  });

  assert.deepEqual(calls.map(({ method }) => method), [
    "POST",
    "GET",
    "GET",
    "POST",
    "PUT",
    "GET",
    "POST",
    "GET",
  ]);
  assert.match(calls[3].url, /:cancelSubmission$/);
  assert.match(calls[4].url, /chromewebstore\/v1\.1\/items\/item-id$/);
  assert.deepEqual(JSON.parse(calls[4].body), buildMetadataPayload({
    title: "OpenBiliClaw",
    category: "PRODUCTIVITY",
    defaultLocale: "zh_CN",
    homepageUrl: "https://old.example/",
    supportUrl: "https://old.example/issues",
    summary: "old summary",
    description: "old description",
  }, canonical));
  assert.match(calls[6].url, /:publish$/);
  assert.deepEqual(result, {
    operation: "apply",
    updated: true,
    published: true,
    reviewState: "PENDING_REVIEW",
  });
  assert.doesNotMatch(logs.join("\n"), /access-token|client-secret|old description/);
});

test("apply stops before publish when exact read-back fails", async () => {
  const calls: Array<{ url: string; method: string; body: string }> = [];

  await assert.rejects(
    runMetadataCommand({
      options: {
        listing: "listing.md",
        mode: "apply",
        replacePending: true,
        publish: true,
      },
      env: oauthEnv,
      fetchImpl: fakeFetch(calls, [
        jsonResponse({ access_token: "access-token" }),
        draftResponse(),
        statusResponse("CANCELLED"),
        jsonResponse({}),
        draftResponse({ summary: canonical.summary, description: "mismatch" }),
      ]),
      readFileImpl,
      log: () => {},
    }),
    /read-back/,
  );

  assert.equal(calls.some(({ url }) => url.endsWith(":publish")), false);
});

test("findReviewState reads the documented v2 status shape", () => {
  assert.equal(
    findReviewState({ submittedItemRevisionStatus: { state: "PENDING_REVIEW" } }),
    "PENDING_REVIEW",
  );
  assert.equal(findReviewState({ publishedItemRevisionStatus: { state: "PUBLISHED" } }), "");
});

test("requestJson retries one 429 but never retries authentication failures", async () => {
  const retryCalls: Array<{ url: string; method: string; body: string }> = [];
  const retryResult = await requestJson(
    "probe",
    "https://example.test/probe",
    { method: "GET" },
    {
      fetchImpl: fakeFetch(retryCalls, [
        jsonResponse({ error: { message: "rate limited" } }, 429),
        jsonResponse({ ok: true }),
      ]),
      sleep: async () => {},
    },
  );
  assert.deepEqual(retryResult, { ok: true });
  assert.equal(retryCalls.length, 2);

  const authCalls: Array<{ url: string; method: string; body: string }> = [];
  await assert.rejects(
    requestJson(
      "probe",
      "https://example.test/probe",
      { method: "GET" },
      {
        fetchImpl: fakeFetch(authCalls, [
          jsonResponse({ error: { message: "unauthorized" } }, 401),
        ]),
        sleep: async () => {},
      },
    ),
    /HTTP 401/,
  );
  assert.equal(authCalls.length, 1);
});

test("requestJson does not retry transport or timeout errors", async () => {
  for (const errorName of ["TypeError", "TimeoutError"]) {
    let calls = 0;
    const error = new Error("network unavailable");
    error.name = errorName;
    await assert.rejects(
      requestJson(
        "probe",
        "https://example.test/probe",
        { method: "GET" },
        {
          fetchImpl: async () => {
            calls += 1;
            throw error;
          },
          sleep: async () => {},
        },
      ),
      /request failed/,
    );
    assert.equal(calls, 1);
  }
});
