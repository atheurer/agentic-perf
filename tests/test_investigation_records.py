"""Tests for Investigation Record models, file provider, and registry.

Covers the backend-agnostic data models, file-based CRUD operations,
query filtering, build history append, and registry configuration.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from providers.investigation.base import (
    InvestigationRecordProvider,
)
from providers.investigation.file import FileRecordProvider
from providers.investigation.models import (
    AnomalyContext,
    BuildHistoryEntry,
    ChangeAttribution,
    InvestigationRecord,
    InvestigationState,
    OperationalMetrics,
)
from providers.investigation.registry import (
    BACKEND_REGISTRY,
    create_record_provider,
)

# --- Fixtures ---


@pytest.fixture
def persist_dir(tmp_path: Path) -> Path:
    """Temporary directory for file-based records."""
    return tmp_path / "records"


@pytest.fixture
def provider(persist_dir: Path) -> FileRecordProvider:
    """File-based provider with a temp directory."""
    return FileRecordProvider(persist_dir=persist_dir)


def _make_record(**overrides) -> InvestigationRecord:
    """Create a test record with sensible defaults."""
    defaults = {
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


# --- Model tests ---


def test_record_generates_id():
    """Records get a unique ID on creation."""
    r = _make_record()
    assert r.investigation_id.startswith("RCA-")
    assert len(r.investigation_id) == 12  # RCA- + 8 hex


def test_record_default_state():
    """New records start in OPEN state."""
    r = _make_record()
    assert r.state == InvestigationState.OPEN


def test_record_serialization_roundtrip():
    """Records serialize to JSON and back without loss."""
    r = _make_record(
        change_attribution=ChangeAttribution(
            classification="ISOLATION",
            causal_commits=["abc123"],
            change_summary="Queue refactoring",
        ),
        build_history=[
            BuildHistoryEntry(
                build_id="2026.05.14",
                action="FULL_INVESTIGATION",
            ),
        ],
    )
    data = r.model_dump_json()
    restored = InvestigationRecord.model_validate_json(data)
    assert restored.investigation_id == r.investigation_id
    assert restored.anomaly_context.subsystem == "storage_io"
    assert restored.change_attribution.classification == "ISOLATION"
    assert len(restored.build_history) == 1


def test_anomaly_context_fields():
    """AnomalyContext captures regression details."""
    ctx = AnomalyContext(
        subsystem="networking",
        metric="throughput_gbps",
        direction="degrading",
        platform="Intel_Xeon",
        magnitude="-15%",
    )
    assert ctx.subsystem == "networking"
    assert ctx.magnitude == "-15%"


def test_operational_metrics_defaults():
    """OperationalMetrics has sensible defaults."""
    m = OperationalMetrics()
    assert m.provision_cycles == 0
    assert m.wall_clock_mins == 0.0
    assert m.info_gain_trajectory == []
    assert m.resource_consumption.llm_tokens_total == 0


# --- File provider CRUD ---


@pytest.mark.asyncio
async def test_create_and_get(provider: FileRecordProvider):
    """Create a record and retrieve it by ID."""
    record = _make_record()
    rid = await provider.create(record)
    assert rid == record.investigation_id

    fetched = await provider.get(rid)
    assert fetched is not None
    assert fetched.investigation_id == rid
    assert fetched.anomaly_context.subsystem == "storage_io"


@pytest.mark.asyncio
async def test_get_nonexistent(provider: FileRecordProvider):
    """Getting a nonexistent record returns None."""
    result = await provider.get("RCA-NONEXISTENT")
    assert result is None


@pytest.mark.asyncio
async def test_create_persists_to_disk(
    provider: FileRecordProvider,
    persist_dir: Path,
):
    """Records are persisted as JSON files."""
    record = _make_record()
    rid = await provider.create(record)
    path = persist_dir / f"{rid}.json"
    assert path.exists()


@pytest.mark.asyncio
async def test_link_jira(provider: FileRecordProvider):
    """Link a Jira ticket to a record."""
    record = _make_record()
    rid = await provider.create(record)

    await provider.link_jira(rid, "RHIVOS-4821")

    fetched = await provider.get(rid)
    assert fetched is not None
    assert fetched.jira_ticket == "RHIVOS-4821"


@pytest.mark.asyncio
async def test_link_jira_already_linked(
    provider: FileRecordProvider,
):
    """Linking a second Jira ticket raises ValueError."""
    record = _make_record(jira_ticket="RHIVOS-0001")
    rid = await provider.create(record)

    with pytest.raises(ValueError, match="already linked"):
        await provider.link_jira(rid, "RHIVOS-0002")


@pytest.mark.asyncio
async def test_link_jira_nonexistent(
    provider: FileRecordProvider,
):
    """Linking to a nonexistent record raises KeyError."""
    with pytest.raises(KeyError):
        await provider.link_jira("RCA-NOPE", "RHIVOS-0001")


@pytest.mark.asyncio
async def test_append_build_history(
    provider: FileRecordProvider,
):
    """Append entries to build history."""
    record = _make_record()
    rid = await provider.create(record)

    await provider.append_build_history(
        rid,
        BuildHistoryEntry(
            build_id="2026.05.14",
            action="FULL_INVESTIGATION",
            comment="Initial discovery",
        ),
    )
    await provider.append_build_history(
        rid,
        BuildHistoryEntry(
            build_id="2026.05.15",
            action="SKIP_MATCHED",
            comment="Still present",
        ),
    )

    fetched = await provider.get(rid)
    assert fetched is not None
    assert len(fetched.build_history) == 2
    assert fetched.build_history[0].build_id == "2026.05.14"
    assert fetched.build_history[1].action == "SKIP_MATCHED"


@pytest.mark.asyncio
async def test_append_build_history_nonexistent(
    provider: FileRecordProvider,
):
    """Appending to a nonexistent record raises KeyError."""
    with pytest.raises(KeyError):
        await provider.append_build_history(
            "RCA-NOPE",
            BuildHistoryEntry(
                build_id="2026.05.14",
                action="FULL_INVESTIGATION",
            ),
        )


@pytest.mark.asyncio
async def test_close_record(provider: FileRecordProvider):
    """Closing a record sets state to RESOLVED."""
    record = _make_record()
    rid = await provider.create(record)

    await provider.close_record(rid)

    fetched = await provider.get(rid)
    assert fetched is not None
    assert fetched.state == InvestigationState.RESOLVED


@pytest.mark.asyncio
async def test_close_nonexistent(
    provider: FileRecordProvider,
):
    """Closing a nonexistent record raises KeyError."""
    with pytest.raises(KeyError):
        await provider.close_record("RCA-NOPE")


# --- Query filtering ---


@pytest.mark.asyncio
async def test_query_all(provider: FileRecordProvider):
    """Query with no filters returns all records."""
    for i in range(3):
        await provider.create(
            _make_record(
                investigation_id=f"RCA-TEST000{i}",
            )
        )

    results = await provider.query()
    assert len(results) == 3


@pytest.mark.asyncio
async def test_query_by_state(
    provider: FileRecordProvider,
):
    """Query filters by state."""
    r1 = _make_record(investigation_id="RCA-OPEN0001")
    r2 = _make_record(investigation_id="RCA-CLOSED01")
    await provider.create(r1)
    await provider.create(r2)
    await provider.close_record("RCA-CLOSED01")

    open_records = await provider.query(state="open")
    assert len(open_records) == 1
    assert open_records[0].investigation_id == "RCA-OPEN0001"

    resolved = await provider.query(state="resolved")
    assert len(resolved) == 1
    assert resolved[0].investigation_id == "RCA-CLOSED01"


@pytest.mark.asyncio
async def test_query_by_subsystem(
    provider: FileRecordProvider,
):
    """Query filters by subsystem."""
    await provider.create(
        _make_record(
            investigation_id="RCA-STORAGE1",
            anomaly_context=AnomalyContext(
                subsystem="storage_io",
                metric="iops",
            ),
        )
    )
    await provider.create(
        _make_record(
            investigation_id="RCA-NETWORK1",
            anomaly_context=AnomalyContext(
                subsystem="networking",
                metric="throughput",
            ),
        )
    )

    results = await provider.query(subsystem="storage_io")
    assert len(results) == 1
    assert results[0].investigation_id == "RCA-STORAGE1"


@pytest.mark.asyncio
async def test_query_by_platform(
    provider: FileRecordProvider,
):
    """Query filters by platform."""
    await provider.create(
        _make_record(
            investigation_id="RCA-NXP00001",
            anomaly_context=AnomalyContext(
                subsystem="storage_io",
                metric="iops",
                platform="NXP_S32G",
            ),
        )
    )
    await provider.create(
        _make_record(
            investigation_id="RCA-INTEL001",
            anomaly_context=AnomalyContext(
                subsystem="storage_io",
                metric="iops",
                platform="Intel_Xeon",
            ),
        )
    )

    results = await provider.query(platform="NXP_S32G")
    assert len(results) == 1
    assert results[0].investigation_id == "RCA-NXP00001"


@pytest.mark.asyncio
async def test_query_combined_filters(
    provider: FileRecordProvider,
):
    """Multiple filters combine as AND."""
    await provider.create(
        _make_record(
            investigation_id="RCA-MATCH001",
            anomaly_context=AnomalyContext(
                subsystem="storage_io",
                metric="iops",
                platform="NXP_S32G",
            ),
        )
    )
    await provider.create(
        _make_record(
            investigation_id="RCA-NOMATCH1",
            anomaly_context=AnomalyContext(
                subsystem="storage_io",
                metric="iops",
                platform="Intel_Xeon",
            ),
        )
    )

    results = await provider.query(
        subsystem="storage_io",
        platform="NXP_S32G",
        state="open",
    )
    assert len(results) == 1
    assert results[0].investigation_id == "RCA-MATCH001"


@pytest.mark.asyncio
async def test_query_limit(provider: FileRecordProvider):
    """Query respects the limit parameter."""
    for i in range(10):
        await provider.create(
            _make_record(
                investigation_id=f"RCA-LIMIT{i:04d}",
            )
        )

    results = await provider.query(limit=3)
    assert len(results) == 3


# --- Registry ---


def test_file_backend_in_registry():
    """File backend is registered."""
    assert "file" in BACKEND_REGISTRY


def test_create_file_provider(tmp_path: Path):
    """Registry creates a file provider."""
    provider = create_record_provider(
        backend="file",
        persist_dir=tmp_path / "test-records",
    )
    assert isinstance(provider, FileRecordProvider)
    assert isinstance(provider, InvestigationRecordProvider)


def test_create_default_provider(tmp_path: Path):
    """Default backend is file when no config exists."""
    provider = create_record_provider(
        persist_dir=tmp_path / "default-records",
    )
    assert isinstance(provider, FileRecordProvider)


def test_unknown_backend_raises():
    """Unknown backend name raises ValueError."""
    with pytest.raises(ValueError, match="Unknown"):
        create_record_provider(backend="nonexistent")
