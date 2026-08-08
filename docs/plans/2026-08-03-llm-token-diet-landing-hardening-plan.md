# LLM Token Diet Landing Hardening — Implementation Plan

> **Spec:** [`2026-08-03-llm-token-diet-landing-hardening-spec.md`](./2026-08-03-llm-token-diet-landing-hardening-spec.md)
> **Owner:** root agent（规格、集成、验收）
> **Implementation:** bounded multi-agent tasks with non-overlapping file ownership
> **Status:** implementation complete; acceptance runs from the resulting clean commit

## 0. Working rules

- 所有实现基于 rebase 后的当前 `main`，rebase 前创建可恢复备份 ref。
- 子 agent 不改自己任务边界以外的文件；共享测试文件必须提前声明 owner。
- 每个任务先补失败测试，再改实现，再跑 focused tests。
- 主 agent 审查每个 diff，不把子 agent 的“完成”当成验收结果。
- 不调用已失效的 cc review。
- 不提交真实 config、Cookie、API key、profile 正文或 DB。

## Task 1 — Freeze landing contract

**Owner:** root
**Files:** this spec/plan

- [x] 记录 replay、cache、reason、rebase 与文档阻塞项。
- [x] 定义 effective-profile、embedding、route、body-cap 否决条件和 artifact contract。
- [x] 定义自动化、真实 replay 和 runtime/E2E 三层验收。

**Gate:** spec 和 plan 在任何生产代码修改前存在并可引用。

## Task 2 — Rebase current main safely

**Owner:** root

1. 记录 branch HEAD、merge-base、ahead/behind、dirty state。
2. 创建 `backup/perf-llm-token-diet-pre-rebase-20260803`。
3. `git rebase main`，逐文件语义化解决冲突。
4. 特别核对 config defaults、route docs、current-main tests 与 token-diet knobs。
5. 运行冲突敏感 focused tests：config、OpenClaw、embedding、memory、recommendation。

**Gate:** worktree clean、无 conflict markers、`git diff --check` pass；备份 ref 可恢复。

**Result:** semantic rebase completed on `main@1d9c2a4e`; backup ref
`backup/perf-llm-token-diet-pre-rebase-20260803` preserves the pre-rebase head. Integration found
and repaired a serializer-move regression: topic-lifecycle filtering remains owned by
`soul/profile_views.py`, while `_utils` is only a compatibility re-export.

## Task 3 — Replay production-equivalence hardening

**Owner:** `luna_max_replay` sub-agent
**Primary files:**

- `scripts/run_profile_diet_ab.py`
- `tests/test_profile_diet_ab.py`

Implementation checklist:

- [x] effective profile loader applies overrides and active speculations；
- [x] strict embedding audit detects exception/empty/nonfinite/dimension mismatch；
- [x] embedding cache lifetime covers the whole run and closes in `finally`；
- [x] per logical run call attribution and route-equivalence validation；
- [x] 临时恢复 faithful `body-cap` legacy-vs-production arm 并完成真实对照；
- [x] 真实 gate 否决 200+100 后删除该正式 arm，并把生产正文路径完整回滚；
- [x] artifact records blocking reasons, recall/route/embedding audit without private payloads；
- [x] unit tests cover every failure and valid zero-tail/zero-similarity case；
- [x] mirror topic lifecycle, production 30-item claim grouping and `mixed` context；reject
      production `eval_prefilter_mode=enforce` because controlled replay intentionally uses `off`；
- [x] extract and test final blocking-reason aggregation so every failed sub-audit blocks landing。
- [x] retry only recovered transient provider rate limits after cooldown, restoring chunk evaluation
      state and retaining failed attempts in route audit；classification stops at the normalized
      provider-limit boundary, retry budgets reset per chunk, and 402/billing errors remain fatal；
      clean-commit sustained throttling expands the bounded schedule to 65 / 130 / 260 / 520 seconds。

**Focused gate:**

```bash
.venv/bin/python -m pytest tests/test_profile_diet_ab.py -q
```

## Task 4 — Evaluation cache input closure

**Owner:** `luna_max_cache` sub-agent
**Primary files:**

- `src/openbiliclaw/discovery/engine.py`
- `tests/test_discovery_engine.py`

Implementation checklist:

