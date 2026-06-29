"""Tests for provider/endpoint-scoped prompt fragment loading."""

from __future__ import annotations

from agents.base import AgentBase


class TestLoadPromptFragments:
    """Tests for AgentBase._load_prompt_fragments."""

    def test_returns_empty_when_no_prompts_dir(self, tmp_path):
        result = AgentBase._load_prompt_fragments(tmp_path, resource_provider="aws")
        assert result == ""

    def test_loads_provider_fragment(self, tmp_path):
        prompts_dir = tmp_path / "prompts"
        prompts_dir.mkdir()
        (prompts_dir / "aws.md").write_text("AWS specific content")

        result = AgentBase._load_prompt_fragments(tmp_path, resource_provider="aws")
        assert "AWS specific content" in result

    def test_loads_endpoint_fragment(self, tmp_path):
        prompts_dir = tmp_path / "prompts"
        prompts_dir.mkdir()
        (prompts_dir / "kube.md").write_text("Kube endpoint content")

        result = AgentBase._load_prompt_fragments(tmp_path, endpoint_type="kube")
        assert "Kube endpoint content" in result

    def test_loads_both_provider_and_endpoint(self, tmp_path):
        prompts_dir = tmp_path / "prompts"
        prompts_dir.mkdir()
        (prompts_dir / "aws.md").write_text("AWS content")
        (prompts_dir / "remotehosts.md").write_text("Remotehosts content")

        result = AgentBase._load_prompt_fragments(
            tmp_path,
            resource_provider="aws",
            endpoint_type="remotehosts",
        )
        assert "AWS content" in result
        assert "Remotehosts content" in result

    def test_no_fragment_for_unknown_provider(self, tmp_path):
        prompts_dir = tmp_path / "prompts"
        prompts_dir.mkdir()
        (prompts_dir / "aws.md").write_text("AWS content")

        result = AgentBase._load_prompt_fragments(tmp_path, resource_provider="quads")
        assert result == ""

    def test_no_fragment_when_no_provider_or_endpoint(self, tmp_path):
        prompts_dir = tmp_path / "prompts"
        prompts_dir.mkdir()
        (prompts_dir / "aws.md").write_text("AWS content")

        result = AgentBase._load_prompt_fragments(tmp_path)
        assert result == ""

    def test_excludes_other_provider_fragments(self, tmp_path):
        prompts_dir = tmp_path / "prompts"
        prompts_dir.mkdir()
        (prompts_dir / "aws.md").write_text("AWS content")
        (prompts_dir / "quads.md").write_text("QUADS content")

        result = AgentBase._load_prompt_fragments(tmp_path, resource_provider="aws")
        assert "AWS content" in result
        assert "QUADS content" not in result

    def test_auto_select_loaded_when_no_provider(self, tmp_path):
        prompts_dir = tmp_path / "prompts"
        prompts_dir.mkdir()
        (prompts_dir / "auto_select.md").write_text("Auto select content")
        (prompts_dir / "aws.md").write_text("AWS content")

        result = AgentBase._load_prompt_fragments(tmp_path)
        assert "Auto select content" in result
        assert "AWS content" not in result

    def test_auto_select_skipped_when_provider_set(self, tmp_path):
        prompts_dir = tmp_path / "prompts"
        prompts_dir.mkdir()
        (prompts_dir / "auto_select.md").write_text("Auto select content")
        (prompts_dir / "aws.md").write_text("AWS content")

        result = AgentBase._load_prompt_fragments(tmp_path, resource_provider="aws")
        assert "AWS content" in result
        assert "Auto select content" not in result


