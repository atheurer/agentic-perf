from __future__ import annotations

import json
from unittest.mock import AsyncMock, MagicMock

import pytest

from providers.workspace.charts import (
    CdmChartAdapter,
    ChartDataset,
    ChartSpec,
    CsvChartAdapter,
    GenericJsonChartAdapter,
    KubeBurnerChartAdapter,
)
from providers.workspace.manager import WorkspaceManager


class TestChartModels:
    def test_chart_spec_to_dict(self):
        ds = ChartDataset(label="Throughput", values=[10.5, 20.2, 30.1], unit="Gbps")
        spec = ChartSpec(
            title="Throughput vs Streams",
            type="bar",
            labels=["1 stream", "2 streams", "4 streams"],
            datasets=[ds],
            x_label="Streams",
            y_label="Gbps",
            unit="Gbps",
        )
        d = spec.to_dict()
        assert d["title"] == "Throughput vs Streams"
        assert d["type"] == "bar"
        assert d["labels"] == ["1 stream", "2 streams", "4 streams"]
        assert len(d["datasets"]) == 1
        assert d["datasets"][0]["label"] == "Throughput"
        assert d["datasets"][0]["values"] == [10.5, 20.2, 30.1]
        assert d["datasets"][0]["unit"] == "Gbps"


class TestGenericJsonChartAdapter:
    def test_flat_records_auto_detect(self):
        adapter = GenericJsonChartAdapter()
        data = [
            {"cpu": "CPU 0", "busy_pct": 45.2},
            {"cpu": "CPU 1", "busy_pct": 88.7},
            {"cpu": "CPU 2", "busy_pct": 12.3},
        ]
        spec = adapter.build_chart(data, title="CPU Busy")
        assert spec.title == "CPU Busy"
        assert spec.labels == ["CPU 0", "CPU 1", "CPU 2"]
        assert len(spec.datasets) == 1
        assert spec.datasets[0].values == [45.2, 88.7, 12.3]

    def test_grouped_records(self):
        adapter = GenericJsonChartAdapter()
        data = [
            {"host": "server", "cpu": "CPU 0", "busy": 90.0},
            {"host": "server", "cpu": "CPU 1", "busy": 85.0},
            {"host": "client", "cpu": "CPU 0", "busy": 40.0},
            {"host": "client", "cpu": "CPU 1", "busy": 35.0},
        ]
        spec = adapter.build_chart(data, group_by="host", x_field="cpu", y_field="busy")
        assert sorted(spec.labels) == ["CPU 0", "CPU 1"]
        assert len(spec.datasets) == 2
        labels = [ds.label for ds in spec.datasets]
        assert "client" in labels
        assert "server" in labels

    def test_direct_dict_mapping(self):
        adapter = GenericJsonChartAdapter()
        data = {"CPU 0": 10.5, "CPU 1": 98.2, "CPU 2": 3.4}
        spec = adapter.build_chart(data, title="CPUs", unit="%")
        assert spec.labels == ["CPU 0", "CPU 1", "CPU 2"]
        assert spec.datasets[0].values == [10.5, 98.2, 3.4]


class TestCsvChartAdapter:
    def test_csv_parsing(self):
        adapter = CsvChartAdapter()
        csv_data = "metric,value\nThroughput,94.2\nLatency,0.15\n"
        assert adapter.can_handle(csv_data)
        spec = adapter.build_chart(
            csv_data, title="Network Metrics", x_field="metric", y_field="value"
        )
        assert spec.labels == ["Throughput", "Latency"]
        assert spec.datasets[0].values == [94.2, 0.15]


class TestCdmChartAdapter:
    def test_cdm_timeseries(self):
        adapter = CdmChartAdapter()
        data = {
            "values": [
                {"time": 1000, "value": 10.0},
                {"time": 2000, "value": 20.0},
                {"time": 3000, "value": 30.0},
            ],
            "usedBreakouts": {"dev": "eth0"},
        }
        assert adapter.can_handle(data, harness="crucible")
        spec = adapter.build_chart(data, metric="uperf::Gbps", unit="Gbps")
        assert spec.type == "line"
        assert len(spec.labels) == 3
        assert spec.datasets[0].values == [10.0, 20.0, 30.0]
        assert "dev=eth0" in spec.datasets[0].label

    def test_cdm_breakout(self):
        adapter = CdmChartAdapter()
        data = {
            "values": [
                {"cpu": "0", "mean": 4.5},
                {"cpu": "1", "mean": 98.2},
            ],
            "usedBreakouts": {},
            "remainingBreakouts": ["cpu"],
        }
        spec = adapter.build_chart(data, metric="mpstat::Busy-CPU", unit="%")
        assert spec.type == "bar"
        assert spec.labels == ["0", "1"]
        assert spec.datasets[0].values == [4.5, 98.2]


