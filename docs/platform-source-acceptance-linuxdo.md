# Linux.do 平台来源验收报告

> 对应 `docs/platform-source-contract-linuxdo.toml`。适用性只写 `required / N/A`，执行状态只写 `PASS / FAIL / NOT_RUN / BLOCKED`；实现存在但缺少本门要求的证据仍是 `NOT_RUN`，不会冒充完成。

## 范围与 provenance

- Integration level: `full`
- Contract: `docs/platform-source-contract-linuxdo.toml`
- Baseline release / current repair: `main@a3dbc7065ddcc37dfafde2e85f3d3f67e029c42c` / `fix/linuxdo-alarm-dispatch` based on `7123d0d5a56d75c5cbf5c9e6587429ca9b78ce7c`
- Python import / CLI: `/Users/white/workspace/OpenBiliClaw/.worktrees/linuxdo-source/src/openbiliclaw/__init__.py / shared .venv with explicit PYTHONPATH`
- Backend bind / data / config root: safe real E2E used an isolated worktree data root; production roots are excluded
- Browser / extension identity: 2026-08-10 hot-loaded the repaired current-worktree Chrome build into the real unpacked extension path and exercised it against an isolated backend; frozen Chrome/Firefox archives below have not both been installed for a release-candidate rerun
- Chrome aggregate-release archive: `openbiliclaw-extension-v0.3.202.zip`, SHA-256 `8ec0ec875cd6f55638246f317344aa4d40b44e89a2cb605b81bf42b1a3815ef4`
- Firefox aggregate-release archive: `openbiliclaw-extension-v0.3.202-firefox.zip`, SHA-256 `e0e5c7b00cf4303edd81db76a270909409db9dbed09cf38c50b5c90974e4f372`
- Existing user changes preserved: yes; the dedicated worktree remains intentionally dirty and no unrelated change was reset

## Gate ledger