- [x] deterministic prompt-visible content digest includes effective source context；
- [x] embedding/recall namespace participates in single and batch cache keys；
- [x] normal cache entries are written only after complete recall or explicit no-recall mode；
- [x] transient/partial recall failure cannot poison the normal cache；
- [x] same content/profile/negative inputs still hit LRU without repeated LLM work；
- [x] changed body/metrics/context/model namespace invalidates；
- [x] heterogeneous outer prompt metadata and actual vision attempts bypass normal per-item cache；
- [x] raw cache hits reapply franchise/style caps with cold/warm-stable caller grouping, including
      enforce-prefilter boundary compression；empty metadata clears stale object state；
- [x] existing legacy tuple compatibility and 4096-entry LRU behavior remain green。

**Focused gate:**

```bash
.venv/bin/python -m pytest tests/test_discovery_engine.py -q
```

## Task 5 — Runtime reason normalization

**Owner:** `luna_max_reason` sub-agent
**Primary files:**

- `src/openbiliclaw/discovery/engine.py` only after Task 4 owner coordination
- reason-specific tests in `tests/test_discovery_engine.py`

Because Task 4 and Task 5 share the same files, Task 5 initially prepares a small patch/design in
an isolated commit or waits for Task 4. The root agent decides integration order; agents must not
edit the same file concurrently.

- [x] add one pure `normalize_evaluation_reason(score, raw_reason)` helper；
- [x] single and batch paths normalize before object/cache persistence；
- [x] `<0.5` always empty；`>=0.5` at most 30 code points；
- [x] missing empty accepted, non-string continues malformed retry/error path；
- [x] scoring/admission semantics unchanged；prompt wording now labels reason as an internal
      diagnostic and states the exact Unicode limit。

**Focused gate:** reason prompt + single/batch/cache/persistence tests.

## Task 6 — Main-agent integration and documentation

**Owner:** root

- [x] review agent diffs against spec invariants；
- [x] resolve overlap without dropping tests；
- [x] update `docs/modules/discovery.md` retry/cache/reason/body-cap rejection truth；
- [x] update `docs/modules/recommendation.md` to document full-body rollback；
- [x] update `docs/modules/llm.md`, `docs/modules/config.md`, changelog and architecture/data-flow
      notes where ownership changed；
- [x] mark superseded historical gate claims explicitly；
- [x] update this plan with automated commands/results；real replay evidence remains in Task 8。

**Gate:** documentation contains no contradictory retry/body-cap-rejection/replay claims found by targeted
`rg`.

## Task 7 — Automated verification

**Owner:** root

Run in this order:

```bash
.venv/bin/ruff format --check src/ tests/ scripts/run_profile_diet_ab.py
.venv/bin/ruff check src/ tests/ scripts/run_profile_diet_ab.py
.venv/bin/mypy src/
git diff --check

.venv/bin/python -m pytest \
  tests/test_profile_diet_ab.py \
  tests/test_discovery_engine.py \
  tests/test_profile_views.py \
  tests/test_profile_views_guards.py \
  tests/test_candidate_eval_coordinator.py \
  tests/test_discovery_candidate_pipeline.py \
  tests/test_config.py \
  tests/test_api_app.py \
  tests/test_llm_service.py \
  tests/test_memory_manager.py \
  tests/test_recommendation_engine.py -q

.venv/bin/python -m pytest -q
```

If a failure also occurs on current `main`, record it as a baseline defect but do not waive it:
either incorporate the current-main fix during rebase or document an environment-only skip with evidence.

**Result (2026-08-03):**

- Ruff format check: 543 files formatted；Ruff lint: pass；MyPy: 236 source files, pass；
  `git diff --check`: pass。
- Replay / discovery / recommendation focused group after the 96 / 16 correction: 305 passed in
  15.66s；required focused integration group: 1356 passed in 129.86s。
- Full repository after the same correction: 7036 passed, 93 environment/platform skips in
  619.93s；zero failures。
- Extension final-main compatibility: TypeScript typecheck pass；1244 Node tests pass after the
  worktree installed lockfile-pinned dev dependencies。
- The rebase exposed two current-main hygiene failures (one missing blank line and one import order);
  both were mechanically formatted so the repository-wide Ruff commands now pass without waiver。

## Task 8 — Replay and end-to-end acceptance