class TestKubeBurnerChartAdapter:
    def test_kubeburner_quantiles(self):
        adapter = KubeBurnerChartAdapter()
        data = {
            "quantiles": [
                {"quantileName": "PodReady", "P50": 120.0, "P95": 350.0, "P99": 510.0},
                {
                    "quantileName": "ContainersReady",
                    "P50": 95.0,
                    "P95": 280.0,
                    "P99": 410.0,
                },
            ]
        }
        assert adapter.can_handle(data, harness="kube-burner")
        spec = adapter.build_chart(data, title="Pod Startup Latency", unit="ms")
        assert spec.labels == ["PodReady", "ContainersReady"]
        assert len(spec.datasets) == 3
        assert spec.datasets[0].label == "P50 Latency"
        assert spec.datasets[0].values == [120.0, 95.0]
        assert spec.datasets[2].label == "P99 Latency"
        assert spec.datasets[2].values == [510.0, 410.0]


class TestWorkspaceManagerGenerateChart:
    def test_generate_and_save_chart(self, tmp_path):
        mgr = WorkspaceManager(ticket_id="PERF-CHART1", workspace_dir=tmp_path)
        metric_file = tmp_path / "metrics.json"
        metric_file.write_text(
            json.dumps(
                [
                    {"stream": "1", "gbps": 23.5},
                    {"stream": "2", "gbps": 45.1},
                    {"stream": "4", "gbps": 89.4},
                ]
            )
        )

        res = mgr.generate_chart(
            file_ref="workspace://metrics.json",
            title="Throughput by Stream Count",
            x_field="stream",
            y_field="gbps",
            unit="Gbps",
            output_name="tp_streams",
        )
        assert res["status"] == "ok"
        assert res["chart_ref"] == "workspace://charts/tp_streams.json"
        chart_file = tmp_path / "charts" / "tp_streams.json"
        assert chart_file.exists()
        saved = json.loads(chart_file.read_text())
        assert saved["title"] == "Throughput by Stream Count"
        assert saved["labels"] == ["1", "2", "4"]
        assert saved["datasets"][0]["values"] == [23.5, 45.1, 89.4]

    def test_generate_chart_with_jq_filter(self, tmp_path):
        mgr = WorkspaceManager(ticket_id="PERF-CHART2", workspace_dir=tmp_path)
        big_data = {
            "metadata": {"harness": "fio"},
            "results": [
                {"bs": "4k", "iops": 120000},
                {"bs": "64k", "iops": 45000},
            ],
        }
        data_file = tmp_path / "fio_run.json"
        data_file.write_text(json.dumps(big_data))

        res = mgr.generate_chart(
            file_ref="workspace://fio_run.json",
            title="IOPS by Block Size",
            jq_filter=".results",
            x_field="bs",
            y_field="iops",
            unit="IOPS",
        )
        assert res["status"] == "ok"
        assert res["chart_data"]["labels"] == ["4k", "64k"]
        assert res["chart_data"]["datasets"][0]["values"] == [120000.0, 45000.0]


@pytest.mark.asyncio
async def test_review_agent_handles_chart_ref(tmp_path):
    from agents.review.agent import ReviewAgent

    ticket = {"id": "PERF-REVIEW1", "custom_fields": {}}
    mgr = WorkspaceManager(ticket_id=ticket["id"], workspace_dir=tmp_path)
    chart_spec = {
        "title": "CPU Utilization",
        "type": "bar",
        "labels": ["CPU 0", "CPU 1"],
        "datasets": [{"label": "Busy %", "values": [10.0, 95.0]}],
    }
    mgr.save_file("charts/cpu_chart.json", json.dumps(chart_spec))

    agent = MagicMock(spec=ReviewAgent)
    agent.agent_name = "review-agent"
    agent._update_fields = AsyncMock()
    agent._add_comment = AsyncMock()
    agent._plan_controls_next_transition = AsyncMock(return_value=False)
    agent._transition_ticket = AsyncMock()
    agent._get_submit_result = MagicMock(
        return_value={
            "review_summary": "High CPU skew on core 1",
            "verdict": "hypothesis_confirmed",
            "detailed_analysis": "Core 1 saturated at 95%.",
            "chart_ref": "workspace://charts/cpu_chart.json",
        }
    )

    response = MagicMock()
    response.tool_calls = [
        {
            "name": "submit_review_result",
            "args": {
                "review_summary": "High CPU skew on core 1",
                "verdict": "hypothesis_confirmed",
                "detailed_analysis": "Core 1 saturated at 95%.",
                "chart_ref": "workspace://charts/cpu_chart.json",
            },
        }
    ]

    # Patch WorkspaceManager in _handle_completion to use our tmp_path
    from unittest.mock import patch

    with patch("providers.workspace.manager.WorkspaceManager", return_value=mgr):
        await ReviewAgent._handle_completion(agent, ticket["id"], response)

    agent._update_fields.assert_called_once()
    called_ticket_id, called_fields = agent._update_fields.call_args[0]
    assert called_ticket_id == "PERF-REVIEW1"
    assert called_fields["chart_ref"] == "workspace://charts/cpu_chart.json"
    assert called_fields["chart_data"]["title"] == "CPU Utilization"
    assert called_fields["chart_data"]["datasets"][0]["values"] == [10.0, 95.0]
