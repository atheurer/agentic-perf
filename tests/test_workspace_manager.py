from __future__ import annotations

import json

import pytest

import paths
from providers.workspace.manager import WorkspaceManager, WorkspaceSecurityError


@pytest.fixture
def workspace(tmp_path, monkeypatch):
    monkeypatch.setattr(paths, "TICKET_DIR", tmp_path / "tickets")
    manager = WorkspaceManager(ticket_id="PERF-6E200FFE")
    return manager


def test_workspace_dir_creation(workspace, tmp_path):
    assert workspace.workspace_dir.exists()
    assert (
        workspace.workspace_dir == tmp_path / "tickets" / "PERF-6E200FFE" / "workspace"
    )
    assert {
        "context",
        "runfiles",
        "results",
        "logs",
        "metadata",
        "scratch",
    }.issubset({p.name for p in workspace.workspace_dir.iterdir()})


def test_path_resolution_and_security(workspace):
    resolved = workspace.resolve_path("workspace://data.json")
    assert resolved == workspace.workspace_dir / "data.json"

    resolved_plain = workspace.resolve_path("subdir/stats.txt")
    assert resolved_plain == workspace.workspace_dir / "subdir" / "stats.txt"

    with pytest.raises(WorkspaceSecurityError):
        workspace.resolve_path("../../etc/passwd")

    with pytest.raises(WorkspaceSecurityError):
        workspace.resolve_path("workspace://../other_ticket/data.json")


def test_save_and_list_files(workspace):
    ref1, p1 = workspace.save_file("metrics.json", json.dumps({"cpu": 95.5}))
    assert ref1 == "workspace://metrics.json"
    assert p1.exists()

    ref2, p2 = workspace.save_file("dmesg.txt", "line1\nline2\n")
    assert ref2 == "workspace://dmesg.txt"
    assert p2.exists()

    files = workspace.list_files()
    assert len(files) == 2
    filenames = {f["filename"] for f in files}
    assert filenames == {"metrics.json", "dmesg.txt"}


def test_namespaced_manifest_kinds_and_artifact_reference(workspace):
    workspace.save_file("context/harnesses/crucible/source.json", "{}")
    workspace.save_file("context/benchmarks/fio/README.md", "guidance")
    workspace.save_file("runfiles/fio.json", "{}")
    workspace.save_file("results/summaries/fio.json", "{}")
    workspace.save_file("results/metrics/fio.json", "{}")
    workspace.save_file("logs/fio.log", "ok")
    workspace.save_file("metadata/run.json", "{}")
    workspace.save_artifact_reference(
        "fio.json", "artifact://PERF-1/run/raw.tar", metadata={"size": 1000}
    )

    entries = {entry["filename"]: entry for entry in workspace.list_files()}
    assert entries["context/harnesses/crucible/source.json"]["kind"] == "source_context"
    assert entries["context/benchmarks/fio/README.md"]["namespace"] == "context"
    assert entries["runfiles/fio.json"]["kind"] == "runfile"
    assert entries["results/summaries/fio.json"]["kind"] == "result_summary"
    assert entries["results/raw/fio.json"]["kind"] == "raw_artifact"
    assert entries["logs/fio.log"]["kind"] == "log"
    assert entries["metadata/run.json"]["kind"] == "metadata"

    assert workspace.jq_query("workspace://results/raw/fio.json", ".artifact_ref")[
        "result"
    ] == ("artifact://PERF-1/run/raw.tar")


def test_effective_context_filters_alternate_sources(workspace):
    github_ref, _ = workspace.save_file("context/sources/github/fio.md", "github")
    controller_ref, _ = workspace.save_file(
        "context/sources/controller/fio.md", "controller"
    )
    workspace.save_effective_context(
        {
            "phase": "benchmark",
            "effective_source": "controller",
            "workspace_refs": [controller_ref],
            "alternate_refs": [github_ref],
        }
    )

    visible = {entry["file_ref"] for entry in workspace.list_effective_files()}
    assert controller_ref not in visible
    assert github_ref not in visible
    assert "workspace://context/effective-context.json" in visible


