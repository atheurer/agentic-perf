"""Tests for sequential teardown limiting and concurrency caps (#1066).

Verifies that:
- max_concurrent_teardowns config defaults to 1 and respects env/config overrides
- dispatcher tracks task statuses and reports active_tasks_for_status accurately
- dispatcher cleans up _task_statuses on task completion and mark_done
- dispatch loop defers excess awaiting_teardown tickets while allowing other tickets
"""

from __future__ import annotations

import asyncio
from unittest.mock import MagicMock

import pytest

from orchestrator.config import OrchestratorConfig
from orchestrator.dispatcher import Dispatcher


def _make_config(cfg_dict: dict | None = None) -> OrchestratorConfig:
    return OrchestratorConfig(raw_config=cfg_dict or {})


class TestTeardownConfig:
    """Config loading and defaults for max_concurrent_teardowns."""

    def test_default_is_one(self):
        config = _make_config({})
        assert config.max_concurrent_teardowns == 1

    def test_config_override(self):
        config = _make_config({"max_concurrent_teardowns": 4})
        assert config.max_concurrent_teardowns == 4

    def test_env_override(self, monkeypatch):
        monkeypatch.setenv("MAX_CONCURRENT_TEARDOWNS", "2")
        config = _make_config({})
        assert config.max_concurrent_teardowns == 2

    def test_minimum_clamped_to_one(self):
        config = _make_config({"max_concurrent_teardowns": 0})
        assert config.max_concurrent_teardowns == 1

    def test_to_dict_includes_teardown_cap(self):
        from orchestrator.config import build_redacted_config

        config = _make_config({"max_concurrent_teardowns": 3})
        d = build_redacted_config(config)
        assert d["orchestrator"]["max_concurrent_teardowns"] == 3


class TestDispatcherTaskStatuses:
    """Dispatcher status tracking and status-specific concurrency counts."""

    @pytest.fixture
    def dispatcher(self):
        return Dispatcher(
            state_store_url="http://localhost:8090",
            llm_provider=MagicMock(),
            skill_provider=MagicMock(),
        )

    @pytest.mark.asyncio
    async def test_set_task_and_active_tasks_for_status(self, dispatcher):
        async def dummy_coro():
            await asyncio.sleep(10)

        t1 = asyncio.create_task(dummy_coro())
        t2 = asyncio.create_task(dummy_coro())
        t3 = asyncio.create_task(dummy_coro())

        try:
            dispatcher.set_task("ticket-1", t1, status="awaiting_teardown")
            dispatcher.set_task("ticket-2", t2, status="awaiting_teardown")
            dispatcher.set_task("ticket-3", t3, status="evaluating_convergence")

            assert dispatcher.active_tasks_for_status("awaiting_teardown") == 2
            assert dispatcher.active_tasks_for_status("evaluating_convergence") == 1
            assert dispatcher.active_tasks_for_status("nonexistent") == 0
        finally:
            t1.cancel()
            t2.cancel()
            t3.cancel()
            await asyncio.gather(t1, t2, t3, return_exceptions=True)

    @pytest.mark.asyncio
    async def test_completed_tasks_pruned(self, dispatcher):
        async def quick_coro():
            return "done"

        t1 = asyncio.create_task(quick_coro())
        dispatcher.set_task("ticket-1", t1, status="awaiting_teardown")
        await t1

        # active_tasks_for_status prunes done tasks
        assert dispatcher.active_tasks_for_status("awaiting_teardown") == 0
        assert "ticket-1" not in dispatcher._task_statuses

    @pytest.mark.asyncio
    async def test_mark_done_cleans_status(self, dispatcher):
        async def dummy_coro():
            await asyncio.sleep(10)

        t1 = asyncio.create_task(dummy_coro())
        try:
            dispatcher.set_task("ticket-1", t1, status="awaiting_teardown")
            assert dispatcher.active_tasks_for_status("awaiting_teardown") == 1

            await dispatcher.mark_done("ticket-1")
            assert "ticket-1" not in dispatcher._task_statuses
            assert dispatcher.active_tasks_for_status("awaiting_teardown") == 0
        finally:
            t1.cancel()
            await asyncio.gather(t1, return_exceptions=True)


class TestDispatchTeardownLimiting:
    """Dispatch loop restricts concurrent teardowns to max_concurrent_teardowns."""

    def test_teardown_capped_while_other_statuses_dispatch(self):
        """Simulate the status dispatch loop logic from orchestrator/main.py.

        Verifies that when awaiting_teardown reaches max_concurrent_teardowns,
        further teardowns are deferred, but tickets of other statuses are processed.
        """
        config = _make_config(
            {
                "max_concurrent_agents": 8,
                "max_concurrent_teardowns": 1,
            }
        )

        mock_dispatcher = MagicMock()
        mock_dispatcher.active_tasks.return_value = {"active-1": MagicMock()}
        mock_dispatcher.is_active.return_value = False
        # 1 active teardown already running
        mock_dispatcher.active_tasks_for_status.side_effect = (
            lambda st: 1 if st == "awaiting_teardown" else 0
        )

        tickets_by_status = {
            "awaiting_teardown": [
                {"id": "td-1", "status": "awaiting_teardown"},
                {"id": "td-2", "status": "awaiting_teardown"},
            ],
            "diagnosing": [
                {"id": "diag-1", "status": "diagnosing"},
            ],
        }

        dispatched = []
        rotated = ["awaiting_teardown", "diagnosing"]
        at_capacity = False

        for status in rotated:
            if at_capacity:
                break

            tickets = tickets_by_status.get(status, [])
            for ticket in tickets:
                active_count = len(mock_dispatcher.active_tasks())
                if active_count >= config.max_concurrent_agents:
                    at_capacity = True
                    break

                if status == "awaiting_teardown":
                    active_teardowns = mock_dispatcher.active_tasks_for_status(
                        "awaiting_teardown"
                    )
                    if active_teardowns >= config.max_concurrent_teardowns:
                        break

                tid = ticket["id"]
                if mock_dispatcher.is_active(tid):
                    continue

                dispatched.append((tid, status))

        assert ("diag-1", "diagnosing") in dispatched
        assert not any(st == "awaiting_teardown" for _, st in dispatched)
