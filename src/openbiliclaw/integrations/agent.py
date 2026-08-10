"""Host-neutral aliases for the OpenBiliClaw agent bridge.

OpenClaw was the first integration name and remains the wire-compatible CLI
entry point.  This module gives Hermes, WorkBuddy and future hosts a stable
semantic import path without duplicating the adapter implementation.
"""

from __future__ import annotations

from .openclaw import (
    OpenClawAdapter,
    OpenClawAdapterServices,
    OpenClawSkillDescriptor,
    build_openclaw_adapter,
    build_openclaw_adapter_services,
    build_openclaw_skills,
)

AgentAdapter = OpenClawAdapter
AgentAdapterServices = OpenClawAdapterServices
AgentSkillDescriptor = OpenClawSkillDescriptor
build_agent_adapter = build_openclaw_adapter
build_agent_adapter_services = build_openclaw_adapter_services
build_agent_skills = build_openclaw_skills

__all__ = [
    "AgentAdapter",
    "AgentAdapterServices",
    "AgentSkillDescriptor",
    "build_agent_adapter",
    "build_agent_adapter_services",
    "build_agent_skills",
]