| Gate | Applicability | Status | Evidence | Remaining risk |
| --- | --- | --- | --- | --- |
| Scope/worktree provenance | required | PASS | branch/HEAD/import/build roots recorded above | merge/rebase must preserve the dirty worktree deliberately |
| Frozen source contract | required | PASS | `docs/platform-source-contract-linuxdo.toml`; audit 42 PASS / 0 MISSING | semantic MANUAL rows remain governed by this ledger |
| Historical precedent + repair review | required | PASS | Bangumi auth/discover, Zhihu bootstrap, Reddit dispatcher, prior Linux.do real-E2E repair audits | none |
| Real upstream spike + redacted fixtures | required | PASS | authenticated Chrome bootstrap, search/hot/feed/creator/related, pagination, 404/challenge/degraded probes | stripped-credential rerun remains NOT_RUN |
| Canonical registry / identity / storage | required | PASS | positive topic IDs, slug-preserving canonical URLs, namespaced item keys, opaque account partition and round-trip schema tests | cross-account real-browser switch has not been exercised |
| Transport / normalizer / error taxonomy | required | PASS | strict JSON content type + route envelope, true-empty evidence, partial preservation, 401/403/429/timeout taxonomy | final packaged artifact real rerun pending |
| Shared capability/auth readiness prerequisite | required | PASS | shared per-capability contract/status/init admission; anonymous discover and login-required profile/bootstrap/incremental are independently represented | none |
| Auth / credential / account resolution | required | PASS | `/session/current.json` authority, 72-hour task evidence, `_t` bool-only heartbeat, authoritative-failure precedence, opaque account key | rejected/expired real-session scenario NOT_RUN |
| Browser task / MV3 recovery | required | PASS | runtime stream reconnects before a blocked recovery barrier; `storage.session` generation distinguishes worker recycle from full reload, and the latter refreshes the runner document before replay. Real Chrome: one two-page feed remained the same task row/ID through reload and terminally completed 37 unique topics in 25 seconds | Firefox installed-account recovery remains a separate NOT_RUN release-artifact item |
| Bootstrap / event / init | required | PASS | backend scope/cap/action/account validation; two real 107-item pulls produced exactly 107 events and bounded seen state | real account-switch reset path NOT_RUN |
| Post-init incremental lifecycle | required | PASS | only `ok/empty` advances `last_success_at`; failed/degraded is immediately due; successful guided init seeds attempt+success atomically | no long-duration daemon soak |
| Formal discover / keyword dual-track / admission | required | PASS | five modes, both keyword generation tracks, retained-only claim settlement and daily budget, persistent cursors, global limit/share policy, prior real LLM path | final archive hot-load rerun pending |
| Eval / publication time / recommendation | required | PASS | topic-owned publication time, reply-owned fields cannot contaminate topic metrics, text-card/canonical URL/eval tests and prior real recommendation path | search serializer omits some topic-owned fields |
| Engagement branch parity | required | PASS | formal search/related tasks globally cap retained rows and hydrate only those topic IDs through `/t/<id>.json`; real Chrome search 2/2 and related 2/2 returned topic author/view/total-like/comment with zero missing fields | detail failures are degraded instead of fabricating zero or using reply metrics |
| Config / API / status convergence | required | PASS | config round-trip/clamps, disabled source cannot claim, task-history fallback, capability status and live convergence tests | final popup click-through NOT_RUN |
| Setup surface | required | PASS | shared source roster and Linux.do capability-aware setup tests | real installed setup flow NOT_RUN |
| Desktop surface | required | PASS | settings/status/source-share/text-card tests plus prior real Chrome evidence | final archive rerun pending |
| Mobile surface | required | PASS | platform-neutral author fallback, text-card/canonical URL and source settings tests | final archive rerun pending |
| Extension popup surface | required | PASS | roster/settings/status/saved-sync tests and built popup assets | real popup interaction on final archive NOT_RUN |
| Mobile credential management | N/A | PASS | global product exclusion; mobile does not edit source credentials | none |
| Extension early-response tap | N/A | PASS | exact exclusion test proves requests start only after the isolated task listener | none |
| Image delivery | N/A | PASS | exact text-card exclusion test | none |
| Image proxy DNS / redirect / SSRF boundary | N/A | PASS | no Linux.do image route | none |
| Mobile deep link | N/A | PASS | exact canonical HTTPS browser-fallback test | none |
| Native save | N/A | PASS | exact no-adapter test; Linux.do is read-only | none |
| Focused + full backend verification | required | PASS | focused 411 passed / 28 skipped; full alphabetic groups total 7,756 passed / 54 skipped | only existing FastAPI deprecation warnings |
| Chrome + Firefox tests/build/assets | required | PASS | extension 1,323/1,323; typecheck; both builds; 17/17 assets each; both build roots contain Linux.do + service worker | installed Firefox account E2E is separate and NOT_RUN |
| Safe real E2E on final repaired archives | required | NOT_RUN | repaired current-worktree Chrome build passed authenticated search/related hydration plus in-progress full-reload recovery; this is not proof that both frozen release archives were installed | install both frozen Chrome/Firefox archives for release-candidate closure |
| State-changing E2E | N/A | PASS | read-only source; no upstream mutation requested or executed | none |
| Documentation / release-readiness | required | PASS | module/config/API/auth/init/runtime/privacy/store docs, README / README_EN, homepage, changelog, contract, audit and this ledger updated for 0.3.202 | Firefox installed-account E2E remains separately NOT_RUN |
| Commit/version/tag/push/publish mutations | required | PASS | `backend/extension/desktop/openbiliclaw-v0.3.202` all point to `a3dbc706`; aggregate Latest contains exactly six 0.3.202 assets; Chrome submission is `PENDING_REVIEW`; AMO accepted listed 0.3.202 as `fileStatus=unreviewed` | AMO `eula_policy` API returned 406; manifest, reviewer notes, listing and bundled privacy policy remain submitted disclosures |

## Transport、身份与数据契约

- Primary / fallback owner: extension browser task / none
- Auth comparison: public discover is anonymous; profile/bootstrap/incremental requires a same-origin `/session/current.json` positive identity. Cookie value never leaves the browser.
- Account resolution: a verified user ID/username is hashed into an opaque account key; a different account fails closed with `linuxdo_account_switch_requires_reset` instead of mixing profiles.
- Stable identity: content ID `topic:<positive id>`; namespaced item key `linuxdo:topic:<id>`; canonical URL preserves a validated same-origin slug.
- Upstream terminal truth: HTTP success alone is insufficient; JSON content type, route envelope, observed response and complete scope/cursor evidence are validated server-side against the original task payload.
- Engagement/publication: `engagement_available` distinguishes unavailable values from real zero; publication is topic-owned. Formal search/related globally cap the returned task set, then fetch authoritative topic details only for retained IDs; reply author/likes are never used as topic fields.

## 命令与自动验证

| Command | Exit | Summary / artifact |
| --- | ---: | --- |
| `audit_platform_source.py --contract ... --check --json` | 0 | 42 PASS / 0 MISSING / 12 MANUAL / 7 N/A; registration gate passed |
| exact exclusion nodeids | 0 | 5 passed; no skip/xfail |
| focused backend tests | 0 | 411 passed / 28 skipped |
| full backend CI | 0 | 8,125 passed / 102 skipped; release consistency, Ruff, MyPy, Firefox smoke, Windows autostart and Web guided-init E2E also passed |
| source contract metrics | 0 | 6/6; canonical registry-derived 11/11 verify coverage |
| extension tests/typecheck | 0 | 1,386/1,386; typecheck passed |
| Chrome build + asset verify | 0 | build passed; 18 required assets; archive hash above |
| Firefox build + asset verify | 0 | build passed; 18 required assets; archive hash above |
| Ruff / MyPy / diff check | 0 | changed-file Ruff, MyPy (258 source files), diff-check and release-version consistency pass |

