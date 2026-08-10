"""Regression locks for the repository's add-platform-source skill."""

from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CODEX_SKILL = ROOT / ".codex/skills/add-platform-source/SKILL.md"
CLAUDE_SKILL = ROOT / ".claude/skills/add-platform-source/SKILL.md"
OPENAI_YAML = ROOT / ".codex/skills/add-platform-source/agents/openai.yaml"
GUIDE = ROOT / "docs/platform-source-integration.md"
HISTORY = ROOT / "docs/platform-source-history-lessons.md"
CONTRACT = ROOT / "docs/platform-source-contract.example.toml"
ACCEPTANCE = ROOT / "docs/platform-source-acceptance.example.md"
AUDIT = ROOT / "scripts/audit_platform_source.py"
SOURCE_AUTH = ROOT / "docs/modules/source-auth.md"


def test_add_platform_source_skill_mirrors_are_byte_identical() -> None:
    assert CODEX_SKILL.read_bytes() == CLAUDE_SKILL.read_bytes()


def test_add_platform_source_skill_is_a_progressive_entrypoint() -> None:
    skill = CODEX_SKILL.read_text(encoding="utf-8")
    metadata = OPENAI_YAML.read_text(encoding="utf-8")

    assert "docs/platform-source-integration.md" in skill
    assert "full`, `discovery-only`, `capability-increment`, or `audit-only`" in skill
    assert "applicability (`required` or `N/A`" in skill
    assert "execution (`PASS`, `FAIL`, `NOT_RUN`, or `BLOCKED`)" in skill
    assert "capability-specific" in skill
    assert "audit_platform_source.py" in skill
    assert "Only all-required `PASS` earns `complete`" in skill
    assert len(skill.splitlines()) < 120
    assert 'display_name: "Add Platform Source"' in metadata
    assert "$add-platform-source" in metadata


def test_platform_source_guide_keeps_machine_checked_execution_anchors() -> None:
    guide = GUIDE.read_text(encoding="utf-8")

    for required in (
        "## Skill 执行协议",
        "### 能力矩阵与跨链路注册地图",
        "docs/platform-source-contract.example.toml",
        "scripts/audit_platform_source.py",
        "### 2.2 浏览器任务准入、恢复与终态",
        "### 2.3 Bootstrap 与周期增量同步",
        "只有全部 required 行 `PASS` 才能写 `complete`",
    ):
        assert required in guide

    assert HISTORY.is_file()
    assert CONTRACT.is_file()
    assert ACCEPTANCE.is_file()
    assert AUDIT.is_file()


def test_optional_and_capability_specific_auth_guidance_is_current() -> None:
    guide = GUIDE.read_text(encoding="utf-8")
    source_auth = SOURCE_AUTH.read_text(encoding="utf-8")
    acceptance = ACCEPTANCE.read_text(encoding="utf-8")

    assert 'auth.mode="capability-specific"' in guide
    assert "本门是 `BLOCKED`" in guide
    assert "hasVerifiableCredential()" in source_auth
    assert "并抑制证据徽章" not in source_auth
    assert "external-cli / shared / none" in acceptance
    assert "| Scenario | Applicability | Status |" in acceptance
    assert "git log --all -E" in HISTORY.read_text(encoding="utf-8")
    assert "## 11. 完成判定" in guide
