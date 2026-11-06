"""Tests for benchmark agent autonomous investigation context.

Validates that:
1. Analysis findings from analysis_result are surfaced into _build_messages context.
2. When anomaly_context is present, _system_prompt() injects skill_context_prompt
   for "workflow/investigation" (provider-agnostic reproduction rules).
3. When resource_provider is "jumpstarter", _system_prompt() injects
   skill_context_prompt for "resource/jumpstarter" (image/mode selection
   including webhook conflict resolution).
4. The two injections are independent — Jumpstarter skill loads for any Jumpstarter
   ticket, investigation skill loads for any webhook ticket.
5. No Horreum/webhook-specific content is embedded in core benchmark prompts.

Issue #1332.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from agents.benchmark.agent import BenchmarkAgent
from agents.benchmark.prompts import BENCHMARK_BASE_PROMPT
from agents.skill_context import skill_context_prompt

SKILLS_ROOT = Path(__file__).resolve().parent.parent / "skills"


@pytest.fixture
def mock_agent():
    agent = BenchmarkAgent(
        llm_provider=MagicMock(),
        state_store_url="http://localhost:8080",
    )
    agent._ticket_id = "TEST-123"
    agent._get_ticket = AsyncMock()
    agent._add_comment = AsyncMock()
    return agent


class TestAnalysisFindingsSurfaced:
    """analysis_result from custom_fields is included in the LLM context."""

    def test_inconclusive_finding_in_context(self, mock_agent):
        """Inconclusive analysis finding appears in _build_messages content."""
        ticket = {
            "id": "TEST-123",
            "summary": "Performance investigation",
            "description": "Investigate regression",
            "custom_fields": {
                "analysis_result": {
                    "conclusive": False,
                    "finding": "Boot time increased by 15% — root cause unclear",
                },
            },
        }
        messages = mock_agent._build_messages(ticket)
        assert messages, "_build_messages returned empty"
        content = messages[0]["content"]
        assert "Boot time increased by 15%" in content

    def test_conclusive_finding_in_context(self, mock_agent):
        """Conclusive analysis result, including root_cause, appears in context."""
        ticket = {
            "id": "TEST-123",
            "summary": "Performance investigation",
            "description": "Investigate regression",
            "custom_fields": {
                "analysis_result": {
                    "conclusive": True,
                    "finding": "Regression caused by kernel v5.15 change",
                    "root_cause": "scheduler: CFS latency increase in v5.15.42",
                },
            },
        }
        messages = mock_agent._build_messages(ticket)
        content = messages[0]["content"]
        assert "scheduler: CFS latency increase" in content

    def test_analysis_section_absent_when_no_result(self, mock_agent):
        """No analysis section injected when analysis_result is not present."""
        ticket = {
            "id": "TEST-123",
            "summary": "Performance investigation",
            "description": "Run a benchmark",
            "custom_fields": {},
        }
        messages = mock_agent._build_messages(ticket)
        content = messages[0]["content"]
        assert "Boot time increased by 15%" not in content
        assert "scheduler: CFS latency increase" not in content

    def test_malformed_analysis_result_does_not_crash(self, mock_agent):
        """Non-dict analysis_result is ignored rather than raising."""
        ticket = {
            "id": "TEST-123",
            "summary": "Performance investigation",
            "description": "Investigate regression",
            "custom_fields": {"analysis_result": '{"conclusive": false}'},
        }
        messages = mock_agent._build_messages(ticket)
        content = messages[0]["content"]
        assert "## Analysis Findings" not in content

    def test_analysis_finding_is_bounded(self, mock_agent):
        """Analysis text is truncated so it cannot dominate the prompt."""
        ticket = {
            "id": "TEST-123",
            "summary": "Performance investigation",
            "description": "Investigate regression",
            "custom_fields": {
                "analysis_result": {"conclusive": False, "finding": "x" * 10_000},
            },
        }
        messages = mock_agent._build_messages(ticket)
        content = messages[0]["content"]
        assert "x" * 10_000 not in content
        assert "...[truncated]" in content


class TestSkillInjection:
    """Skill context prompts are injected based on ticket properties independently."""

    def test_investigation_skill_injected_for_webhook_ticket(self, mock_agent):
        """anomaly_context present → investigation skill context injected."""
        ticket = {
            "id": "TEST-123",
            "custom_fields": {
                "anomaly_context": {"test_name": "boot-time-ostree-autosd10"},
            },
        }
        prompt = mock_agent._system_prompt(ticket)
        assert skill_context_prompt("workflow/investigation") in prompt

    def test_investigation_skill_absent_for_ordinary_ticket(self, mock_agent):
        """No anomaly_context → investigation skill context not injected."""
        ticket = {"id": "TEST-123", "custom_fields": {}}
        prompt = mock_agent._system_prompt(ticket)
        assert skill_context_prompt("workflow/investigation") not in prompt

    def test_jumpstarter_skill_injected_for_jumpstarter_provider(self, mock_agent):
        """resource_provider=jumpstarter → jumpstarter skill context injected."""
        ticket = {
            "id": "TEST-123",
            "custom_fields": {"resource_provider": "jumpstarter"},
        }
        prompt = mock_agent._system_prompt(ticket)
        assert skill_context_prompt("resource/jumpstarter") in prompt

    def test_jumpstarter_skill_absent_without_jumpstarter_provider(self, mock_agent):
        """No jumpstarter provider → jumpstarter skill context not injected."""
        ticket = {"id": "TEST-123", "custom_fields": {}}
        prompt = mock_agent._system_prompt(ticket)
        assert skill_context_prompt("resource/jumpstarter") not in prompt

    def test_both_skills_injected_for_jumpstarter_webhook_ticket(self, mock_agent):
        """Jumpstarter webhook ticket gets both investigation and jumpstarter skills."""
        ticket = {
            "id": "TEST-123",
            "custom_fields": {
                "resource_provider": "jumpstarter",
                "anomaly_context": {"test_name": "boot-time-ostree-autosd10"},
            },
        }
        prompt = mock_agent._system_prompt(ticket)
        assert skill_context_prompt("workflow/investigation") in prompt
        assert skill_context_prompt("resource/jumpstarter") in prompt

    def test_base_prompt_contains_no_webhook_specifics(self):
        """Core BENCHMARK_BASE_PROMPT has no Horreum or webhook-specific content."""
        assert "Horreum" not in BENCHMARK_BASE_PROMPT
        assert "anomaly_context" not in BENCHMARK_BASE_PROMPT
        assert "Autonomous Investigation Rules" not in BENCHMARK_BASE_PROMPT


class TestSkillFileContent:
    """Skill files exist in the correct locations with required content."""

    def test_webhook_autonomy_skill_exists(self):
        """skills/investigation/webhook-autonomy.md exists."""
        path = SKILLS_ROOT / "investigation" / "webhook-autonomy.md"
        assert path.exists(), f"Missing: {path}"

    def test_webhook_autonomy_covers_reproduction_decision(self):
        content = (SKILLS_ROOT / "investigation" / "webhook-autonomy.md").read_text()
        assert "conclusive" in content.lower()
        assert "reproduce" in content.lower()
        assert "ticket comment" in content.lower()

    def test_jumpstarter_image_selection_covers_webhook_conflict_resolution(self):
        """skills/jumpstarter/image-selection.md covers webhook image/mode conflicts."""
        content = (SKILLS_ROOT / "jumpstarter" / "image-selection.md").read_text()
        assert "ostree" in content
        assert "package" in content
        # The webhook conflict resolution section should be present
        assert "anomaly_context" in content or "webhook" in content.lower()

    def test_no_investigation_autonomy_in_jumpstarter_skills(self):
        """investigation-autonomy.md no longer exists under jumpstarter/."""
        path = SKILLS_ROOT / "jumpstarter" / "investigation-autonomy.md"
        assert not path.exists(), (
            "investigation-autonomy.md should not be in jumpstarter/ — "
            "reproduction rules are in skills/investigation/"
        )
