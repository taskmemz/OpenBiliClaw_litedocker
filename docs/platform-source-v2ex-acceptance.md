# V2EX 平台来源验收报告

> Integration level 固定为 `full`。适用性只写 `required / N/A`，执行状态只写 `PASS / FAIL / NOT_RUN / BLOCKED`；只有全部 required 行 PASS 才能报告 complete。

## 范围与 provenance

- Integration level: `full`
- Contract: `docs/platform-source-v2ex.toml`
- Worktree / base commit: `/Users/white/workspace/OpenBiliClaw/.worktrees/v2ex-source` / `f9f4f3bf22b3eea511d907e55c448e02fe2137f7` + preserved working tree
- Python import / CLI: worktree `.venv` resolves to this worktree / `.venv/bin/openbiliclaw`
- Backend: worktree executable, PID `29291`, real config/data root, `127.0.0.1:8420`; V2EX enablement is process-scoped and no credential value is printed
- Installed browser build: Chrome development extension loaded from `/Users/white/workspace/OpenBiliClaw/.worktrees/v2ex-source/extension`; final `/api/extension/reload` returned `{"ok":true,"delivered":true}` and the extension reconnected with logged-in/private-bootstrap readiness
- Chrome package: `extension/openbiliclaw-extension-v0.3.201.zip`, SHA-256 `b470ac3a0d898b828bf2a288c9ee642debab3ef236dfaa3564f14dba3b09ad83`
- Firefox package: `extension/openbiliclaw-extension-v0.3.201-firefox.zip`, SHA-256 `9f054a82ce480109216a7e035b61f697623d2413eed170470334574bf24d86b7`; unsigned local AMO artifact as expected
- Existing user changes preserved: yes; pre-main-sync stash `codex-v2ex-pre-main-sync-20260810` remains as a recovery backup
- Upstream mutations: none; no post/reply/thank/favorite/follow action was sent

## Gate ledger

| Gate | Applicability | Status | Evidence | Remaining risk |
| --- | --- | --- | --- | --- |
| Scope/worktree provenance | required | PASS | Dedicated worktree, import path, backend PID, package hashes and recovery stash recorded | None |
| Frozen source contract | required | PASS | `docs/platform-source-v2ex.toml`; static registration check passed | Auditor intentionally leaves semantic E2E rows manual |
| Historical precedent + repair review | required | PASS | Bangumi optional PAT and browser-task repair precedents are reflected in the contract/tests | None |
| Real upstream spike + redacted fixtures | required | PASS | Five public branches plus installed-browser four-scope reads; fixtures contain synthetic/redacted values only | Hidden-profile and challenge pages remain fixture-covered |
| Canonical registry / identity / storage | required | PASS | `v2ex:<topic_id>`, canonical URLs, account-scoped seen/Affinity/snapshots and contract tests | None |
| Transport / normalizer / error taxonomy | required | PASS | Real JSON/Feed/API requests, bounded body/content-type checks, XML safety, gzip double-decode regression and live Node retest | None |
| Shared capability/auth readiness prerequisite | required | PASS | Discover is anonymous/optional PAT; private bootstrap/incremental are browser-login gated independently | None |
| Auth / credential / account resolution | required | PASS | PAT verified → browser observed → config/accepted, stale/failure/mismatch tests and real observed browser identity | No real PAT was supplied; optional PAT branches use deterministic tests |
| Browser task / MV3 recovery | required | PASS | Lease/mutex before claim, session recovery, idle/absolute deadlines, first-final wins and exact-payload 2xx ACK retry tests | None |
| Bootstrap / event / init | required | PASS | Four scope completeness, true-empty/hidden/partial semantics, Reply aggregation and 24 canonical event assertions | None |
| Post-init incremental lifecycle | required | PASS | Scheduler ownership, bounded cursors, initial snapshot seed, double-confirm retraction/restore and identity isolation tests | None |
| Formal discover / keyword dual-track / admission | required | PASS | Search/Node/Tab/Hot/Latest producer, Exa/You recall + official detail, 40/40/10/5/5 mix, budgets/diversity and candidate-pipeline tests | None |
| Eval / publication time / recommendation | required | PASS | Latest real configured LLM rerun: 3 discovered, 3 enqueued, 3 evaluated, 1 admitted and 2 honestly rejected below threshold; one usage row in an auto-deleted isolated DB | Main pool was intentionally not consumed to manufacture headroom |
| Config / API / status convergence | required | PASS | Config bounds/dedupe, source share, status/auth, verify, task, identity and scheduler tests; real 8420 reports enabled/logged-in/private-bootstrap ready | None |
| Setup surface | required | PASS | Shared status renderer + guided-init tests and final desktop settings artifact | None |
| Desktop surface | required | PASS | Compact V2EX text-card/source-card tests and `desktop-recommend.png` / `desktop-settings.png` | Built-artifact fixture, not a forced mutation of the user's recommendation pool |
| Mobile surface | required | PASS | Compact text-card/action tests and `mobile-recommend.png` | Built-artifact fixture |
| Extension popup surface | required | PASS | Popup API/helper/source tests and `extension-recommend.png`; final installed build reconnected after hot reload | Built-artifact fixture |
| Mobile credential management | N/A | PASS | Product-wide mobile credential editing is intentionally excluded; desktop/popup own optional PAT editing | None |
| Image delivery | N/A | PASS | V2EX contract is an intentional no-cover text card; no image fallback is claimed | None |
| Image proxy DNS / redirect / SSRF boundary | N/A | PASS | No V2EX image path is used | None |
| Mobile deep link | N/A | PASS | Canonical HTTPS browser fallback only | None |
| Native save | N/A | PASS | Source is strictly read-only; UI says local save | None |
| Focused + full backend verification | required | PASS | Full suite `7843 passed, 99 skipped`; Ruff, MyPy and `git diff --check` pass | Existing cross-test packaging-thread warning is non-failing and outside V2EX |
| Chrome + Firefox tests/build/assets | required | PASS | Extension `1322/1322`, typecheck, both builds, both 17-asset preflights and both package hashes pass | Firefox artifact is unsigned until AMO signing |
| Safe real E2E | required | PASS | Real anonymous requests, installed logged-in browser task, final hot reload, smoke sink isolation and real LLM replay | None |
| State-changing E2E | N/A | PASS | Contract contains no upstream mutation and none was authorized or run | None |
| Documentation / release-readiness | required | PASS | Module/API/config/runtime/storage/privacy/store/AMO/changelog/acceptance docs and final store screenshots updated | No publish was requested |
| Commit/version/tag/push/publish mutations | N/A | PASS | Not requested; no commit/version/tag/push/publish performed | None |

