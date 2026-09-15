from __future__ import annotations

import os

import httpx
import pytest

from providers.tracing import (
    ActionDescriptor,
    ActionType,
    LifecycleDescriptor,
    LifecycleState,
    SpoolBackpressure,
    SpoolCorruption,
    TraceClient,
    TraceDeliveryError,
    TraceEventV1,
    TraceSpool,
    drain_abandoned_spools,
)


def event() -> TraceEventV1:
    return TraceEventV1(
        ticket_id="PERF-1",
        action=ActionDescriptor(type=ActionType.DISPATCH),
        lifecycle=LifecycleDescriptor(state=LifecycleState.STARTED),
    )


def test_append_is_replayed_after_crash_before_send(tmp_path) -> None:
    first = TraceSpool(tmp_path, name="producer")
    first.append(event())
    first.close()
    second = TraceSpool(tmp_path, name="producer")
    pending = list(second.pending())
    assert len(pending) == 1
    second.acknowledge(pending[0][0])
    second.compact()
    assert list(second.pending()) == []


def test_new_spool_directory_entry_is_fsynced(tmp_path, monkeypatch) -> None:
    observations: list[bool] = []
    monkeypatch.setattr(
        TraceSpool,
        "_fsync_dir",
        lambda self: observations.append(self.path.exists()),
    )
    spool = TraceSpool(tmp_path, name="durable-create")
    assert observations == [True]
    spool.close()


def test_ack_crash_window_replays_idempotently(tmp_path) -> None:
    spool = TraceSpool(tmp_path, name="producer")
    original = event()
    spool.append(original)
    delivered: list[str] = []
    for _, pending in spool.pending():  # server commits, process dies before ack
        delivered.append(str(pending.event_id))
    for offset, pending in spool.pending():
        delivered.append(str(pending.event_id))
        spool.acknowledge(offset)
    assert delivered == [str(original.event_id), str(original.event_id)]
    spool.close()


def test_cap_and_corrupt_or_truncated_frames_are_not_silently_skipped(tmp_path) -> None:
    spool = TraceSpool(tmp_path, name="producer", max_bytes=1_500)
    spool.append(event())
    with pytest.raises(SpoolBackpressure):
        spool.append(event())
    with spool.path.open("ab") as stream:
        stream.write(b"\x00\x00")
        stream.flush()
        os.fsync(stream.fileno())
    with pytest.raises(SpoolCorruption):
        list(spool.pending())
    assert list(tmp_path.glob("*.bad"))
    spool.close()


def test_abandoned_producer_is_drained(tmp_path) -> None:
    producer = TraceSpool(tmp_path, name="subprocess")
    original = event()
    producer.append(original)
    producer.close()
    delivered: list[str] = []
    assert (
        drain_abandoned_spools(
            tmp_path, lambda item: delivered.append(str(item.event_id))
        )
        == 1
    )
    assert delivered == [str(original.event_id)]


def test_active_spool_is_not_swept_and_bad_ack_is_quarantined(tmp_path) -> None:
    active = TraceSpool(tmp_path, name="active")
    active.append(event())
    assert drain_abandoned_spools(tmp_path, lambda _: None) == 0
    active.close()
    bad = TraceSpool(tmp_path, name="bad")
    bad.append(event())
    bad.ack_path.write_text("1")
    with pytest.raises(SpoolCorruption):
        list(bad.pending())
    bad.close()


def test_active_compacted_producer_remains_exclusive_to_sweeper(tmp_path) -> None:
    active = TraceSpool(tmp_path, name="active-compact")
    active.append(event())
    offset, _ = next(active.pending())
    active.acknowledge(offset)
    active.compact()
    second = event()
    active.append(second)
    assert drain_abandoned_spools(tmp_path, lambda _: None) == 0
    assert [item.event_id for _, item in active.pending()] == [second.event_id]
    active.close()


def test_bad_spool_does_not_block_later_good_spool(tmp_path, caplog) -> None:
    bad = TraceSpool(tmp_path, name="bad")
    bad.append(event())
    with bad.path.open("r+b") as stream:
        stream.seek(5)
        stream.write(b"!")
    bad.close()
    good = TraceSpool(tmp_path, name="good")
    expected = event()
    good.append(expected)
    good.close()
    delivered: list[str] = []
    assert (
        drain_abandoned_spools(
            tmp_path, lambda item: delivered.append(str(item.event_id))
        )
        == 1
    )
    assert delivered == [str(expected.event_id)]
    assert list(tmp_path.glob("*.bad"))
    assert "Quarantined corrupt trace spool" in caplog.text


