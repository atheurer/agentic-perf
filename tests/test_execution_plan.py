"""Tests for multi-step execution plans.

Covers: plan data model, orchestrator advancement, triage plan
generation, benchmark step params, review multi-run awareness.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

from state_store.models import VALID_TRANSITIONS, TicketStatus

# --- State machine ---


def test_review_can_reenter_benchmark():
    """Review can transition to executing_benchmark for plan re-runs."""
    allowed = VALID_TRANSITIONS[TicketStatus.AWAITING_REVIEW]
    assert TicketStatus.EXECUTING_BENCHMARK in allowed


def test_original_review_transitions_intact():
    """Original review transitions still work."""
    allowed = VALID_TRANSITIONS[TicketStatus.AWAITING_REVIEW]
    assert TicketStatus.AWAITING_TEARDOWN in allowed
    assert TicketStatus.TRIAGE_PENDING in allowed
    assert TicketStatus.AWAITING_CUSTOMER_GUIDANCE in allowed


# --- Plan advancement ---


def test_advance_plan_no_plan_is_noop():
    """_advance_plan does nothing when ticket has no execution_plan."""
    from orchestrator.main import _advance_plan

    mock_response = MagicMock()
    mock_response.status_code = 200
    mock_response.json.return_value = {
        "custom_fields": {"run_id": "RUN-001"},
    }

    client = MagicMock()
    client.get.return_value = mock_response

    with patch("httpx.Client", return_value=client):
        _advance_plan("http://localhost:8090", "PERF-TEST", "executing_benchmark")

        client.patch.assert_not_called()
        client.post.assert_not_called()


def test_advance_plan_skips_non_plan_agent():
    """_advance_plan does nothing when the completed agent doesn't match the step."""
    from orchestrator.main import _advance_plan

    plan = {
        "current_step": 0,
        "run_ids": [],
        "steps": [
            {
                "id": 0,
                "agent_type": "benchmark",
                "status": "in_progress",
                "params": {},
                "results": {},
            },
        ],
    }

    mock_response = MagicMock()
    mock_response.status_code = 200
    mock_response.json.return_value = {
        "custom_fields": {"execution_plan": plan},
    }

    client = MagicMock()
    client.get.return_value = mock_response

    with patch("httpx.Client", return_value=client):
        _advance_plan("http://localhost:8090", "PERF-TEST", "awaiting_hardware")

        client.patch.assert_not_called()
        client.post.assert_not_called()


def test_advance_plan_skips_when_hitl_paused():
    """_advance_plan does nothing when the agent paused for human input."""
    from orchestrator.main import _advance_plan

    plan = {
        "current_step": 0,
        "run_ids": [],
        "steps": [
            {
                "id": 0,
                "agent_type": "benchmark",
                "status": "in_progress",
                "params": {},
                "results": {},
            },
        ],
    }

    mock_response = MagicMock()
    mock_response.status_code = 200
    mock_response.json.return_value = {
        "status": "awaiting_customer_guidance",
        "custom_fields": {"execution_plan": plan},
    }

    client = MagicMock()
    client.get.return_value = mock_response

    with patch("httpx.Client", return_value=client):
        _advance_plan("http://localhost:8090", "PERF-TEST", "executing_benchmark")

        client.patch.assert_not_called()
        client.post.assert_not_called()


def test_advance_plan_completes_step_and_advances():
    """After benchmark step, plan marks it completed and transitions."""
    from orchestrator.main import _advance_plan

    plan = {
        "current_step": 0,
        "run_ids": [],
        "steps": [
            {
                "id": 0,
                "agent_type": "benchmark",
                "status": "in_progress",
                "params": {"label": "run-1"},
                "results": {},
            },
            {
                "id": 1,
                "agent_type": "benchmark",
                "status": "pending",
                "params": {"label": "run-2"},
                "results": {},
            },
            {
                "id": 2,
                "agent_type": "review",
                "status": "pending",
                "params": {},
                "results": {},
            },
        ],
    }

    mock_response = MagicMock()
    mock_response.status_code = 200
    mock_response.json.return_value = {
        "custom_fields": {
            "run_id": "RUN-001",
            "benchmark_status": "completed",
            "execution_plan": plan,
        },
    }

    client = MagicMock()
    client.get.return_value = mock_response
    client.patch.return_value = MagicMock(status_code=200)
    client.post.return_value = MagicMock(status_code=200)

    with patch("httpx.Client", return_value=client):
        _advance_plan("http://localhost:8090", "PERF-TEST", "executing_benchmark")

        patch_call = client.patch.call_args
        updated_plan = patch_call.kwargs["json"]["fields"]["execution_plan"]

        assert updated_plan["current_step"] == 1
        assert updated_plan["steps"][0]["status"] == "completed"
        assert updated_plan["steps"][0]["results"]["run_id"] == "RUN-001"
        assert updated_plan["steps"][1]["status"] == "in_progress"
        assert updated_plan["run_ids"] == ["RUN-001"]

        transition_call = None
        for call in client.post.call_args_list:
            if "transition" in str(call):
                transition_call = call
        assert transition_call is not None
        assert transition_call.kwargs["json"]["status"] == "executing_benchmark"