The static auditor reports `PASS=39`, `MISSING=0`, `N/A=10`, `MANUAL=12`, with central registration passing. The twelve `MANUAL` rows are semantic/upstream assertions; their evidence is recorded in this report rather than inferred from source text.

## Transport、身份与数据契约

- Primary/fallback owner: backend official API/feed for discovery; installed extension browser task for bootstrap/incremental; no upstream-write fallback.
- Auth comparison: anonymous public requests carry no Authorization; optional PAT uses Bearer only for API 2.0; private browser scopes require current browser readiness.
- Account resolution: verified PAT > fresh browser observation > configured username > accepted username. Mismatch pauses account projection while anonymous discovery remains available.
- Stable identity/dedupe: `v2ex:<topic_id>` and `https://www.v2ex.com/t/<topic_id>`; Replies aggregate by Topic.
- Response boundary: decoded-byte cap, strict JSON/feed content types, bounded XML, `success/result` envelope, safe error taxonomy and rate-limit reset handling. Re-materialized decoded responses strip consumed content/transfer encoding headers.
- Completeness: challenge/login/hidden/parse/rate-limit/page/item-cap outcomes never become affirmative empty or complete snapshots.
- Engagement: V2EX Reply count maps only to `reply_count`; unavailable cross-route metrics remain zero.
- Publication time: V2EX created epoch or feed timestamp normalized to UTC; task/discovery time is never substituted.

## 命令与自动验证

| Command | Exit | Summary / artifact |
| --- | ---: | --- |
| `scripts/audit_platform_source.py --contract docs/platform-source-v2ex.toml --check --json` | 0 | PASS 39 / MISSING 0 / N/A 10 / MANUAL 12; registration pass |
| `.venv/bin/pytest -q` | 0 | 7843 passed / 99 skipped |
| `.venv/bin/mypy src/` | 0 | 253 source files, no issues |
| `.venv/bin/ruff check src/ tests/` | 0 | all checks passed |
| `git diff --check` | 0 | no whitespace errors |
| `extension: npm test` | 0 | 1322 passed / 0 failed |
| `extension: npm run typecheck` | 0 | TypeScript pass |
| `extension: npm run build && npm run verify:assets` | 0 | Chrome build; 17 manifest scripts/WAR assets present |
| `extension: npm run build:firefox && npm run verify:assets:firefox` | 0 | Firefox build; 17 manifest scripts/WAR assets present |
| `extension: npm run package:only` | 0 | Chrome ZIP, 1008.0 KB, hash above |
| `extension: npm run package:firefox:only` | 0 | Firefox ZIP, 924.6 KB, hash above |

## 真实 E2E

