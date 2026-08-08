# Firefox AMO source build instructions

This archive contains the complete human-readable source needed to reproduce the Firefox
package submitted to Mozilla Add-ons. No commercial or private build tools are required.

## Build environment

- Ubuntu 24.04 LTS (the GitHub Actions runner used for submission)
- Node.js 22
- npm, using the committed `extension/package-lock.json`

## Reproduce the submitted package

From the archive root:

```bash
cd extension
npm ci
npm run build:firefox
```

The reviewed extension is written to `extension/dist-firefox/`. The build uses TypeScript and
the open-source esbuild package declared in `extension/package.json`; it bundles readable
TypeScript entry points and copies the shared browser UI module from
`src/openbiliclaw/web/shared/`. Source maps are included in the submitted extension.

The submission workflow packages this source directly from the same Git commit, builds
`dist-firefox`, synchronizes `docs/privacy.md`, and invokes `web-ext sign --channel=listed`.

## Privacy-relevant build note

`extension/src/main/xhs-token-sniffer.ts` is bundled as a document-start MAIN-world script for
Xiaohongshu pages. It observes the page's own fetch/XHR responses without changing requests.
For the exact web search-notes endpoint it forwards only a bounded, normalized set of public
card fields and existing per-note access tokens to the same-tab isolated task executor; it never
forwards raw responses or cookie values. The resulting task payload is sent only to the
user-configured OpenBiliClaw backend. See `docs/privacy.md` for the complete disclosure.
