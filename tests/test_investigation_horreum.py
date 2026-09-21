"""Tests for Horreum Investigation Record provider.

Tests use httpx mocking to simulate Horreum API responses
without requiring a live Horreum instance.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import httpx
import pytest

from providers.investigation.horreum import (
    _SCHEMA_URI,
    HorreumRecordProvider,
)
from providers.investigation.models import (
    AnomalyContext,
    InvestigationRecord,
)


def _make_record(**overrides) -> InvestigationRecord:
    defaults = {
        "investigation_id": "RCA-TEST0001",
        "anomaly_context": AnomalyContext(
            subsystem="storage_io",
            metric="iops_4k_randread",
            direction="degrading",
            platform="NXP_S32G",
            magnitude="-31%",
        ),
        "root_cause_summary": "virtio-blk regression",
        "confidence": 0.92,
    }
    defaults.update(overrides)
    return InvestigationRecord(**defaults)


def _mock_response(json_data=None, status_code=200):
    """Create a mock httpx.Response."""
    resp = MagicMock(spec=httpx.Response)
    resp.status_code = status_code
    resp.json.return_value = json_data or {}
    resp.raise_for_status = MagicMock()
    return resp


# --- Constructor ---


def test_requires_url():
    """Provider requires a URL."""
    with pytest.raises(ValueError, match="url"):
        HorreumRecordProvider(test_id=1)


def test_requires_test_id():
    """Provider requires a test_id."""
    with pytest.raises(ValueError, match="test_id"):
        HorreumRecordProvider(url="https://horreum.example.com")


def test_accepts_url():
    """Provider accepts URL and token."""
    p = HorreumRecordProvider(
        url="https://horreum.example.com",
        token="test-token",
        test_id=42,
    )
    assert p.provider_name == "horreum"
    assert p._url == "https://horreum.example.com"


# --- Test auto-creation ---


# --- Create ---


@pytest.mark.asyncio
async def test_create_uploads_run():
    """Create uploads the record as a Horreum run."""
    p = HorreumRecordProvider(
        url="https://horreum.example.com",
        test_id=42,
    )
    p._test_id = 42

    posted_payload = {}

    async def mock_post(*args, **kwargs):
        nonlocal posted_payload
        posted_payload = kwargs.get("json", {})
        return _mock_response(json_data=101)

    p._client.post = mock_post

    record = _make_record()
    record.record_url = "https://horreum.example.com/run/101"
    rid = await p.create(record)

    assert rid == "RCA-TEST0001"
    assert posted_payload.get("$schema") == _SCHEMA_URI
    assert posted_payload.get("investigation_id") == "RCA-TEST0001"
    assert "record_url" not in posted_payload


# --- Get ---


@pytest.mark.asyncio
async def test_get_finds_record():
    """Get retrieves a record by investigation ID."""
    p = HorreumRecordProvider(
        url="https://horreum.example.com",
        test_id=42,
    )
    p._test_id = 42

    record = _make_record()
    payload = record.model_dump(mode="json")
    payload["$schema"] = _SCHEMA_URI

    call_count = 0

    async def mock_get(*args, **kwargs):
        nonlocal call_count
        call_count += 1
        url = args[0] if args else kwargs.get("url", "")
        if "list" in str(url):
            return _mock_response(
                json_data={
                    "runs": [
                        {
                            "id": 101,
                            "description": (
                                "RCA-TEST0001: storage_io iops_4k_randread"
                            ),
                        }
                    ]
                }
            )
        else:
            return _mock_response(json_data={"data": payload})

    p._client.get = mock_get

    result = await p.get("RCA-TEST0001")
    assert result is not None
    assert result.investigation_id == "RCA-TEST0001"
    assert result.anomaly_context.subsystem == "storage_io"
    assert result.record_url == "https://horreum.example.com/run/101"


@pytest.mark.asyncio
async def test_query_populates_direct_run_url():
    """Query derives the direct Horreum URL rather than persisting it."""
    p = HorreumRecordProvider(
        url="https://horreum.example.com/",
        test_id=42,
    )
    record = _make_record()
    payload = record.model_dump(mode="json")
    payload["$schema"] = _SCHEMA_URI

    async def mock_get(*args, **kwargs):
        url = args[0] if args else kwargs.get("url", "")
        if "list" in str(url):
            return _mock_response(json_data={"runs": [{"id": 101}]})
        return _mock_response(json_data={"data": payload})

    p._client.get = mock_get

    result = await p.query(metric="iops_4k_randread")

    assert len(result) == 1
    assert result[0].record_url == "https://horreum.example.com/run/101"


@pytest.mark.asyncio
async def test_get_returns_none_when_missing():
    """Get returns None when the record doesn't exist."""
    p = HorreumRecordProvider(
        url="https://horreum.example.com",
        test_id=42,
    )
    p._test_id = 42

    async def mock_get(*args, **kwargs):
        return _mock_response(json_data={"runs": []})

    p._client.get = mock_get

    result = await p.get("RCA-NONEXIST")
    assert result is None


# --- Registry ---


def test_horreum_in_registry():
    """Horreum is registered as a backend."""
    from providers.investigation.registry import (
        BACKEND_REGISTRY,
    )

    assert "horreum" in BACKEND_REGISTRY


def test_create_horreum_provider():
    """Registry creates a Horreum provider."""
    from providers.investigation.registry import (
        create_record_provider,
    )

    provider = create_record_provider(
        backend="horreum",
        url="https://horreum.example.com",
        token="test-token",
        test_id=42,
    )
    assert isinstance(provider, HorreumRecordProvider)