| Scenario | Applicability | Status | Counts / isolation | Evidence |
| --- | --- | --- | --- | --- |
| Anonymous public discovery | required | PASS | Search 3; Node 5; Tab 3; Hot 3; Latest 3 | Real GET requests; 0 local writes and 0 LLM in branch smoke |
| Authenticated browser identity | required | PASS | Resolved observed identity; `logged_in=true`; private bootstrap available | Final 8420 status after extension hot reload |
| Four-scope browser task | required | PASS | topics 4 / discussion Topics 19 / favorite Topics 1 / favorite Nodes 0; all four complete; 24 events normalized | Installed Chrome development extension, real logged-in pages |
| Smoke projection boundary | required | PASS | snapshot runs 0 / projected events 0 / seen updates 0 / Affinity updates 0 | Latest `smoke_only` task completed in about 14 seconds; CLI total about 20 seconds |
| Duplicate/retry/crash recovery | required | PASS | first canonical ingest 24/0; replay 0/24; staged result replay/ACK and stale lease recovery pass | Isolated projection plus task protocol tests |
| Formal evaluator/admission | required | PASS | Node producer 3 discovered / 3 enqueued / 3 evaluated / 1 cached / 2 rejected low-score; 1 LLM usage row | Real V2EX request and user-configured `openai_compatible` LLM/Embedding; temporary DB removed on exit |
| Built recommendation surfaces | required | PASS | Desktop, mobile and popup render compact no-cover V2EX cards with badge, Node, author, time, replies, summary and actions | Final build screenshots under `docs/images/chrome-web-store/source/` |
| State-changing upstream action | N/A | PASS | 0 | V2EX client and task executor expose read-only operations only |

- Credentials and private page bodies were not printed or persisted in the report.
- The real account has no configured PAT; anonymous discovery and browser-login bootstrap remain independent as designed.
- The main pool was already at its projected target (`available + pending copy`), so the LLM acceptance replay used an isolated temporary DB instead of consuming or demoting the user's recommendations.

## 最终结论

- Verdict: `complete`
- Required rows not PASS: none
- Intentional exclusions: V2EX images, native deep link, native/upstream save and all upstream mutation actions
- Residual non-blockers: optional real-PAT smoke was not possible without a user-supplied PAT; hidden/challenge pages are deterministic fixture coverage; Firefox ZIP is unsigned until AMO submission
- Release mutations performed: none

## 2026-08-11 Search 关键词能力增量复验

> Integration level: `capability-increment`。本节追加修复证据，不改写上方首次 full-source 验收 provenance。

- 修复范围：legacy / hybrid / inspiration 三种关键词生成模式进入 V2EX 正式 Search 后的真实召回，不涉及 bootstrap、PAT、扩展任务或任何站内写操作。
- 回归合同：完整 query 命中始终优先；外部 Exa / You 与关键词生成模式解耦；外部 provider 不可用时仅在匿名 latest/hot 有界窗口内按最多 8 个非通用核心词放宽；结果继续经过统一候选 evaluator / admission。
- 自动证据：V2EX client 覆盖长词放宽、精确排序；producer 覆盖 legacy 模式仍构建外部 provider；CLI 覆盖 `--platform v2ex` 注册、grounding client 注入和异步关闭。
- 真实模型：当前默认日日新 `deepseek-v4-flash` 实际返回 `429 insufficient_quota`；只在临时内存配置中改用同一日日新 endpoint / credential 可调用的 `sensenova-6.8-flash-lite` 完成复验，未持久化模型变更。
- legacy：生成 3 个关键词；正式 Search 发现 / 入池 / 评估 `3 / 3 / 3`，准入 1、低分拒绝 2；4 次 V2EX 请求全为 GET，3 条候选均保留 `source_keyword_id`。
- hybrid：生成 9 个关键词；正式 Search 发现 / 入池 / 评估 `3 / 3 / 3`，准入 2、低分拒绝 1；8 次 V2EX 请求全为 GET，usage 同时记录 `discovery.keyword_planner`、`discovery.keyword_inspiration` 和 `discovery.evaluate_batch`。
- inspiration-only：生成 6 个关键词；正式 Search 发现 / 入池 / 评估 `3 / 3 / 3`，模型评分 `0.35 / 0.60 / 0.45`，三条均按 V2EX admission 门槛诚实拒绝；8 次 V2EX 请求全为 GET，usage 只记录 inspiration 与 evaluator caller，不误跑 merged planner。
- 隔离边界：真实请求复验使用三个独立临时数据库和内存配置覆盖，不修改 8420 配置、真实推荐池或用户事件；关键词报告只保留长度和 SHA-256 短指纹，不落原文；临时目录退出即删除。