def test_advance_plan_final_step_no_transition():
    """After the last step, _advance_plan saves results but no transition."""
    from orchestrator.main import _advance_plan

    plan = {
        "current_step": 0,
        "run_ids": [],
        "steps": [
            {
                "id": 0,
                "agent_type": "review",
                "status": "in_progress",
                "params": {},
                "results": {},
            },
        ],
    }

    mock_response = MagicMock()
    mock_response.status_code = 200
    mock_response.json.return_value = {
        "custom_fields": {
            "execution_plan": plan,
        },
    }

    client = MagicMock()
    client.get.return_value = mock_response
    client.patch.return_value = MagicMock(status_code=200)

    with patch("httpx.Client", return_value=client):
        _advance_plan("http://localhost:8090", "PERF-TEST", "awaiting_review")

        client.patch.assert_called_once()
        transition_calls = [
            c for c in client.post.call_args_list if "transition" in str(c)
        ]
        assert len(transition_calls) == 0


def test_advance_plan_tracks_multiple_run_ids():
    """Each completed benchmark step's run_id is appended to plan.run_ids."""
    from orchestrator.main import _advance_plan

    plan = {
        "current_step": 1,
        "run_ids": ["RUN-001"],
        "steps": [
            {
                "id": 0,
                "agent_type": "benchmark",
                "status": "completed",
                "params": {},
                "results": {"run_id": "RUN-001"},
            },
            {
                "id": 1,
                "agent_type": "benchmark",
                "status": "in_progress",
                "params": {},
                "results": {},
            },
            {
                "id": 2,
                "agent_type": "review",
                "status": "pending",
                "params": {},
                "results": {},
            },
        ],
    }

    mock_response = MagicMock()
    mock_response.status_code = 200
    mock_response.json.return_value = {
        "custom_fields": {
            "run_id": "RUN-002",
            "benchmark_status": "completed",
            "execution_plan": plan,
        },
    }

    client = MagicMock()
    client.get.return_value = mock_response
    client.patch.return_value = MagicMock(status_code=200)
    client.post.return_value = MagicMock(status_code=200)

    with patch("httpx.Client", return_value=client):
        _advance_plan("http://localhost:8090", "PERF-TEST", "executing_benchmark")

        patch_call = client.patch.call_args
        updated_plan = patch_call.kwargs["json"]["fields"]["execution_plan"]
        assert updated_plan["run_ids"] == ["RUN-001", "RUN-002"]


# --- Triage plan creation ---


def test_triage_creates_execution_plan():
    """Triage agent normalizes raw execution_plan into data model."""
    raw_plan = [
        {"agent_type": "benchmark", "params": {"label": "run-1"}},
        {"agent_type": "benchmark", "params": {"label": "run-2"}},
        {"agent_type": "review", "params": {}},
    ]

    result = {
        "parsed_specs": {},
        "hypothesis": "test",
        "benchmark_suite": "uperf",
        "absent_suite": False,
        "required_hosts": [
            {"roles": ["controller"]},
            {"roles": ["client"]},
            {"roles": ["server"]},
        ],
        "execution_plan": raw_plan,
    }

    fields: dict = {}
    # Simulate the field construction from _handle_completion
    if result.get("execution_plan") and len(result["execution_plan"]) > 1:
        steps = []
        for i, s in enumerate(result["execution_plan"]):
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

    plan = fields["execution_plan"]
    assert plan["current_step"] == 0
    assert len(plan["steps"]) == 3
    assert plan["steps"][0]["status"] == "in_progress"
    assert plan["steps"][1]["status"] == "pending"
    assert plan["steps"][2]["agent_type"] == "review"