**Owner:** root

1. Validate DB/config prerequisites without printing secrets。
2. Run compact、reason-diet and replay-only reason-off commands from Spec §6B；retain and independently validate the rejected
   body-cap diagnostic artifact without rerunning a removed production feature。
3. Validate each JSON artifact structurally and recompute key metrics independently from raw scores。
4. Run deterministic candidate-pipeline E2E cases from Spec §6C。
5. Smoke `openbiliclaw config-show` and relevant API config serialization。
6. Record exact commit, commands, pass counts, skips, artifact paths/digests and unresolved environmental
   limitations。

**Final gate:** no code/test/docs/replay blocker remains. If a required real-data or provider prerequisite
is unavailable, the branch is reported blocked rather than described as release-ready.

**Progress:** deterministic SQLite E2E now covers enqueue → 60s coalescing wait → tokenized claim →
real batch parser/runtime reason normalization → eval LRU → admission/content cache, including a warm
eval-cache replay with zero additional provider calls. Production `config-show` exits 0 and the safe
acceptance fields resolve to prefilter `shadow`, admission `0.6`, coalescing `15 / 90s`, topic lifecycle
`off`. A pre-hardening compact run passed, but it is intentionally not final evidence because a subsequent
provider 429 caused the replay-only bounded cooldown retry change. The first compact run on that hardened
commit then correctly failed the unchanged relative
quality gate at 64 / 12 (Spearman median `0.494686 < 0.570454`; admission delta median
`-0.09 < -0.07`). Root diagnosis found that this cut saved only about 11% on the then-current profile
while removing model-visible semantic tail, so the first correction used 80 interests / 16 specifics.
The full-body rollback's focused discovery/replay/recommendation group passed 305 tests；the required
integration group passed 1356 tests, and the full repository passed 7036 tests with 93
environment/platform skips. Extension TypeScript typecheck and all 1244 Node tests also passed.
The 80 / 16 compact artifact on `11f77a64` passed its final gate, while the strict Reddit 100×3 body-cap artifact failed all three quality
dimensions (18% flip vs 8% ceiling, 0.192031 Spearman vs 0.632378 floor, -11pp admission vs -3pp floor).
Because the cap retained only 12.95% of affected body characters, all discovery/recommendation body
truncation and the formal replay arm were removed instead of tuning the gate. Compact and reason-diet are
rerun from the resulting clean commit；the rejected artifact remains diagnostic evidence only. The first
80 / 16 rerun also
exposed and closed a replay-only classifier bug where raw SDK 429 metadata overrode the adapter's
normalized transient-rate-limit decision；no quality metrics were emitted by that aborted run.
The next clean-commit compact run on `0395e138` progressed for 97 minutes but correctly exited nonzero
when the final 10-item chunk exhausted 65 / 130 second retries: the gateway's successful empty/think
responses forced immediate protocol-level follow-up calls that repeatedly hit genuine 429s. No artifact
or quality result was emitted. Replay-only cooldown is therefore still bounded but extended to
65 / 130 / 260 / 520 seconds, with the schedule recorded in artifact v2；production retry behavior,
model-visible inputs and every quality threshold remain unchanged.

The resulting clean-commit 80 / 16 replay on `397fe03e` completed its infrastructure audit but failed
the unchanged quality gate: treatment flip-rate median `15% > 7%` ceiling and Spearman median
`0.356586 < 0.564777` floor；admission delta median `+1pp` passed. Structural comparison showed that the
effective profile had grown to 87 interests, so the compact block now dropped ranks 81..87 even though
the older frozen snapshot had fit inside 80. The failed artifact is archived as
`data/eval/profile-diet-compact-failed-397fe03e.json` with SHA-256
`d58bb6888276e9c0b40c821d2d450f478645771380a8b4f88d46cdbc06dadcdc`.

The second correction used 96 interests / 16 specifics and recall ranks 97..256. It preserved
all current 87 interests (`98.29%` of current serialized profile characters) while the maxed fixture's
actual layered prompt still shrinks `61.17%`, preserving the existing ≥60% contract；128 would only
shrink the same layered fixture by `56.74%`. After this correction, the 305-test focused group, 1356-test
required integration group and full 7036-test repository all pass with zero failures. A new clean-commit
compact replay, followed by reason-diet, remained required before Task 8 could close.