## 真实 E2E

| Scenario | Applicability | Status | Counts / idempotency | Diagnostic / artifact |
| --- | --- | --- | --- | --- |
| Anonymous / stripped credential | required | NOT_RUN |  | final archive must prove public discover without a login session |
| Authenticated / verified identity | required | PASS | two bootstrap pulls each 107 canonical signals (2 bookmark / 5 like / 100 read) | prior Chrome worktree build; Cookie forbidden-key scan 0 |
| Rejected/expired credential | required | NOT_RUN |  | needs a safe signed-out/expired-session browser state |
| Empty vs no-observer vs partial vs rate-limit | required | NOT_RUN | automated rules pass | repaired final archive real matrix not rerun |
| Duplicate/retry/crash recovery | required | PASS | full runtime reload produced two runtime-stream accepts but one durable task row, one task ID and one terminal payload; 37/37 returned topic IDs were unique and no pending/in-progress task remained | installed Firefox replay remains in the release-artifact matrix |
| `account → event → profile/init` | required | PASS | 107 accepted events; 102 canonical topics; bounded account-partitioned seen keys | single verified account only |
| `discover → pending_eval → real LLM → recommendation` | required | PASS | five modes reached canonical candidates/recommendation with configured provider | isolated DB; no provider secret recorded |
| Setup surface and actions | required | NOT_RUN |  | final installed archive |
| Desktop surface and actions | required | PASS | real text-card/settings/status | prior Chrome build |
| Mobile surface and actions | required | PASS | real text-card/canonical URL | prior Chrome build |
| Extension popup surface and actions | required | NOT_RUN |  | final installed archive |
| Firefox installed authenticated path | required | NOT_RUN |  | build/package proof is not an installed-account E2E substitute |

- LLM provider / model / route: prior configured real-provider evidence exists; no key or credential was recorded.
- Task-mode passive event delta: 0 on prior real Chrome task.
- State-changing upstream actions actually run: none.
- Current Chrome worktree hot reload is evidenced below; frozen Chrome/Firefox archive installation is intentionally not inferred from successful builds and remains `NOT_RUN`.

### 2026-08-10 current-worktree Chrome rerun

- Isolated backend: `127.0.0.1:18420`; production/V2EX backend on `8420` was not stopped. Main `config.toml` hash was unchanged and the temporary build/config/data root was private and restored after the run.
- Authenticated smoke returned 13 canonical signals (`2 bookmark / 1 like / 10 read`) with zero forbidden Cookie/token/raw-response keys. Smoke left event/seen/candidate projections at zero. Two subsequent `profile_update + incremental` pulls returned the same 13 keys; durable ingress stayed at exactly 13 events (`2 favorite positive / 1 like positive / 10 view unknown`), 12 canonical topics and 13 bounded seen keys.
- A real profile rebuild used `deepseek-v4-flash`: 13 events, two LLM calls, about 37 seconds and reported cost about ¥0.0235. Formal discovery then completed all modes: `hot 30`, `feed 5`, `creator 25`, `related 14`, and `search 25` raw items; each producer globally retained at most 5. Real evaluation left no Linux.do `pending_eval/evaluating` rows and the recommendation API returned a canonical Linux.do card.
- Candidate canonical violations were zero. `hot/feed/creator` carried topic-level engagement, while all five retained `search` and all five retained `related` candidates lacked authoritative topic author/views/likes; these remain unavailable rather than being filled from reply-level fields.
- The MV3 reload probe is a real failure: a two-page `feed` task was claimed, then the installed development extension was reloaded. The runtime stream reconnected and one runner marker remained, but no final arrived after 193 seconds; a second reload and 97-second probe still left the same task `in_progress` with zero items. The isolated row was then failed with `extension_result_timeout`, the task tab was closed, and the original extension files/backend connection were restored.

### 2026-08-10 repaired Chrome rerun

