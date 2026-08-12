"""V2EX identity ladder and mismatch-gate regression tests."""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from openbiliclaw.api.source_auth.probe_cache import LiveProbeCache
from openbiliclaw.api.source_auth.providers import SourceAuthContext, auth_v2ex
from openbiliclaw.api.source_auth.verify import run_live_probe
from openbiliclaw.api.source_auth.write import credential_fingerprint
from openbiliclaw.config import Config
from openbiliclaw.sources.v2ex_identity import (
    resolve_v2ex_identity,
    resolve_v2ex_identity_state,
    v2ex_identity_detail,
)
from openbiliclaw.storage.database import Database

if TYPE_CHECKING:
    from pathlib import Path


def test_v2ex_identity_uses_strict_evidence_priority_and_detects_conflicts() -> None:
    resolved = resolve_v2ex_identity(
        pat_username="Alice",
        pat_verified=True,
        browser_username="alice",
        browser_logged_in=True,
        configured_username="ALICE",
    )
    assert resolved.status == "resolved"
    assert resolved.username == "Alice"
    assert resolved.evidence == "verified"
    assert resolved.account_bootstrap_allowed is True
    assert resolved.private_bootstrap_available is True

    mismatch = resolve_v2ex_identity(
        pat_username="alice",
        pat_verified=True,
        browser_username="bob",
        browser_logged_in=True,
        configured_username="charlie",
    )
    assert mismatch.status == "identity_mismatch"
    assert mismatch.account_bootstrap_allowed is False
    assert mismatch.claims == {
        "pat": "alice",
        "browser": "bob",
        "configured": "charlie",
    }
    assert "PAT=alice" in v2ex_identity_detail(mismatch)
    assert "公开发现仍可用" in v2ex_identity_detail(mismatch)


def test_v2ex_explicit_choice_only_resolves_to_the_observed_browser_account() -> None:
    selected = resolve_v2ex_identity(
        pat_username="alice",
        pat_verified=True,
        browser_username="bob",
        browser_logged_in=True,
        configured_username="charlie",
        accepted_username="BOB",
    )
    assert selected.status == "resolved"
    assert selected.username == "bob"
    assert selected.evidence == "observed"
    assert selected.selection_applied is True
    assert selected.private_bootstrap_available is True
    assert "按用户选择" in v2ex_identity_detail(selected)

    cannot_relabel_browser = resolve_v2ex_identity(
        pat_username="alice",
        pat_verified=True,
        browser_username="bob",
        browser_logged_in=True,
        accepted_username="alice",
    )
    assert cannot_relabel_browser.status == "identity_mismatch"
    assert cannot_relabel_browser.private_bootstrap_available is False


def test_v2ex_persisted_pat_identity_is_bound_to_exact_token(tmp_path: Path) -> None:
    database = Database(tmp_path / "v2ex-identity.db")
    database.initialize()
    cfg = Config()
    cfg.sources.v2ex.access_token = "token-a"
    fingerprint = credential_fingerprint("v2ex", "token-a")
    database.set_v2ex_pat_identity("alice", credential_fingerprint=fingerprint)

    resolution = resolve_v2ex_identity_state(
        cfg=cfg,
        database=database,
        probes=LiveProbeCache(),
    )
    assert resolution.status == "resolved"
    assert resolution.username == "alice"
    assert resolution.evidence == "verified"

    cfg.sources.v2ex.access_token = "token-b"
    changed = resolve_v2ex_identity_state(
        cfg=cfg,
        database=database,
        probes=LiveProbeCache(),
    )
    assert changed.status == "unknown"
    assert changed.evidence == "unknown"

    database.set_v2ex_pat_identity(
        "alice",
        credential_fingerprint=fingerprint,
        when_iso="2026-01-01T00:00:00+00:00",
    )
    cfg.sources.v2ex.access_token = "token-a"
    stale = resolve_v2ex_identity_state(
        cfg=cfg,
        database=database,
        probes=LiveProbeCache(),
    )
    assert stale.status == "unknown"


def test_v2ex_conclusive_pat_rejection_overrides_persisted_identity(tmp_path: Path) -> None:
    database = Database(tmp_path / "v2ex-rejected-identity.db")
    database.initialize()
    cfg = Config()
    cfg.sources.v2ex.access_token = "rejected-token"
    fingerprint = credential_fingerprint("v2ex", "rejected-token")
    database.set_v2ex_pat_identity("alice", credential_fingerprint=fingerprint)
    probes = LiveProbeCache()
    probes.record(
        "v2ex",
        authenticated=False,
        detail="rejected",
        network_error=False,
        fingerprint=fingerprint,
    )

    resolution = resolve_v2ex_identity_state(
        cfg=cfg,
        database=database,
        probes=probes,
    )

    assert resolution.status == "unknown"
    assert resolution.evidence == "unknown"


