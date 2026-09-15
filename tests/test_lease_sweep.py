"""Tests for Jumpstarter orphaned lease sweep."""

from __future__ import annotations

import json
import re
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


def _jmp_leases_json(leases: list[dict]) -> str:
    """Build jmp get leases -o json output."""
    return json.dumps({"leases": leases})


class TestLeaseNameParsing:
    """Test the ticket ID extraction regex."""

    _TICKET_RE = re.compile(r"^(perf-[0-9a-f]{8})(?:-|$)", re.IGNORECASE)

    def test_standard_lease_name(self):
        m = self._TICKET_RE.match("perf-abcd1234")
        assert m and m.group(1) == "perf-abcd1234"

    def test_suffixed_lease_name(self):
        m = self._TICKET_RE.match("perf-abcd1234-2")
        assert m and m.group(1) == "perf-abcd1234"

    def test_retry_suffix(self):
        m = self._TICKET_RE.match("perf-abcd1234-retry-2")
        assert m and m.group(1) == "perf-abcd1234"

    def test_uuid_not_matched(self):
        m = self._TICKET_RE.match("019f6a02-23e9-7d53-b537-489691750758")
        assert m is None

    def test_other_name_not_matched(self):
        m = self._TICKET_RE.match("some-other-lease")
        assert m is None

    def test_uppercase_matched(self):
        m = self._TICKET_RE.match("PERF-ABCD1234")
        assert m and m.group(1) == "PERF-ABCD1234"


class TestLeaseReleaseStatuses:
    """Test which statuses trigger lease release."""

    def test_release_statuses(self):
        """Only truly terminal statuses trigger lease release."""
        from providers.resource.jumpstarter_lifecycle import (
            LEASE_RELEASE_STATUSES as _LEASE_RELEASE_STATUSES,
        )

        assert "closed" in _LEASE_RELEASE_STATUSES

    def test_non_terminal_not_released(self):
        """Non-terminal statuses do not trigger release."""
        from providers.resource.jumpstarter_lifecycle import (
            LEASE_RELEASE_STATUSES as _LEASE_RELEASE_STATUSES,
        )

        # Human decision point — lease kept for resume
        assert "awaiting_customer_guidance" not in _LEASE_RELEASE_STATUSES
        # Not terminal — transitions to closed
        assert "retrospective_pending" not in _LEASE_RELEASE_STATUSES
        # Active pipeline statuses
        assert "executing_benchmark" not in _LEASE_RELEASE_STATUSES
        assert "awaiting_provision" not in _LEASE_RELEASE_STATUSES
        assert "awaiting_hardware" not in _LEASE_RELEASE_STATUSES