def test_invalid_length_and_compaction_crash_boundary_are_safe(
    tmp_path, monkeypatch
) -> None:
    spool = TraceSpool(tmp_path, name="length")
    spool.append(event())
    with spool.path.open("r+b") as stream:
        stream.write((2_000_000).to_bytes(4, "big"))
    with pytest.raises(SpoolCorruption):
        list(spool.pending())
    spool.close()
    compact = TraceSpool(tmp_path, name="compact")
    compact.append(event())
    offset, _ = next(compact.pending())
    compact.acknowledge(offset)
    original_replace = os.replace

    def crash_before_replace(source, target):
        if target == compact.path:
            raise OSError("simulated crash")
        return original_replace(source, target)

    monkeypatch.setattr(os, "replace", crash_before_replace)
    with pytest.raises(OSError):
        compact.compact()
    assert list(compact.pending())  # ack reset before replace replays safely
    compact.close()


def test_outage_and_partial_batch_leave_unacknowledged_events_for_replay(
    tmp_path,
) -> None:
    calls = 0

    def flaky(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        if calls == 1:
            return httpx.Response(503)
        import json

        events = json.loads(request.content)["events"]
        return httpx.Response(
            200,
            json={
                "acknowledgements": [
                    {
                        "event_id": events[0]["event_id"],
                        "accepted": True,
                        "status": "stored",
                    }
                ]
            },
        )

    client = TraceClient(
        "http://store",
        "token",
        spool_dir=tmp_path,
        client=httpx.Client(transport=httpx.MockTransport(flaky)),
    )
    client.record(event())
    client.record(event())
    with pytest.raises(TraceDeliveryError):
        client.flush()
    assert client.spool.bytes_pending() > 0
    with pytest.raises(TraceDeliveryError):
        client.flush()
    client._client.close()
    client.spool.close()


def test_critical_fails_closed_and_clean_close_drains(tmp_path) -> None:
    def unavailable(_: httpx.Request) -> httpx.Response:
        return httpx.Response(503)

    client = TraceClient(
        "http://store",
        "token",
        spool_dir=tmp_path,
        client=httpx.Client(transport=httpx.MockTransport(unavailable)),
    )
    with pytest.raises(TraceDeliveryError):
        client.record_critical(event())
    client.close()

    delivered: list[str] = []

    def accepted(request: httpx.Request) -> httpx.Response:
        import json

        body = json.loads(request.content)
        if request.url.path.endswith("/batch"):
            delivered.extend(item["event_id"] for item in body["events"])
            return httpx.Response(
                200,
                json={
                    "acknowledgements": [
                        {
                            "event_id": item["event_id"],
                            "accepted": True,
                            "status": "stored",
                        }
                        for item in body["events"]
                    ]
                },
            )
        delivered.append(body["event_id"])
        return httpx.Response(200, json={"event": body, "status": "stored"})

    sender = TraceClient(
        "http://store",
        "token",
        spool_dir=tmp_path / "ok",
        client=httpx.Client(transport=httpx.MockTransport(accepted)),
    )
    sender.record(event())
    sender.close()
    assert len(delivered) == 1
    assert not list((tmp_path / "ok").glob("*.spool"))
    assert not list((tmp_path / "ok").glob("*.ack"))


def test_outage_during_close_leaves_durable_spool_for_sweeper(tmp_path) -> None:
    expected = event()

    def unavailable(_: httpx.Request) -> httpx.Response:
        return httpx.Response(503)

    client = TraceClient(
        "http://store",
        "token",
        spool_dir=tmp_path,
        client=httpx.Client(transport=httpx.MockTransport(unavailable)),
    )
    client.record(expected)
    with pytest.raises(TraceDeliveryError):
        client.close()

    delivered: list[str] = []
    assert (
        drain_abandoned_spools(
            tmp_path, lambda item: delivered.append(str(item.event_id))
        )
        == 1
    )
    assert delivered == [str(expected.event_id)]