def test_clear_v2ex_pat_identity_only_clears_matching_fingerprint(tmp_path: Path) -> None:
    database = Database(tmp_path / "v2ex-clear-identity.db")
    database.initialize()
    fingerprint = credential_fingerprint("v2ex", "token-a")
    database.set_v2ex_pat_identity("alice", credential_fingerprint=fingerprint)

    assert database.clear_v2ex_pat_identity(credential_fingerprint="different") is False
    assert database.get_v2ex_pat_identity()[0] == "alice"

    assert database.clear_v2ex_pat_identity(credential_fingerprint=fingerprint) is True
    assert database.get_v2ex_pat_identity() == ("", "", "")


def test_v2ex_source_status_surfaces_mismatch_without_disabling_public_access(
    tmp_path: Path,
) -> None:
    database = Database(tmp_path / "v2ex-status-identity.db")
    database.initialize()
    database.set_v2ex_login_state(True)
    database.set_v2ex_browser_identity("bob")
    cfg = Config()
    cfg.sources.v2ex.enabled = True
    cfg.sources.v2ex.username = "alice"

    contract = auth_v2ex(SourceAuthContext(cfg=cfg, database=database, probes=LiveProbeCache()))
    assert contract.auth_required is False
    assert contract.legacy_state == "no_auth"
    assert contract.legacy_logged_in is True
    assert "身份冲突" in contract.detail
    assert "浏览器=bob" in contract.detail
    assert "配置=alice" in contract.detail
    assert "公开发现仍可用" in contract.detail
    assert contract.capabilities["discover"].ready is True
    assert contract.capabilities["bootstrap"].ready is False
    assert contract.capabilities["bootstrap"].state == "identity_mismatch"


def test_v2ex_capability_readiness_separates_public_discover_from_browser_bootstrap(
    tmp_path: Path,
) -> None:
    database = Database(tmp_path / "v2ex-capabilities.db")
    database.initialize()
    cfg = Config()
    cfg.sources.v2ex.enabled = True

    anonymous = auth_v2ex(SourceAuthContext(cfg=cfg, database=database, probes=LiveProbeCache()))
    assert anonymous.auth_required is False
    assert anonymous.capabilities["discover"].model_dump() == {
        "mode": "optional-credential",
        "required": True,
        "ready": True,
        "state": "ready",
        "detail": "公开 API 与 Feed 可匿名发现；PAT 仅用于增强。",
    }
    assert anonymous.capabilities["bootstrap"].ready is False
    assert anonymous.capabilities["bootstrap"].state == "login_required"
    assert anonymous.capabilities["cookie-sync"].required is False

    database.set_v2ex_login_state(True)
    database.set_v2ex_browser_identity("alice")
    ready = auth_v2ex(SourceAuthContext(cfg=cfg, database=database, probes=LiveProbeCache()))
    for capability in ("profile", "bootstrap", "incremental"):
        assert ready.capabilities[capability].ready is True
        assert ready.capabilities[capability].state == "ready"
    assert ready.capabilities["cookie-sync"].ready is True


@pytest.mark.asyncio
async def test_v2ex_live_probe_persists_verified_identity_without_token(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class FakeClient:
        def __init__(self, **_kwargs: object) -> None:
            pass

        async def __aenter__(self) -> FakeClient:
            return self

        async def __aexit__(self, *_args: object) -> None:
            return None

        async def get_member(self) -> dict[str, str]:
            return {"username": "alice"}

    monkeypatch.setattr("openbiliclaw.sources.v2ex_client.V2EXClient", FakeClient)
    database = Database(tmp_path / "v2ex-probe-identity.db")
    database.initialize()
    cfg = Config()
    cfg.sources.v2ex.access_token = "pat-secret"
    probes = LiveProbeCache()

    outcome = await run_live_probe(
        "v2ex",
        cfg=cfg,
        database=database,
        probes=probes,
    )

    assert outcome.authenticated is True
    username, fingerprint, _verified_at = database.get_v2ex_pat_identity()
    assert username == "alice"
    assert fingerprint == credential_fingerprint("v2ex", "pat-secret")
    assert "pat-secret" not in str(database.get_v2ex_pat_identity())
