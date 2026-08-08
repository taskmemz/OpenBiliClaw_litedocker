# Evaluator Sparse Payload and Row Wire Experiment Spec

**Date:** 2026-08-05
**Branch:** `perf/llm-token-diet`
**Scope:** discovery batch-evaluator candidate input and result identity only.

## 1. Decision context

The rejected JSON-minify replay proved that candidate-input bytes are worth reducing, but it also showed
that a transport-only change can move admission quality. The next work therefore separates two questions:

1. can redundant and empty candidate fields be removed without losing evaluator quality (`sparse-json`)?
2. once that exact sparse payload is fixed, can its JSON container be replaced by a deterministic row
   protocol without changing semantics (`row-wire-v1`)?

On the frozen 100-candidate cohort used by the JSON-minify replay, content-block character counts are:

| Shape | Characters | Delta vs production |
| --- | ---: | ---: |
| production pretty JSON | 114861 | baseline |
| compact production JSON | 99245 | -13.60% |
| implemented sparse compact JSON | 39836 | -65.32% |
| implemented row-wire-v1 | 30280 | -73.64% |

These measurements use the implemented serializers over the same four production-sized batches; row wire
is another `23.99%` smaller than sparse JSON. They are still character proxies, not provider-token claims.
There is no model-independent tokenizer; real prompt/completion/total usage must be recorded from each
replay response. The design must not depend on provider caching, a provider-specific tokenizer, reasoning
controls or structured-output extensions.

## 2. Canonical sparse candidate schema

Both treatments consume one shared canonical sparse representation. Transport renderers may not build
their own field semantics.

### 2.1 Identity

- Assign each request member a decimal batch-local `id` (`"0"`, `"1"`, ...).
- Do not expose `bvid`, `content_id`, `item_key` or URLs to the evaluator.
- The model must return the local `id`; the engine resolves it through the request-scoped map.
- Unknown, duplicate or missing IDs are invalid members and use the existing member-repair path.
- Multi-member positional fallback is forbidden. A singleton may retain the existing tolerant fallback.

### 2.2 Always-present semantic fields

- `id`
- `title`
- `author` (canonical `author_name or up_name`)

An empty title or author remains an empty cell/value so row width and field meaning stay deterministic.

### 2.3 Conditional semantic fields

- `source_platform`: per item only for mixed-platform batches; otherwise a batch default.
- `content_type`: per item only for mixed-type batches; otherwise a batch default.
- `mode`: only `explore`; set it only when the normalized effective context (`source_context` when
  explicitly supplied, otherwise `source_strategy`) is exactly `explore`. Prefix/suffix lookalikes such as
  `explore-*` or `xhs-extension-explore` remain normal. Every non-explore discovery path has the common batch
  default `normal` because the evaluator contract explicitly forbids search/feed/hot/related provenance
  from affecting scores.
- `body_text`, `description`, `duration`, `tags`: only when non-empty/non-zero. Existing description/body
  deduplication remains authoritative and full body text remains lossless.
- `view_count`, `like_count`, `favorite_count`, `collect_count`, `comment_count`, `share_count`,
  `danmaku_count`: only when positive.
- `rating_score`, `rating_count`, `source_rank`: only when positive.
- `related_interests`: only when recall produced a non-empty list.
- `cover_image_ref`: only when a corresponding image input was actually prepared.

### 2.4 Fields excluded from the LLM wire

- `content_url` and `cover_url`: runtime fetch/navigation metadata; the evaluator cannot visit them.
- `bvid` and `content_id`: replaced by the local ID.
- `up_name`: duplicate of canonical `author`.
- per-item `source_context`: duplicate of batch context.
- full non-explore `source_strategy`: forbidden as a scoring signal; reduced to `mode`.
- `reply_count`, `retweet_count`, `bookmark_count`: platform-specific duplicates/subsets already represented
  by generic comment/share/favorite metrics at normalization time.
