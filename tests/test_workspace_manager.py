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
