"""Backend-owned V2EX identity resolution and mismatch gating."""

from __future__ import annotations

import os
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any, Literal

from openbiliclaw.sources.v2ex_client import validate_v2ex_username

if TYPE_CHECKING:
    from openbiliclaw.api.source_auth.probe_cache import LiveProbeCache

V2EXIdentityEvidence = Literal["verified", "observed", "accepted", "unknown"]
V2EXIdentityStatus = Literal["resolved", "identity_mismatch", "unknown"]
V2EX_PAT_IDENTITY_TTL_SECONDS = 6 * 60 * 60
V2EX_BROWSER_IDENTITY_TTL_SECONDS = 72 * 60 * 60


def _username(value: object) -> str:
    try:
        return validate_v2ex_username(value)
    except ValueError:
        return ""


@dataclass(frozen=True)
class V2EXIdentityResolution:
    """One backend-owned identity verdict across independent evidence axes."""

    status: V2EXIdentityStatus
    username: str
    evidence: V2EXIdentityEvidence
    claims: dict[str, str]
    browser_logged_in: bool
    selection_applied: bool = False

    @property
    def account_bootstrap_allowed(self) -> bool:
        return self.status == "resolved" and bool(self.username)

    @property
    def private_bootstrap_available(self) -> bool:
        browser = self.claims.get("browser", "")
        return (
            self.account_bootstrap_allowed
            and self.browser_logged_in
            and bool(browser)
            and browser.casefold() == self.username.casefold()
        )

    def as_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "username": self.username,
            "evidence": self.evidence,
            "claims": dict(self.claims),
            "selection_applied": self.selection_applied,
            "account_bootstrap_allowed": self.account_bootstrap_allowed,
            "private_bootstrap_available": self.private_bootstrap_available,
        }


def resolve_v2ex_identity(
    *,
    pat_username: object = "",
    pat_verified: bool = False,
    browser_username: object = "",
    browser_logged_in: bool = False,
    configured_username: object = "",
    accepted_username: object = "",
) -> V2EXIdentityResolution:
    """Resolve the strict PAT > browser > explicit/accepted identity ladder."""

    pat = _username(pat_username) if pat_verified else ""
    browser = _username(browser_username) if browser_logged_in else ""
    configured = _username(configured_username)
    accepted = _username(accepted_username)
    claims = {
        key: value
        for key, value in (
            ("pat", pat),
            ("browser", browser),
            ("configured", configured),
            ("accepted", accepted),
        )
        if value
    }
    distinct = {value.casefold() for value in claims.values()}
    # ``accepted`` is an explicit conflict-resolution choice, but browser
    # bootstrap rows still belong to the account visible in that browser.  A
    # selection therefore overrides a conflicting PAT/config claim only when
    # it selects the currently observed browser account.  This prevents an
    # accepted PAT username from relabelling another signed-in user's DOM rows.
    selection_applied = bool(
        accepted and browser and accepted.casefold() == browser.casefold() and len(distinct) > 1
    )
    selected = browser if selection_applied else pat or browser or configured or accepted
    evidence: V2EXIdentityEvidence = (
        "verified"
        if pat and pat.casefold() == selected.casefold()
        else "observed"
        if browser and browser.casefold() == selected.casefold()
        else "accepted"
        if selected
        else "unknown"
    )
    status: V2EXIdentityStatus
    if len(distinct) > 1 and not selection_applied:
        status = "identity_mismatch"
    elif selected:
        status = "resolved"
    else:
        status = "unknown"
    return V2EXIdentityResolution(
        status=status,
        username=selected,
        evidence=evidence,
        claims=claims,
        browser_logged_in=bool(browser_logged_in),
        selection_applied=selection_applied,
    )


def _configured_token(v2ex_cfg: Any) -> str:
    token_env = str(
        getattr(v2ex_cfg, "token_env", "OPENBILICLAW_V2EX_TOKEN") or "OPENBILICLAW_V2EX_TOKEN"
    ).strip()
    env_token = str(os.environ.get(token_env, "") or "").strip() if token_env else ""
    return env_token or str(getattr(v2ex_cfg, "access_token", "") or "").strip()


def _fresh_timestamp(value: object, *, ttl_seconds: int) -> bool:
    raw = str(value or "").strip()
    if not raw:
        return False
    try:
        checked_at = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return False
    if checked_at.tzinfo is None:
        checked_at = checked_at.replace(tzinfo=UTC)
    age = datetime.now(UTC) - checked_at.astimezone(UTC)
    return -timedelta(minutes=5) <= age < timedelta(seconds=ttl_seconds)