class TestResourceAgentPromptFragments:
    """Test that the resource agent loads real fragments."""

    def test_aws_fragment_loaded(self):
        from agents.resource.agent import ResourceAgent

        ticket = {
            "id": "PERF-TEST",
            "summary": "test",
            "description": "test",
            "custom_fields": {
                "directives": {"resource_provider": "aws"},
            },
            "comments": [],
        }
        agent = ResourceAgent.__new__(ResourceAgent)
        prompt = agent._system_prompt(ticket)
        assert "Cloud Provider" in prompt
        assert "GPU Cluster" not in prompt

    def test_quads_fragment_loaded(self):
        from agents.resource.agent import ResourceAgent

        ticket = {
            "id": "PERF-TEST",
            "summary": "test",
            "description": "test",
            "custom_fields": {
                "directives": {"resource_provider": "quads"},
            },
            "comments": [],
        }
        agent = ResourceAgent.__new__(ResourceAgent)
        prompt = agent._system_prompt(ticket)
        assert "QUADS" in prompt
        assert "Cloud Provider" not in prompt

    def test_psap_fragment_loaded(self):
        from agents.resource.agent import ResourceAgent

        ticket = {
            "id": "PERF-TEST",
            "summary": "test",
            "description": "test",
            "custom_fields": {
                "directives": {"resource_provider": "psap-cc"},
            },
            "comments": [],
        }
        agent = ResourceAgent.__new__(ResourceAgent)
        prompt = agent._system_prompt(ticket)
        assert "GPU Cluster" in prompt

    def test_kube_endpoint_loaded(self):
        from agents.resource.agent import ResourceAgent

        ticket = {
            "id": "PERF-TEST",
            "summary": "test",
            "description": "test",
            "custom_fields": {
                "directives": {
                    "resource_provider": "aws",
                    "endpoint_type": "kube",
                },
            },
            "comments": [],
        }
        agent = ResourceAgent.__new__(ResourceAgent)
        prompt = agent._system_prompt(ticket)
        assert "Kube Endpoints" in prompt
        assert "Remotehosts" not in prompt

    def test_no_provider_loads_auto_select(self):
        from agents.resource.agent import ResourceAgent

        ticket = {
            "id": "PERF-TEST",
            "summary": "test",
            "description": "test",
            "custom_fields": {},
            "comments": [],
        }
        agent = ResourceAgent.__new__(ResourceAgent)
        prompt = agent._system_prompt(ticket)
        assert "Resource Agent" in prompt
        assert "Auto-Select" in prompt
        assert "Cloud Provider" not in prompt
        assert "QUADS" not in prompt


class TestProvisioningAgentPromptFragments:
    """Test that the provisioning agent loads real fragments."""

    def test_cloud_fragment_loaded_for_aws(self):
        from agents.provisioning.agent import ProvisioningAgent

        ticket = {
            "id": "PERF-TEST",
            "summary": "test",
            "description": "test",
            "custom_fields": {
                "directives": {"resource_provider": "aws"},
            },
            "comments": [],
        }
        agent = ProvisioningAgent.__new__(ProvisioningAgent)
        prompt = agent._system_prompt(ticket)
        assert "Bootstrap Root SSH" in prompt

    def test_no_cloud_for_quads(self):
        from agents.provisioning.agent import ProvisioningAgent

        ticket = {
            "id": "PERF-TEST",
            "summary": "test",
            "description": "test",
            "custom_fields": {
                "directives": {"resource_provider": "quads"},
            },
            "comments": [],
        }
        agent = ProvisioningAgent.__new__(ProvisioningAgent)
        prompt = agent._system_prompt(ticket)
        assert "Bootstrap Root SSH" not in prompt

    def test_kube_fragment_loaded(self):
        from agents.provisioning.agent import ProvisioningAgent

        ticket = {
            "id": "PERF-TEST",
            "summary": "test",
            "description": "test",
            "custom_fields": {
                "directives": {"endpoint_type": "kube"},
            },
            "comments": [],
        }
        agent = ProvisioningAgent.__new__(ProvisioningAgent)
        prompt = agent._system_prompt(ticket)
        assert "Kubernetes" in prompt


class TestBenchmarkAgentPromptFragments:
    """Test that the benchmark agent loads real fragments."""

    def test_cloud_fragment_loaded_for_aws(self):
        from agents.benchmark.agent import BenchmarkAgent

        ticket = {
            "id": "PERF-TEST",
            "summary": "test",
            "description": "test",
            "custom_fields": {
                "directives": {"resource_provider": "aws"},
            },
            "comments": [],
        }
        agent = BenchmarkAgent.__new__(BenchmarkAgent)
        agent._repo_cache = None
        prompt = agent._system_prompt(ticket)
        assert "Cloud Provider IP" in prompt

    def test_kube_fragment_loaded(self):
        from agents.benchmark.agent import BenchmarkAgent

        ticket = {
            "id": "PERF-TEST",
            "summary": "test",
            "description": "test",
            "custom_fields": {
                "directives": {"endpoint_type": "kube"},
            },
            "comments": [],
        }
        agent = BenchmarkAgent.__new__(BenchmarkAgent)
        agent._repo_cache = None
        prompt = agent._system_prompt(ticket)
        assert "Kube Endpoints" in prompt
