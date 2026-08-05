"""Tests for webhook enrichment provider."""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest

from providers.webhook_enrichment import enrich_webhook_ticket


class TestEnrichWebhookTicket:
    @pytest.mark.asyncio
    async def test_skips_non_webhook_tickets(self):
        result = await enrich_webhook_ticket(
            "http://localhost:8090",
            "PERF-TEST",
            {"custom_fields": {}},
        )
        assert result is False

    @pytest.mark.asyncio
    async def test_skips_already_enriched(self):
        result = await enrich_webhook_ticket(
            "http://localhost:8090",
            "PERF-TEST",
            {
                "custom_fields": {
                    "trigger_source": "horreum",
                    "run_metadata": {"target": "rcar_s4"},
                },
            },
        )
        assert result is True

    @pytest.mark.asyncio
    async def test_skips_missing_run_id(self):
        result = await enrich_webhook_ticket(
            "http://localhost:8090",
            "PERF-TEST",
            {
                "custom_fields": {
                    "trigger_source": "horreum",
                    "anomaly_context": {},
                },
            },
        )
        assert result is False

    @pytest.mark.asyncio
    async def test_writes_run_metadata(self):
        mock_run_info = {
            "target": "ride4_sa8775p_sx_r3",
            "os_id": "rhivos",
            "mode": "bootc",
            "build": "12345.abc",
            "labels": {"RHIVOS Release": "latest-RHIVOS-2-202607240103"},
        }

        with patch(
            "providers.webhook_enrichment._call_get_run_info",
            new_callable=AsyncMock,
            return_value=mock_run_info,
        ):
            mock_client = AsyncMock()
            with patch("httpx.AsyncClient") as mock_httpx:
                mock_httpx.return_value.__aenter__ = AsyncMock(
                    return_value=mock_client,
                )
                mock_httpx.return_value.__aexit__ = AsyncMock(
                    return_value=False,
                )

                result = await enrich_webhook_ticket(
                    "http://localhost:8090",
                    "PERF-TEST",
                    {
                        "custom_fields": {
                            "trigger_source": "horreum",
                            "anomaly_context": {"run_id": 281971},
                        },
                    },
                )

        assert result is True
        # Verify the PATCH was called with run_metadata
        mock_client.patch.assert_called_once()
        call_args = mock_client.patch.call_args
        fields = call_args.kwargs.get(
            "json",
            call_args[1].get("json", {}),
        )["fields"]
        assert fields["run_metadata"] == mock_run_info

    @pytest.mark.asyncio
    async def test_returns_false_on_mcp_failure(self):
        with patch(
            "providers.webhook_enrichment._call_get_run_info",
            new_callable=AsyncMock,
            return_value={"error": "not found"},
        ):
            result = await enrich_webhook_ticket(
                "http://localhost:8090",
                "PERF-TEST",
                {
                    "custom_fields": {
                        "trigger_source": "horreum",
                        "anomaly_context": {"run_id": 999},
                    },
                },
            )
        assert result is False