def test_phase_and_audience_visibility_blocks_triage_source_from_benchmark(
    workspace,
):
    triage = WorkspaceManager(
        workspace_dir=workspace.workspace_dir,
        agent_name="triage-agent",
        phase="triage",
    )
    snapshot = triage.save_source_snapshot(
        "github", {"commit": "github-pin"}, {"README.md": "triage github"}, "fio"
    )
    triage.save_effective_context(
        {
            "phase": "triage",
            "effective_source": "github",
            "workspace_refs": list(snapshot["files"].values()),
        }
    )
    benchmark = WorkspaceManager(
        workspace_dir=workspace.workspace_dir,
        agent_name="benchmark-agent",
        phase="benchmark",
    )
    github_ref = next(iter(snapshot["files"].values()))

    assert github_ref not in {entry["file_ref"] for entry in benchmark.list_files()}
    with pytest.raises(WorkspaceSecurityError):
        benchmark.read_file_slice(github_ref)
    with pytest.raises(WorkspaceSecurityError):
        benchmark.grep_file(github_ref, "github")
    with pytest.raises(WorkspaceSecurityError):
        benchmark.jq_query(github_ref, ".")

    # Explicit comparison access remains available, and legacy root files do
    # not acquire a visibility restriction merely because they lack metadata.
    assert (
        benchmark.read_file_slice(github_ref, include_alternates=True)["status"] == "ok"
    )
    legacy_ref, _ = benchmark.save_file("legacy.json", '{"ok": true}')
    assert benchmark.jq_query(legacy_ref, ".ok")["result"] is True


def test_context_index_does_not_short_circuit_a_different_phase(workspace):
    triage = WorkspaceManager(
        workspace_dir=workspace.workspace_dir,
        agent_name="triage-agent",
        phase="triage",
    )
    snapshot = triage.save_source_snapshot(
        "github", {"commit": "abc"}, {"README.md": "triage only"}, "fio"
    )
    workspace_ref = snapshot["files"]["README.md"]
    triage.save_effective_context(
        {"effective_source": "github", "workspace_refs": [workspace_ref]}
    )
    triage.index_context_documents(
        [
            {
                "ref": "benchmark/fio/README.md",
                "namespace": "benchmark/fio",
                "source": "github",
                "authority": "effective",
                "workspace_ref": workspace_ref,
            }
        ]
    )

    benchmark = WorkspaceManager(
        workspace_dir=workspace.workspace_dir,
        agent_name="benchmark-agent",
        phase="benchmark",
    )
    assert benchmark.context_scope_indexed("benchmark/fio") is False
    assert benchmark.read_document("benchmark/fio/README.md")["status"] == "error"
    assert benchmark.search_documents("triage")["results"] == []


def test_jq_query_cdm_dataset(workspace):
    # Simulate a realistic CDM 100-point time-series dataset
    cdm_data = {
        "time_sec": list(range(0, 100)),
        "uperf_100": {
            "metric": "throughput_gbps",
            "values": [round(10.0 + i * 0.1, 2) for i in range(100)],
        },
        "mpstat_server": {
            "metric": "cpu_busy_pct",
            "values": [round(20.0 + (i % 10) * 5.0, 1) for i in range(100)],
        },
    }
    workspace.save_file("cdm_ts.json", json.dumps(cdm_data))

    # Query top-level keys
    res = workspace.jq_query("workspace://cdm_ts.json", "keys")
    assert res["status"] == "ok"
    assert set(res["result"]) == {"time_sec", "uperf_100", "mpstat_server"}

    # Query slice of values
    res = workspace.jq_query("workspace://cdm_ts.json", ".uperf_100.values[0:5]")
    assert res["status"] == "ok"
    assert res["result"] == [10.0, 10.1, 10.2, 10.3, 10.4]

    # Query with truncation limit
    res = workspace.jq_query("workspace://cdm_ts.json", ".time_sec", limit=10)
    assert res["status"] == "ok"
    assert len(res["result"]) == 10
    assert res["truncated"] is True
    assert res["total_items"] == 100


def test_jq_query_bounds_oversized_single_list_item_by_bytes(workspace):
    workspace.save_file("large.json", json.dumps({"items": ["é" * 5000]}))

    res = workspace.jq_query("workspace://large.json", ".items", max_bytes=100)

    assert res["truncated"] is True
    assert res["total_items"] == 1
    assert len(res["result_slice"].encode("utf-8")) <= 100
    assert res["result_format"] == "json"
    assert res["next_offset_bytes"] is not None


