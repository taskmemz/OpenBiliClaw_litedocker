# Eval Reason Diet — Implementation Plan

> **Spec:** [`2026-07-18-eval-reason-diet-spec.md`](./2026-07-18-eval-reason-diet-spec.md)
> **Status:** implementation complete; corrected production-equivalent replay
> gate still required before merge.

## Invariants

- System prompts remain static; the `0.5` floor is baked into prompt text.
- Configured admission floors are restricted to `[0.5, 1]`; explore remains
  `0.58`.
- Empty reasons remain valid parser/cache/persistence output.
- Evaluator reasons are bounded internal diagnostics, not delight UI copy.
- A gate failure, timeout, incomplete response, or missing artifact blocks
  merge.

## Completed implementation

- [x] Batch and single prompts emit `reason=""` below `0.5` and cap other
      reasons at 30 Chinese characters.
- [x] Prompt-call invariance and empty-reason parsing/storage tests.
- [x] Admission config lower bound and runtime normalization.
- [x] Replay sampling includes rejected low scores and preserves recent
      production traffic weights.
- [x] Replay DB is read-only; embedding L2 uses a run-scoped temporary cache.
- [x] A/A and A/B use one frozen snapshot, identical source context,
      alternating order, and at least three repeats.
- [x] Replay keeps the production 4096 output ceiling and rejects missing
      parsed responses instead of scoring them as zero.
- [x] Required JSON artifact contains snapshot digests, raw paired scores,
      routes, usage, and gate metrics.

## Required supervisor gate

```bash
.venv/bin/python scripts/run_profile_diet_ab.py \
  --arm-b reason-diet \
  --sample 100 \
  --repeats 3 \
  --db /path/to/openbiliclaw.db \
  --config /path/to/config.toml \
  --output data/eval/reason-diet.json
```

The old 2026-07-18 `21% A/A / 17% A/B` result is superseded for the
methodological reasons recorded in the spec. Do not mark this branch
release-ready until the corrected command exits zero and its artifact is
attached to the landing record.

## Verification after merge

- Observe 48 hours of `openbiliclaw cost --by caller` for
  `discovery.evaluate_batch` and `recommendation.evaluate_batch`.
- Check score/admission distributions and gateway failures; rollback on a
  valid-gate regression or material post-merge quality complaint.

## Out of scope

- Expression-copy length caps.
- Admission-threshold changes beyond rejecting unsupported values below 0.5.
- Production output-budget changes.