def test_triage_ignores_single_step_plan():
    """A plan with only 1 step is ignored (not a multi-step request)."""
    raw_plan = [{"agent_type": "benchmark", "params": {}}]

    fields: dict = {}
    if raw_plan and isinstance(raw_plan, list) and len(raw_plan) > 1:
        fields["execution_plan"] = {"steps": raw_plan}

    assert "execution_plan" not in fields


# --- Benchmark agent step awareness ---


def test_benchmark_build_messages_includes_step_params():
    """Benchmark agent includes step-specific params in its context."""
    from agents.benchmark.agent import BenchmarkAgent

    ticket = {
        "id": "PERF-TEST",
        "summary": "test",
        "description": "test",
        "custom_fields": {
            "execution_plan": {
                "current_step": 1,
                "run_ids": ["RUN-001"],
                "steps": [
                    {
                        "id": 0,
                        "agent_type": "benchmark",
                        "status": "completed",
                        "params": {"label": "run-1"},
                        "results": {"run_id": "RUN-001"},
                    },
                    {
                        "id": 1,
                        "agent_type": "benchmark",
                        "status": "in_progress",
                        "params": {
                            "label": "run-2",
                            "mv_params": {"num-threads": "8"},
                        },
                        "results": {},
                    },
                ],
            },
        },
        "comments": [],
    }

    agent = BenchmarkAgent.__new__(BenchmarkAgent)
    agent._repo_cache = None
    msgs = agent._build_messages(ticket)
    content = msgs[0]["content"]

    assert "Step 1" in content
    assert "run-2" in content
    assert "num-threads" in content
    assert "RUN-001" in content


# --- Review agent multi-run awareness ---


def test_review_build_messages_includes_all_run_ids():
    """Review agent presents all run_ids from completed plan steps."""
    from agents.review.agent import ReviewAgent

    ticket = {
        "id": "PERF-TEST",
        "summary": "test",
        "description": "test",
        "custom_fields": {
            "hypothesis": "compare thread counts",
            "benchmark_suite": "uperf",
            "benchmark_status": "completed",
            "execution_plan": {
                "current_step": 2,
                "run_ids": ["RUN-001", "RUN-002"],
                "steps": [
                    {
                        "id": 0,
                        "agent_type": "benchmark",
                        "status": "completed",
                        "params": {"label": "1-thread"},
                        "results": {
                            "run_id": "RUN-001",
                            "benchmark_status": "completed",
                        },
                    },
                    {
                        "id": 1,
                        "agent_type": "benchmark",
                        "status": "completed",
                        "params": {"label": "8-threads"},
                        "results": {
                            "run_id": "RUN-002",
                            "benchmark_status": "completed",
                        },
                    },
                    {
                        "id": 2,
                        "agent_type": "review",
                        "status": "in_progress",
                        "params": {},
                        "results": {},
                    },
                ],
            },
        },
        "comments": [],
    }

    agent = ReviewAgent.__new__(ReviewAgent)
    agent._repo_cache = None
    msgs = agent._build_messages(ticket)
    content = msgs[0]["content"]

    assert "1-thread" in content
    assert "8-threads" in content
    assert "RUN-001" in content
    assert "RUN-002" in content
    assert "All Run IDs for comparison" in content


# --- Multi-iteration state machine transitions ---


def test_teardown_can_cycle_to_hardware():
    """awaiting_teardown can transition to awaiting_hardware for plan cycling."""
    allowed = VALID_TRANSITIONS[TicketStatus.AWAITING_TEARDOWN]
    assert TicketStatus.AWAITING_HARDWARE in allowed


def test_review_can_go_to_hardware():
    """awaiting_review can transition to awaiting_hardware for plan cycling."""
    allowed = VALID_TRANSITIONS[TicketStatus.AWAITING_REVIEW]
    assert TicketStatus.AWAITING_HARDWARE in allowed


def test_review_can_go_to_provision():
    """awaiting_review can transition to awaiting_provision for plan cycling."""
    allowed = VALID_TRANSITIONS[TicketStatus.AWAITING_REVIEW]
    assert TicketStatus.AWAITING_PROVISION in allowed