- every empty optional string/list and every zero optional number.

No title/body truncation, summarization, tag deletion, profile change, negative-example change, batch-size
change, prefilter change, output-field deletion or score-threshold change is part of this experiment.

## 3. Batch envelope and transports

### 3.1 Sparse JSON

`sparse-json` uses deterministic readable JSON with the canonical sparse schema:

```json
{
  "defaults": {"mode": "normal", "source_platform": "twitter"},
  "items": [
    {"id": "0", "content_type": "thread", "title": "...", "author": "...", "body_text": "..."}
  ]
}
```

The experiment renderer uses `ensure_ascii=False`, `sort_keys=True` and compact separators. Profile and
negative-example blocks remain on production serialization so this arm changes candidate payload only.

### 3.2 Row wire v1

`row-wire-v1` serializes the exact same canonical defaults and items as UTF-8 tab-separated rows:

```text
ROW-WIRE-V1
defaults\tmode=normal\tsource_platform=twitter
columns\tid\tsource_platform\tcontent_type\tmode\ttitle\tauthor\tbody_text\tdescription\tduration\tview_count\tlike_count\tfavorite_count\tcollect_count\tcomment_count\tshare_count\tdanmaku_count\ttags\trating_score\trating_count\tsource_rank\trelated_interests\tcover_image_ref
row\t0\t\tthread\t\t...\t...\t...\t\t\t1200\t83\t12\t\t9\t4\t\t[]\t\t\t\t[]\t
```

Wire escaping is deterministic and reversible, in this order:

1. `\` → `\\`
2. tab → `\t`
3. carriage return → `\r`
4. line feed → `\n`

Lists use compact Unicode JSON inside one cell. Every row has exactly the declared number of cells. Literal
pipe syntax and exotic ASCII record/unit separators are rejected because ordinary content can contain pipes
and control-character tokenization is not portable across models.

The row renderer must have a strict decoder used by tests and replay audit. Decoding a rendered row payload
must reproduce the canonical sparse structure exactly.

## 4. Output and multimodal contract

Output remains strict JSON and retains `score`, `reason`, `topic_group`, `style_key` and `franchise_key`.
Only the identity field changes from a global content ID to `id`. The reason-diet behavior is unchanged.

Prepared images use the same request-local ID in both the text anchor and the image metadata visible to the
multimodal service. Raw image bytes, MIME type and ordering must be identical across paired arms. Text-only
fallback must omit `cover_image_ref` rather than leave a dangling anchor.

## 5. Experiment isolation

Two replay arms are required and must not be combined into one attribution claim:

### 5.1 `sparse-json`

- A: current production candidate JSON and global result identity.
- B: canonical sparse JSON and local result identity.
- Unchanged: profile, negative examples, system scoring rules, output semantic fields, model route, batch
  size, images, reasoning setting, admission and repair policy.

This arm measures semantic field pruning plus identity shortening.

### 5.2 `row-wire-v1`

- A: canonical sparse JSON.
- B: row-wire-v1 encoding of the exact same canonical structure.
- The replay audit must decode both sides and prove semantic equality before provider results are accepted.

This arm measures transport format only. A final production-vs-row summary may be computed only after both
independent gates pass; it may not replace either gate.

All treatment wiring remains replay-only until the corresponding real-provider gate passes. Production
prompt bytes and eval-cache version stay unchanged during screening.

## 6. Correctness and quality gates

### 6.1 Automated gates

- deterministic sparse normalization independent of mapping insertion order;
- exact row encode/decode round trip for Unicode, tabs, CR/LF, backslashes, empty cells and list values;
- no raw global identifier or URL in a treatment content block;
- no dangling/mismatched image anchor;
- duplicate/unknown/missing local IDs cannot bind to the wrong member;
- malformed row widths and malformed escapes fail closed;
- production default remains byte-identical;
- existing member repair, cache, multimodal and classification tests pass;
- Ruff, MyPy, full Pytest and applicable candidate-pipeline E2E pass.

### 6.2 Real replay gates

Each arm uses the frozen current 100-candidate cohort and at least three repeats. Existing A/A-relative
score, Spearman, admission, classification fill/agreement, repair amplification, route, embedding, recall,
usage completeness and privacy gates remain mandatory.

Locked savings gates:

- `sparse-json`: median paired prompt-token saving ≥ 20% and total-token saving ≥ 15%;
- `row-wire-v1`: median paired prompt-token saving ≥ 5% over sparse JSON and total-token saving > 0%;
- final row wire vs production: report aggregate savings, but do not invent a third quality threshold.

Provider cache ratios are recorded as diagnostics, not a portable correctness requirement: the feature may
be used with providers that expose no cache metric. No missing billable usage may be silently treated as zero.
Thresholds may not be relaxed after results are observed.

Artifacts remain privacy-safe: no prompts, titles, bodies, raw IDs, URLs, profile text, credentials, image
bytes or reusable unsalted identity hashes.

## 7. Landing, documentation and rollback

Landing is allowed only when both arms pass independently. Then:

- make row-wire-v1 the batch-evaluator candidate transport;
- keep strict JSON output;
- bump the evaluator cache namespace;
- document the candidate schema and transport in `docs/modules/discovery.md`, `docs/changelog.md` and
  `CLAUDE.md` prompt-cache conventions;
- retain the production JSON serializer behind one isolated rollback seam until the next release proves
  stable.

No CLI/config setting is introduced, so config, CLI, installer and architecture diagrams are out of scope.
Rollback restores the production candidate JSON renderer and prior cache namespace without reverting replay
evidence or parser hardening.

## 8. Real replay result and decision

The locked two-stage gate was completed without relaxing thresholds:

- `sparse-json` ran on commit `c3540abd` over 100 candidates × 3 repeats in `5262s`.
  Paired prompt-token savings were `17.12% / 29.19% / 27.99%` (median `27.99%`) and total-token
  savings were `10.68% / 24.32% / 24.05%` (median `24.05%`). The A/A-relative score,
  Spearman, admission, classification, repair, route, embedding, recall, usage and privacy gates passed.
  The original artifact reported a false prompt-contract failure because one seven-item A/A control response
  omitted one `reason` while every changed-transport response retained it. Commit `f644bbe9` corrected the
  auditor to attribute transport contract drift only to treatment B; offline re-audit of the immutable calls
  then passed with no blockers. Artifact SHA-256:
  `f183fce2a98ac9e0edf188c8e741b60ec78652df42b2762735ac31b2507b23f7`.
- `row-wire-v1` ran on commit `f644bbe9` over the same 100 × 3 design in `5446.6s`. Relative to sparse
  JSON, paired prompt-token savings were `-4.67% / 2.20% / 9.99%` (median `2.20%`, below the locked
  `5%` gate) and total-token savings were `-4.18% / 2.63% / 10.81%` (median `2.63%`). The general
  score/Spearman/admission gate passed, but `style_key` agreement (`0.6333` vs A/A floor `0.7667`) and
  `franchise_key` agreement (`0.8000` vs A/A floor `0.8667`) failed. One repeat also had an A-arm sparse
  root request without billable usage; this was recorded as an additional fail-closed incident rather than
  treated as zero. Artifact SHA-256:
  `8fc7065df93e2d82e7cd3647b3e0245f9462971ca0e091c823babcfed8b573e0`.
- An independent source-row scan checked 692 title/body/description/identity/URL/author values in each
  artifact and found zero raw-value hits.

Decision at completion of this two-arm experiment: reject `row-wire-v1` and do not tune the locked gates.
The subsequent sparse-only production decision is specified separately in
`docs/plans/2026-08-05-eval-sparse-json-landing-spec.md`; it does not change the row rejection or reinterpret
this experiment's thresholds.
