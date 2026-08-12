"""Legacy ``state`` compatibility for the source-auth contract.

**Why the old ``state`` is passed through instead of derived.**

The spec originally proposed a ``derive_legacy_state(contract)`` function. That
turns out to be impossible, and the reason is itself the strongest evidence for
D1 (the legacy ``state`` conflates independent dimensions):

    platform   credential  verification   legacy state      logged_in
    bilibili   present     unverified     "ready"           True
    douyin     present     unverified     "unverified"      False

Identical orthogonal state, opposite legacy verdicts. B站 gets the benefit of
the doubt for having the right cookie fields; 抖音 does not, because its branch
was written to never claim success. No function of the orthogonal fields can
produce both answers — the legacy value carries platform-specific history that
the new fields deliberately discard.

The compatibility value therefore stays provider-owned and ships alongside the
orthogonal fields. It cannot be produced by one global mapping. A provider can,
however, intentionally move its own compatibility value when stronger evidence
arrives: 抖音 now reports ``ready`` after a successful live probe instead of
returning a response whose compatibility and orthogonal views disagree.

What we *can* enforce is that the two views never contradict each other. That
is what :func:`check_legacy_consistency` does, and the contract tests run it on
every platform × fixture combination.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from openbiliclaw.api.source_auth.contract import SourceAuthContract

# Legacy states that the old UI treated as "logged in" (models.py:627).
_LEGACY_LOGGED_IN_STATES = frozenset({"ok", "ready", "no_auth"})

# Every ``Credential`` value, for the states that genuinely constrain nothing.
# Spelled out rather than left absent: at runtime "no entry" and "any value"
# behave identically, but to a reader they do not — absence reads as an
# oversight, and in the reviewed version of this table most of them were.
_ANY_CREDENTIAL = frozenset({"present", "none", "invalid"})

# Each legacy state constrains the orthogonal fields, even though it cannot be
# derived from them.
#
# **The keys are the states the backend actually emits — all of them, and
# nothing else.** The reviewed table was built from a hand-kept list and had
# drifted in both directions at once: it constrained ``expired``, which no
# provider can produce, while saying nothing about ``missing_cookie`` /
# ``expired_cookie``, which X emits routinely. A dead key is worse than a
# missing one, because it reads as coverage — "the expired case is handled"
# was true of a state that never occurs and false of the two that do.
# ``tests/test_source_auth_contract.py`` pins the key set against the states
# the frozen cases prove reachable, so neither drift can recur silently.
#
# Several entries are permissive, each for a reason worth stating:
#
# * ``partial`` — three platforms emit it for unrelated reasons (bilibili: a
#   cookie missing login fields, i.e. ``invalid``; zhihu / reddit: an unrelated
#   task timeout, which says nothing about the credential). Pinning it to
#   ``invalid`` would force those two to call a credential structurally broken
#   on the strength of a timeout: inventing evidence, which invariant I3
#   forbids. One legacy state carrying incompatible meanings — more D1.
# * ``no_auth`` — bilibili with ``auth_method=none`` keeps whatever cookie is
#   configured, so any credential value is legitimate; YouTube has none.
#   ``auth_required`` is checked separately below, which is the real constraint.
# * X's four failure states — the health row survives the credential. A user who
#   deletes a throttled account's cookie leaves ``rate_limited`` standing with
#   nothing stored, and flagging that legitimate pair as a contract violation is
#   log noise that teaches readers to ignore the check.
_REQUIRED_CREDENTIAL: dict[str, frozenset[str]] = {
    "ok": frozenset({"present"}),
    "ready": frozenset({"present"}),
    "no_auth": _ANY_CREDENTIAL,
    "unverified": frozenset({"present", "none"}),
    "missing": frozenset({"none"}),
    "missing_cookie": frozenset({"present", "none"}),
    "expired_cookie": frozenset({"present", "none"}),
    "rate_limited": frozenset({"present", "none"}),
    "blocked": frozenset({"present", "none"}),
    "partial": _ANY_CREDENTIAL,
    "stale": frozenset({"present"}),
    "login_required": frozenset({"none"}),
    "error": frozenset({"invalid"}),
}

# The verdict axis. A partial table by design — most states say nothing about
# how hard anyone looked — but it must never name a state nothing emits.
#
# ``ok`` admits ``unverified`` because the legacy state cannot distinguish
# "a real request succeeded" from "this row was created with its default value"
# (see ``storage/x_health.py``). The orthogonal field can, and does; requiring
# them to agree would force the provider back into claiming the second is the
# first. Every X state admits ``unverified`` for the neighbouring reason: with
# no credential stored there is no verdict to hold, whatever the row says.
_REQUIRED_VERIFICATION: dict[str, frozenset[str]] = {
    "ok": frozenset({"verified", "unverified"}),
    "stale": frozenset({"stale"}),
    "missing_cookie": frozenset({"failed", "unverified"}),
    "expired_cookie": frozenset({"failed", "unverified"}),
    "rate_limited": frozenset({"rate_limited", "unverified"}),
    "blocked": frozenset({"blocked", "unverified"}),
}


def check_legacy_consistency(platform: str, contract: SourceAuthContract) -> list[str]:
    """Return a list of contradictions between legacy and orthogonal fields.

    Empty list means consistent. This is a *compatibility* check, not equality:
    ``ready`` legitimately maps to either ``verified`` or ``unverified``,
    because the legacy value never distinguished them.
    """
    problems: list[str] = []
    state = contract.legacy_state

    expected_logged_in = state in _LEGACY_LOGGED_IN_STATES
    if contract.legacy_logged_in != expected_logged_in:
        problems.append(
            f"{platform}: legacy_logged_in={contract.legacy_logged_in} contradicts "
            f"legacy_state={state!r} (models.py rule: logged_in = state in "
            f"{{ok, ready, no_auth}})"
        )

    if state == "no_auth" and contract.auth_required:
        problems.append(f"{platform}: legacy_state='no_auth' but auth_required=True")
    if state != "no_auth" and not contract.auth_required:
        problems.append(
            f"{platform}: auth_required=False but legacy_state={state!r} (expected 'no_auth')"
        )

    allowed_credential = _REQUIRED_CREDENTIAL.get(state)
    if allowed_credential is not None and contract.credential not in allowed_credential:
        problems.append(
            f"{platform}: legacy_state={state!r} requires credential in "
            f"{sorted(allowed_credential)}, got {contract.credential!r}"
        )

    allowed_verification = _REQUIRED_VERIFICATION.get(state)
    if allowed_verification is not None and contract.verification not in allowed_verification:
        problems.append(
            f"{platform}: legacy_state={state!r} requires verification in "
            f"{sorted(allowed_verification)}, got {contract.verification!r}"
        )

    # Honesty guard (invariant I3): claiming a verdict without a method, or a
    # method without a verdict, means the provider is making things up.
    if contract.verification in {"verified", "failed"} and contract.verify_method == "none":
        problems.append(
            f"{platform}: verification={contract.verification!r} with verify_method='none' "
            f"— a verdict must state how it was reached (I3)"
        )
    # An ``auth_required=False`` source with *no* credential has nothing to
    # verify, so a live method there is an overclaim (YouTube must stay ``none``).
    # But an anonymous source with an *optional* credential present — Bangumi and
    # its personal token — genuinely can verify that credential, and saying so is
    # honest, not an overclaim. The gate is credential presence, not
    # ``auth_required`` alone: a method only needs backing when there is a
    # credential it could be about. One narrow exception is a browser
    # heartbeat that positively reports an optional session is logged out:
    # that is real negative evidence about the personal tier even though the
    # public source itself remains anonymous and stores no credential.
    optional_login_evidence_without_credential = (
        not contract.auth_required
        and contract.credential == "none"
        and (
            (contract.verify_method == "browser_heartbeat" and contract.verification == "failed")
            # Linux.do discovery is anonymous-public, so task history can
            # truthfully report an explicit login rejection or an operational
            # result without inventing a stored credential. Keep this exception
            # platform-specific: no other optional-auth source currently has
            # an extension task-history channel with this meaning.
            or (
                platform == "linuxdo"
                and contract.verify_method == "task_history"
                and contract.verification in {"failed", "unverified"}
            )
        )
    )
    if (
        contract.verify_method != "none"
        and not contract.auth_required
        and contract.credential == "none"
        and not optional_login_evidence_without_credential
    ):
        problems.append(
            f"{platform}: auth_required=False with credential='none' should not carry "
            f"verify_method={contract.verify_method!r} — nothing to verify (I3)"
        )

    # A verdict that expires must say when it was reached, or the TTL is unusable.
    if (
        contract.verify_ttl_seconds is not None
        and contract.verification == "verified"
        and not contract.verified_at
    ):
        problems.append(
            f"{platform}: verify_ttl_seconds={contract.verify_ttl_seconds} but "
            f"verified_at is empty — freshness cannot be evaluated"
        )

    return problems