def test_jq_query_bounds_oversized_object_by_bytes(workspace):
    workspace.save_file("large.json", json.dumps({"payload": "x" * 5000}))

    res = workspace.jq_query("workspace://large.json", ".", max_bytes=256)

    assert res["truncated"] is True
    assert len(res["result_slice"].encode("utf-8")) <= 256
    assert res["result_format"] == "json"
    assert res["next_offset_bytes"] is not None


def test_jq_query_bounds_multidocument_stream_by_bytes(workspace):
    workspace.save_file("large.json", json.dumps({"items": ["x" * 5000, "y" * 5000]}))

    res = workspace.jq_query("workspace://large.json", ".items[]", max_bytes=100)

    assert res["truncated"] is True
    assert res["total_items"] == 2
    assert len(res["result_slice"].encode("utf-8")) == 100
    assert res["next_offset_bytes"] is not None


def test_jq_query_pages_large_string_result_and_caps_requested_size(workspace):
    text = "éabc" * 7000
    workspace.save_file("large.json", json.dumps({"document": text}))

    first = workspace.jq_query("workspace://large.json", ".document", max_bytes=32768)
    assert first["bytes_returned"] <= 16384
    assert first["total_bytes"] == len(text.encode("utf-8"))
    assert first["next_offset_bytes"] is not None

    pages = [first["result_slice"]]
    offset = first["next_offset_bytes"]
    while offset is not None:
        page = workspace.jq_query(
            "workspace://large.json",
            ".document",
            max_bytes=16384,
            offset_bytes=offset,
        )
        assert page["bytes_returned"] <= 16384
        pages.append(page["result_slice"])
        offset = page["next_offset_bytes"]
    assert "".join(pages) == text


def test_jq_query_pages_large_json_result(workspace):
    items = [{"id": index, "payload": "x" * 80} for index in range(300)]
    workspace.save_file("large.json", json.dumps({"items": items}))
    pages = []
    offset = 0
    while True:
        result = workspace.jq_query(
            "workspace://large.json",
            ".items",
            limit=500,
            max_bytes=512,
            offset_bytes=offset,
        )
        assert result["bytes_returned"] <= 512
        pages.append(result["result_slice"])
        offset = result["next_offset_bytes"]
        if offset is None:
            break

    assert json.loads("".join(pages)) == items


def test_jq_query_rejects_invalid_page_request(workspace):
    workspace.save_file("small.json", '{"ok": true}')
    assert (
        workspace.jq_query("workspace://small.json", ".", offset_bytes=-1)["status"]
        == "error"
    )
    assert (
        workspace.jq_query("workspace://small.json", ".", max_bytes=3)["status"]
        == "error"
    )


def test_grep_file_ethtool_dump(workspace):
    ethtool_content = (
        "NIC statistics for eth0:\n"
        "     rx_packets: 15482910\n"
        "     tx_packets: 18920194\n"
        "     rx_bytes: 10485760000\n"
        "     tx_bytes: 15728640000\n"
        "     rx_dropped: 12\n"
        "     tx_dropped: 0\n"
        "     rx_queue_0_drops: 12\n"
        "     rx_queue_1_drops: 0\n"
    )
    workspace.save_file("ethtool_stats.txt", ethtool_content)

    # Search for dropped packets
    res = workspace.grep_file("workspace://ethtool_stats.txt", r"drop")
    assert res["status"] == "ok"
    assert res["total_matches"] == 4
    matches = [line["content"].strip() for line in res["lines"] if line["is_match"]]
    assert "rx_dropped: 12" in matches
    assert "rx_queue_0_drops: 12" in matches
    assert "rx_queue_1_drops: 0" in matches


def test_read_file_slice(workspace):
    lines = [f"Log entry {i}: status=ok" for i in range(100)]
    workspace.save_file("app.log", "\n".join(lines))

    # Line-based slicing
    res = workspace.read_file_slice("workspace://app.log", start_line=10, max_lines=5)
    assert res["status"] == "ok"
    assert res["start_line"] == 10
    assert res["lines_returned"] == 5
    assert "Log entry 9:" in res["content"]
    assert res["eof"] is False

    # Byte-based slicing
    res_bytes = workspace.read_file_slice(
        "workspace://app.log", offset_bytes=0, max_bytes=50
    )
    assert res_bytes["status"] == "ok"
    assert len(res_bytes["content"]) <= 50


