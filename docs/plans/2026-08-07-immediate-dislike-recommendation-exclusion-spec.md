# Immediate Dislike Recommendation Exclusion Spec

**Created:** 2026-08-07
**Status:** implemented and accepted
**Incident evidence:** `.cc-connect/attachments/openbiliclaw (1).log`

## 1. Product decision

An ordinary dislike means “do not recommend this item/topic to me”. It does **not** mean “never
search or fetch this keyword”. Discovery may continue to search broadly because the same query can
produce unrelated or useful supply; the user-facing recommendation boundary is where the preference
must be enforced.

Two scopes must remain separate:

1. A card dislike synchronously removes that exact recommendation/content identity.
2. A confirmed topic dislike excludes matching recommendation output as soon as the preference write
   is durable, without waiting for Soul rebuild, pool purge, refresh, or cache expiry.

No ordinary dislike expires planner keywords, revokes queued source tasks, or blocks an outbound
search request. A future “never fetch this” feature would require a separate explicit user intent and
is outside this change.

## 2. Incident and root cause

The log shows a successful semantic pool purge on 2026-08-01 for confirmed rehabilitation-related
dislikes. On 2026-08-06 discovery later searched for and admitted new rehabilitation/waist/core
training candidates. The search itself is expected under the product decision above. The defect is
that a one-time purge only cleans the inventory that exists at that moment, while later candidates
can still reach recommendation surfaces.

The existing safeguards were individually correct but did not form one immediate output invariant:

- feedback projection synchronously marks the exact card processed;
- `RecommendationEngine.serve()` filters the profile's `disliked_topics`;
- new dislikes trigger exact and semantic pool cleanup;
- content evaluation receives `disliked_topics` and should lower matching candidates.

Three timing gaps remained:

1. flat `preference.disliked_topics` could be durable before asynchronous Soul rebuild, while
   `get_profile()` still returned the older Soul dislike tree;
2. `GET /api/recommendations` could reuse a one-second snapshot or return history rows without
   rechecking the latest effective dislikes;
3. reshuffle/append, OpenClaw cached fallback, and notification output did not all perform a final
   latest-preference check, so an in-flight serve could finish against an older snapshot.
4. cross-digest keyword reconciliation mixed ordinary profile dislikes into its blocked-term list,
   so a dislike-only digest change could expire an otherwise reusable pending query even though
   search authorization was not part of the user's intent.

## 3. Required invariant

```text
card dislike commit ───────────────► exact card is immediately non-actionable

confirmed topic dislike commit
        │
        ├──► effective dislike snapshot (flat preference + Soul + user overrides)
        │
        ├──► async pool purge / later Soul rebuild (inventory optimization)
        │
        └──► every recommendation output boundary rechecks latest snapshot
                    │
                    ├── history / cached first page
                    ├── reshuffle / append
                    ├── OpenClaw recommendation fallback and fresh output
                    └── proactive notification
```

Once the durable preference layer contains a topic, no later Soul rebuild or cache TTL is required
for output enforcement. Background purge reduces wasted inventory but is not the correctness
boundary.

## 4. Preference authority and cache coherence

`SoulEngine.get_effective_disliked_topics()` is the authoritative synchronous snapshot. It merges:

- current effective Soul dislikes after explicit user overrides;
- the flat preference layer, which is often written first by dialogue/feedback learning;
- domain/specific removals, so a user removal is not reintroduced from stale flat data.

`SoulEngine.get_profile()` must overlay this snapshot into the returned onion profile before any
downstream evaluation or serve call. This closes the flat-preference-to-Soul-rebuild window.

Recommendation snapshots store a digest of the normalized effective dislike set. A cache hit is
valid only when both TTL and digest match. The endpoint re-reads the digest inside its single-flight
lock and once after loading rows; if the preference changes during the read, it reloads against the
new snapshot.

Explicit profile edits also invalidate the recommendation snapshot immediately. Card feedback keeps
its existing synchronous recommendation projection and explicit cache invalidation.

## 5. Output matching and false-positive policy

History/fallback filtering mirrors the established `RecommendationEngine` serve policy:

1. normalize case and whitespace; ignore empty and one-character dislike terms;
2. hard-exclude exact structured topic matches (`topic`, `topic_key`, `topic_group`,
   `pool_topic_label` and API aliases);