- The repaired current-worktree build was hot-loaded into the real unpacked Chrome extension and pointed at a fresh isolated `18420` backend/data root. Formal `search` and `related` tasks each retained 2 topics; all 4/4 rows had topic-owned author, views, total likes and replies, with no reply-level fallback.
- A `feed(max_items=40,max_pages=2,request_interval_seconds=10)` task entered `in_progress`, then `/api/extension/reload` delivered a full runtime reload. The backend accepted two runtime-stream connections, while the same task ID and sole row terminally completed after about 25 seconds with 37 unique topics. No active row or Cookie/token/raw-response key remained.
- Loaded extension artifacts and the backend endpoint were restored after the run; isolated backend/config/data and staging directories were moved to Trash. No Linux.do mutation endpoint was called.

### 2026-08-10 post-cap-fix real Chrome rerun

- The current worktree build (`HEAD 7fdb1dba`) was rebuilt, hashed, hot-loaded into the real logged-in unpacked Chrome extension, and connected to a new private `127.0.0.1:18420` project/data root. The original loaded manifest, service worker and popup were hash-checked after restoration; ports `8420` and `18420` were both free at cleanup.
- Two real `bootstrap_events` pulls each returned the same 23 canonical keys (`2 bookmark / 1 like / 20 read`). Durable ingress remained exactly 23 distinct events and 22 canonical topics after replay; every task result was terminal `ok` and the recursive Cookie/token/raw-response forbidden-key count was zero.
- Real source tasks completed with `search=2`, `hot=5`, `feed=5`, `creator=3`, and `related=3`. Every retained row had a unique numeric topic ID, canonical Linux.do URL, `content_type=post`, topic author, views, total likes and replies. A two-key search with `max_items_per_keyword=3` and backend-owned global `max_items=1` returned exactly one item.
- The formal producer ran all five modes against the live extension and retained exactly 10 rows (`2` per mode), proving global limit and branch balancing. No LLM evaluation was run in this isolated rerun because the local environment had no configured usable LLM instance; the prior real-provider evaluation evidence above remains separate.
- A runner-owned two-page `feed(max_items=40, request_interval_seconds=10)` task entered `in_progress`, received a full `/api/extension/reload`, and terminally completed on the same sole task row with 36/36 unique topics. No task marker tab or active task remained, and ordinary Linux.do event count stayed at 23. A real nonexistent topic (`999999999`) returned `failed/linuxdo_http_error`, not `empty`.
- No upstream mutation was executed. The three Linux.do tabs that existed before the run were still the only Linux.do tabs after it; task tabs were closed. Temporary build/config/data/backup roots were moved to Trash after restoration.

### 2026-08-11 background-dispatch real Chrome rerun

- The current `fix/linuxdo-alarm-dispatch` worktree build was hashed and hot-loaded into the real logged-in unpacked Chrome extension, then connected to a private isolated project/data root on the normal `8420` endpoint during a bounded backend swap. The original main service worker hash and main backend were restored afterwards; no production task/database row was used.
- Formal `openbiliclaw discover --source linuxdo --limit 5` created one real `hot` task. The installed extension claimed and completed the task in 14 seconds with terminal `ok`, `response_observed=1`, 5 canonical topics and 5 retained candidates. Candidate preview contained only `linuxdo-*` strategies. Recursive result checks found zero invalid canonical IDs/URLs/types, zero per-task duplicates and zero Cookie/token/raw-response keys.
- The configured real SenseNova-compatible provider evaluated the five retained candidates with model `deepseek-v4-flash`: one batch, 10,229 input tokens, 3,889 output tokens and about 41.4 seconds. Durable outcomes were 3 cached, 1 rejected for low score and 1 rejected as recently viewed. The local Ollama embedding prefilter was observed separately and is not counted as the remote provider call.
- Runtime scheduling also completed a real `bootstrap_events` task with 296 canonical signals and a second real `hot` task with 30 raw / 5 retained topics. Bootstrap used a foreground task tab and restored the original Linux.do tab; discovery remained on the inactive-tab path. The explicit hot task completed too quickly for a mid-flight visual marker capture, so the inactive-tab assertion remains backed by the dispatcher test plus real task/tab terminal evidence rather than a screenshot.
- No Linux.do write endpoint or upstream state-changing action was called. Unrelated runtime cognition jobs later reached provider cooldown in the isolated backend; this happened after the formal discovery evaluation had terminally succeeded and is not counted as Linux.do discovery evidence.

## 最终结论

- Verdict: `incremental only`
- Required rows not PASS: stripped-credential/rejected-session scenarios, real account-switch, real product-surface click-through, and frozen Chrome/Firefox installed release-artifact scenarios listed above. The two code defects reopened by the previous real run—MV3 recovery and engagement branch parity—are now PASS.
- Intentional exclusions: early-response tap, image delivery, native deep link/save, favorite/share/danmaku aggregates.
- Safe usable slice: authenticated Chrome collection/bootstrap/discover/recommendation, durable incremental replay, all static/API/UI/build paths.
- Release mutations performed: none.
