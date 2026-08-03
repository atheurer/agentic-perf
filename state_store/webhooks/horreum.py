"""Horreum ``change/new`` webhook translator.

Maps Horreum change-detection payloads to ``anomaly_context``
custom fields on the ticket.
"""

from __future__ import annotations

from typing import Any


def translate(payload: dict[str, Any]) -> dict[str, Any]:
    """Map Horreum change/new payload to ticket fields."""
    change = payload.get("change", {})
    # variable and dataset are nested under change
    # in Horreum's Change.Event payload.
    variable = change.get("variable", {})
    dataset = change.get("dataset", {})

    test_name = payload.get("testName", "")
    variable_name = variable.get("name", "")
    run_id = dataset.get("runId")
    change_id = change.get("id")

    summary = f"Horreum change detected: {test_name}"
    if variable_name:
        summary += f" / {variable_name}"

    anomaly_context: dict[str, Any] = {
        "source": "horreum",
        "test_name": test_name,
        "variable_name": variable_name,
        "variable_group": variable.get("group"),
        "run_id": run_id,
        "change_id": change_id,
        "dataset_id": dataset.get("id"),
        "dataset_ordinal": dataset.get("ordinal"),
        "change_description": change.get("description"),
        "timestamp": change.get("timestamp"),
    }

    return {
        "summary": summary,
        "description": (
            f"Horreum detected a change in test '{test_name}', "
            f"variable '{variable_name}', run {run_id}."
        ),
        "custom_fields": {
            "trigger_source": "horreum",
            "trigger_payload": payload,
            "anomaly_context": anomaly_context,
        },
    }


def dedup_key(payload: dict[str, Any]) -> str | None:
    """Return a dedup key based on Horreum change ID."""
    change = payload.get("change", {})
    change_id = change.get("id")
    if change_id is not None:
        return f"horreum:change:{change_id}"
    return None
