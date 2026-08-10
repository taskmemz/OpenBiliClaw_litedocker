# `<slug>` 平台来源验收报告

> 复制本文件填写。适用性只写 `required / N/A`，执行状态只写 `PASS / FAIL / NOT_RUN / BLOCKED`。能力型 `N/A` 附产品/架构理由和可执行的契约测试；未获请求的外部 mutation 附 scope/authorization 证据，不伪造测试。`deferred` 是 `NOT_RUN`。只有全部 required 行 PASS，最终结论才可为 `complete`。

## 范围与 provenance

- Integration level: `<full | discovery-only | capability-increment | audit-only>`
- Contract: `<path>`
- Worktree / commit: `<absolute path> / <sha>`
- Python import / CLI: `<openbiliclaw.__file__> / <executable>`
- Backend bind / data / config root: `<redacted paths>`
- Browser / extension ID / version: `<values>`
- Installed build/package path + SHA-256: `<Chrome>` / `<Firefox>`
- Existing user changes preserved: `<yes + scope>`

## Gate ledger

| Gate | Applicability | Status | Evidence | Remaining risk |
| --- | --- | --- | --- | --- |
| Scope/worktree provenance | required | NOT_RUN |  |  |
| Frozen source contract | required | NOT_RUN |  |  |
| Historical precedent + repair review | required | NOT_RUN |  |  |
| Real upstream spike + redacted fixtures | `<required or N/A>` | NOT_RUN |  |  |
| Canonical registry / identity / storage | required | NOT_RUN |  |  |
| Transport / normalizer / error taxonomy | required | NOT_RUN |  |  |
| Shared capability/auth readiness prerequisite | `<required for capability-specific, otherwise N/A>` | NOT_RUN |  |  |
| Auth / credential / account resolution | `<required or N/A>` | NOT_RUN |  |  |
| Browser task / MV3 recovery | `<required or N/A>` | NOT_RUN |  |  |
| Bootstrap / event / init | `<required or N/A>` | NOT_RUN |  |  |
| Post-init incremental lifecycle | `<required or N/A>` | NOT_RUN |  |  |
| Formal discover / keyword dual-track / admission | `<required or N/A>` | NOT_RUN |  |  |
| Eval / publication time / recommendation | `<required or N/A>` | NOT_RUN |  |  |
| Config / API / status convergence | `<required or N/A>` | NOT_RUN |  |  |
| Setup surface | `<required or N/A>` | NOT_RUN |  |  |
| Desktop surface | `<required or N/A>` | NOT_RUN |  |  |
| Mobile surface | `<required or N/A>` | NOT_RUN |  |  |
| Extension popup surface | `<required or N/A>` | NOT_RUN |  |  |
| Mobile credential management | `<required or N/A with contract test>` | NOT_RUN |  |  |
| Image delivery | `<required or N/A>` | NOT_RUN |  |  |
| Image proxy DNS / redirect / SSRF boundary | `<required for proxy, otherwise N/A>` | NOT_RUN |  |  |
| Mobile deep link | `<required or N/A>` | NOT_RUN |  |  |
| Native save | `<required or N/A>` | NOT_RUN |  |  |
| Focused + full backend verification | `<required or N/A>` | NOT_RUN |  |  |
| Chrome + Firefox tests/build/assets | `<required or N/A>` | NOT_RUN |  |  |
| Safe real E2E | `<required or N/A>` | NOT_RUN |  |  |
| State-changing E2E | `<required or N/A>` | NOT_RUN |  |  |
| Documentation / release-readiness | required | NOT_RUN |  |  |
| Commit/version/tag/push/publish mutations | `<required if requested, otherwise N/A>` | NOT_RUN |  |  |

## Transport、身份与数据契约

- Primary / fallback owner: `<backend / browser / extension / external-cli / shared / none>`
- Auth comparison: `<anonymous / authenticated / stripped-credential evidence>`
- Account resolution and mismatch behavior: `<evidence>`
- Stable identity / URL / dedupe: `<sample, no private account identifiers>`
- Upstream envelope / pagination / empty / partial / rate-limit: `<fixture paths>`
- Engagement availability and publication time: `<six-count + UTC evidence>`

## 命令与自动验证

| Command | Exit | Summary / artifact |
| --- | ---: | --- |
| `PYTHONPATH="$PWD/src" "$SOURCE_SKILL_PYTHON" scripts/audit_platform_source.py ... --check --json` |  |  |
| exact contract exclusion nodeids (all capability N/A; no skip/xfail) |  |  |
| focused backend tests |  |  |
| full backend tests |  |  |
| extension tests/typecheck |  |  |
| Chrome build + asset verify |  |  |
| Firefox build + asset verify |  |  |
| package/install provenance |  |  |

## 真实 E2E

| Scenario | Applicability | Status | Counts / DB sample / idempotency | Diagnostic / artifact |
| --- | --- | --- | --- | --- |
| Anonymous / stripped credential | `<required or N/A>` | NOT_RUN |  |  |
| Authenticated / verified identity | `<required or N/A>` | NOT_RUN |  |  |
| Rejected/expired credential | `<required or N/A>` | NOT_RUN |  |  |
| Empty vs no-observer vs partial vs rate-limit | `<required or N/A>` | NOT_RUN |  |  |
| Duplicate/retry/crash recovery | `<required or N/A>` | NOT_RUN |  |  |
| `account → event → profile/init` | `<required or N/A>` | NOT_RUN |  |  |
| `discover → pending_eval → real LLM → recommendation` | `<required or N/A>` | NOT_RUN |  |  |
| Setup surface and actions | `<required or N/A>` | NOT_RUN |  |  |
| Desktop surface and actions | `<required or N/A>` | NOT_RUN |  |  |
| Mobile surface and actions | `<required or N/A>` | NOT_RUN |  |  |
| Extension popup surface and actions | `<required or N/A>` | NOT_RUN |  |  |

- LLM provider / model / route: `<configured values; no key>`
- Task-mode passive event delta: `<0 or N/A>`
- Smoke projection deltas: `<before/after delta for all nine [smoke.sinks], matching its per-sink policy>`
- State-changing upstream actions actually run: `none` by default; otherwise list exact authorized platform/action/public item/target.

## 最终结论

- Verdict: `<complete | incremental only | blocked>` (`incremental only` = 已交付安全可用切片但 required 证据未全；`blocked` = 缺外部前提/授权或共享架构安全前置能力，无法继续)
- Required rows not PASS: `<none or list>`
- Intentional exclusions: `<contract keys + evidence>`
- Deferred work / blockers: `<NOT_RUN/BLOCKED rows>`
- Release mutations performed: `<none unless explicitly requested>`
