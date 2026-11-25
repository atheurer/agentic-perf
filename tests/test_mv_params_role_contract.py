"""#1385: user-specified multiplex role contracts are enforced mechanically."""

from __future__ import annotations

import pytest

from agents.benchmark.server import _check_plan_mv_params


def _run_file(params):
    return {
        "benchmarks": [{"name": "uperf", "mv-params": {"sets": [{"params": params}]}}]
    }


class TestPlanRoleEnforcement:
    def test_requested_role_omitted_in_run_file_is_rejected(self):
        """PERF-92D9775E: the plan requested shared params; the first run
        file omitted the roles entirely."""
        run_file = _run_file(
            [
                {"arg": "bitrate-range", "vals": ["1G-25G"]},
                {"arg": "cpu-pin", "vals": ["cpu:192-207"]},
            ]
        )
        plan = [
            {"arg": "bitrate-range", "vals": ["1G-25G"], "role": "all"},
            {"arg": "cpu-pin", "vals": ["cpu:192-207"], "role": "all"},
        ]
        reason = _check_plan_mv_params({}, run_file, plan)
        assert "role contract" in reason
        assert "bitrate-range" in reason
        assert "'all'" in reason
        # An omitted run-file role surfaces as the empty role string.
        assert "''" in reason

    def test_requested_role_wrong_value_is_rejected(self):
        run_file = _run_file(
            [{"arg": "ifname", "vals": ["eno16695np0"], "role": "client"}]
        )
        plan = [{"arg": "ifname", "vals": ["eno16695np0"], "role": "server"}]
        reason = _check_plan_mv_params({}, run_file, plan)
        assert "role contract" in reason
        assert "server" in reason and "client" in reason

    def test_matching_roles_pass(self):
        run_file = _run_file(
            [
                {"arg": "length", "vals": ["60"], "role": "client"},
                {"arg": "time", "vals": ["30"], "role": "client"},
                {"arg": "bitrate-range", "vals": ["1G-25G"], "role": "all"},
                {"arg": "cpu-pin", "vals": ["cpu:192-207"], "role": "all"},
                {"arg": "ifname", "vals": ["eno16695np0"], "role": "server"},
            ]
        )
        plan = [
            {"arg": "length", "vals": ["60"], "role": "client"},
            {"arg": "time", "vals": ["30"], "role": "client"},
            {"arg": "bitrate-range", "vals": ["1G-25G"], "role": "all"},
            {"arg": "cpu-pin", "vals": ["cpu:192-207"], "role": "all"},
            {"arg": "ifname", "vals": ["eno16695np0"], "role": "server"},
        ]
        assert _check_plan_mv_params({}, run_file, plan) == ""

    def test_repeated_arg_with_distinct_roles_is_valid(self):
        """Existing skill guidance represents cpu-pin twice — once per role."""
        run_file = _run_file(
            [
                {"arg": "cpu-pin", "vals": ["cpu:192-207"], "role": "client"},
                {"arg": "cpu-pin", "vals": ["cpu:192-207"], "role": "server"},
            ]
        )
        plan = [
            {"arg": "cpu-pin", "vals": ["cpu:192-207"], "role": "client"},
            {"arg": "cpu-pin", "vals": ["cpu:192-207"], "role": "server"},
        ]
        assert _check_plan_mv_params({}, run_file, plan) == ""

    def test_duplicate_arg_role_pair_in_run_file_is_rejected(self):
        run_file = _run_file(
            [
                {"arg": "cpu-pin", "vals": ["cpu:1"], "role": "client"},
                {"arg": "cpu-pin", "vals": ["cpu:2"], "role": "client"},
            ]
        )
        reason = _check_plan_mv_params({}, run_file, [{"arg": "other", "vals": ["1"]}])
        assert "duplicate mv-params" in reason

    def test_no_role_in_plan_imposes_no_constraint(self):
        """Roles are a user preference: without a contract, omitted roles
        remain valid."""
        run_file = _run_file([{"arg": "wsize", "vals": ["64"]}])
        plan = [{"arg": "wsize", "vals": ["64"]}]
        assert _check_plan_mv_params({}, run_file, plan) == ""

    def test_dict_form_with_role(self):
        run_file = _run_file([{"arg": "wsize", "vals": ["64"], "role": "all"}])
        plan = {"wsize": {"vals": ["64"], "role": "all"}}
        assert _check_plan_mv_params({}, run_file, plan) == ""

    def test_extra_role_for_requested_arg_is_rejected(self):
        """'server-only' means server-only: a run file carrying both server
        and client entries for the requested arg changes scope and must be
        rejected, not passed because the requested role exists."""
        run_file = _run_file(
            [
                {"arg": "ifname", "vals": ["eno16695np0"], "role": "server"},
                {"arg": "ifname", "vals": ["eno16695np0"], "role": "client"},
            ]
        )
        plan = [{"arg": "ifname", "vals": ["eno16695np0"], "role": "server"}]
        reason = _check_plan_mv_params({}, run_file, plan)
        assert "role contract" in reason
        assert "'client'" in reason

    def test_exact_multi_role_set_passes(self):
        run_file = _run_file(
            [
                {"arg": "cpu-pin", "vals": ["cpu:1"], "role": "client"},
                {"arg": "cpu-pin", "vals": ["cpu:1"], "role": "server"},
            ]
        )
        plan = [
            {"arg": "cpu-pin", "vals": ["cpu:1"], "role": "client"},
            {"arg": "cpu-pin", "vals": ["cpu:1"], "role": "server"},
        ]
        assert _check_plan_mv_params({}, run_file, plan) == ""

    def test_missing_one_of_requested_roles_is_rejected(self):
        run_file = _run_file([{"arg": "cpu-pin", "vals": ["cpu:1"], "role": "client"}])
        plan = [
            {"arg": "cpu-pin", "vals": ["cpu:1"], "role": "client"},
            {"arg": "cpu-pin", "vals": ["cpu:1"], "role": "server"},
        ]
        reason = _check_plan_mv_params({}, run_file, plan)
        assert "role contract" in reason
        assert "'server'" in reason

    def test_duplicate_plan_arg_role_pair_is_rejected(self):
        run_file = _run_file([{"arg": "wsize", "vals": ["64"], "role": "all"}])
        plan = [
            {"arg": "wsize", "vals": ["64"], "role": "all"},
            {"arg": "wsize", "vals": ["64"], "role": "all"},
        ]
        reason = _check_plan_mv_params({}, run_file, plan)
        assert "duplicate entries" in reason

    def test_mixed_roleless_and_roled_plan_entries_are_rejected(self):
        run_file = _run_file([{"arg": "wsize", "vals": ["64"]}])
        plan = [
            {"arg": "wsize", "vals": ["64"]},
            {"arg": "wsize", "vals": ["64"], "role": "all"},
        ]
        reason = _check_plan_mv_params({}, run_file, plan)
        assert "mix roleless and role-scoped" in reason

    def test_vals_mismatch_still_reported_with_roles(self):
        run_file = _run_file([{"arg": "wsize", "vals": ["128"], "role": "all"}])
        plan = [{"arg": "wsize", "vals": ["64"], "role": "all"}]
        reason = _check_plan_mv_params({}, run_file, plan)
        assert "does not match the plan" in reason


