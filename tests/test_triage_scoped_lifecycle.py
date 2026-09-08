"""Tests for scoped-context lifecycle across triage completion and step overrides.

Covers issue #697: step 0's scoped_context must survive triage completion,
and _apply_step_overrides must use correct agent keys for later-step clearing.
"""

from __future__ import annotations

from unittest.mock import MagicMock

from orchestrator.main import _apply_step_overrides

# --- Triage completion: step 0 scoped_context preservation ---


def _build_triage_fields(result: dict) -> dict:
    """Simulate _handle_completion's field-building logic for scoped_context
    and step-0 overrides.

    Mirrors agents/triage/agent.py lines ~605-738.  Only covers the
    scoped_context and step-0 override paths — production also normalizes
    step ordering (prepending resource when the first step is not
    resource/analyze), validates context keys, and handles single-step
    plans.  Tests that need those paths should use realistic step-0 types.
    """
    from typing import Any

    fields: dict[str, Any] = {
        "parsed_specs": result.get("parsed_specs", {}),
        "hypothesis": result.get("hypothesis", ""),
        "benchmark_suite": result.get("benchmark_suite", ""),
        "absent_suite": result.get("absent_suite", False),
        "required_hosts": result.get("required_hosts", []),
    }

    scoped_context = result.get("scoped_context")
    if scoped_context and isinstance(scoped_context, dict):
        fields["scoped_context"] = scoped_context

    raw_plan = result.get("execution_plan")
    if raw_plan and isinstance(raw_plan, list) and len(raw_plan) > 1:
        steps = []
        for i, s in enumerate(raw_plan):
            steps.append(
                {
                    "id": i,
                    "agent_type": s.get("agent_type", "benchmark"),
                    "status": "in_progress" if i == 0 else "pending",
                    "params": s.get("params", {}),
                    "results": {},
                }
            )
        fields["execution_plan"] = {
            "current_step": 0,
            "run_ids": [],
            "steps": steps,
        }

        first_params = steps[0].get("params", {})
        first_type = steps[0]["agent_type"]

        if first_type == "resource" and first_params.get("required_hosts"):
            fields["required_hosts"] = first_params["required_hosts"]

        # The fix: apply step 0's per-step scoped_context if provided,
        # but never clear ticket-level scoped_context for step 0.
        if first_params.get("scoped_context"):
            fields.setdefault("scoped_context", {}).update(
                first_params["scoped_context"]
            )

    return fields


class TestStep0ScopedContextPreservation:
    """Ticket-level scoped_context must survive triage completion for step 0."""

    def test_resource_context_survives_step0(self):
        """scoped_context.resource persists when step 0 is resource."""
        result = {
            "benchmark_suite": "uperf",
            "required_hosts": [{"roles": ["controller"]}, {"roles": ["server"]}],
            "scoped_context": {
                "shared": "AWS m5n.4xlarge, RHEL9",
                "resource": (
                    "Use existing hosts: "
                    "dhcp-10-26-9-207.perf.eng.bos2.dc.redhat.com (controller), "
                    "nfv-amd-4.perf.eng.bos2.dc.redhat.com (server)"
                ),
            },
            "execution_plan": [
                {"agent_type": "resource", "params": {}},
                {"agent_type": "benchmark", "params": {}},
                {"agent_type": "review", "params": {}},
            ],
        }

        fields = _build_triage_fields(result)
        assert "resource" in fields["scoped_context"]
        assert "dhcp-10-26-9-207" in fields["scoped_context"]["resource"]
        assert "shared" in fields["scoped_context"]

    def test_step0_params_scoped_context_replaces(self):
        """Step 0's params.scoped_context replaces the ticket-level section."""
        result = {
            "benchmark_suite": "uperf",
            "required_hosts": [{"roles": ["controller"]}],
            "scoped_context": {
                "shared": "RHEL9",
                "resource": "original ticket-level resource context",
            },
            "execution_plan": [
                {
                    "agent_type": "resource",
                    "params": {
                        "scoped_context": {
                            "resource": "step-0 override resource context",
                        },
                    },
                },
                {"agent_type": "benchmark", "params": {}},
            ],
        }

        fields = _build_triage_fields(result)
        assert fields["scoped_context"]["resource"] == (
            "step-0 override resource context"
        )
        assert fields["scoped_context"]["shared"] == "RHEL9"

    def test_shared_never_cleared(self):
        """The 'shared' key is never removed regardless of step type."""
        result = {
            "benchmark_suite": "uperf",
            "required_hosts": [],
            "scoped_context": {
                "shared": "common context across all agents",
                "resource": "resource-specific context",
                "benchmark": "benchmark-specific context",
            },
            "execution_plan": [
                {"agent_type": "resource", "params": {}},
                {"agent_type": "benchmark", "params": {}},
            ],
        }

        fields = _build_triage_fields(result)
        assert fields["scoped_context"]["shared"] == (
            "common context across all agents"
        )

    def test_other_agent_context_survives_step0(self):
        """scoped_context keys for later agents persist when step 0 is resource."""
        result = {
            "benchmark_suite": "uperf",
            "required_hosts": [],
            "scoped_context": {
                "resource": "use existing hosts",
                "benchmark": "run with 64k message size",
                "review": "compare against baseline run XYZ",
            },
            "execution_plan": [
                {"agent_type": "resource", "params": {}},
                {"agent_type": "benchmark", "params": {}},
                {"agent_type": "review", "params": {}},
            ],
        }

        fields = _build_triage_fields(result)
        assert fields["scoped_context"]["resource"] == "use existing hosts"
        assert fields["scoped_context"]["benchmark"] == "run with 64k message size"
        assert fields["scoped_context"]["review"] == "compare against baseline run XYZ"

    def test_step0_params_without_top_level_context(self):
        """Step 0 params.scoped_context works even without top-level context."""
        result = {
            "benchmark_suite": "uperf",
            "required_hosts": [],
            "execution_plan": [
                {
                    "agent_type": "resource",
                    "params": {
                        "scoped_context": {
                            "resource": "step-0 injected context",
                        },
                    },
                },
                {"agent_type": "benchmark", "params": {}},
            ],
        }

        fields = _build_triage_fields(result)
        assert fields["scoped_context"]["resource"] == "step-0 injected context"

    def test_no_scoped_context_is_fine(self):
        """Triage result without scoped_context doesn't crash step-0 logic."""
        result = {
            "benchmark_suite": "uperf",
            "required_hosts": [],
            "execution_plan": [
                {"agent_type": "resource", "params": {}},
                {"agent_type": "benchmark", "params": {}},
            ],
        }

        fields = _build_triage_fields(result)
        assert "scoped_context" not in fields


