from __future__ import annotations

import re
from typing import Any

from .base import BenchmarkSuite, RunfileTemplate, SkillProvider

KEYWORD_MAP = {
    "forge": ["forge-rhaiis", "forge-llm-d"],
    "psap": ["forge-rhaiis", "forge-llm-d"],
    "guidellm": ["forge-rhaiis", "forge-llm-d"],
    "inference": ["forge-rhaiis", "forge-llm-d"],
    "llm": ["forge-rhaiis", "forge-llm-d"],
    "language model": ["forge-rhaiis", "forge-llm-d"],
    "vllm": ["forge-rhaiis", "forge-llm-d"],
    "kserve": ["forge-rhaiis", "forge-llm-d"],
    "serving": ["forge-rhaiis", "forge-llm-d"],
    "gpu inference": ["forge-rhaiis", "forge-llm-d"],
    "llama": ["forge-rhaiis", "forge-llm-d"],
    "mistral": ["forge-rhaiis", "forge-llm-d"],
    "granite": ["forge-rhaiis", "forge-llm-d"],
    "qwen": ["forge-rhaiis", "forge-llm-d"],
    "deepseek": ["forge-rhaiis", "forge-llm-d"],
    "gemma": ["forge-rhaiis", "forge-llm-d"],
    "nemotron": ["forge-rhaiis", "forge-llm-d"],
    "phi": ["forge-rhaiis", "forge-llm-d"],
    "gpu": ["forge-rhaiis", "forge-llm-d"],
    "a100": ["forge-rhaiis", "forge-llm-d"],
    "h100": ["forge-rhaiis", "forge-llm-d"],
    "h200": ["forge-rhaiis", "forge-llm-d"],
    "cuda": ["forge-rhaiis", "forge-llm-d"],
    "nvidia": ["forge-rhaiis", "forge-llm-d"],
    "amd": ["forge-rhaiis", "forge-llm-d"],
    "rhaiis": ["forge-rhaiis"],
    "red hat ai inference": ["forge-rhaiis"],
    "llm-d": ["forge-llm-d"],
    "llmd": ["forge-llm-d"],
    "epp": ["forge-llm-d"],
    "distributed inference": ["forge-llm-d"],
}

_COMMON_PARAMS: dict[str, dict[str, Any]] = {
    "model": {
        "type": "string",
        "description": (
            "Model preset key (e.g., 'llama-3-1-8b', 'llama-3-3-70b-fp8', "
            "'qwen3-235b-instruct-fp8'). See skill docs for full catalog."
        ),
    },
    "accelerator": {
        "type": "string",
        "description": "GPU vendor: 'nvidia' or 'amd'",
        "default": "nvidia",
    },
    "rates": {
        "type": "string",
        "description": "Comma-separated request rates in req/s (e.g., '1,50,100,200')",
    },
    "max_seconds": {
        "type": "integer",
        "description": "Benchmark duration per rate point in seconds",
        "default": 450,
    },
    "tensor_parallel": {
        "type": "integer",
        "description": "Tensor parallelism GPU count (overrides model default)",
    },
    "replicas": {
        "type": "integer",
        "description": "Number of serving replicas",
        "default": 1,
    },
}

_RHAIIS_PARAMS: dict[str, dict[str, Any]] = {
    **_COMMON_PARAMS,
    "workload_profile": {
        "type": "string",
        "description": (
            "Workload profile: 'profile1' (balanced 1k/1k tokens), "
            "'profile2' (variable-length generation), "
            "'profile3' (prefill-heavy 2k/128), "
            "'profile4' (long-context 8k/1k)"
        ),
        "default": "profile1",
    },
}

_LLM_D_PARAMS: dict[str, dict[str, Any]] = {
    **_COMMON_PARAMS,
    "scheduler_profile": {
        "type": "string",
        "description": (
            "EPP scheduler profile: 'approximate' (default), 'precise', "
            "or 'approximate-prefix-cache'"
        ),
        "default": "approximate",
    },
    "benchmark_key": {
        "type": "string",
        "description": (
            "Benchmark workload key: 'short' (2min, 256/128 tokens) "
            "or 'concurrent-1k-1k' (10min, 1000/1000 tokens)"
        ),
        "default": "short",
    },
}