3. exclude conservative substring matches in title, personalized topic label, description/body,
   author, and tags;
4. if fuzzy matching alone removes an entire multi-item recommendation window, restore rows that do
   not have an exact structured-topic match. This is the existing starvation/false-positive guard;
5. single-item push/notification surfaces disable restoration: returning no push is safer than
   sending a known fuzzy match.

This change does not add a second semantic LLM judge to the hot output path. Semantic handling stays
in the existing content evaluator and asynchronous recall-plus-LLM pool purge. The final boundary is
deterministic, fast, and consistent with current serve behavior.

The intentional trade-off is conservative: an adjacent item can survive when there is no structured
or lexical evidence, while a broad natural-language dislike cannot blank the whole feed forever.
False positives are therefore bounded and observable instead of becoming silent search suppression.

## 6. Surface contract

- `POST /api/feedback`: exact card is projected to processed synchronously; snapshot invalidates.
- `GET /api/recommendations`: reads only unprocessed history and applies the latest topic filter
  before franchise capping; the dislike digest participates in cache validity.
- profile edit API: invalidates cached recommendations after the durable edit.
- reshuffle/append: fetch profile with the flat dislike overlay, then recheck the completed in-flight
  batch against the latest effective snapshot immediately before serialization.
- OpenClaw: cached history excludes processed rows and latest dislike matches; generated output is
  rechecked at the adapter boundary.
- proactive notification: the runtime and API boundary both refuse a latest-dislike match; no fuzzy
  all-window restoration applies to a single push.
- keyword planner: cross-digest reconciliation may still retire aged, duplicate, over-cap, or
  supply-saturated pending terms, but ordinary user dislikes are not passed as blocked terms and do
  not revoke a pending search query.
- delight: existing fresh dislike filtering remains unchanged.
- CLI: recommendation generation already consumes `SoulEngine.get_profile()`, so it inherits the
  immediate flat-preference overlay; no CLI contract or command changes.

## 7. Non-goals

- no pre-fetch query guard;
- no keyword-row expiration or task revocation caused by ordinary dislikes;
- no prompt rule that turns `disliked_topics` into a search authorization list;
- no storage migration or configuration switch;
- no synchronous wait for semantic purge, Soul rebuild, or model inference in feedback/profile-edit
  requests;
- no attempt to infer a broad topic ban directly from one card click before the learning pipeline has
  durably confirmed that topic.

## 8. Tests and acceptance

Automated gates must prove:

- flat preference dislike appears in `get_profile()` before Soul rebuild while raw profile stays
  unchanged;
- an immediate second recommendations read inside the snapshot TTL sees a changed dislike digest and
  excludes the matching history row;
- exact structured matches and ordinary fuzzy matches are excluded;
- total fuzzy wipeout restores exact-safe multi-item rows, while a single push remains suppressed;
- OpenClaw fallback excludes processed/latest-disliked history and safely falls through to fresh
  generation;
- reshuffle/append final checks cannot serialize an item filtered by a dislike committed during the
  in-flight serve;
- ordinary discovery with a user-supplied query still reaches a real source transport despite a
  same-topic dislike;
- a pending query created under the pre-dislike profile digest survives reconciliation, is claimed,
  and reaches the real source transport after the dislike changes the digest;
- no previous recommendation, Soul, API, storage, runtime, or OpenClaw tests regress.

Real acceptance uses an isolated temporary database/profile and the machine's real configured
provider/source routes. It must make a genuine external search request for the incident-style query,
then prove that a matching candidate cannot cross the recommendation API after the dislike becomes
durable. The artifact records only route names, timestamps, counts, hashes, HTTP/result status, and
assertions—never cookies, keys, profile text, raw prompts, or raw responses.

Acceptance passed on 2026-08-08 against the configured authenticated Bilibili route: the same pending
query produced three real results both before and after the dislike-only digest change; an isolated
Uvicorn server was reached through real loopback TCP/HTTP, and its API moved from two visible rows to
one immediately, hiding the matching real candidate while retaining the safe control. No Soul
rebuild or snapshot-TTL wait occurred.

## 9. Documentation impact

Update recommendation, Soul, storage/API-facing documentation, architecture diagrams, bilingual
README architecture text, and changelog. Discovery documentation must explicitly state that ordinary
dislikes guide evaluation/supply efficiency but do not authorize or forbid search requests.
