"""Tests for the image builder agent and CAIB provider."""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, MagicMock

from agents.image_builder.agent import ImageBuilderAgent
from providers.image_build.caib import (
    generate_manifest,
    resolve_build_mode,
    resolve_target,
)

# --- Helper factories ---


def _make_agent():
    """Create an image builder agent with mocked HTTP client."""
    agent = ImageBuilderAgent(
        state_store_url="http://localhost:8090",
    )
    agent._client = AsyncMock()
    return agent


def _mock_ticket(
    *,
    image_build=None,
    board_selector="board-type=nxp-s32g-vnp-rdb3",
    image_version="AutoSD-10",
):
    """Build a mock ticket dict with image_build directives."""
    cf = {
        "board_selector": board_selector,
        "directives": {
            "board_selector": board_selector,
            "image_version": image_version,
        },
    }
    if image_build is not None:
        cf["image_build"] = image_build
    return {
        "id": "PERF-TEST",
        "status": "building_image",
        "custom_fields": cf,
        "comments": [],
    }


def _mock_get(ticket):
    """Create a mock GET response for a ticket."""
    resp = AsyncMock()
    resp.status_code = 200
    # .json() is sync in httpx, so use MagicMock
    resp.json = MagicMock(return_value=ticket)
    resp.raise_for_status = MagicMock()
    return resp


def _mock_post():
    """Create a mock POST response."""
    resp = AsyncMock()
    resp.status_code = 200
    resp.raise_for_status = MagicMock()
    return resp


# --- Target and mode resolution ---


class TestTargetResolution:
    def test_s32g_resolves_to_ebbr(self):
        assert resolve_target("board-type=nxp-s32g-vnp-rdb3") == "ebbr"

    def test_rcar_s4_resolves_to_ebbr(self):
        assert resolve_target("board-type=renesas-rcar-s4") == "ebbr"

    def test_qc8775_resolves_to_ride4(self):
        assert resolve_target("board-type=qc8775") == "ride4_sa8775p_sx"

    def test_unknown_board_defaults_to_ebbr(self):
        assert resolve_target("board-type=unknown-board") == "ebbr"

    def test_no_selector_defaults_to_ebbr(self):
        assert resolve_target("") == "ebbr"


class TestBuildModeResolution:
    def test_s32g_uses_build_dev(self):
        assert resolve_build_mode("board-type=nxp-s32g-vnp-rdb3") == "build-dev"

    def test_qc8775_uses_build(self):
        assert resolve_build_mode("board-type=qc8775") == "build"

    def test_unknown_defaults_to_build_dev(self):
        assert resolve_build_mode("board-type=unknown") == "build-dev"


# --- Manifest generation ---


class TestManifestGeneration:
    def test_basic_manifest(self):
        manifest = generate_manifest(
            {"masked_services": ["foo.service"]},
            name="test-build",
        )
        assert manifest["name"] == "test-build"
        assert "foo.service" in manifest["content"]["systemd"]["masked_services"]

    def test_rpms_in_manifest(self):
        manifest = generate_manifest(
            {"rpms": ["strace", "perf"]},
        )
        # Extra RPMs are added to the base RPM list
        assert "strace" in manifest["content"]["rpms"]
        assert "perf" in manifest["content"]["rpms"]

    def test_base_rpms_always_present(self):
        manifest = generate_manifest({})
        # Base RPMs (openssh-server, chrony, iproute) always included
        assert "openssh-server" in manifest["content"]["rpms"]

    def test_sshd_always_enabled(self):
        manifest = generate_manifest({})
        assert "sshd.service" in manifest["content"]["systemd"]["enabled_services"]

    def test_repos_in_manifest(self):
        manifest = generate_manifest(
            {
                "repos": [
                    {
                        "id": "test-repo",
                        "baseurl": "https://example.com/repo",
                    }
                ]
            },
        )
        assert len(manifest["content"]["repos"]) == 1
        assert manifest["content"]["repos"][0]["id"] == "test-repo"


# --- Agent error handling ---