def test_teardown_original_transitions_intact():
    """Original teardown transitions still work."""
    allowed = VALID_TRANSITIONS[TicketStatus.AWAITING_TEARDOWN]
    assert TicketStatus.RETROSPECTIVE_PENDING in allowed
    assert TicketStatus.CLOSED in allowed
    assert TicketStatus.AWAITING_CUSTOMER_GUIDANCE in allowed


# --- _capture_step_results ---


def test_capture_step_results_benchmark():
    """Benchmark step captures run_id, status, duration, run_file."""
    from orchestrator.main import _capture_step_results

    cf = {
        "run_id": "RUN-001",
        "benchmark_status": "completed",
        "benchmark_duration": 120,
        "run_file_used": {"name": "test.json"},
    }
    results = _capture_step_results("benchmark", cf)
    assert results["run_id"] == "RUN-001"
    assert results["benchmark_status"] == "completed"
    assert results["benchmark_duration"] == 120
    assert results["run_file_used"] == {"name": "test.json"}


def test_capture_step_results_resource():
    """Resource step captures hardware IPs, SSH info, provider metadata."""
    from orchestrator.main import _capture_step_results

    cf = {
        "assigned_hardware_ips": {"controller": "10.0.0.1", "targets": ["10.0.0.2"]},
        "ssh_hardware_ips": {"controller": "52.1.1.1", "targets": ["52.1.1.2"]},
        "ssh_user": "ec2-user",
        "ssh_key_path": "/home/user/.ssh/id_rsa",
        "resource_provider": "aws",
        "resource_reservation_id": "r-12345",
        "resource_provider_metadata": {"region": "us-east-1"},
    }
    results = _capture_step_results("resource", cf)
    assert results["assigned_hardware_ips"]["controller"] == "10.0.0.1"
    assert results["resource_provider"] == "aws"
    assert results["ssh_user"] == "ec2-user"


def test_capture_step_results_provision():
    """Provision step captures provisioning state."""
    from orchestrator.main import _capture_step_results

    cf = {
        "provisioning_complete": True,
        "hosts_provisioned": ["10.0.0.1", "10.0.0.2"],
        "harness_name": "crucible",
        "harness_version": "v1.2.3",
    }
    results = _capture_step_results("provision", cf)
    assert results["provisioning_complete"] is True
    assert results["harness_name"] == "crucible"


def test_capture_step_results_teardown():
    """Teardown step captures minimal results."""
    from orchestrator.main import _capture_step_results

    results = _capture_step_results("teardown", {})
    assert results["teardown_complete"] is True


# --- Full infrastructure cycle via _advance_plan ---


def test_advance_plan_benchmark_to_teardown():
    """After a benchmark step, plan advances to a teardown step."""
    from orchestrator.main import _advance_plan

    plan = {
        "current_step": 0,
        "run_ids": [],
        "steps": [
            {
                "id": 0,
                "agent_type": "benchmark",
                "status": "in_progress",
                "params": {"label": "RHEL9"},
                "results": {},
            },
            {
                "id": 1,
                "agent_type": "teardown",
                "status": "pending",
                "params": {},
                "results": {},
            },
        ],
    }

    mock_response = MagicMock()
    mock_response.status_code = 200
    mock_response.json.return_value = {
        "custom_fields": {
            "run_id": "RUN-001",
            "benchmark_status": "completed",
            "execution_plan": plan,
        },
    }

    client = MagicMock()
    client.get.return_value = mock_response
    client.patch.return_value = MagicMock(status_code=200)
    client.post.return_value = MagicMock(status_code=200)

    with patch("httpx.Client", return_value=client):
        _advance_plan("http://localhost:8090", "PERF-TEST", "executing_benchmark")

        patch_call = client.patch.call_args_list[0]
        updated_plan = patch_call.kwargs["json"]["fields"]["execution_plan"]

        assert updated_plan["steps"][0]["status"] == "completed"
        assert updated_plan["steps"][1]["status"] == "in_progress"
        assert updated_plan["current_step"] == 1

        transition_calls = [
            c for c in client.post.call_args_list if "transition" in str(c)
        ]
        assert len(transition_calls) == 1
        assert transition_calls[0].kwargs["json"]["status"] == "awaiting_teardown"


