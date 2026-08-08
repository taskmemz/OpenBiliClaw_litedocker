# Evaluator JSON Minify Experiment Spec

**Date:** 2026-08-04
**Branch:** `perf/llm-token-diet`
**Scope:** discovery batch-evaluator prompt serialization only.

## 1. Decision context

The real 100-candidate × 3-repeat `reason-off` replay showed that successful-call prompt tokens are
about 91% of the evaluator total. Removing a small output field increased member-repair calls and total
tokens, so the next experiment targets repeated input bytes without deleting semantic content.

On the same current 100-row cohort, the four production-sized evaluator prompts contain about 230033
characters before per-item tail-recall labels. Their approximate character mix is 50% candidate JSON,
38% profile layers, 8% static system rules and 4% negative examples/wrappers. Replacing only pretty
JSON whitespace with deterministic compact separators reduces this local character proxy by 44276
characters (`19.25%`). This is not a provider-token claim; only a real replay usage artifact may establish
token savings.

## 2. Experiment boundary

### 2.1 Control A

Current production evaluator serialization:

- `ensure_ascii=False`;
- `indent=2`;
- `sort_keys=True`;
- current profile layers, negative examples and content fields;
- current reason diet, output schema, parser, cache semantics and member retry.

### 2.2 Treatment B

Treatment changes JSON whitespace only:

- `ensure_ascii=False`;
- `sort_keys=True`;
- `separators=(",", ":")`;
- no indentation or spaces outside string values.

Profile values, negative examples, candidate fields, field names, array order, XML tag order, recall
labels, system prompt, output schema and all runtime behavior remain identical to A. Field omission,
short batch IDs, body compression, batch-size changes, prefilter enforcement and two-stage classification
are explicitly out of scope.

The treatment is replay-only until every gate below passes. Production prompt bytes remain unchanged
during screening.

## 3. Determinism and prompt-cache contract

The repository convention currently prescribes pretty deterministic JSON. Treatment B must prove that
compact JSON is equally deterministic:

1. identical input renders byte-identical text repeatedly;
2. mapping insertion order does not affect output;
3. Unicode remains unescaped;
4. string contents containing spaces/newlines are not modified;
5. only JSON whitespace differs between A and B after parsing each tagged block;
6. the static system message remains byte-identical across arms and calls.

The helper must be opt-in and default to the existing pretty form. If the experiment lands, the prompt
cache convention in `CLAUDE.md` must be updated to document this narrow deterministic exception. Provider
`cached_input_tokens`, prompt tokens and cache ratio are recorded per logical run; unexplained cache
regression blocks landing even when raw prompt tokens fall.

## 4. Replay artifact contract

Add a replay-only `json-minify` arm with A=pretty production baseline and B=compact JSON. In addition to
the existing frozen snapshot, route, embedding, recall, A/A noise envelope, score, Spearman, admission and
blocking-reason evidence, the artifact records:

- per logical run prompt/completion/total/cached/uncached input tokens;
- per raw OpenAI-protocol adapter attempt usage, including successful empty-content attempts that
  trigger the adapter's response-format fallback before the final logical response;
- call, success, error and usage-missing counts;
- standard-call and member-repair counts;
- per arm prompt character and UTF-8 byte totals built from the exact messages;
- system-message digest and tagged JSON semantic digests;
- confirmation that A/B parsed JSON blocks are semantically identical;
- privacy-safe image-input digests and classification agreement/fill-rate summaries;
- provider/model/temperature/max-token/batch-size equality.

The artifact must not contain prompt text, candidate titles/body, full profile, configuration, credentials,
API keys, Cookies or raw content/image identifiers. Identity digests use a per-process random salt so the
artifact cannot be used as a reusable content-ID lookup table. Artifact schema v3 records cache accounting
semantics explicitly: supported providers normalize cold misses to zero, while Claude cache-read/create
tokens are folded into comparable total prompt input before ratio and savings checks.

## 5. Acceptance gates

### 5.1 Automated correctness

- compact renderer determinism and Unicode tests pass;
- replay arm proves production default remains pretty outside its scoped treatment;
- A/B semantic JSON equality and static-system equality pass;
- A is exact deterministic pretty JSON and B is exact deterministic compact JSON;
- multimodal image inputs and topic/style/franchise classifications remain paired across arms;
- malformed output, missing-member, retry attribution and usage aggregation tests pass;
- focused discovery/replay/cache tests, Ruff, MyPy and `git diff --check` pass.

### 5.2 Real provider gate

Use the same read-only DB/config, frozen 100-candidate cohort and at least three repeats:

```bash
.venv/bin/python scripts/run_profile_diet_ab.py \
  --arm-b json-minify --sample 100 --repeats 3 \
  --db /Users/white/workspace/OpenBiliClaw/data/openbiliclaw.db \
  --config /Users/white/workspace/OpenBiliClaw/config.toml \
  --output data/eval/json-minify.json
```

Landing requires all existing infrastructure and relative-quality gates plus:

- B prompt-token median per 100-candidate logical run is at least 10% below paired A;
- B total-token median is below paired A by at least 8%;
- B repair amplification does not exceed the A/A-derived ceiling;
- B cached-input ratio has no unexplained material regression;
- no treatment call changes route, model, temperature, max tokens or batch size;
- artifact privacy and raw-score independent recomputation pass.

Failed quality, repair, route or token gates reject the production change; thresholds are not relaxed after
observing results. A run is also invalid if a billable successful adapter attempt cannot be attributed or
lacks usage; the final response's usage must never silently overwrite an earlier empty-response attempt.

## 6. Landing and rollback

If the replay passes, production enables compact JSON only for batch evaluator profile layers, negative
examples and content batch. The eval-cache schema version is bumped because provider-visible bytes changed.
Single evaluator and recommendation prompts remain unchanged unless separately replayed.

Required landing documentation: `CLAUDE.md` prompt-cache convention, `docs/modules/discovery.md`,
`docs/changelog.md`, this spec and its plan. No architecture diagram, CLI, config, installer or UI change is
expected.

Rollback is one isolated production wiring commit: restore pretty evaluator serialization and its previous
cache version while retaining the replay arm and rejected artifact metadata for diagnosis.

## 7. Result

The clean `ad4ba670` replay completed 100 candidates × 3 repeats in `6227.3s`. All 46 successful
empty-content format fallbacks were attributed and billed in the attempt totals; three transient rate
limits recovered and remained visible in route evidence. Compact JSON reduced treatment prompt characters
by `25.05%` and UTF-8 bytes by `20.81%`. Attempt-inclusive paired median prompt/total token savings were
`13.57% / 11.29%`; aggregate three-repeat savings were `15.72% / 13.16%`, so the nominal savings gates
passed. Repair and topic/style/franchise classification gates also passed.

The experiment nevertheless failed the locked landing gate. Treatment admission deltas were
`-6pp / -6pp / +4pp`, whose `-6pp` median fell below the A/A-relative `-4pp` floor. Provider cache ratio
fell by `32.20pp` and `31.98pp` in the two otherwise complete first/third treatment repeats, while a
recovered A rate-limit call made repeat two's full usage/cache evidence incomplete. Production therefore
keeps pretty JSON; no eval-cache or `CLAUDE.md` convention change is made.

The local schema-v3 artifact is `data/eval/json-minify-ad4ba670.json` at SHA-256
`873d90c9d46c6b45465201a883044d49d921d54eaa18878e012f377a87c2c8c9`. Independent recomputation matched
all score and usage aggregates, and a scan against the 100 selected database rows found no retained title,
body, raw content ID or URL.
