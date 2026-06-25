"""Tests for Investigation Record MCP server tools.

Tests the tool functions directly (without MCP transport) to verify
they correctly wrap the provider interface. Records are write-once;
tests verify immutability constraints.
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch

import pytest

from agents.investigation.server import (
    append_build_history,
    close_investigation_record,
    create_investigation_record,
    get_investigation_record,
    link_jira_ticket,
    query_investigation_records,
)
from providers.investigation.file import FileRecordProvider


@pytest.fixture(autouse=True)
def _patch_provider(tmp_path: Path):
    """Patch the server's provider with a temp file provider."""
    provider = FileRecordProvider(
        persist_dir=tmp_path / "records",
    )
    with patch(
        "agents.investigation.server._get_provider",
        return_value=provider,
    ):
        yield


# --- Create ---


@pytest.mark.asyncio
async def test_create_record():
    """Create a record and get back an ID."""
    result = json.loads(
        await create_investigation_record(
            subsystem="storage_io",
            metric="iops_4k_randread",
            direction="degrading",
            platform="NXP_S32G",
            magnitude="-31%",
            root_cause_summary="virtio-blk regression",
            confidence=0.92,
            build_id="2026.05.14",
            convergence_outcome="ISOLATION",
        )
    )
    assert result["status"] == "created"
    assert result["investigation_id"].startswith("RCA-")
    assert result["state"] == "open"


@pytest.mark.asyncio
async def test_create_without_build_id():
    """Create a record with no initial build history."""
    result = json.loads(
        await create_investigation_record(
            subsystem="networking",
            metric="throughput_gbps",
        )
    )
    assert result["status"] == "created"


# --- Query ---


@pytest.mark.asyncio
async def test_query_empty():
    """Query with no records returns empty list."""
    result = json.loads(await query_investigation_records())
    assert result["count"] == 0
    assert result["records"] == []


@pytest.mark.asyncio
async def test_query_returns_created_records():
    """Records created via the tool appear in queries."""
    await create_investigation_record(
        subsystem="storage_io",
        metric="iops",
        platform="NXP_S32G",
    )
    await create_investigation_record(
        subsystem="networking",
        metric="throughput",
        platform="Intel_Xeon",
    )

    result = json.loads(await query_investigation_records())
    assert result["count"] == 2


@pytest.mark.asyncio
async def test_query_filters_by_subsystem():
    """Query filters by subsystem."""
    await create_investigation_record(
        subsystem="storage_io",
        metric="iops",
    )
    await create_investigation_record(
        subsystem="networking",
        metric="throughput",
    )

    result = json.loads(
        await query_investigation_records(
            subsystem="storage_io",
        )
    )
    assert result["count"] == 1
    assert result["records"][0]["subsystem"] == "storage_io"


@pytest.mark.asyncio
async def test_query_filters_by_state():
    """Query filters by state."""
    create_result = json.loads(
        await create_investigation_record(
            subsystem="storage_io",
            metric="iops",
        )
    )
    rid = create_result["investigation_id"]
    await close_investigation_record(rid)

    await create_investigation_record(
        subsystem="networking",
        metric="throughput",
    )

    open_result = json.loads(await query_investigation_records(state="open"))
    assert open_result["count"] == 1

    resolved_result = json.loads(
        await query_investigation_records(
            state="resolved",
        )
    )
    assert resolved_result["count"] == 1


# --- Get ---


@pytest.mark.asyncio
async def test_get_existing_record():
    """Get a record by ID returns full details."""
    create_result = json.loads(
        await create_investigation_record(
            subsystem="storage_io",
            metric="iops",
            root_cause_summary="virtio-blk regression",
            confidence=0.92,
        )
    )
    rid = create_result["investigation_id"]

    result = json.loads(await get_investigation_record(rid))
    assert result["found"] is True
    assert result["record"]["investigation_id"] == rid
    assert result["record"]["root_cause_summary"] == "virtio-blk regression"


@pytest.mark.asyncio
async def test_get_nonexistent_record():
    """Get a nonexistent record returns found=False."""
    result = json.loads(await get_investigation_record("RCA-NONEXIST"))
    assert result["found"] is False


# --- Build history ---


@pytest.mark.asyncio
async def test_append_build_history():
    """Append build history entries to a record."""
    create_result = json.loads(
        await create_investigation_record(
            subsystem="storage_io",
            metric="iops",
            build_id="2026.05.14",
        )
    )
    rid = create_result["investigation_id"]

    result = json.loads(
        await append_build_history(
            investigation_id=rid,
            build_id="2026.05.15",
            action="SKIP_MATCHED",
            comment="Still present",
        )
    )
    assert result["status"] == "appended"

    # Verify via get
    get_result = json.loads(await get_investigation_record(rid))
    history = get_result["record"]["build_history"]
    assert len(history) == 2
    assert history[1]["build_id"] == "2026.05.15"
    assert history[1]["action"] == "SKIP_MATCHED"


@pytest.mark.asyncio
async def test_append_build_history_nonexistent():
    """Append to nonexistent record returns not_found."""
    result = json.loads(
        await append_build_history(
            investigation_id="RCA-NONEXIST",
            build_id="2026.05.14",
        )
    )
    assert result["status"] == "not_found"


# --- Link Jira ---


@pytest.mark.asyncio
async def test_link_jira_ticket():
    """Link a Jira ticket to a record."""
    create_result = json.loads(
        await create_investigation_record(
            subsystem="storage_io",
            metric="iops",
        )
    )
    rid = create_result["investigation_id"]

    result = json.loads(
        await link_jira_ticket(
            investigation_id=rid,
            jira_ticket="RHIVOS-4821",
        )
    )
    assert result["status"] == "linked"

    # Verify via get
    get_result = json.loads(await get_investigation_record(rid))
    assert get_result["record"]["jira_ticket"] == "RHIVOS-4821"


@pytest.mark.asyncio
async def test_link_jira_already_linked():
    """Linking a second Jira ticket is rejected."""
    create_result = json.loads(
        await create_investigation_record(
            subsystem="storage_io",
            metric="iops",
            jira_ticket="RHIVOS-0001",
        )
    )
    rid = create_result["investigation_id"]

    result = json.loads(
        await link_jira_ticket(
            investigation_id=rid,
            jira_ticket="RHIVOS-0002",
        )
    )
    assert result["status"] == "already_linked"


@pytest.mark.asyncio
async def test_link_jira_nonexistent():
    """Linking to nonexistent record returns not_found."""
    result = json.loads(
        await link_jira_ticket(
            investigation_id="RCA-NONEXIST",
            jira_ticket="RHIVOS-0001",
        )
    )
    assert result["status"] == "not_found"


# --- Close ---


@pytest.mark.asyncio
async def test_close_record():
    """Close sets state to resolved."""
    create_result = json.loads(
        await create_investigation_record(
            subsystem="storage_io",
            metric="iops",
        )
    )
    rid = create_result["investigation_id"]

    result = json.loads(await close_investigation_record(rid))
    assert result["status"] == "closed"

    # Verify via get
    get_result = json.loads(await get_investigation_record(rid))
    assert get_result["record"]["state"] == "resolved"


@pytest.mark.asyncio
async def test_close_nonexistent():
    """Close a nonexistent record returns not_found."""
    result = json.loads(await close_investigation_record("RCA-NONEXIST"))
    assert result["status"] == "not_found"