class TestSweepIntegration:
    @pytest.mark.asyncio
    async def test_sweep_releases_closed_lease(self):
        """Full sweep releases lease for closed ticket."""
        from providers.resource.jumpstarter_lifecycle import (
            sweep_orphaned_leases as _sweep_orphaned_leases,
        )

        leases_json = _jmp_leases_json([{"name": "perf-abcd1234"}])
        sub_calls = []

        def _mock_run(cmd, **kwargs):
            sub_calls.append(cmd)
            result = MagicMock()
            if "get" in cmd:
                result.returncode = 0
                result.stdout = leases_json
            else:
                result.returncode = 0
            return result

        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {"status": "closed"}

        mock_client = AsyncMock()
        mock_client.get.return_value = mock_resp

        with (
            patch("subprocess.run", side_effect=_mock_run),
            patch("httpx.AsyncClient") as MockClient,
        ):
            MockClient.return_value.__aenter__ = AsyncMock(return_value=mock_client)
            MockClient.return_value.__aexit__ = AsyncMock()
            await _sweep_orphaned_leases("http://localhost:8090")

        delete_cmds = [c for c in sub_calls if "delete" in c]
        assert len(delete_cmds) == 1
        assert "perf-abcd1234" in delete_cmds[0]

    @pytest.mark.asyncio
    async def test_sweep_skips_active_lease(self):
        """Sweep does not release lease for active ticket."""
        from providers.resource.jumpstarter_lifecycle import (
            sweep_orphaned_leases as _sweep_orphaned_leases,
        )

        leases_json = _jmp_leases_json([{"name": "perf-abcd1234"}])
        sub_calls = []

        def _mock_run(cmd, **kwargs):
            sub_calls.append(cmd)
            result = MagicMock()
            result.returncode = 0
            result.stdout = leases_json
            return result

        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {
            "status": "executing_benchmark",
        }

        mock_client = AsyncMock()
        mock_client.get.return_value = mock_resp

        with (
            patch("subprocess.run", side_effect=_mock_run),
            patch("httpx.AsyncClient") as MockClient,
        ):
            MockClient.return_value.__aenter__ = AsyncMock(return_value=mock_client)
            MockClient.return_value.__aexit__ = AsyncMock()
            await _sweep_orphaned_leases("http://localhost:8090")

        delete_cmds = [c for c in sub_calls if "delete" in c]
        assert len(delete_cmds) == 0

    @pytest.mark.asyncio
    async def test_sweep_releases_404_lease(self):
        """Sweep releases lease when ticket doesn't exist."""
        from providers.resource.jumpstarter_lifecycle import (
            sweep_orphaned_leases as _sweep_orphaned_leases,
        )

        leases_json = _jmp_leases_json([{"name": "perf-deadbeef"}])
        sub_calls = []

        def _mock_run(cmd, **kwargs):
            sub_calls.append(cmd)
            result = MagicMock()
            result.returncode = 0
            result.stdout = leases_json
            return result

        mock_resp = MagicMock()
        mock_resp.status_code = 404

        mock_client = AsyncMock()
        mock_client.get.return_value = mock_resp

        with (
            patch("subprocess.run", side_effect=_mock_run),
            patch("httpx.AsyncClient") as MockClient,
        ):
            MockClient.return_value.__aenter__ = AsyncMock(return_value=mock_client)
            MockClient.return_value.__aexit__ = AsyncMock()
            await _sweep_orphaned_leases("http://localhost:8090")

        delete_cmds = [c for c in sub_calls if "delete" in c]
        assert len(delete_cmds) == 1

    @pytest.mark.asyncio
    async def test_sweep_ignores_non_ticket_leases(self):
        """Sweep ignores leases with non-ticket names."""
        from providers.resource.jumpstarter_lifecycle import (
            sweep_orphaned_leases as _sweep_orphaned_leases,
        )

        leases_json = _jmp_leases_json(
            [
                {"name": "019f6a02-23e9-7d53-b537-489691750758"},
                {"name": "some-other-lease"},
            ]
        )
        sub_calls = []

        def _mock_run(cmd, **kwargs):
            sub_calls.append(cmd)
            result = MagicMock()
            result.returncode = 0
            result.stdout = leases_json
            return result

        with patch("subprocess.run", side_effect=_mock_run):
            await _sweep_orphaned_leases("http://localhost:8090")

        delete_cmds = [c for c in sub_calls if "delete" in c]
        assert len(delete_cmds) == 0

    @pytest.mark.asyncio
    async def test_sweep_empty_leases(self):
        """Empty lease list is a no-op."""
        from providers.resource.jumpstarter_lifecycle import (
            sweep_orphaned_leases as _sweep_orphaned_leases,
        )

        sub_calls = []

        def _mock_run(cmd, **kwargs):
            sub_calls.append(cmd)
            result = MagicMock()
            result.returncode = 0
            result.stdout = _jmp_leases_json([])
            return result

        with patch("subprocess.run", side_effect=_mock_run):
            await _sweep_orphaned_leases("http://localhost:8090")

        delete_cmds = [c for c in sub_calls if "delete" in c]
        assert len(delete_cmds) == 0
