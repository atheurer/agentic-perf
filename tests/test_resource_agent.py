"""Tests for the resource agent."""

from unittest.mock import AsyncMock, MagicMock

import pytest

from agents.resource.agent import _jumpstarter_selector_changed


class TestJumpstarterSelectorChanged:
    """Test _jumpstarter_selector_changed logic."""

    def test_no_selector_requested_empty_metadata_returns_false(self):
        """No selector requested and metadata is empty → no change."""
        fields = {"resource_provider_metadata": {}}
        directives = {}
        assert _jumpstarter_selector_changed(fields, directives) is False

    def test_selector_requested_but_metadata_empty_returns_true(self):
        """Explicit selector requested but metadata is empty → changed."""
        fields = {"resource_provider_metadata": {}}
        directives = {"jumpstarter_selector": "board-type=rpi4"}
        assert _jumpstarter_selector_changed(fields, directives) is True

    def test_selector_requested_differs_from_metadata_returns_true(self):
        """Requested selector differs from metadata selector → changed."""
        fields = {
            "resource_provider_metadata": {
                "selector": "board-type=rpi3",
            }
        }
        directives = {"jumpstarter_selector": "board-type=rpi4"}
        assert _jumpstarter_selector_changed(fields, directives) is True

    def test_selector_requested_matches_metadata_returns_false(self):
        """Requested selector matches metadata → no change."""
        fields = {
            "resource_provider_metadata": {
                "selector": "board-type=rpi4,enabled=true",
            }
        }
        directives = {"jumpstarter_selector": "board-type=rpi4"}
        assert _jumpstarter_selector_changed(fields, directives) is False

    def test_exporter_name_requested_but_metadata_empty_returns_true(self):
        """Explicit exporter_name requested but metadata is empty → changed."""
        fields = {"resource_provider_metadata": {}}
        directives = {"exporter_name": "my-device"}
        assert _jumpstarter_selector_changed(fields, directives) is True

    def test_exporter_name_differs_returns_true(self):
        """Requested exporter_name differs from metadata → changed."""
        fields = {
            "resource_provider_metadata": {
                "exporter_name": "device-old",
                "selector": "board-type=rpi4",
            }
        }
        directives = {
            "exporter_name": "device-new",
            "jumpstarter_selector": "board-type=rpi4",
        }
        assert _jumpstarter_selector_changed(fields, directives) is True

    def test_exporter_name_matches_returns_false(self):
        """Requested exporter_name matches metadata → no change."""
        fields = {
            "resource_provider_metadata": {
                "exporter_name": "my-device",
                "selector": "board-type=rpi4",
            }
        }
        directives = {
            "exporter_name": "my-device",
            "jumpstarter_selector": "board-type=rpi4",
        }
        assert _jumpstarter_selector_changed(fields, directives) is False

    def test_invalid_metadata_type_returns_true(self):
        """Invalid metadata (not a dict) → changed."""
        fields = {"resource_provider_metadata": "invalid"}
        directives = {}
        assert _jumpstarter_selector_changed(fields, directives) is True

    def test_board_selector_field_works(self):
        """board_selector field (legacy) works like jumpstarter_selector."""
        fields = {
            "resource_provider_metadata": {
                "selector": "board-type=rpi4",
            }
        }
        directives = {"board_selector": "board-type=rpi4"}
        assert _jumpstarter_selector_changed(fields, directives) is False

    def test_target_key_maps_to_board_type(self):
        """target= key in request maps to board-type= in selector."""
        fields = {
            "resource_provider_metadata": {
                "selector": "board-type=rpi4",
            }
        }
        directives = {"jumpstarter_selector": "target=rpi4"}
        assert _jumpstarter_selector_changed(fields, directives) is False

    def test_none_metadata_returns_false_when_no_selector_requested(self):
        """None metadata (same as empty) with no selector → no change."""
        fields = {"resource_provider_metadata": None}
        directives = {}
        assert _jumpstarter_selector_changed(fields, directives) is False