def resolve_v2ex_identity_state(
    *,
    cfg: Any,
    database: Any,
    probes: LiveProbeCache,
    configured_username: object | None = None,
    observed_username: object | None = None,
    observed_logged_in: bool | None = None,
) -> V2EXIdentityResolution:
    """Resolve current local evidence without performing network I/O."""

    from openbiliclaw.api.source_auth.write import credential_fingerprint

    sources = getattr(cfg, "sources", None)
    v2ex_cfg = getattr(sources, "v2ex", None)
    token = _configured_token(v2ex_cfg)
    fingerprint = credential_fingerprint("v2ex", token) if token else ""

    pat_username = ""
    if fingerprint:
        verdict = probes.peek_matching("v2ex", fingerprint)
        pat_rejected = bool(
            verdict is not None and not verdict.authenticated and not verdict.network_error
        )
        if (
            verdict is not None
            and verdict.authenticated
            and not verdict.network_error
            and verdict.is_fresh(V2EX_PAT_IDENTITY_TTL_SECONDS)
        ):
            pat_username = _username(verdict.username)
        if not pat_username and not pat_rejected and hasattr(database, "get_v2ex_pat_identity"):
            try:
                stored_username, stored_fingerprint, verified_at = database.get_v2ex_pat_identity()
            except Exception:  # pragma: no cover - defensive storage fallback
                stored_username, stored_fingerprint, verified_at = "", "", ""
            if stored_fingerprint == fingerprint and _fresh_timestamp(
                verified_at,
                ttl_seconds=V2EX_PAT_IDENTITY_TTL_SECONDS,
            ):
                pat_username = _username(stored_username)

    browser_logged_in = False
    browser_username = ""
    if observed_logged_in is None:
        if hasattr(database, "get_v2ex_login_state"):
            try:
                logged_in, observed_at = database.get_v2ex_login_state()
                browser_logged_in = bool(logged_in) and _fresh_timestamp(
                    observed_at,
                    ttl_seconds=V2EX_BROWSER_IDENTITY_TTL_SECONDS,
                )
            except Exception:  # pragma: no cover - defensive storage fallback
                browser_logged_in = False
    else:
        browser_logged_in = observed_logged_in
    if observed_username is not None:
        browser_username = _username(observed_username)
    elif hasattr(database, "get_v2ex_browser_identity"):
        try:
            stored_username, _, observed_at = database.get_v2ex_browser_identity()
            if _fresh_timestamp(
                observed_at,
                ttl_seconds=V2EX_BROWSER_IDENTITY_TTL_SECONDS,
            ):
                browser_username = _username(stored_username)
        except Exception:  # pragma: no cover - defensive storage fallback
            browser_username = ""

    accepted_username = ""
    if hasattr(database, "get_v2ex_accepted_identity"):
        try:
            accepted_username = _username(database.get_v2ex_accepted_identity()[0])
        except Exception:  # pragma: no cover - defensive storage fallback
            accepted_username = ""
    effective_configured = (
        configured_username
        if configured_username is not None
        else getattr(v2ex_cfg, "username", "")
    )
    return resolve_v2ex_identity(
        pat_username=pat_username,
        pat_verified=bool(pat_username),
        browser_username=browser_username,
        browser_logged_in=browser_logged_in,
        configured_username=effective_configured,
        accepted_username=accepted_username,
    )


def v2ex_identity_detail(resolution: V2EXIdentityResolution) -> str:
    """Return status-card copy without exposing credentials or private data."""

    if resolution.selection_applied:
        return (
            f"已按用户选择使用浏览器账号 {resolution.username}；其他账号证据不会用于浏览器初始化。"
        )
    if resolution.status != "identity_mismatch":
        return ""
    labels = {"pat": "PAT", "browser": "浏览器", "configured": "配置", "accepted": "已接受"}
    claims = " / ".join(
        f"{labels.get(origin, origin)}={username}" for origin, username in resolution.claims.items()
    )
    return f"身份冲突（{claims}）；账号初始化已暂停，公开发现仍可用。"


__all__ = [
    "V2EXIdentityResolution",
    "V2EX_BROWSER_IDENTITY_TTL_SECONDS",
    "V2EX_PAT_IDENTITY_TTL_SECONDS",
    "resolve_v2ex_identity",
    "resolve_v2ex_identity_state",
    "v2ex_identity_detail",
]
