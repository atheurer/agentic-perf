from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any

# Execution model constants.
#
# CONTROLLER: a dedicated host runs the benchmark framework;
#   the orchestrator relays commands to it.  Examples: Crucible,
#   benchmark-runner.  Hardware allocation produces a controller
#   host plus target/endpoint hosts.
#
# DIRECT: the orchestrator runs benchmark tools itself (via SSH
#   or local subprocess).  No separate controller host exists.
#   Examples: boot-time, Arcaflow.  Hardware allocation produces
#   target hosts only.
EXECUTION_MODEL_CONTROLLER = "controller"
EXECUTION_MODEL_DIRECT = "direct"

# Harness that handles workflow_source tickets.  Defined here
# so both AgentBase._effective_harness and catalog resolution
# reference the same constant rather than hardcoding the name.
WORKFLOW_HARNESS = "arcaflow-workflows"

# User-facing harness names that map to canonical provider
# registry names.  Defined in the skills layer (not agents)
# because alias resolution is harness metadata.
HARNESS_ALIASES: dict[str, str] = {
    "arcaflow": "arcaflow-plugins",
}


@dataclass
class BenchmarkSuite:
    name: str
    description: str
    supported_params: dict[str, Any] = field(default_factory=dict)
    endpoint_types: list[str] = field(default_factory=list)
    visibility: str = "public"
    roles: list[str] = field(default_factory=list)
    min_hosts: int = 1
    harness: str = ""
    source: dict[str, Any] = field(default_factory=dict)
    architectures: list[str] = field(default_factory=list)
    execution_model: str = EXECUTION_MODEL_CONTROLLER
    # True when the harness needs no host-side installation.
    # The provisioning agent auto-completes for self-installing
    # harnesses on Jumpstarter boards.
    self_installing: bool = False


@dataclass
class RunfileTemplate:
    benchmark: str
    template: dict[str, Any] = field(default_factory=dict)


class SkillProvider(ABC):
    @abstractmethod
    async def list_benchmarks(self) -> list[BenchmarkSuite]: ...

    @abstractmethod
    async def get_benchmark(self, name: str) -> BenchmarkSuite | None: ...

    @abstractmethod
    async def resolve_benchmark(self, requirements: dict[str, Any]) -> str | None: ...

    @abstractmethod
    async def generate_runfile(
        self, benchmark: str, params: dict[str, Any]
    ) -> RunfileTemplate: ...

    async def get_default_config(self) -> dict[str, Any]:
        return {}

    async def get_private_config(self, suite_name: str, key: str) -> Any | None:
        return None

    async def get_runfile_schema(self) -> dict[str, Any] | None:
        return None

    async def get_benchmark_params(self, benchmark: str) -> dict[str, Any] | None:
        return None

    async def get_tool_params(self, tool: str) -> dict[str, Any] | None:
        return None

    async def get_tool_metadata(self, tool: str) -> dict[str, Any] | None:
        return None

    async def list_tools(self) -> list[str]:
        return []

    async def get_example_runfile(
        self, benchmark: str, endpoint_type: str = "remotehosts"
    ) -> dict[str, Any] | None:
        return None

    async def validate_runfile(
        self, run_file: dict[str, Any], harness: str | None = None
    ) -> dict[str, Any]:
        return {"valid": True, "errors": []}

    def get_directive_schema(self) -> dict[str, Any]:
        """Return recognized directives and aliases for this harness.

        Returns a dict with:
          recognized: set of canonical directive key names
          aliases: dict mapping variant names to canonical keys

        The default returns empty sets.  Harness providers
        override this to declare their directive vocabulary.
        """
        return {"recognized": set(), "aliases": {}}
