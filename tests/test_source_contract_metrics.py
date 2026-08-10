"""Forward tests for the registry-driven source-auth metric denominator."""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest
from scripts import source_contract_metrics as metrics

if TYPE_CHECKING:
    from pathlib import Path


def _write(root: Path, relative: str, text: str) -> None:
    path = root / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def test_source_family_inventory_reads_every_literal_family_and_alias(
    tmp_path: Path,
    monkeypatch: object,
) -> None:
    monkeypatch.setattr(metrics, "REPO_ROOT", tmp_path)  # type: ignore[attr-defined]
    relative = "src/openbiliclaw/sources/platforms.py"
    _write(
        tmp_path,
        relative,
        'PLATFORM_EXAMPLE = "example"\n'
        'PLATFORM_NINTH = "ninth"\n'
        "SOURCE_FAMILY_RULES = (\n"
        "    SourceFamilyRule(family=PLATFORM_EXAMPLE, "
        'platform_aliases=frozenset({"example", "ex"})),\n'
        "    SourceFamilyRule(family=PLATFORM_NINTH, "
        'platform_aliases=frozenset({"ninth", "n9"})),\n'
        ")\n",
    )

    families, aliases = metrics._source_family_inventory()  # noqa: SLF001

    assert families == ("example", "ninth")
    assert aliases["ex"] == "example"
    assert aliases["n9"] == "ninth"

    _write(
        tmp_path,
        relative,
        'PLATFORM_EXAMPLE = "example"\n'
        "SOURCE_FAMILY_RULES = (\n"
        "    SourceFamilyRule(family=PLATFORM_EXAMPLE, "
        'platform_aliases=frozenset({"example"})),\n'
        "    build_rule(),\n"
        ")\n",
    )

    with pytest.raises(RuntimeError, match="every SOURCE_FAMILY_RULES entry"):
        metrics._source_family_inventory()  # noqa: SLF001


def test_templated_verify_route_requires_provider_and_action_for_new_family(
    tmp_path: Path,
    monkeypatch: object,
) -> None:
    monkeypatch.setattr(metrics, "REPO_ROOT", tmp_path)  # type: ignore[attr-defined]
    monkeypatch.setattr(metrics, "PLATFORMS", ("example", "ninth"))  # type: ignore[attr-defined]
    monkeypatch.setattr(  # type: ignore[attr-defined]
        metrics,
        "API_SLUG_ALIASES",
        {"example": "example", "ninth": "ninth"},
    )
    _write(
        tmp_path,
        "src/openbiliclaw/api/app.py",
        '@app.post("/api/sources/{slug}/verify")\ndef verify(slug): return slug\n',
    )
    _write(
        tmp_path,
        "src/openbiliclaw/api/source_auth/providers.py",
        'SOURCE_AUTH_PROVIDERS = {"example": object()}\n',
    )
    _write(
        tmp_path,
        "src/openbiliclaw/api/source_auth/verify.py",
        'VERIFY_ACTIONS = {"example": "none"}\n',
    )

    incomplete = metrics.measure_platforms_with_verify()

    assert incomplete.target == 2
    assert incomplete.value == 1

    _write(
        tmp_path,
        "src/openbiliclaw/api/source_auth/providers.py",
        'SOURCE_AUTH_PROVIDERS = {"example": object(), "ninth": object()}\n',
    )
    _write(
        tmp_path,
        "src/openbiliclaw/api/source_auth/verify.py",
        'VERIFY_ACTIONS = {"example": "none", "ninth": "none"}\n',
    )

    complete = metrics.measure_platforms_with_verify()

    assert complete.target == 2
    assert complete.value == 2


def test_literal_verify_route_normalizes_canonical_alias(
    tmp_path: Path,
    monkeypatch: object,
) -> None:
    monkeypatch.setattr(metrics, "REPO_ROOT", tmp_path)  # type: ignore[attr-defined]
    monkeypatch.setattr(metrics, "PLATFORMS", ("example",))  # type: ignore[attr-defined]
    monkeypatch.setattr(  # type: ignore[attr-defined]
        metrics,
        "API_SLUG_ALIASES",
        {"example": "example", "ex": "example"},
    )
    _write(
        tmp_path,
        "src/openbiliclaw/api/app.py",
        '@app.post("/api/sources/ex/verify")\ndef verify(): return None\n',
    )
    _write(
        tmp_path,
        "src/openbiliclaw/api/source_auth/providers.py",
        'SOURCE_AUTH_PROVIDERS = {"example": object()}\n',
    )
    _write(
        tmp_path,
        "src/openbiliclaw/api/source_auth/verify.py",
        'VERIFY_ACTIONS = {"example": "none"}\n',
    )

    result = metrics.measure_platforms_with_verify()

    assert result.value == result.target == 1
