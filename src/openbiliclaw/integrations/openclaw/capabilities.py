"""Capability negotiation for the host-neutral agent bridge."""

from __future__ import annotations

from typing import Any

from openbiliclaw import __version__

from .schemas import CapabilitiesResponse

AGENT_BRIDGE_PROTOCOL_VERSION = "agent-bridge/v2"
AGENT_HOST_NAMES = ["openclaw", "hermes", "workbuddy"]


def build_capabilities(adapter: Any) -> CapabilitiesResponse:
    """Return the exact descriptor set registered by this adapter build."""
    from .skill import build_openclaw_skills

    skills = build_openclaw_skills(adapter)
    return CapabilitiesResponse(
        protocol_version=AGENT_BRIDGE_PROTOCOL_VERSION,
        adapter_version=__version__,
        host_names=list(AGENT_HOST_NAMES),
        skill_names=[skill.name for skill in skills],
    )
