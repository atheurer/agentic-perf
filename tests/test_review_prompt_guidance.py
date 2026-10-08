"""Regression checks for evidence and verdict instructions in review prompts."""

from agents.review.prompts import REVIEW_SYSTEM_PROMPT


def test_inconclusive_remains_available_when_relevant_evidence_is_insufficient():
    prompt = " ".join(REVIEW_SYSTEM_PROMPT.split())
    assert (
        "Use inconclusive when the available evidence is insufficient to determine "
        "whether the hypothesis is confirmed or refuted." in prompt
    )
    assert "measure unrelated metrics or conditions" in prompt
    assert "no defensible comparison baseline" in prompt


def test_valid_zeros_and_unexpected_patterns_are_still_reported_as_evidence():
    prompt = " ".join(REVIEW_SYSTEM_PROMPT.split())
    assert "Do not use inconclusive merely because results are complex" in (prompt)
    assert "High variability, bimodal distributions" in prompt
    assert "A valid measured value of zero is not missing data" in prompt


def test_boot_time_review_keeps_artifact_tools_and_describes_artifact_analysis():
    from agents.review.agent import ReviewAgent

    agent = ReviewAgent.__new__(ReviewAgent)
    agent._skill_provider = None
    agent.tools = [
        type("Tool", (), {"name": name})()
        for name in (
            "list_benchmark_artifacts",
            "read_benchmark_artifact",
            "get_crucible_benchmark_context",
        )
    ]
    ticket = {"custom_fields": {"directives": {"harness": "boot-time"}}}

    prompt = agent._system_prompt(ticket)
    agent._apply_review_tool_scoping(ticket)

    tool_names = {tool.name for tool in agent.tools}
    assert "list_benchmark_artifacts" in tool_names
    assert "read_benchmark_artifact" in tool_names
    assert "get_crucible_benchmark_context" not in tool_names
    assert "## Boot-Time Results" in prompt
    assert "list_benchmark_artifacts and read_benchmark_artifact" in prompt
    assert "There are no external result files" not in prompt


def test_crucible_review_always_uses_gateway_even_after_analysis():
    from agents.review.agent import ReviewAgent

    agent = ReviewAgent.__new__(ReviewAgent)
    agent._skill_provider = None
    prompt = agent._system_prompt(
        {
            "custom_fields": {
                "directives": {"harness": "crucible"},
                "analysis_result": {
                    "finding": "Existing measurements are inconclusive."
                },
            }
        }
    )

    assert 'get_skill_context(subject="harness/crucible"' in prompt
    assert "temporary fallback with the lowest default authority" in prompt
