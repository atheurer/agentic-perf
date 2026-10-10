"""Tests for shared MCP server utilities."""

import pytest

from agents.server_utils import ticket_controller_host


@pytest.mark.parametrize(
    ("fields", "expected"),
    [
        (
            {
                "crucible_controller_context": {
                    "host": " context-host ",
                    "controller": "context-controller",
                },
                "ssh_hardware_ips": {"controller": "ssh-controller"},
                "assigned_hardware_ips": {"controller": "assigned-controller"},
            },
            "context-host",
        ),
        (
            {
                "crucible_controller_context": {
                    "host": " ",
                    "controller": " context-controller ",
                },
                "ssh_hardware_ips": {"controller": "ssh-controller"},
                "assigned_hardware_ips": {"controller": "assigned-controller"},
            },
            "context-controller",
        ),
        (
            {
                "ssh_hardware_ips": {"controller": " ssh-controller "},
                "assigned_hardware_ips": {"controller": "assigned-controller"},
            },
            "ssh-controller",
        ),
        (
            {"assigned_hardware_ips": {"controller": " assigned-controller "}},
            "assigned-controller",
        ),
        ({}, None),
    ],
)
def test_ticket_controller_host_precedence(
    fields: dict[str, object], expected: str | None
) -> None:
    """Prefer explicit controller context, SSH address, then assigned address."""
    assert ticket_controller_host({"custom_fields": fields}) == expected
