"""Tests for optional Docker proxy bootstrap."""

from __future__ import annotations

import socket
from typing import TYPE_CHECKING

from openbiliclaw.docker_runtime import (
    bootstrap_runtime_environment,
    bootstrap_runtime_root,
    can_connect,
    is_running_in_container,
    resolve_optional_proxy_env,
)

if TYPE_CHECKING:
    from pathlib import Path

    import pytest


def _closed_local_port() -> int:
    """Bind then release an ephemeral port so nothing listens on it."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


def test_can_connect_returns_false_when_endpoint_is_unreachable() -> None:
    """A refused/absent endpoint must return False, never raise.

    Regression: the real can_connect used to let ConnectionRefusedError
    propagate, crashing the Docker runtime bootstrapper before serve-api
    launched whenever no host proxy listened on port 7897.
    """
    assert can_connect("127.0.0.1", _closed_local_port(), timeout=0.5) is False


def test_resolve_optional_proxy_env_returns_empty_with_real_can_connect_and_no_proxy() -> None:
    """End-to-end with the real can_connect: no proxy → no updates, no crash."""
    updates = resolve_optional_proxy_env(
        {},
        proxy_host="127.0.0.1",
        proxy_port=_closed_local_port(),
        timeout=0.5,
    )

    assert updates == {}


def test_resolve_optional_proxy_env_skips_when_proxy_already_configured() -> None:
    env = {
        "HTTP_PROXY": "http://custom-proxy:8080",
        "NO_PROXY": "example.com",
    }

    updates = resolve_optional_proxy_env(
        env,
        can_connect=lambda host, port, timeout: True,
    )

    assert updates == {}


def test_resolve_optional_proxy_env_adds_proxy_when_host_proxy_is_reachable() -> None:
    env = {
        "NO_PROXY": "example.com",
    }

    updates = resolve_optional_proxy_env(
        env,
        can_connect=lambda host, port, timeout: host == "host.docker.internal" and port == 7897,
    )

    expected_proxy = "http://host.docker.internal:7897"
    assert updates["HTTP_PROXY"] == expected_proxy
    assert updates["HTTPS_PROXY"] == expected_proxy
    assert updates["ALL_PROXY"] == expected_proxy
    assert updates["http_proxy"] == expected_proxy
    assert updates["https_proxy"] == expected_proxy
    assert updates["all_proxy"] == expected_proxy
    assert updates["NO_PROXY"] == "example.com,127.0.0.1,localhost,host.docker.internal"
    assert updates["no_proxy"] == "example.com,127.0.0.1,localhost,host.docker.internal"


def test_resolve_optional_proxy_env_returns_empty_when_host_proxy_is_unreachable() -> None:
    updates = resolve_optional_proxy_env(
        {},
        can_connect=lambda host, port, timeout: False,
    )

    assert updates == {}


def test_bootstrap_runtime_root_creates_default_config_and_directories(tmp_path: Path) -> None:
    runtime_root = tmp_path / "runtime"
    template = tmp_path / "config.example.toml"
    template.write_text('[general]\nlanguage = "zh"\n', encoding="utf-8")

    bootstrap_runtime_root(runtime_root=runtime_root, template_path=template)

    assert (runtime_root / "config.toml").read_text(encoding="utf-8") == template.read_text(
        encoding="utf-8"
    )
    assert (runtime_root / "data").is_dir()
    assert (runtime_root / "logs").is_dir()


def test_bootstrap_runtime_root_keeps_existing_config(tmp_path: Path) -> None:
    runtime_root = tmp_path / "runtime"
    runtime_root.mkdir()
    existing = runtime_root / "config.toml"
    existing.write_text('[general]\nlanguage = "en"\n', encoding="utf-8")
    template = tmp_path / "config.example.toml"
    template.write_text('[general]\nlanguage = "zh"\n', encoding="utf-8")

    bootstrap_runtime_root(runtime_root=runtime_root, template_path=template)

    assert existing.read_text(encoding="utf-8") == '[general]\nlanguage = "en"\n'


def test_bootstrap_runtime_root_seeds_embedding_base_url_for_ollama_sidecar(
    tmp_path: Path,
) -> None:
    runtime_root = tmp_path / "runtime"
    template = tmp_path / "config.example.toml"
    template.write_text(
        "\n".join(
            [
                "[llm.ollama]",
                'base_url = ""',
                "",
                "[llm.embedding]",
                'provider = ""',
                'model = ""',
                'base_url = ""',
            ]
        ),
        encoding="utf-8",
    )

    bootstrap_runtime_root(
        runtime_root=runtime_root,
        template_path=template,
        env={
            "OPENBILICLAW_SEED_OLLAMA_DEFAULTS": "1",
            "OPENBILICLAW_OLLAMA_BASE_URL": "http://ollama:11434/v1",
            "OPENBILICLAW_EMBEDDING_MODEL": "bge-m3",
        },
    )

    text = (runtime_root / "config.toml").read_text(encoding="utf-8")
    assert "[llm.embedding]" in text
    assert 'provider = "ollama"' in text
    assert 'model = "bge-m3"' in text
    assert 'base_url = "http://ollama:11434/v1"' in text


def test_bootstrap_runtime_environment_prepares_runtime_root_and_proxy(tmp_path: Path) -> None:
    runtime_root = tmp_path / "runtime"
    template = tmp_path / "config.example.toml"
    template.write_text('[general]\nlanguage = "zh"\n', encoding="utf-8")
    env = {
        "OPENBILICLAW_PROJECT_ROOT": str(runtime_root),
        "OPENBILICLAW_CONFIG_TEMPLATE": str(template),
    }

    bootstrap_runtime_environment(
        env,
        can_connect=lambda host, port, timeout: host == "host.docker.internal" and port == 7897,
        in_container=lambda _env: True,
    )

    expected_proxy = "http://host.docker.internal:7897"
    assert env["OPENBILICLAW_PROJECT_ROOT"] == str(runtime_root)
    assert env["HTTP_PROXY"] == expected_proxy
    assert env["HTTPS_PROXY"] == expected_proxy
    assert env["ALL_PROXY"] == expected_proxy
    assert env["OPENBILICLAW_NETWORK_MODE"] == "system"
    assert (runtime_root / "config.toml").exists()
    assert (runtime_root / "data").is_dir()
    assert (runtime_root / "logs").is_dir()


def test_bootstrap_runtime_environment_marks_existing_container_proxy_as_system(
    tmp_path: Path,
) -> None:
    runtime_root = tmp_path / "runtime"
    template = tmp_path / "config.example.toml"
    template.write_text('[general]\nlanguage = "zh"\n', encoding="utf-8")
    env = {
        "OPENBILICLAW_PROJECT_ROOT": str(runtime_root),
        "OPENBILICLAW_CONFIG_TEMPLATE": str(template),
        "HTTPS_PROXY": "http://proxy.internal:8080",
    }

    bootstrap_runtime_environment(env, in_container=lambda _env: True)

    assert env["OPENBILICLAW_NETWORK_MODE"] == "system"


def test_bootstrap_runtime_environment_preserves_explicit_network_mode(tmp_path: Path) -> None:
    runtime_root = tmp_path / "runtime"
    template = tmp_path / "config.example.toml"
    template.write_text('[general]\nlanguage = "zh"\n', encoding="utf-8")
    env = {
        "OPENBILICLAW_PROJECT_ROOT": str(runtime_root),
        "OPENBILICLAW_CONFIG_TEMPLATE": str(template),
        "HTTPS_PROXY": "http://proxy.internal:8080",
        "OPENBILICLAW_NETWORK_MODE": "direct",
    }

    bootstrap_runtime_environment(env, in_container=lambda _env: True)

    assert env["OPENBILICLAW_NETWORK_MODE"] == "direct"


def test_bootstrap_runtime_environment_skips_proxy_outside_container(tmp_path: Path) -> None:
    """On a native host the proxy bootstrap must not touch HTTP(S)_PROXY.

    Even when ``host.docker.internal`` is reachable (Docker Desktop always
    resolves it on macOS), we only want to route traffic through the
    host's Clash when we're actually running inside a container.
    """
    runtime_root = tmp_path / "runtime"
    template = tmp_path / "config.example.toml"
    template.write_text('[general]\nlanguage = "zh"\n', encoding="utf-8")
    env = {
        "OPENBILICLAW_PROJECT_ROOT": str(runtime_root),
        "OPENBILICLAW_CONFIG_TEMPLATE": str(template),
    }

    bootstrap_runtime_environment(
        env,
        can_connect=lambda host, port, timeout: True,
        in_container=lambda _env: False,
    )

    assert env["OPENBILICLAW_PROJECT_ROOT"] == str(runtime_root)
    assert "HTTP_PROXY" not in env
    assert "HTTPS_PROXY" not in env
    assert "ALL_PROXY" not in env
    # Runtime root still gets set up — only the proxy step is gated.
    assert (runtime_root / "config.toml").exists()
    assert (runtime_root / "data").is_dir()
    assert (runtime_root / "logs").is_dir()


def test_bootstrap_runtime_environment_survives_malformed_proxy_port(tmp_path: Path) -> None:
    """A bad OPENBILICLAW_PROXY_PORT must not crash startup.

    Regression: int() on a malformed value used to raise and propagate
    through main(), exiting the container before serve-api launched — the
    same failure mode as an unreachable proxy port. The optional proxy
    step must degrade to "no proxy" and let runtime setup finish.
    """
    runtime_root = tmp_path / "runtime"
    template = tmp_path / "config.example.toml"
    template.write_text('[general]\nlanguage = "zh"\n', encoding="utf-8")
    env = {
        "OPENBILICLAW_PROJECT_ROOT": str(runtime_root),
        "OPENBILICLAW_CONFIG_TEMPLATE": str(template),
        "OPENBILICLAW_PROXY_PORT": "not-a-number",
    }

    # Reachable proxy would normally inject env, but the port never parses.
    bootstrap_runtime_environment(
        env,
        can_connect=lambda host, port, timeout: True,
        in_container=lambda _env: True,
    )

    assert "HTTP_PROXY" not in env
    # Runtime root setup still completes despite the malformed proxy port.
    assert (runtime_root / "config.toml").exists()
    assert (runtime_root / "data").is_dir()


def test_bootstrap_runtime_environment_treats_empty_proxy_port_as_default(tmp_path: Path) -> None:
    """An empty OPENBILICLAW_PROXY_PORT falls back to the default (7897)."""
    runtime_root = tmp_path / "runtime"
    template = tmp_path / "config.example.toml"
    template.write_text('[general]\nlanguage = "zh"\n', encoding="utf-8")
    env = {
        "OPENBILICLAW_PROJECT_ROOT": str(runtime_root),
        "OPENBILICLAW_CONFIG_TEMPLATE": str(template),
        "OPENBILICLAW_PROXY_PORT": "",
    }

    seen_ports: list[int] = []

    def _record(host: str, port: int, timeout: float) -> bool:
        seen_ports.append(port)
        return False

    bootstrap_runtime_environment(
        env,
        can_connect=_record,
        in_container=lambda _env: True,
    )

    assert seen_ports == [7897]
    assert "HTTP_PROXY" not in env


def test_is_running_in_container_respects_explicit_env() -> None:
    assert is_running_in_container({"OPENBILICLAW_IN_CONTAINER": "1"}) is True
    assert is_running_in_container({"OPENBILICLAW_IN_CONTAINER": "yes"}) is True


def test_is_running_in_container_ignores_blank_env(monkeypatch) -> None:
    """Blank value must NOT count as a container marker.

    On a developer machine without a Docker/Podman marker file present,
    the function should return False even if the env var exists but is
    whitespace-only.
    """
    from openbiliclaw import docker_runtime as module

    monkeypatch.setattr(
        module.Path,
        "exists",
        lambda self: False,
    )
    assert is_running_in_container({"OPENBILICLAW_IN_CONTAINER": "   "}) is False
    assert is_running_in_container({}) is False


def test_docker_runtime_root_survives_a_version_rollback(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Unknown config keys must not brick the container on a downgrade.

    A newer image writes a field this build's dataclass has never heard of; the
    user rolls back. ``load_config()`` used to splat the raw section straight
    into the dataclass and die with a bare TypeError, taking every code path
    that loads config down with it — in Docker that is a container that never
    becomes healthy, with an unreadable traceback. Verified in the Docker shape
    specifically (own runtime root + config template + ollama seeding) because
    the fix is install/update-adjacent.
    """
    from openbiliclaw.config import load_config

    runtime_root = tmp_path / "runtime"
    template = tmp_path / "config.example.toml"
    template.write_text(
        '[general]\nlanguage = "zh"\n\n[scheduler]\nenabled = true\n', encoding="utf-8"
    )

    bootstrap_runtime_root(runtime_root=runtime_root, template_path=template)
    config_path = runtime_root / "config.toml"
    config_path.write_text(
        config_path.read_text(encoding="utf-8").replace(
            "[scheduler]", "[scheduler]\nfuture_scheduler_field = 24", 1
        ),
        encoding="utf-8",
    )

    monkeypatch.setenv("OPENBILICLAW_PROJECT_ROOT", str(runtime_root))
    with caplog.at_level("WARNING"):
        config = load_config()

    assert config.scheduler.enabled is True, "known siblings keep working"
    assert "future_scheduler_field" in caplog.text
    assert "scheduler" in caplog.text
