"""Shared utilities for agent MCP servers.

All agent MCP servers run as subprocesses and need to set up the Python
path, construct providers, and resolve SSH credentials from tickets.
This module centralizes that setup to avoid duplication.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import Any


def setup_project_path() -> str:
    """Add the project root to sys.path. Returns the project root path."""
    root = str(Path(__file__).resolve().parents[1])
    if root not in sys.path:
        sys.path.insert(0, root)
    return root


def build_skill_provider():
    """Construct a MultiHarnessSkillProvider from environment variables.

    Reads CRUCIBLE_HOME and ZATHRAS_HOME from env vars.
    """
    from providers.skills.benchmark_runner import BenchmarkRunnerSkillProvider
    from providers.skills.clusterbuster import ClusterbusterSkillProvider
    from providers.skills.crucible import CrucibleSkillProvider
    from providers.skills.forge import ForgeSkillProvider
    from providers.skills.ioscale import IoscaleSkillProvider
    from providers.skills.k8s_netperf import K8sNetperfSkillProvider
    from providers.skills.kube_burner import KubeBurnerSkillProvider
    from providers.skills.multi import MultiHarnessSkillProvider
    from providers.skills.private import PrivateSkillProvider
    from providers.skills.vstorm import VstormSkillProvider
    from providers.skills.zathras import ZathrasSkillProvider

    crucible_home = os.environ.get("CRUCIBLE_HOME", "/opt/crucible")
    zathras_home = os.environ.get("ZATHRAS_HOME", "")

    harnesses: dict[str, Any] = {
        "crucible": CrucibleSkillProvider(crucible_home),
        "kube-burner": KubeBurnerSkillProvider(),
        "k8s-netperf": K8sNetperfSkillProvider(),
        "benchmark-runner": BenchmarkRunnerSkillProvider(),
        "clusterbuster": ClusterbusterSkillProvider(),
        "vstorm": VstormSkillProvider(),
        "ioscale": IoscaleSkillProvider(),
        "forge": ForgeSkillProvider(),
    }

    if zathras_home:
        harnesses["zathras"] = ZathrasSkillProvider(zathras_home)
    else:
        private = PrivateSkillProvider()
        zathras_tests = private._load_config("zathras").get("tests")
        if zathras_tests:
            harnesses["zathras"] = ZathrasSkillProvider(fallback_tests=zathras_tests)

    return MultiHarnessSkillProvider(
        harnesses, PrivateSkillProvider(), default_harness="crucible"
    )


def build_secrets_provider():
    """Construct a SecretsProvider from environment variables."""
    from providers.secrets.factory import create_secrets_provider

    backend = os.environ.get("SECRETS_BACKEND", "local")
    config: dict[str, Any] = {}
    secrets_path = os.environ.get("SECRETS_PATH")
    if secrets_path:
        config["path"] = secrets_path
    return create_secrets_provider(backend, **config)


def build_repo_cache():
    """Construct a RepoCache with harness repos from environment variables."""
    import json
    import logging
    from providers.skills.repo_cache import RepoCache

    logger = logging.getLogger(__name__)
    cache = RepoCache()

    default_repos = {
        "crucible": "https://github.com/perftool-incubator/crucible.git",
        "crucible-examples": "https://github.com/perftool-incubator/crucible-examples.git",
        "zathras": "https://github.com/redhat-performance/zathras.git",
        "kube-burner": "https://github.com/kube-burner/kube-burner.git",
        "k8s-netperf": "https://github.com/cloud-bulldozer/k8s-netperf.git",
        "benchmark-runner": "https://github.com/redhat-performance/benchmark-runner.git",
        "clusterbuster": "https://github.com/redhat-performance/clusterbuster.git",
        "vstorm": "https://github.com/gqlo/vstorm.git",
        "ioscale": "https://github.com/ekuric/ioscale.git",
        "forge": "https://github.com/openshift-psap/forge.git",
    }

    env_repos = os.environ.get("HARNESS_REPOS")
    if env_repos:
        try:
            default_repos.update(json.loads(env_repos))
        except json.JSONDecodeError:
            pass

    for name, url in default_repos.items():
        try:
            cache.ensure_repo(name, url)
        except Exception:
            logger.debug("Failed to cache repo %s", name)

    return cache


async def build_ssh_from_ticket(
    ticket_id: str | None = None,
    state_store_url: str | None = None,
) -> tuple[Any, dict[str, Any]]:
    """Fetch a ticket and create an SSHExecutor from its custom_fields.

    Returns (SSHExecutor, ticket_dict). If ticket_id is None, reads from
    TICKET_ID env var. If state_store_url is None, reads from STATE_STORE_URL.
    """
    import httpx
    from providers.ssh import SSHExecutor

    ticket_id = ticket_id or os.environ.get("TICKET_ID", "")
    state_store_url = (
        state_store_url or os.environ.get("STATE_STORE_URL", "http://localhost:8090")
    )

    if not ticket_id:
        return SSHExecutor(user="root"), {}

    async with httpx.AsyncClient(timeout=15.0) as client:
        r = await client.get(f"{state_store_url}/api/v1/tickets/{ticket_id}")
        r.raise_for_status()
        ticket = r.json()

    fields = ticket.get("custom_fields", {})
    ssh_key = fields.get("ssh_key_path")
    # Always use root — provisioning bootstraps root SSH access.
    # The ticket's ssh_user is the initial cloud login user (e.g., ec2-user),
    # not the runtime user for harness operations.
    ssh_user = "root"

    return SSHExecutor(user=ssh_user, key_path=ssh_key), ticket