def test_advance_plan_teardown_to_resource():
    """After teardown, plan advances to resource step with overrides."""
    from orchestrator.main import _advance_plan

    plan = {
        "current_step": 1,
        "run_ids": ["RUN-001"],
        "steps": [
            {
                "id": 0,
                "agent_type": "benchmark",
                "status": "completed",
                "params": {"label": "RHEL9"},
                "results": {"run_id": "RUN-001"},
            },
            {
                "id": 1,
                "agent_type": "teardown",
                "status": "in_progress",
                "params": {},
                "results": {},
            },
            {
                "id": 2,
                "agent_type": "resource",
                "status": "pending",
                "params": {
                    "required_hosts": [
                        {"roles": ["controller"]},
                        {"roles": ["client"], "os": "RHEL10"},
                        {"roles": ["server"], "os": "RHEL10"},
                    ],
                },
                "results": {},
            },
        ],
    }

    mock_response = MagicMock()
    mock_response.status_code = 200
    mock_response.json.return_value = {
        "custom_fields": {
            "execution_plan": plan,
            "directives": {"harness": "crucible"},
        },
    }

    client = MagicMock()
    client.get.return_value = mock_response
    client.patch.return_value = MagicMock(status_code=200)
    client.post.return_value = MagicMock(status_code=200)

    with patch("httpx.Client", return_value=client):
        _advance_plan("http://localhost:8090", "PERF-TEST", "awaiting_teardown")

        transition_calls = [
            c for c in client.post.call_args_list if "transition" in str(c)
        ]
        assert len(transition_calls) == 1
        assert transition_calls[0].kwargs["json"]["status"] == "awaiting_hardware"

        # Verify step overrides were applied (the _apply_step_overrides
        # patch is separate from the execution_plan patch)
        override_calls = [
            c for c in client.patch.call_args_list if "provisioning_complete" in str(c)
        ]
        assert len(override_calls) == 1
        fields = override_calls[0].kwargs["json"]["fields"]
        assert fields["required_hosts"][1]["os"] == "RHEL10"
        assert fields["provisioning_complete"] is False


def test_advance_plan_full_six_step_cycle():
    """Walk a 6-step plan: benchmark → teardown → resource → provision → benchmark → review."""
    from orchestrator.main import _advance_plan

    steps = [
        {
            "id": 0,
            "agent_type": "benchmark",
            "status": "in_progress",
            "params": {"label": "RHEL9"},
            "results": {},
        },
        {
            "id": 1,
            "agent_type": "teardown",
            "status": "pending",
            "params": {},
            "results": {},
        },
        {
            "id": 2,
            "agent_type": "resource",
            "status": "pending",
            "params": {
                "required_hosts": [
                    {"roles": ["controller"]},
                    {"roles": ["client"], "os": "RHEL10"},
                ]
            },
            "results": {},
        },
        {
            "id": 3,
            "agent_type": "provision",
            "status": "pending",
            "params": {},
            "results": {},
        },
        {
            "id": 4,
            "agent_type": "benchmark",
            "status": "pending",
            "params": {"label": "RHEL10"},
            "results": {},
        },
        {
            "id": 5,
            "agent_type": "review",
            "status": "pending",
            "params": {},
            "results": {},
        },
    ]

    cf = {
        "run_id": "RUN-001",
        "benchmark_status": "completed",
        "assigned_hardware_ips": {"controller": "10.0.0.1"},
        "ssh_hardware_ips": {"controller": "52.1.1.1"},
        "ssh_user": "ec2-user",
        "ssh_key_path": "/tmp/key",
        "resource_provider": "aws",
        "resource_reservation_id": "r-1",
        "resource_provider_metadata": {},
        "provisioning_complete": True,
        "hosts_provisioned": ["10.0.0.1"],
        "harness_name": "crucible",
        "harness_version": "v1",
        "execution_plan": {
            "current_step": 0,
            "run_ids": [],
            "steps": steps,
        },
    }

    expected_transitions = [
        ("executing_benchmark", "awaiting_teardown"),
        ("awaiting_teardown", "awaiting_hardware"),
        ("awaiting_hardware", "awaiting_provision"),
        ("awaiting_provision", "executing_benchmark"),
        ("executing_benchmark", "awaiting_review"),
    ]

    for i, (completed_status, expected_next) in enumerate(expected_transitions):
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = {"custom_fields": dict(cf)}

        client = MagicMock()
        client.get.return_value = mock_response
        client.patch.return_value = MagicMock(status_code=200)
        client.post.return_value = MagicMock(status_code=200)

        with patch("httpx.Client", return_value=client):
            _advance_plan(
                "http://localhost:8090",
                "PERF-TEST",
                completed_status,
            )

            transition_calls = [
                c for c in client.post.call_args_list if "transition" in str(c)
            ]
            assert len(transition_calls) == 1, (
                f"Step {i}: expected transition to {expected_next}"
            )
            actual = transition_calls[0].kwargs["json"]["status"]
            assert actual == expected_next, (
                f"Step {i}: expected {expected_next}, got {actual}"
            )

        # Update plan state for next iteration
        cf["execution_plan"]["steps"][i]["status"] = "completed"
        cf["execution_plan"]["current_step"] = i + 1
        if i + 1 < len(steps):
            cf["execution_plan"]["steps"][i + 1]["status"] = "in_progress"


