"""Tests for infrastructure error detection and variant retry blocking.

Covers the fix for #439: when flash fails with an infrastructure error
(TaskGroup, ExceptionGroup, U-Boot timeout), the platform agent must
not speculatively retry with a different image variant.
"""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, patch

import pytest

from providers.resource.jumpstarter_provision import (
    ProvisionResult,
    is_infrastructure_error,
)

# ── is_infrastructure_error classification ────────────────────────────


class TestIsInfrastructureError:
    """Test the infrastructure error classifier."""

    def test_exception_group_is_infra(self):
        exc = ExceptionGroup("tasks", [RuntimeError("boom")])
        assert is_infrastructure_error(exc) is True

    def test_base_exception_group_is_infra(self):
        exc = BaseExceptionGroup("tasks", [RuntimeError("boom")])
        assert is_infrastructure_error(exc) is True

    def test_uboot_timeout_is_infra(self):
        exc = RuntimeError("Failed to get U-Boot prompt after 120s")
        assert is_infrastructure_error(exc) is True

    def test_connection_refused_is_infra(self):
        exc = ConnectionRefusedError("connection refused")
        assert is_infrastructure_error(exc) is True

    def test_connection_reset_is_infra(self):
        exc = ConnectionResetError("connection reset by peer")
        assert is_infrastructure_error(exc) is True

    def test_connection_timed_out_is_infra(self):
        exc = TimeoutError("connection timed out")
        assert is_infrastructure_error(exc) is True

    def test_grpc_error_is_infra(self):
        exc = RuntimeError("gRPC call failed: UNAVAILABLE")
        assert is_infrastructure_error(exc) is True

    def test_lease_expired_is_infra(self):
        exc = RuntimeError("lease expired during provisioning")
        assert is_infrastructure_error(exc) is True

    def test_network_unreachable_is_infra(self):
        exc = OSError("network is unreachable")
        assert is_infrastructure_error(exc) is True

    def test_image_not_found_is_not_infra(self):
        exc = RuntimeError("404: image not found at https://example.com/img.xz")
        assert is_infrastructure_error(exc) is False

    def test_auth_error_is_not_infra(self):
        exc = RuntimeError("401 Unauthorized: access denied")
        assert is_infrastructure_error(exc) is False

    def test_generic_flash_error_is_not_infra(self):
        exc = RuntimeError("flash checksum mismatch")
        assert is_infrastructure_error(exc) is False

    def test_value_error_is_not_infra(self):
        exc = ValueError("invalid image URL")
        assert is_infrastructure_error(exc) is False

    def test_nested_exception_group_is_infra(self):
        inner = ExceptionGroup("inner", [RuntimeError("Failed to get U-Boot prompt")])
        outer = ExceptionGroup("outer", [inner])
        assert is_infrastructure_error(outer) is True


# ── ProvisionResult.infrastructure_error field ────────────────────────


class TestProvisionResultInfraField:
    """Test that ProvisionResult carries the infrastructure_error flag."""

    def test_default_is_false(self):
        r = ProvisionResult()
        assert r.infrastructure_error is False

    def test_can_set_true(self):
        r = ProvisionResult(infrastructure_error=True)
        assert r.infrastructure_error is True


# ── provision_platform variant retry blocking ────────────────────────