class TestApprovalRoleMapDerivation:
    """The approval carries the JSON's actual roles, not the prose's claim."""

    @pytest.fixture
    def store_with_validation(self, tmp_path):
        from uuid import uuid4

        from state_store.models import (
            AcquireOrchestratorLeaseRequest,
            CreateTicketRequest,
        )
        from state_store.store import TicketStore

        store = TicketStore(persist_dir=tmp_path)
        ticket = store.create_ticket(
            CreateTicketRequest(summary="role contract", description="run uperf")
        )
        session_id = uuid4()
        store.acquire_orchestrator_lease(
            AcquireOrchestratorLeaseRequest(
                session_id=session_id,
                instance_name="test",
                host="localhost",
                pid=1,
                process_start_id="test",
                ttl_seconds=60,
            )
        )
        claim = store.claim_ticket(
            ticket.id, "benchmark", session_id=session_id, epoch=1
        )
        run_file = _run_file(
            [
                {"arg": "bitrate-range", "vals": ["1G-25G"], "role": "all"},
                {"arg": "ifname", "vals": ["eno16695np0"], "role": "server"},
                {"arg": "length", "vals": ["60"]},
            ]
        )
        ticket.custom_fields["benchmark_validations"] = {
            "records": {
                "val-1": {
                    "validation_id": "val-1",
                    "state": "executable",
                    "runfile_fingerprint": "a" * 64,
                    "execution_intent_digest": "b" * 64,
                    "run_file": run_file,
                }
            }
        }
        store._persist_ticket(ticket)
        return store, ticket, claim

    @staticmethod
    def _create(store, ticket, claim):
        from state_store.models import CreateApprovalRequest

        return store.create_approval_request(
            ticket.id,
            CreateApprovalRequest(
                kind="benchmark_run_file",
                validation_id="val-1",
                presented_run_file_digest="a" * 64,
                execution_intent_digest="b" * 64,
                summary="shared bitrate-range, server-only ifname",
                invocation_id="invocation-1",
                tool_call_id="tool-call-1",
                session_id=claim["session_id"],
                session_epoch="1",
                waiter_owner="invocation-1",
                ticket_attempt=claim["claim_id"],
                claim_id=claim["claim_id"],
            ),
            created_by="benchmark-agent",
        )

    def test_approval_carries_derived_role_map(self, store_with_validation):
        store, ticket, claim = store_with_validation
        approval = self._create(store, ticket, claim)
        assert approval.mv_params_roles == {
            "bitrate-range": ["all"],
            "ifname": ["server"],
            "length": ["omitted"],
        }
        # Persisted and serialized for the human reviewer.
        listed = store.list_approval_requests(ticket.id)
        assert listed[0].mv_params_roles == approval.mv_params_roles

    def test_derivation_helper_direct(self):
        from state_store.store import _derive_mv_params_roles

        run_file = _run_file(
            [
                {"arg": "cpu-pin", "vals": ["cpu:1"], "role": "client"},
                {"arg": "cpu-pin", "vals": ["cpu:1"], "role": "server"},
                {"arg": "wsize", "vals": ["64"]},
            ]
        )
        assert _derive_mv_params_roles(run_file) == {
            "cpu-pin": ["client", "server"],
            "wsize": ["omitted"],
        }
        assert _derive_mv_params_roles(None) == {}
        assert _derive_mv_params_roles({"benchmarks": "nope"}) == {}