# --- _apply_step_overrides: later-step clearing and key correctness ---


def _make_client(cf: dict) -> MagicMock:
    """Build a mock httpx.Client whose GET returns the given custom_fields."""
    mock_response = MagicMock()
    mock_response.status_code = 200
    mock_response.json.return_value = {"custom_fields": cf}

    client = MagicMock()
    client.get.return_value = mock_response
    client.patch.return_value = MagicMock(status_code=200)
    return client


class TestApplyStepOverridesScopedContext:
    """_apply_step_overrides must clear/replace scoped_context for later steps."""

    def test_later_resource_step_clears_resource_key(self):
        """A resource step without params.scoped_context clears the resource key."""
        cf = {
            "scoped_context": {
                "shared": "common context",
                "resource": "stale resource text from prior iteration",
            },
        }
        next_step = {
            "agent_type": "resource",
            "params": {},
        }
        client = _make_client(cf)
        _apply_step_overrides("http://localhost", client, "T-1", next_step, cf)

        patch_call = client.patch.call_args
        fields = patch_call.kwargs["json"]["fields"]
        assert "resource" not in fields["scoped_context"]
        assert fields["scoped_context"]["shared"] == "common context"

    def test_later_resource_step_replaces_with_params(self):
        """A resource step with params.scoped_context merges it over."""
        cf = {
            "scoped_context": {
                "shared": "common",
                "resource": "old resource text",
            },
        }
        next_step = {
            "agent_type": "resource",
            "params": {
                "scoped_context": {
                    "resource": "fresh iteration-2 resource context",
                },
            },
        }
        client = _make_client(cf)
        _apply_step_overrides("http://localhost", client, "T-1", next_step, cf)

        patch_call = client.patch.call_args
        fields = patch_call.kwargs["json"]["fields"]
        assert fields["scoped_context"]["resource"] == (
            "fresh iteration-2 resource context"
        )
        assert fields["scoped_context"]["shared"] == "common"

    def test_provision_key_mismatch_fixed(self):
        """The provision key now correctly clears 'provision', not 'provisioning'."""
        cf = {
            "scoped_context": {
                "shared": "common",
                "provision": "stale provisioning context",
            },
        }
        next_step = {
            "agent_type": "provision",
            "params": {},
        }
        client = _make_client(cf)
        _apply_step_overrides("http://localhost", client, "T-1", next_step, cf)

        patch_call = client.patch.call_args
        fields = patch_call.kwargs["json"]["fields"]
        assert "provision" not in fields["scoped_context"]
        assert fields["scoped_context"]["shared"] == "common"

    def test_benchmark_step_clears_benchmark_key(self):
        """A benchmark step without params.scoped_context clears benchmark."""
        cf = {
            "scoped_context": {
                "shared": "common",
                "benchmark": "stale benchmark text",
            },
        }
        next_step = {
            "agent_type": "benchmark",
            "params": {},
        }
        client = _make_client(cf)
        _apply_step_overrides("http://localhost", client, "T-1", next_step, cf)

        patch_call = client.patch.call_args
        fields = patch_call.kwargs["json"]["fields"]
        assert "benchmark" not in fields["scoped_context"]

    def test_shared_never_cleared_by_overrides(self):
        """The 'shared' key survives _apply_step_overrides for all agent types."""
        for agent_type in ("resource", "provision", "benchmark", "review"):
            cf = {
                "scoped_context": {
                    "shared": "common context",
                    agent_type
                    if agent_type != "provision"
                    else "provision": "stale text",
                },
            }
            next_step = {"agent_type": agent_type, "params": {}}
            client = _make_client(cf)
            _apply_step_overrides("http://localhost", client, "T-1", next_step, cf)

            patch_call = client.patch.call_args
            fields = patch_call.kwargs["json"]["fields"]
            assert fields["scoped_context"]["shared"] == "common context", (
                f"shared was cleared for {agent_type}"
            )

    def test_managed_provider_unaffected(self):
        """Steps without scoped_context in custom_fields don't crash."""
        cf = {}
        next_step = {"agent_type": "resource", "params": {}}
        client = _make_client(cf)
        _apply_step_overrides("http://localhost", client, "T-1", next_step, cf)

        # resource step still patches provisioning_complete etc.
        patch_call = client.patch.call_args
        fields = patch_call.kwargs["json"]["fields"]
        assert "scoped_context" not in fields