class TestAgentErrorHandling:
    async def test_get_ticket_failure_transitions_to_guidance(self):
        agent = _make_agent()
        agent._client.get = AsyncMock(
            side_effect=Exception("connection failed"),
        )
        agent._client.post = AsyncMock(return_value=_mock_post())

        await agent.run("PERF-TEST")

        # Should transition to awaiting_customer_guidance
        post_calls = agent._client.post.call_args_list
        transition_call = None
        for call in post_calls:
            url = call.args[0] if call.args else ""
            if "transition" in str(url):
                transition_call = call
        assert transition_call is not None
        body = transition_call.kwargs.get("json", {})
        assert body["status"] == "awaiting_customer_guidance"

    async def test_no_image_build_skips_to_hardware(self):
        ticket = _mock_ticket(image_build=None)
        agent = _make_agent()
        agent._client.get = AsyncMock(return_value=_mock_get(ticket))
        agent._client.post = AsyncMock(return_value=_mock_post())
        agent._client.patch = AsyncMock(return_value=_mock_post())

        await agent.run("PERF-TEST")

        # Should transition to awaiting_hardware
        post_calls = agent._client.post.call_args_list
        transition_call = None
        for call in post_calls:
            url = call.args[0] if call.args else ""
            if "transition" in str(url):
                transition_call = call
        assert transition_call is not None
        body = transition_call.kwargs.get("json", {})
        assert body["status"] == "awaiting_hardware"


# --- Triage merge ---


class TestTriageMerge:
    """Test merge_image_build — user-provided fields take precedence."""

    def test_user_target_preserved(self):
        """User-provided target should not be overwritten by triage."""
        from agents.triage.agent import merge_image_build

        merged = merge_image_build(
            user_build={"provider": "caib", "target": "ebbr"},
            triage_build={"provider": "caib", "target": "rcar_s4"},
        )
        assert merged["target"] == "ebbr"

    def test_customizations_merge_deeply(self):
        """Customizations from both sides should be preserved."""
        from agents.triage.agent import merge_image_build

        merged = merge_image_build(
            user_build={
                "customizations": {"masked_services": ["foo.service"]},
            },
            triage_build={
                "customizations": {"rpms": ["strace"]},
            },
        )
        assert "masked_services" in merged["customizations"]
        assert "rpms" in merged["customizations"]

    def test_user_only(self):
        """Works when triage provides no image_build."""
        from agents.triage.agent import merge_image_build

        merged = merge_image_build(
            user_build={"provider": "caib", "target": "ebbr"},
            triage_build={},
        )
        assert merged["target"] == "ebbr"

    def test_triage_only(self):
        """Works when user provides no image_build."""
        from agents.triage.agent import merge_image_build

        merged = merge_image_build(
            user_build={},
            triage_build={"provider": "caib", "target": "ebbr"},
        )
        assert merged["target"] == "ebbr"


# --- Quay token resolution ---


class TestQuayTokenResolution:
    def test_robot_account_token_from_registry_auth(self, tmp_path):
        from providers.image_build.caib import CAIBProvider

        provider = CAIBProvider()

        # Create mock registry-auth.json
        caib_dir = tmp_path / "caib"
        caib_dir.mkdir()
        import base64

        auth = base64.b64encode(b"robot+user:secret-token").decode()
        auth_file = caib_dir / "registry-auth.json"
        auth_file.write_text(json.dumps({"auths": {"quay.io": {"auth": auth}}}))

        token = provider._resolve_quay_token(tmp_path)
        assert token == "secret-token"

    def test_oauth_token_fallback(self, tmp_path):
        from providers.image_build.caib import CAIBProvider

        provider = CAIBProvider()

        # No registry-auth.json, but oauth token exists
        quay_dir = tmp_path / "quay"
        quay_dir.mkdir()
        (quay_dir / "api-token").write_text("oauth-token-123\n")

        token = provider._resolve_quay_token(tmp_path)
        assert token == "oauth-token-123"

    def test_no_token_returns_empty(self, tmp_path):
        from providers.image_build.caib import CAIBProvider

        provider = CAIBProvider()
        token = provider._resolve_quay_token(tmp_path)
        assert token == ""

    def test_robot_preferred_over_oauth(self, tmp_path):
        from providers.image_build.caib import CAIBProvider

        provider = CAIBProvider()
        import base64

        # Both exist — robot should win
        caib_dir = tmp_path / "caib"
        caib_dir.mkdir()
        auth = base64.b64encode(b"robot+user:robot-token").decode()
        (caib_dir / "registry-auth.json").write_text(
            json.dumps({"auths": {"quay.io": {"auth": auth}}})
        )

        quay_dir = tmp_path / "quay"
        quay_dir.mkdir()
        (quay_dir / "api-token").write_text("oauth-token")

        token = provider._resolve_quay_token(tmp_path)
        assert token == "robot-token"
