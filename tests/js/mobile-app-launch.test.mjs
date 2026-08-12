import test from "node:test";
import assert from "node:assert/strict";

import {
  buildAppDeepLink,
  isMobileUserAgent,
} from "../../src/openbiliclaw/web/js/app-launch.js";

test("buildAppDeepLink maps bilibili video URLs to the app scheme", () => {
  assert.equal(
    buildAppDeepLink("https://www.bilibili.com/video/BV1xx411c7mD"),
    "bilibili://video/BV1xx411c7mD",
  );
  assert.equal(
    buildAppDeepLink("https://m.bilibili.com/video/BV1xx411c7mD/"),
    "bilibili://video/BV1xx411c7mD",
  );
  assert.equal(
    buildAppDeepLink("https://www.bilibili.com/video/av170001"),
    "bilibili://video/170001",
  );
  // Short links carry no video id — no scheme, caller falls back to web.
  assert.equal(buildAppDeepLink("https://b23.tv/abc123"), "");
});

test("buildAppDeepLink maps xiaohongshu notes and keeps the share token", () => {
  assert.equal(
    buildAppDeepLink("https://www.xiaohongshu.com/explore/66aabbcc000000001e00dead"),
    "xhsdiscover://item/66aabbcc000000001e00dead",
  );
  assert.equal(
    buildAppDeepLink(
      "https://www.xiaohongshu.com/explore/66aabbcc000000001e00dead?xsec_token=ABtok%3D&xsec_source=pc_feed",
    ),
    "xhsdiscover://item/66aabbcc000000001e00dead?xsec_token=ABtok%3D&xsec_source=pc_feed",
  );
  assert.equal(
    buildAppDeepLink("https://www.xiaohongshu.com/discovery/item/66aabbcc000000001e00dead"),
    "xhsdiscover://item/66aabbcc000000001e00dead",
  );
  assert.equal(buildAppDeepLink("https://xhslink.com/abcdef"), "");
});

test("buildAppDeepLink maps douyin / youtube / twitter / zhihu", () => {
  assert.equal(
    buildAppDeepLink("https://www.douyin.com/video/7300000000000000000"),
    "snssdk1128://aweme/detail/7300000000000000000",
  );
  assert.equal(
    buildAppDeepLink("https://www.youtube.com/watch?v=dQw4w9WgXcQ"),
    "vnd.youtube://www.youtube.com/watch?v=dQw4w9WgXcQ",
  );
  assert.equal(
    buildAppDeepLink("https://youtu.be/dQw4w9WgXcQ"),
    "vnd.youtube://www.youtube.com/watch?v=dQw4w9WgXcQ",
  );
  assert.equal(
    buildAppDeepLink("https://www.youtube.com/shorts/dQw4w9WgXcQ"),
    "vnd.youtube://www.youtube.com/watch?v=dQw4w9WgXcQ",
  );
  assert.equal(
    buildAppDeepLink("https://x.com/i/status/1801234567890123456"),
    "twitter://status?id=1801234567890123456",
  );
  assert.equal(
    buildAppDeepLink("https://twitter.com/someone/statuses/1801234567890123456"),
    "twitter://status?id=1801234567890123456",
  );
  assert.equal(
    buildAppDeepLink("https://www.zhihu.com/question/123/answer/456"),
    "zhihu://answers/456",
  );
  assert.equal(
    buildAppDeepLink("https://zhuanlan.zhihu.com/p/789"),
    "zhihu://articles/789",
  );
  assert.equal(
    buildAppDeepLink("https://www.zhihu.com/question/123"),
    "zhihu://questions/123",
  );
});

test("buildAppDeepLink returns empty for unknown or malformed URLs", () => {
  assert.equal(buildAppDeepLink("https://www.reddit.com/r/foo/comments/abc/"), "");
  // Weibo has no verified mobile-app deep-link contract. Keep navigation on
  // its canonical HTTPS URL even though init can use the browser task bridge.
  assert.equal(buildAppDeepLink("https://m.weibo.cn/detail/5023456789012345"), "");
  assert.equal(buildAppDeepLink("https://weibo.com/1234567890/P9Example"), "");
  assert.equal(buildAppDeepLink("https://example.com/whatever"), "");
  assert.equal(buildAppDeepLink(""), "");
  assert.equal(buildAppDeepLink(null), "");
  assert.equal(buildAppDeepLink("::not a url::"), "");
  // Hostname must match the platform domain, not merely contain it.
  assert.equal(buildAppDeepLink("https://bilibili.com.evil.com/video/BV1xx411c7mD"), "");
});

test("isMobileUserAgent detects phones, tablets, and iPadOS-as-Mac", () => {
  assert.equal(
    isMobileUserAgent(
      "Mozilla/5.0 (iPhone; CPU iPhone OS 17_5 like Mac OS X) AppleWebKit/605.1.15",
    ),
    true,
  );
  assert.equal(
    isMobileUserAgent("Mozilla/5.0 (Linux; Android 14; Pixel 8) AppleWebKit/537.36"),
    true,
  );
  // iPadOS 13+ masquerades as desktop Safari but has multitouch.
  assert.equal(
    isMobileUserAgent("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15", 5),
    true,
  );
  assert.equal(
    isMobileUserAgent("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15", 0),
    false,
  );
  assert.equal(
    isMobileUserAgent("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36", 0),
    false,
  );
  assert.equal(isMobileUserAgent("", 0), false);
});