_MODEL_FAMILIES = {
    "llama-3-1-8b": {"tp": 1, "family": "Llama 3.1", "size": "8B"},
    "llama-3-3-70b": {"tp": 4, "family": "Llama 3.3", "size": "70B"},
    "llama-3-3-70b-fp8": {"tp": 4, "family": "Llama 3.3", "size": "70B"},
    "llama-3-3-70b-w8a8": {"tp": 4, "family": "Llama 3.3", "size": "70B"},
    "llama-3-3-70b-w4a16": {"tp": 4, "family": "Llama 3.3", "size": "70B"},
    "llama-4-scout": {"tp": 4, "family": "Llama 4 Scout", "size": "17B-16E"},
    "llama-4-scout-fp8": {"tp": 2, "family": "Llama 4 Scout", "size": "17B-16E"},
    "llama-4-maverick": {"tp": 8, "family": "Llama 4 Maverick", "size": "17B-128E"},
    "llama-4-maverick-fp8": {"tp": 8, "family": "Llama 4 Maverick", "size": "17B-128E"},
    "mistral-2503": {"tp": 1, "family": "Mistral Small 3.1", "size": "24B"},
    "mistral-2503-fp8": {"tp": 1, "family": "Mistral Small 3.1", "size": "24B"},
    "granite-3-1-8b-instruct": {"tp": 1, "family": "Granite 3.1", "size": "8B"},
    "granite-3-1-8b-fp8": {"tp": 1, "family": "Granite 3.1", "size": "8B"},
    "qwen25-7b-instruct": {"tp": 1, "family": "Qwen 2.5", "size": "7B"},
    "qwen25-7b-fp8": {"tp": 1, "family": "Qwen 2.5", "size": "7B"},
    "qwen3-0_6b": {"tp": 1, "family": "Qwen 3", "size": "0.6B"},
    "qwen3-235b-instruct": {"tp": 4, "family": "Qwen 3", "size": "235B"},
    "qwen3-235b-instruct-fp8": {"tp": 4, "family": "Qwen 3", "size": "235B"},
    "deepseek-r1-0528": {"tp": 8, "family": "DeepSeek R1", "size": "671B"},
    "deepseek-v3-2": {"tp": 8, "family": "DeepSeek V3", "size": "671B"},
    "gemma-9b": {"tp": 1, "family": "Gemma 2", "size": "9B"},
    "gemma-9b-fp8": {"tp": 1, "family": "Gemma 2", "size": "9B"},
    "nemotron-3-1-70b": {"tp": 2, "family": "Nemotron", "size": "70B"},
    "nemotron-3-1-70b-fp8": {"tp": 1, "family": "Nemotron", "size": "70B"},
    "gpt-oss-120b": {"tp": 1, "family": "GPT-OSS", "size": "120B"},
    "gpt-oss-120b-fp8": {"tp": 1, "family": "GPT-OSS", "size": "120B"},
}

_BENCHMARKS: dict[str, dict[str, Any]] = {
    "forge-rhaiis": {
        "description": (
            "LLM inference benchmark using RHAIIS (Red Hat AI Inference Service). "
            "Deploys vLLM on KServe InferenceService, benchmarks with GuideLLM. "
            "53 model presets (Llama, Mistral, Granite, Qwen, DeepSeek, Gemma, "
            "Nemotron, GPT-OSS) with FP8/W8A8/W4A16 quantization variants. "
            "4 workload profiles (balanced, variable-length, prefill-heavy, "
            "long-context). Requires KServe + GPU operator pre-installed on cluster."
        ),
        "roles": ["client"],
        "min_hosts": 1,
        "params": _RHAIIS_PARAMS,
        "project": "rhaiis",
    },
    "forge-llm-d": {
        "description": (
            "LLM inference benchmark using LLM-D (distributed inference with "
            "EPP scheduler). Deploys LLMInferenceService CRD with intelligent "
            "request routing, prefix caching, and prefill-decode separation. "
            "Installs full stack from scratch (GPU operator, NFD, cert-manager, "
            "RHOAI, KServe, model download). Requires bare OpenShift >= 4.19.9."
        ),
        "roles": ["client"],
        "min_hosts": 1,
        "params": _LLM_D_PARAMS,
        "project": "llm_d",
    },
}

_VALID_PROJECTS = {"rhaiis", "llm_d"}
_VALID_PROFILES = {"profile1", "profile2", "profile3", "profile4"}


