"""Verdict cache for the live-probe sources (B站 / 抖音).

**Why the status endpoint reads a cache instead of probing.**

``GET /api/sources/status`` is polled by every open settings page (desktop Web,
extension popup) on a ~30s timer. Some credential-backed platforms can be verified by
an outbound request — B站's nav endpoint and 抖音's
``/aweme/v1/web/user/profile/self/`` (spec D11) — and the naive wiring would
fire that request from the status handler. That is exactly the shape that gets
an account risk-flagged: an idle settings tab would hit 抖音 twice a minute,
forever, without the user ever asking for a verification.

Nor can the handler simply ``await`` a probe: ``sources_status`` is a sync
FastAPI endpoint, and :func:`~openbiliclaw.sources.douyin_login_probe.probe_douyin_login`
is async. Making the endpoint async to accommodate one platform would widen the
blast radius of a refactor whose whole premise is "legacy output unchanged".

So the split is:

* **write** — the verify action (``POST /api/sources/{slug}/verify``) and any
  other code path that genuinely talked to the platform calls
  :meth:`LiveProbeCache.record`.
* **read** — the status providers call :meth:`LiveProbeCache.peek`, which never
  performs I/O of any kind. Until something records a verdict the platform
  honestly reports ``verification="unverified"``.

Freshness is evaluated against ``time.monotonic()`` so a system clock jump
cannot make a verdict look arbitrarily fresh or stale, while ``checked_at``
keeps a wall-clock ISO string for display (the contract's ``verified_at``).

**This is the single store for B站's live verdict.** ``runtime.init_prereqs``
used to keep its own, feeding ``GET /api/init-status`` while this one fed
``GET /api/sources/status`` — two caches, one credential, neither aware of the
other. That is D3's exact shape on the verdict axis (Task 5 having already
closed it on the credential axis), and it would have become user-visible the
moment the frontends started reading the orthogonal fields. ``init_prereqs``
now records into and reads from this store, so a probe fired by either entry
point is seen by both.

The two surfaces still *render* one verdict differently, and that is correct
rather than a leak: guided-init shows a transport failure as ``failed`` with a
proxy hint because it must block a broken setup, while the contract maps the
same ``network_error`` verdict to ``verification="unverified"`` because a flaky
proxy is not an expired cookie. One verdict, two honest readings — as opposed
to two verdicts, which is what this replaced.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from datetime import UTC, datetime

# Freshness policy for live-probe verdicts, owned here because this module owns
# the verdicts. A success is trusted for a minute; a failure is re-checked
# promptly so a credential the user just fixed turns green quickly instead of
# staying red for the full success window.
#
# These were duplicated in ``runtime.init_prereqs`` and
# ``api.source_auth.providers`` — two copies of one policy, which is the same
# shape of drift the whole contract exists to remove. Both now import from here.
PROBE_OK_TTL_SECONDS = 60
PROBE_FAIL_TTL_SECONDS = 10


@dataclass(frozen=True)
class ProbeVerdict:
    """One recorded outcome of an outbound login probe."""

    authenticated: bool
    # Wall clock, ISO-8601. Surfaced to users as ``verified_at``.
    checked_at: str
    # ``time.monotonic()`` at record time — immune to clock adjustments, and
    # the only value TTL math is allowed to use.
    recorded_at: float
    detail: str = ""
    # True when the probe failed at the transport layer (proxy, timeout, risk
    # control) rather than because the credential is logged out. Such a verdict
    # says nothing about the credential and must not be reported as ``failed``.
    network_error: bool = False
    #: Digest of the *login-bearing* names in the credential this verdict is
    #: about (see ``write.credential_fingerprint``). "" for a verdict recorded
    #: before this field existed, or by a caller that had no credential in hand.
    #:
    #: A verdict is evidence about one credential, not about a platform. Without
    #: this the write gate reused any fresh "logged in" for the slug, so a dead
    #: cookie submitted within the success window was waved onto disk unprobed —
    #: the one promise the write path exists to keep, broken in the one case the
    #: user cannot see. It is a digest and never a value: nothing here may be
    #: logged or serialised, and a hash keeps that structurally true.
    credential_fingerprint: str = ""
    #: Who the platform said this credential belongs to. Carried so a cache hit
    #: can answer as completely as the probe that filled it — the deprecated
    #: ``POST /api/bilibili/cookie`` returns these two verbatim, and an installed
    #: extension reading ``user_id`` cannot tell "cache hit" from "logged out".
    username: str = ""
    user_id: int = 0

    def age_seconds(self) -> float:
        return max(0.0, time.monotonic() - self.recorded_at)

    def is_fresh(self, ttl_seconds: float) -> bool:
        return self.age_seconds() < ttl_seconds

    @property
    def ttl_seconds(self) -> int:
        """Freshness window that applies to *this* verdict."""
        return PROBE_OK_TTL_SECONDS if self.authenticated else PROBE_FAIL_TTL_SECONDS

    def is_current(self) -> bool:
        """Whether this verdict is still inside its own freshness window."""
        return self.is_fresh(self.ttl_seconds)


class LiveProbeCache:
    """Last known live-probe verdict per platform slug.

    Intentionally tiny and process-local. Losing it on restart is harmless:
    a missing verdict degrades to ``unverified``, which is the truth for a
    process that has not probed anything yet.
    """

    def __init__(self) -> None:
        self._verdicts: dict[str, ProbeVerdict] = {}

    def record(
        self,
        slug: str,
        *,
        authenticated: bool,
        detail: str = "",
        network_error: bool = False,
        fingerprint: str = "",
        username: str = "",
        user_id: int = 0,
    ) -> ProbeVerdict:
        """Store the outcome of a probe that actually went out."""
        verdict = ProbeVerdict(
            authenticated=authenticated,
            checked_at=datetime.now(UTC).isoformat(),
            recorded_at=time.monotonic(),
            detail=detail,
            network_error=network_error,
            credential_fingerprint=fingerprint,
            username=username,
            user_id=user_id,
        )
        self._verdicts[slug] = verdict
        return verdict

    def peek(self, slug: str) -> ProbeVerdict | None:
        """Last verdict for *slug*, or None. Never performs I/O.

        Answers "what did we last conclude about this platform". Callers
        holding a specific credential want :meth:`peek_matching` instead.
        """
        return self._verdicts.get(slug)

    def peek_matching(self, slug: str, fingerprint: str) -> ProbeVerdict | None:
        """Last verdict for *slug*, but only if it is about *this* credential.

        Strict on purpose, in both directions: an unfingerprinted verdict and a
        blank *fingerprint* both return None. The only caller is the write gate,
        where "not sure this is the same credential" must mean "go and probe",
        never "close enough" — a wrong answer here puts a dead credential on
        disk, whereas a redundant probe merely costs one request.
        """
        verdict = self._verdicts.get(slug)
        if verdict is None or not fingerprint:
            return None
        if verdict.credential_fingerprint != fingerprint:
            return None
        return verdict

    def contradicts(self, verdict: ProbeVerdict | None, fingerprint: str) -> bool:
        """Whether *verdict* is demonstrably about a different credential.

        The read path's counterpart to :meth:`peek_matching`, and deliberately
        laxer: only a *known* mismatch disqualifies a verdict, so one recorded
        without a fingerprint still answers. The status endpoint's exposure is
        bounded by the 60s TTL and self-heals; refusing to display an otherwise
        good verdict because its origin predates this field would be a
        regression traded for nothing.
        """
        if verdict is None or not fingerprint or not verdict.credential_fingerprint:
            return False
        return verdict.credential_fingerprint != fingerprint

    def clear(self, slug: str | None = None) -> None:
        """Drop one or all verdicts (config reload, tests)."""
        if slug is None:
            self._verdicts.clear()
        else:
            self._verdicts.pop(slug, None)


# Process-wide default. Task 6's verify endpoint writes here; the status
# providers read here. Kept module-level rather than bound to RuntimeContext so
# a verdict survives the context rebuild that a config save triggers — the
# cookie may be untouched by an unrelated config edit, and discarding a fresh
# verification would send the UI back to "unverified" for no reason.
LIVE_PROBES = LiveProbeCache()
