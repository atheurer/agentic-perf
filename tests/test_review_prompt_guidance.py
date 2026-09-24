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