class ForgeSkillProvider(SkillProvider):
    async def list_benchmarks(self) -> list[BenchmarkSuite]:
        results = []
        for name, info in _BENCHMARKS.items():
            results.append(
                BenchmarkSuite(
                    name=name,
                    description=info["description"],
                    supported_params=info.get("params", {}),
                    endpoint_types=["kube"],
                    roles=info["roles"],
                    min_hosts=info["min_hosts"],
                    harness="forge",
                )
            )
        return results

    async def get_benchmark(self, name: str) -> BenchmarkSuite | None:
        info = _BENCHMARKS.get(name)
        if info is None:
            return None
        return BenchmarkSuite(
            name=name,
            description=info["description"],
            supported_params=info.get("params", {}),
            endpoint_types=["kube"],
            roles=info["roles"],
            min_hosts=info["min_hosts"],
            harness="forge",
        )

    async def resolve_benchmark(self, requirements: dict[str, Any]) -> str | None:
        description = str(requirements.get("description", "")).lower()
        workload_type = str(requirements.get("workload_type", "")).lower()
        search_text = f"{description} {workload_type}"

        scores: dict[str, int] = {}
        for keyword, benchmarks in KEYWORD_MAP.items():
            if re.search(rf"\b{re.escape(keyword)}\b", search_text):
                for bench in benchmarks:
                    scores[bench] = scores.get(bench, 0) + 1

        scored = {k: v for k, v in scores.items() if k in _BENCHMARKS}
        if not scored:
            return None
        return max(scored, key=scored.get)

    async def generate_runfile(
        self, benchmark: str, params: dict[str, Any]
    ) -> RunfileTemplate:
        info = _BENCHMARKS.get(benchmark)
        if info is None:
            return RunfileTemplate(benchmark=benchmark, template={})

        project = info["project"]
        presets: list[str] = []
        cli_args: list[str] = []

        model = params.get("model")
        if model:
            presets.append(model)

        if project == "rhaiis":
            profile = params.get("workload_profile", "profile1")
            if profile:
                presets.append(profile)

        accelerator = params.get("accelerator", "nvidia")
        if accelerator:
            presets.append(accelerator)

        if project == "llm_d":
            scheduler = params.get("scheduler_profile")
            if scheduler:
                presets.append(scheduler)
            bench_key = params.get("benchmark_key")
            if bench_key:
                presets.append(bench_key)

        rates = params.get("rates")
        if rates:
            cli_args.extend(["--rates", str(rates)])

        max_seconds = params.get("max_seconds")
        if max_seconds:
            cli_args.extend(["--max-seconds", str(max_seconds)])

        tp = params.get("tensor_parallel")
        if tp:
            cli_args.extend(["--tensor-parallel", str(tp)])

        replicas = params.get("replicas")
        if replicas and replicas != 1:
            cli_args.extend(["--replicas", str(replicas)])

        config_overrides = params.get("config_overrides", {})

        template: dict[str, Any] = {
            "harness": "forge",
            "project": project,
            "presets": presets,
            "cli_args": cli_args,
        }
        if config_overrides:
            template["config_overrides"] = config_overrides

        return RunfileTemplate(benchmark=benchmark, template=template)

    async def get_default_config(self) -> dict[str, Any]:
        return {
            "provisioning": {
                "install_method": "git_clone",
                "controller_only_install": True,
                "git_url": "https://github.com/openshift-psap/forge.git",
                "install_target_path": "/opt/forge",
                "post_install_commands": [
                    "cd /opt/forge && pip install -e . 2>&1",
                ],
                "verify_command": ("cd /opt/forge && python -m projects.core --help"),
                "on_existing_install": "skip",
                "pre_install_commands": [
                    "dnf install -y python3.12 python3.12-pip git 2>/dev/null; true",
                ],
            },
            "execution": {
                "controller_required": True,
                "run_command": "cd /opt/forge && ./bin/run_cli",
                "endpoint_type": "kube",
                "endpoint_user": "root",
                "run_file_format": "forge_project",
            },
        }

    async def get_benchmark_params(self, benchmark: str) -> dict[str, Any] | None:
        info = _BENCHMARKS.get(benchmark)
        if info is None:
            return None
        return info.get("params", {})

    async def get_runfile_schema(self) -> dict[str, Any] | None:
        return {
            "type": "object",
            "description": "Forge run-file: project + presets + CLI args",
            "properties": {
                "harness": {"type": "string", "const": "forge"},
                "project": {
                    "type": "string",
                    "enum": sorted(_VALID_PROJECTS),
                    "description": "Forge project to run",
                },
                "presets": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": (
                        "Preset names to stack (model, workload profile, "
                        "accelerator). Applied left to right."
                    ),
                },
                "cli_args": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Additional CLI arguments for the test phase",
                },
                "config_overrides": {
                    "type": "object",
                    "description": "Config key=value overrides (dot-separated paths)",
                },
            },
            "required": ["harness", "project", "presets"],
        }

    async def get_example_runfile(
        self, benchmark: str, endpoint_type: str = "remotehosts"
    ) -> dict[str, Any] | None:
        info = _BENCHMARKS.get(benchmark)
        if info is None:
            return None

        if benchmark == "forge-rhaiis":
            return {
                "harness": "forge",
                "project": "rhaiis",
                "presets": ["llama-3-1-8b", "profile1", "nvidia"],
                "cli_args": ["--max-seconds", "300", "--rates", "1,50,100"],
            }
        elif benchmark == "forge-llm-d":
            return {
                "harness": "forge",
                "project": "llm_d",
                "presets": ["smoke", "nvidia"],
                "cli_args": [],
            }

        result = await self.generate_runfile(benchmark, {})
        return result.template

    async def validate_runfile(
        self, run_file: dict[str, Any], harness: str | None = None
    ) -> dict[str, Any]:
        errors: list[str] = []

        project = run_file.get("project")
        if not project:
            errors.append("Missing 'project' field")
        elif project not in _VALID_PROJECTS:
            errors.append(
                f"Invalid project '{project}': must be one of {sorted(_VALID_PROJECTS)}"
            )

        presets = run_file.get("presets")
        if not isinstance(presets, list) or not presets:
            errors.append("Missing or empty 'presets' list")

        cli_args = run_file.get("cli_args", [])
        if not isinstance(cli_args, list):
            errors.append("'cli_args' must be a list")

        config_overrides = run_file.get("config_overrides")
        if config_overrides is not None and not isinstance(config_overrides, dict):
            errors.append("'config_overrides' must be a dict")

        return {"valid": len(errors) == 0, "errors": errors}