def test_line_read_can_continue_when_byte_limit_splits_selected_lines(workspace):
    content = "".join(f"line {index}: {'x' * 30}\n" for index in range(8))
    workspace.save_file("long-lines.log", content)

    first = workspace.read_file_slice(
        "workspace://long-lines.log", start_line=2, max_lines=4, max_bytes=64
    )
    assert first["status"] == "ok"
    assert first["next_offset_bytes"] is not None

    pages = [first]
    while pages[-1]["next_offset_bytes"] is not None:
        pages.append(
            workspace.read_file_slice(
                "workspace://long-lines.log",
                start_line=2,
                max_lines=4,
                offset_bytes=pages[-1]["next_offset_bytes"],
                max_bytes=64,
            )
        )
    assert all(page["status"] == "ok" for page in pages)
    assert "".join(page["content"] for page in pages) == "".join(
        f"line {index}: {'x' * 30}\n" for index in range(1, 5)
    )
    assert pages[-1]["next_start_line"] == 6


def test_read_document_and_generic_reader_share_offset_paging(workspace):
    text = "workspace context: " + "é" * 4000
    snapshot = workspace.save_source_snapshot(
        "github", {"commit": "abc"}, {"guide.md": text}, "fio"
    )
    workspace.index_context_documents(
        [
            {
                "ref": "benchmark/fio/guide.md",
                "namespace": "benchmark/fio",
                "source": "github",
                "authority": "effective",
                "workspace_ref": snapshot["files"]["guide.md"],
            }
        ]
    )

    first = workspace.read_document("benchmark/fio/guide.md", max_bytes=256)
    assert first["size_bytes"] == len(text.encode("utf-8"))
    assert first["bytes_returned"] <= 256
    assert first["next_offset_bytes"] is not None

    second = workspace.read_file_slice(
        "benchmark/fio/guide.md",
        offset_bytes=first["next_offset_bytes"],
        max_bytes=32768,
    )
    assert first["content"] + second["content"] == text
    assert second["next_offset_bytes"] is None


def test_search_and_context_manifest_return_document_sizes(workspace):
    text = "a matching passage\n"
    snapshot = workspace.save_source_snapshot(
        "github", {"commit": "abc"}, {"guide.md": text}, "fio"
    )
    workspace.index_context_documents(
        [
            {
                "ref": "benchmark/fio/guide.md",
                "namespace": "benchmark/fio",
                "source": "github",
                "authority": "effective",
                "workspace_ref": snapshot["files"]["guide.md"],
            }
        ]
    )
    search = workspace.search_documents("matching")
    assert search["results"][0]["size_bytes"] == len(text.encode("utf-8"))
    manifest = workspace.context_manifest("benchmark/fio")
    assert manifest["documents"][0]["size_bytes"] == len(text.encode("utf-8"))


def test_generate_preview_json_and_text():
    json_payload = json.dumps(
        {
            "time_series": [1, 2, 3],
            "system_info": {"hostname": "node1", "arch": "x86_64"},
        }
    )
    prev = WorkspaceManager.generate_preview("data.json", json_payload)
    assert prev["format"] == "json"
    assert prev["type"] == "object"
    assert "time_series" in prev["keys"]
    assert "system_info" in prev["keys"]

    text_payload = "Line 1\nLine 2\nLine 3\nLine 4\nLine 5"
    prev_txt = WorkspaceManager.generate_preview("output.log", text_payload)
    assert prev_txt["format"] == "log"
    assert prev_txt["type"] == "text"
    assert len(prev_txt["head_preview"]) == 3


def test_agent_base_workspace_manifest(workspace, monkeypatch):
    """Verify that AgentBase workspace_prompt dynamically lists existing files."""
    workspace.save_file("provisioning_summary.json", json.dumps({"ready": True}))
    workspace.save_file("hardware_topology.json", json.dumps({"ccds": [0, 1]}))

    files = workspace.list_files()
    assert len(files) == 2
    refs = {f["file_ref"] for f in files}
    assert "workspace://provisioning_summary.json" in refs
    assert "workspace://hardware_topology.json" in refs