That clean-commit replay on `f26c63e5` completed in 6034.3s and still failed: treatment flip median
`18% > 16%` control ceiling, while Spearman `0.616499` and admission delta `+10pp` passed. The artifact
is archived as `data/eval/profile-diet-compact-failed-f26c63e5.json` (SHA-256
`734fad9310a125d1763e69d3f0f89600862c1715d6a64e84ecd4f1f984231428`). Independent usage aggregation
showed that 96 / 16 only saves 696 input tokens per standard 100-candidate / 4-batch run (`0.67%`)；with
actual member-repair calls, the B arm used 17 successful calls vs A's 14 and `14.75%` more total tokens.

The user therefore selected a materially smaller 48-interest experiment rather than 32. The code now
uses 48 / 16 with recall ranks 49..256. On the current 93-interest effective profile, deterministic
layered-profile characters shrink `26.02%`；the maxed fixture shrinks `67.82%`. Focused profile-view /
discovery / recommendation / replay tests pass 310 cases. A clean commit and unchanged 100×3 replay are
still required before this experiment can be accepted or rejected.

The clean experiment commit is `55e89797`. A one-repeat provider diagnostic completed its calls but was
intentionally rejected by the existing minimum-three relative gate before artifact serialization, so no
provider-token claim is made from that run. The replacement no-evaluator diagnostic used the same 100
real candidates and actual prompt construction with 300 deterministic recalled labels: full prompts were
249918 characters / 124141 `cl100k_base` tokens, while 48 / 16 prompts were 224580 characters / 115833
tokens, a `10.14%` character and `6.69%` diagnostic-token reduction. Without recall the token reduction is
`9.33%`; the 3-label-per-item quality counterweight consumes about `2.64pp`. Formal DeepSeek usage and
quality still require the unchanged 100×3 run.

The user then requested a true evaluator `reason-off` measurement. A new replay-only arm keeps A on the
current production reason diet and removes the reason field only from B; production prompts remain
unchanged. The artifact fails closed if B still emits reason, and compares privacy-safe
`topic_group/style_key/franchise_key` fill/agreement against the repeated A/A envelope in addition to
score/Spearman/admission. It also aggregates prompt/completion/total usage per logical run; downstream
style/franchise cap-drop remains explicitly unmeasured. A deterministic proxy over the current 100-row
cohort (58 below 0.5, 42 at/above 0.5) estimates that omitting the field reduces minified structured-output
characters by `16.66%` and UTF-8 bytes by `24.77%` versus the current reason diet. These are not provider
tokens; the real DeepSeek usage and quality conclusion require the new 100×3 artifact.

The clean-commit `reason-off` replay on `c6327506` completed 100 candidates × 3 repeats and correctly
exited nonzero on the unchanged relative quality gate. B emitted zero reason fields, yet application-visible
successful-call usage increased from A's `313906 / 35498 / 349404` prompt / completion / total tokens to
B's `410877 / 49300 / 460177`, or `+30.89% / +38.88% / +31.70%`. The first repeat had three error calls in
both arms; the two zero-error repeats still increased total usage by `7.18%` and `25.97%`, independently
ruling out rate-limit noise as the explanation. B required 7 / 6 / 5 successful calls versus A's 4 / 6 / 4,
showing that omitted reason made structured member completion/repair operationally more expensive.
Treatment Spearman median `0.709700`, flip median `5%`, and all privacy-safe classification gates passed,
but admission delta median `-5pp` fell below the A/A-derived `+2pp` floor. Raw scores and admission vectors
were independently recomputed exactly. The ignored local artifact is
`data/eval/reason-off-c6327506.json`, SHA-256
`f6b7276fc5024faa3a61c2524124bf43644044029fb6c93490a50ea30aaa81b2`; it contains no API key, Cookie,
authorization or token credential keys. Production therefore keeps the current reason diet; `reason-off`
remains a rejected diagnostic arm rather than a landing change.

## Task 9 — Landing handoff

**Owner:** root

- [ ] concise change summary；
- [ ] explicit review findings and how each was closed；
- [ ] test/replay evidence；
- [ ] remaining rollout observation and rollback points；
- [ ] confirm no secrets/artifacts intended to stay local were committed。
