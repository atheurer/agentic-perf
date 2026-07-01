"""Tests for Jumpstarter lease cleanup on parked tickets."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest


def _make_ticket_response(custom_fields: dict) -> MagicMock:
    """Create a mock httpx response for a ticket GET."""
    resp = MagicMock()
    resp.status_code = 200
    resp.json.return_value = {
        "id": "PERF-JMP-001",
        "custom_fields": custom_fields,
    }
    return resp


def _ok_response() -> MagicMock:
    resp = MagicMock()
    resp.status_code = 200
    resp.json.return_value = {}
    return resp


class TestLeaseCleanup:
    @pytest.mark.asyncio
    async def test_cleans_up_jumpstarter_lease(self):
        """Terminates lease for a parked Jumpstarter ticket."""
        client = AsyncMock()
        client.get = AsyncMock(
            return_value=_make_ticket_response(
                {
                    "resource_provider": "jumpstarter",
                    "resource_reservation_id": "perf-jmp-001",
                    "resource_provider_metadata": {
                        "lease_id": "perf-jmp-001",
                    },
                }
            )
        )
        client.patch = AsyncMock(return_value=_ok_response())
        client.post = AsyncMock(return_value=_ok_response())

        mock_provider = AsyncMock()
        mock_provider.terminate = AsyncMock(return_value={"status": "terminated"})
        mock_provider.close = AsyncMock()

        with patch(
            "orchestrator.main.JumpstarterResourceProvider",
            create=True,
        ):
            # Patch the import inside the function
            import orchestrator.main as om

            # We need to mock the dynamic imports inside the function
            with (
                patch.dict(
                    "sys.modules",
                    {
                        "providers.resource.jumpstarter": MagicMock(
                            JumpstarterResourceProvider=MagicMock(
                                from_secrets=AsyncMock(return_value=mock_provider)
                            )
                        ),
                        "providers.secrets.file": MagicMock(
                            FileSecretsProvider=MagicMock()
                        ),
                    },
                ),
            ):
                await om._cleanup_jumpstarter_lease(
                    "http://localhost:8090",
                    "PERF-JMP-001",
                    client=client,
                )

        mock_provider.terminate.assert_called_once_with("perf-jmp-001")
        mock_provider.close.assert_called_once()

        # Should mark as cleaned up
        client.patch.assert_called_once()
        patch_body = client.patch.call_args.kwargs.get(
            "json", client.patch.call_args[1].get("json", {})
        )
        assert patch_body["fields"]["jumpstarter_lease_cleaned_up"] is True

    @pytest.mark.asyncio
    async def test_skips_non_jumpstarter(self):
        """Does not attempt cleanup for non-Jumpstarter tickets."""
        client = AsyncMock()
        client.get = AsyncMock(
            return_value=_make_ticket_response({"resource_provider": "aws"})
        )

        from orchestrator.main import _cleanup_jumpstarter_lease

        await _cleanup_jumpstarter_lease(
            "http://localhost:8090",
            "PERF-AWS-001",
            client=client,
        )

        # Should not call patch or post (no cleanup needed)
        client.patch.assert_not_called()

    @pytest.mark.asyncio
    async def test_skips_already_cleaned(self):
        """Does not re-clean a lease that was already terminated."""
        client = AsyncMock()
        client.get = AsyncMock(
            return_value=_make_ticket_response(
                {
                    "resource_provider": "jumpstarter",
                    "resource_reservation_id": "perf-jmp-001",
                    "jumpstarter_lease_cleaned_up": True,
                }
            )
        )

        from orchestrator.main import _cleanup_jumpstarter_lease

        await _cleanup_jumpstarter_lease(
            "http://localhost:8090",
            "PERF-JMP-001",
            client=client,
        )

        client.patch.assert_not_called()

    @pytest.mark.asyncio
    async def test_skips_no_lease_id(self):
        """Does not attempt cleanup when no lease ID is recorded."""
        client = AsyncMock()
        client.get = AsyncMock(
            return_value=_make_ticket_response({"resource_provider": "jumpstarter"})
        )

        from orchestrator.main import _cleanup_jumpstarter_lease

        await _cleanup_jumpstarter_lease(
            "http://localhost:8090",
            "PERF-JMP-001",
            client=client,
        )

        client.patch.assert_not_called()