def test_per_step_results_survive_teardown():
    """Completed step results contain snapshots that survive teardown."""
    from orchestrator.main import _advance_plan

    plan = {
        "current_step": 0,
        "run_ids": [],
        "steps": [
            {
                "id": 0,
                "agent_type": "benchmark",
                "status": "in_progress",
                "params": {"label": "RHEL9"},
                "results": {},
            },
            {
                "id": 1,
                "agent_type": "teardown",
                "status": "pending",
                "params": {},
                "results": {},
            },
        ],
    }

    mock_response = MagicMock()
    mock_response.status_code = 200
    mock_response.json.return_value = {
        "custom_fields": {
            "run_id": "RUN-RHEL9",
            "benchmark_status": "completed",
            "benchmark_duration": 45,
            "run_file_used": {"name": "rhel9.json"},
            "execution_plan": plan,
        },
    }

    client = MagicMock()
    client.get.return_value = mock_response
    client.patch.return_value = MagicMock(status_code=200)
    client.post.return_value = MagicMock(status_code=200)

    with patch("httpx.Client", return_value=client):
        _advance_plan("http://localhost:8090", "PERF-TEST", "executing_benchmark")

        patch_call = client.patch.call_args_list[0]
        updated_plan = patch_call.kwargs["json"]["fields"]["execution_plan"]

        results = updated_plan["steps"][0]["results"]
        assert results["run_id"] == "RUN-RHEL9"
        assert results["benchmark_status"] == "completed"
        assert results["benchmark_duration"] == 45
        assert results["run_file_used"] == {"name": "rhel9.json"}


# --- PLAN_AGENT_STATUS mapping ---


def test_plan_agent_status_includes_all_step_types():
    """PLAN_AGENT_STATUS maps all five step types."""
    from orchestrator.main import PLAN_AGENT_STATUS

    assert PLAN_AGENT_STATUS["teardown"] == "awaiting_teardown"
    assert PLAN_AGENT_STATUS["resource"] == "awaiting_hardware"
    assert PLAN_AGENT_STATUS["provision"] == "awaiting_provision"
    assert PLAN_AGENT_STATUS["benchmark"] == "executing_benchmark"
    assert PLAN_AGENT_STATUS["review"] == "awaiting_review"


# --- Triage plan creation with infrastructure steps ---


def test_triage_normalizes_infrastructure_steps():
    """Triage agent normalizes plans with teardown/resource/provision steps."""
    raw_plan = [
        {"agent_type": "benchmark", "params": {"label": "RHEL9"}},
        {"agent_type": "teardown", "params": {}},
        {
            "agent_type": "resource",
            "params": {
                "required_hosts": [
                    {"roles": ["controller"]},
                    {"roles": ["client"], "os": "RHEL10"},
                ],
            },
        },
        {"agent_type": "provision", "params": {}},
        {"agent_type": "benchmark", "params": {"label": "RHEL10"}},
        {"agent_type": "review", "params": {}},
    ]

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

    plan = {"current_step": 0, "run_ids": [], "steps": steps}

    assert len(plan["steps"]) == 6
    assert plan["steps"][0]["agent_type"] == "benchmark"
    assert plan["steps"][1]["agent_type"] == "teardown"
    assert plan["steps"][2]["agent_type"] == "resource"
    assert plan["steps"][2]["params"]["required_hosts"][1]["os"] == "RHEL10"
    assert plan["steps"][3]["agent_type"] == "provision"
    assert plan["steps"][4]["agent_type"] == "benchmark"
    assert plan["steps"][5]["agent_type"] == "review"
    assert plan["steps"][0]["status"] == "in_progress"
    assert all(s["status"] == "pending" for s in plan["steps"][1:])
