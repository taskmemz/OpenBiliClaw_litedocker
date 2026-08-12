"""Orthogonal source-auth contract.

The legacy ``SourceStatusItem.state`` packs four independent questions into one
string, which is why the original platform set ended up with mutually incomparable green
lights (see ``docs/plans/2026-07-18-source-auth-contract-spec.md`` D1/D2). This
module defines the replacement: four dimensions that vary independently, plus
an explicit statement of *how strong* the evidence behind the verdict is.

This ships alongside compatibility fields in the old vocabulary. See
``legacy.py`` for why those fields remain provider-owned rather than globally
derived from the orthogonal dimensions.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Literal

from pydantic import BaseModel, Field, field_validator, model_validator

# Is there a credential at all? Orthogonal to whether it works.
Credential = Literal[
    "none",  # nothing stored
    "present",  # stored and structurally plausible
    "invalid",  # stored but structurally broken (e.g. B站 cookie missing fields)
]

# Where the credential lives. Surfaced so the UI can say "set via env var" and
# so support questions ("where do I clear it?") have one answer per platform.
CredentialOrigin = Literal[
    "config",  # config.toml (only B站 today — see spec D5)
    "env",  # environment variable override
    "data_file",  # data/*.json
    "extension",  # browser extension holds it; backend stores only a flag
    "external_cli",  # a third-party tool's credential store (rdt-cli)
    "none",
]

# What the last verification concluded. Orthogonal to whether a credential exists.
Verification = Literal[
    "verified",  # the credential was confirmed working
    "failed",  # confirmed NOT working (logged out / rejected)
    "stale",  # was verified once, but the freshness window has lapsed
    "unverified",  # never confirmed either way
    "rate_limited",  # platform is throttling us; says nothing about the credential
    "blocked",  # platform refused us; says nothing about the credential
]

# How the verdict was reached. This is the field that makes a green light
# honest: it tells the user whether "ready" means "we asked the platform" or
# "a file exists on disk".
VerifyMethod = Literal[
    "live_probe",  # an outbound request was made to the platform
    "passive_health",  # inferred from errors on real traffic
    "browser_heartbeat",  # the extension reported the login cookie exists
    "local_file",  # a local credential file was read; no network
    "task_history",  # inferred from the outcome of past tasks
    "none",  # no verification capability (or none needed)
]

# Some sources expose more than one independently authenticated capability.  A
# V2EX public feed, for example, is usable anonymously while its browser-owned
# account bootstrap needs a fresh signed-in session.  Keep this axis separate
# from the source-wide compatibility fields below so a public green light can
# never be mistaken for proof that private account collection is ready.
CapabilityAuthMode = Literal[
    "anonymous",
    "optional-credential",
    "login-required",
]

CapabilityReadinessState = Literal[
    "ready",
    "login_required",
    "identity_required",
    "identity_mismatch",
    "identity_switch_required",
    "stale",
    "unavailable",
]
CapabilityReadiness = Literal[
    "ready",
    "login_required",
    "unverified",
    "stale",
    "rate_limited",
    "blocked",
]


class SourceCapabilityAuth(BaseModel):
    """Authentication/readiness contract with legacy readiness projection."""

    mode: CapabilityAuthMode
    required: bool = True
    ready: bool = False
    state: CapabilityReadinessState = "unavailable"
    # Compatibility projection emitted only when a legacy provider explicitly
    # supplies it. New capability contracts expose canonical ``ready`` and
    # ``state`` fields without growing a duplicate JSON key.
    readiness: CapabilityReadiness | None = Field(
        default=None,
        exclude_if=lambda value: value is None,
    )
    detail: str = ""

    @model_validator(mode="after")
    def _sync_readiness_projections(self) -> SourceCapabilityAuth:
        fields = self.model_fields_set
        if "readiness" in fields and "state" not in fields and "ready" not in fields:
            self.ready = self.readiness == "ready"
            if self.readiness == "ready":
                self.state = "ready"
            elif self.readiness == "login_required":
                self.state = "login_required"
            elif self.readiness == "stale":
                self.state = "stale"
            else:
                self.state = "unavailable"
        return self


def normalize_timestamp(value: str) -> str:
    """Return *value* as timezone-qualified ISO-8601, or "" / *value* unchanged.

    Four verdicts are minted in Python (``datetime.now(UTC)``) and already carry
    an offset. Three are read back out of SQLite, where ``CURRENT_TIMESTAMP``
    stores UTC and omits the marker (``"2026-07-18 09:12:33"``) — X's health
    row, and the 知乎 / Reddit task-history fallbacks. ``Date.parse`` reads an
    unmarked string as *local* time, so a UTC+8 user saw a verdict from a minute
    ago rendered as eight hours old, the error running in the direction that
    makes the freshest evidence look the stalest.

    A module-level function rather than only a field validator because the same
    strings also reach users as prose: ``verify._verify_twitter`` prints the
    last-request time into its message, and a naive timestamp there is the same
    lie in a place no validator would ever see.
    """
    text = str(value or "").strip()
    if not text:
        return ""
    candidate = f"{text[:-1]}+00:00" if text.endswith("Z") else text
    try:
        parsed = datetime.fromisoformat(candidate)
    except ValueError:
        # Not a timestamp we can read. Passed through rather than blanked: an
        # odd string is at least visible, whereas "" silently reads as "never
        # verified" and would trip the freshness rule in
        # ``check_legacy_consistency``.
        return text
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.isoformat()


class SourceAuthContract(BaseModel):
    """Per-source auth state, with each dimension independently meaningful.

    Invariant I2 (orthogonality): changing any one field must not change the
    meaning of the others. Notably ``enabled`` lives on the enclosing
    ``SourceStatusItem``, not here — whether a source is scheduled is a
    scheduling question, not an auth question, and conflating the two is what
    made Bangumi's credential state invisible whenever it was switched off.
    """

    auth_required: bool = True
    credential: Credential = "none"
    credential_origin: CredentialOrigin = "none"
    verification: Verification = "unverified"
    verify_method: VerifyMethod = "none"
    # ISO-8601 string, empty when never verified. Kept as ``str`` rather than
    # ``datetime`` to match every other timestamp in ``api/models.py`` (e.g.
    # ``XStatusResponse.updated_at``) — the API surface has no datetime fields.
    verified_at: str = ""
    # Freshness window for this method; None means the verdict does not expire.
    verify_ttl_seconds: int | None = None
    # Whether POST /api/sources/{slug}/verify can do anything useful right now.
    can_verify_now: bool = False
    # Human-readable, platform-specific note. User-facing copy lives here so the
    # frontends never hardcode per-platform strings (invariant I4).
    detail: str = ""
    # Empty for legacy single-auth sources.  Mixed-auth sources populate every
    # active capability declared by their platform-source contract.  Consumers
    # must use the requested capability (normally ``bootstrap`` during guided
    # init), not infer private readiness from ``auth_required``.
    capabilities: dict[str, SourceCapabilityAuth] = Field(default_factory=dict)

    # ── Legacy compatibility (Wave A only) ────────────────────────────────
    # The old ``state``/``logged_in`` vocabulary, owned by each provider. The
    # old state is NOT globally derivable from the fields above — see legacy.py
    # for the proof — though a provider may map stronger evidence into it.
    # Delete once all three frontends read the orthogonal fields.
    legacy_state: str = Field(default="missing", exclude=False)
    legacy_logged_in: bool = False

    @field_validator("verified_at")
    @classmethod
    def _qualify_timezone(cls, value: str) -> str:
        """Make every ``verified_at`` carry an explicit UTC offset.

        Applied here rather than in each provider because this is the one place
        every contract passes through: a new provider — or the mobile Web and
        CLI surfaces that have yet to read this field — cannot reintroduce a
        naive timestamp, and no consumer has to defend against one a second
        time (CLAUDE.md pitfall #5: shared logic in the backend).
        """
        return normalize_timestamp(value)

    def capability_ready(self, capability: str) -> bool:
        """Return admission readiness for *capability*.

        Sources without a capability map retain the legacy source-wide
        behaviour. Capability-specific sources always populate the map and
        therefore cannot accidentally fall back to ``auth_required=False``.
        """

        state = self.capabilities.get(str(capability).strip())
        if state is not None:
            return state.ready
        return not self.auth_required or (
            self.credential == "present" and self.verification == "verified"
        )