class TestVariantRetryBlocking:
    """Test that the platform server blocks variant retries after infra errors."""

    @pytest.fixture(autouse=True)
    def _reset_server_state(self):
        """Reset module-level state between tests."""
        import agents.platform.server as srv

        srv._last_infra_error = None
        srv._initialized = True
        srv._ticket = {
            "id": "T-TEST",
            "custom_fields": {
                "resource_provider": "jumpstarter",
                "jumpstarter_flash": {
                    "flash_targets": [
                        {"url": "https://example.com/image.xz"},
                    ],
                    "ssh_public_key": "ssh-ed25519 AAAA test",
                },
                "resource_provider_metadata": {
                    "exporter_name": "board-01",
                    "lease_id": "lease-123",
                },
            },
        }
        yield
        srv._last_infra_error = None

    @pytest.mark.asyncio
    async def test_variant_blocked_after_infra_error(self):
        """After an infra error, calling with a different variant is rejected."""
        import agents.platform.server as srv

        # Simulate a previous infrastructure failure
        srv._last_infra_error = "INFRASTRUCTURE_ERROR: TaskGroup failed"

        result_json = await srv.provision_platform(image_variant="ps-regular")
        result = json.loads(result_json)

        assert result["success"] is False
        assert result["infrastructure_error"] is True
        assert "infrastructure" in result["error"].lower()
        assert "image variant" in result["error"].lower()

    @pytest.mark.asyncio
    async def test_no_variant_allowed_after_infra_error(self):
        """After an infra error, calling without a variant still proceeds."""
        import agents.platform.server as srv

        srv._last_infra_error = "INFRASTRUCTURE_ERROR: TaskGroup failed"

        infra_result = ProvisionResult(
            success=False,
            infrastructure_error=True,
            diagnostics=["INFRASTRUCTURE_ERROR: still broken"],
        )

        with patch(
            "providers.resource.jumpstarter_provision.provision_jumpstarter",
            new_callable=AsyncMock,
            return_value=infra_result,
        ):
            result_json = await srv.provision_platform(image_variant="")
            result = json.loads(result_json)

        # Should proceed (not short-circuit) since no variant change
        assert result["infrastructure_error"] is True

    @pytest.mark.asyncio
    async def test_infra_error_tracked_from_result(self):
        """Infrastructure error from provision result sets the tracking flag."""
        import agents.platform.server as srv

        infra_result = ProvisionResult(
            success=False,
            infrastructure_error=True,
            diagnostics=[
                "Flash failed",
                "INFRASTRUCTURE_ERROR: test environment failure",
            ],
        )

        with patch(
            "providers.resource.jumpstarter_provision.provision_jumpstarter",
            new_callable=AsyncMock,
            return_value=infra_result,
        ):
            await srv.provision_platform()

        assert srv._last_infra_error is not None
        assert "INFRASTRUCTURE_ERROR" in srv._last_infra_error

    @pytest.mark.asyncio
    async def test_successful_provision_clears_infra_flag(self):
        """A successful provision clears any previous infra error tracking."""
        import agents.platform.server as srv

        srv._last_infra_error = "previous failure"

        success_result = ProvisionResult(
            success=True,
            ip="10.0.0.1",
            infrastructure_error=False,
        )

        with patch(
            "providers.resource.jumpstarter_provision.provision_jumpstarter",
            new_callable=AsyncMock,
            return_value=success_result,
        ):
            await srv.provision_platform()

        assert srv._last_infra_error is None

    @pytest.mark.asyncio
    async def test_non_infra_failure_allows_variant_retry(self):
        """A non-infra failure should allow retrying with a different variant."""
        import agents.platform.server as srv

        # First call: non-infra failure
        image_result = ProvisionResult(
            success=False,
            infrastructure_error=False,
            diagnostics=["Flash failed: checksum mismatch"],
        )

        with patch(
            "providers.resource.jumpstarter_provision.provision_jumpstarter",
            new_callable=AsyncMock,
            return_value=image_result,
        ):
            await srv.provision_platform()

        assert srv._last_infra_error is None

        # Second call with different variant should proceed
        with patch(
            "providers.resource.jumpstarter_provision.provision_jumpstarter",
            new_callable=AsyncMock,
            return_value=image_result,
        ) as mock_prov:
            await srv.provision_platform(image_variant="ps-regular")

        mock_prov.assert_called_once()

    @pytest.mark.asyncio
    async def test_infra_error_in_json_response(self):
        """The infrastructure_error flag appears in the JSON response."""
        import agents.platform.server as srv

        infra_result = ProvisionResult(
            success=False,
            infrastructure_error=True,
            diagnostics=["INFRASTRUCTURE_ERROR: U-Boot timeout"],
        )

        with patch(
            "providers.resource.jumpstarter_provision.provision_jumpstarter",
            new_callable=AsyncMock,
            return_value=infra_result,
        ):
            result_json = await srv.provision_platform()
            result = json.loads(result_json)

        assert "infrastructure_error" in result
        assert result["infrastructure_error"] is True
        assert result["success"] is False


# ── Prompt guidance ──────────────────────────────────────────────────


class TestPromptInfraGuidance:
    """Verify the system prompt mentions infrastructure error handling."""

    def test_prompt_mentions_infrastructure_error(self):
        from agents.platform.prompts import PLATFORM_SYSTEM_PROMPT

        assert "infrastructure_error" in PLATFORM_SYSTEM_PROMPT

    def test_prompt_warns_against_variant_change_for_infra(self):
        from agents.platform.prompts import PLATFORM_SYSTEM_PROMPT

        assert "NEVER change the image variant" in PLATFORM_SYSTEM_PROMPT