class TestTerminateProviderResourcesJumpstarter:
    """Test _terminate_provider_resources handles Jumpstarter 404/already-released."""

    @pytest.mark.asyncio
    async def test_jumpstarter_404_treated_as_success(self):
        """Jumpstarter 404 / already_released should be treated as idempotent success."""
        from agents.resource.agent import ResourceAgent

        agent = ResourceAgent(
            llm_provider=MagicMock(),
            state_store_url="http://test",
            mode="teardown",
            secrets_provider=MagicMock(),
        )
        agent._registry = MagicMock()
        agent._add_comment = AsyncMock()

        # Mock provider that returns error with "not found" indicator
        mock_provider = AsyncMock()
        mock_provider.terminate = AsyncMock(
            return_value={
                "provider": "jumpstarter",
                "lease_id": "test-lease",
                "status": "error",
                "error": "lease not found or already been released",
                "already_released": True,
            }
        )
        agent._registry.get_provider = AsyncMock(return_value=mock_provider)

        # This should return True (success) because the lease is already released
        result = await agent._terminate_provider_resources(
            ticket_id="TEST-123",
            provider_name="jumpstarter",
            reservation_id="test-lease",
            provider_metadata={},
        )

        assert result is True
        # Should have added a comment about successful termination
        agent._add_comment.assert_called_once()
        comment_call = agent._add_comment.call_args
        assert (
            "terminated" in comment_call[0][1].lower()
            or "released" in comment_call[0][1].lower()
        )

    @pytest.mark.asyncio
    async def test_jumpstarter_genuine_error_treated_as_failure(self):
        """Jumpstarter genuine errors (not already-released) should fail."""
        from agents.resource.agent import ResourceAgent

        agent = ResourceAgent(
            llm_provider=MagicMock(),
            state_store_url="http://test",
            mode="teardown",
            secrets_provider=MagicMock(),
        )
        agent._registry = MagicMock()
        agent._add_comment = AsyncMock()

        # Mock provider that returns genuine error
        mock_provider = AsyncMock()
        mock_provider.terminate = AsyncMock(
            return_value={
                "provider": "jumpstarter",
                "lease_id": "test-lease",
                "status": "error",
                "error": "connection timeout",
            }
        )
        agent._registry.get_provider = AsyncMock(return_value=mock_provider)

        # This should return False (failure) for genuine errors
        result = await agent._terminate_provider_resources(
            ticket_id="TEST-123",
            provider_name="jumpstarter",
            reservation_id="test-lease",
            provider_metadata={},
        )

        assert result is False
        # Should have added a comment about failure
        agent._add_comment.assert_called_once()
        comment_call = agent._add_comment.call_args
        assert "could not confirm" in comment_call[0][1].lower()

    @pytest.mark.asyncio
    async def test_error_text_mentioning_not_found_is_not_success(self):
        """Text alone must not mark success; only the structured flag counts."""
        from agents.resource.agent import ResourceAgent

        agent = ResourceAgent(
            llm_provider=MagicMock(),
            state_store_url="http://test",
            mode="teardown",
            secrets_provider=MagicMock(),
        )
        agent._registry = MagicMock()
        agent._add_comment = AsyncMock()

        mock_provider = AsyncMock()
        mock_provider.terminate = AsyncMock(
            return_value={
                "provider": "jumpstarter",
                "lease_id": "test-lease",
                "status": "error",
                "error": "permission denied: provider endpoint not found",
            }
        )
        agent._registry.get_provider = AsyncMock(return_value=mock_provider)

        result = await agent._terminate_provider_resources(
            ticket_id="TEST-123",
            provider_name="jumpstarter",
            reservation_id="test-lease",
            provider_metadata={},
        )

        assert result is False

    @pytest.mark.asyncio
    async def test_jumpstarter_terminated_status_succeeds(self):
        """Jumpstarter 'terminated' status should succeed."""
        from agents.resource.agent import ResourceAgent

        agent = ResourceAgent(
            llm_provider=MagicMock(),
            state_store_url="http://test",
            mode="teardown",
            secrets_provider=MagicMock(),
        )
        agent._registry = MagicMock()
        agent._add_comment = AsyncMock()

        # Mock provider that returns success
        mock_provider = AsyncMock()
        mock_provider.terminate = AsyncMock(
            return_value={
                "provider": "jumpstarter",
                "lease_id": "test-lease",
                "status": "terminated",
            }
        )
        agent._registry.get_provider = AsyncMock(return_value=mock_provider)

        # This should return True (success)
        result = await agent._terminate_provider_resources(
            ticket_id="TEST-123",
            provider_name="jumpstarter",
            reservation_id="test-lease",
            provider_metadata={},
        )

        assert result is True
        agent._add_comment.assert_called_once()
