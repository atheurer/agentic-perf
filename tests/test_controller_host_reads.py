"""#1422: controller document reads work from the installed tree over SSH,
with MCP as an additive channel rather than a requirement."""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

import agents.server_utils as server_utils
from agents.server_utils import controller_context_gateway

# macOS realpath lacks -e; the remote contract is coreutils realpath, so
# local functional tests run the generated scripts against a shim that
# implements the -e semantics.
_REALPATH_SHIM = """#!/bin/bash
args=()
for a in "$@"; do
  case "$a" in
    -e|-q|--) ;;
    *) args+=("$a") ;;
  esac
done
for p in "${args[@]}"; do
  if [ -e "$p" ]; then
    if [ -d "$p" ]; then
      (cd "$p" && pwd -P) || exit 1
    else
      dn=$(cd "$(dirname "$p")" && pwd -P) || exit 1
      printf '%s/%s\\n' "$dn" "$(basename "$p")"
    fi
  else
    echo "$p: No such file or directory" >&2
    exit 1
  fi
done
"""


class ShimSSH:
    """Executes the generated controller scripts against a local tree."""

    def __init__(self, root: Path, tmp_path: Path) -> None:
        self.root = root
        shim_dir = tmp_path / "shim"
        shim_dir.mkdir(exist_ok=True)
        shim = shim_dir / "realpath"
        shim.write_text(_REALPATH_SHIM)
        shim.chmod(0o755)
        self.env = dict(os.environ, PATH=f"{shim_dir}:{os.environ['PATH']}")
        self.commands: list[str] = []

    async def run(self, host, command, timeout=30, **kwargs):
        self.commands.append(command)
        result = subprocess.run(
            ["bash", "-c", command],
            capture_output=True,
            text=True,
            env=self.env,
        )
        return SimpleNamespace(
            stdout=result.stdout,
            stderr=result.stderr,
            exit_code=result.returncode,
        )


@pytest.fixture
def controller_tree(tmp_path):
    root = tmp_path / "opt" / "crucible"
    root.mkdir(parents=True)
    (root / "AGENTS.md").write_text("# controller guide\n" + "x" * 100)
    (root / "docs").mkdir()
    (root / "docs" / "run.md").write_text("# run\n")
    (root / "secrets").mkdir()
    (root / "secrets" / "token.txt").write_text("secret")
    (root / ".git").mkdir()
    (root / ".git" / "config").write_text("[core]")
    (root / "escape").symlink_to("/etc")
    return root


def _ssh(controller_tree, tmp_path):
    return ShimSSH(controller_tree, tmp_path)


@pytest.mark.asyncio
async def test_read_with_mcp_disabled(controller_tree, tmp_path):
    """The incident: AGENTS.md exists on the controller but is not among
    the MCP resources — the read must still work."""
    result = json.loads(
        await controller_context_gateway(
            ssh=_ssh(controller_tree, tmp_path),
            controller_host="controller",
            mcp_adapter=None,
            install_root=str(controller_tree),
            ticket_id="PERF-1422B",
            agent_name="benchmark",
            phase="benchmark",
            operation="read",
            path="AGENTS.md",
        )
    )
    assert result["found"] is True
    assert "controller guide" in result["document"]["content"]


@pytest.mark.asyncio
async def test_mcp_failure_falls_back_to_host(controller_tree, tmp_path):
    class FailingAdapter:
        async def read_resource(self, uri):
            raise RuntimeError("resource not exposed")

        async def list_resources(self):
            return []

    result = json.loads(
        await controller_context_gateway(
            ssh=_ssh(controller_tree, tmp_path),
            controller_host="controller",
            mcp_adapter=FailingAdapter(),
            install_root=str(controller_tree),
            ticket_id="PERF-1422B",
            agent_name="benchmark",
            phase="benchmark",
            operation="read",
            path="AGENTS.md",
        )
    )
    assert result["found"] is True
    assert result.get("reason") != "controller_transport_error"


@pytest.mark.asyncio
async def test_path_outside_root_is_rejected(controller_tree, tmp_path):
    """`..` pointers are rejected by the client-side path validator before
    any controller contact; the symlink-escape test covers confinement of
    otherwise-valid pointers."""
    result = json.loads(
        await controller_context_gateway(
            ssh=_ssh(controller_tree, tmp_path),
            controller_host="controller",
            mcp_adapter=None,
            install_root=str(controller_tree),
            ticket_id="PERF-1422A",
            agent_name="benchmark",
            phase="benchmark",
            operation="read",
            path="../etc/passwd",
        )
    )
    assert result["found"] is False
    assert result["reason"] == "invalid_controller_path"
    assert not any("/etc/passwd" in c for c in _ssh(controller_tree, tmp_path).commands)


@pytest.mark.asyncio
async def test_ssh_reader_excludes_secret_paths(controller_tree, tmp_path):
    """The shell-level exclusion guard (exit 27) fires for paths the
    client-side validator cannot see (e.g. .git internals of a linked
    tree)."""
    ssh = _ssh(controller_tree, tmp_path)
    result = await server_utils._read_controller_document_ssh(
        ssh, "controller", str(controller_tree), ".git/config"
    )
    assert result.exit_code == 27


@pytest.mark.asyncio
async def test_symlink_escape_is_confined(controller_tree, tmp_path):
    result = json.loads(
        await controller_context_gateway(
            ssh=_ssh(controller_tree, tmp_path),
            controller_host="controller",
            mcp_adapter=None,
            install_root=str(controller_tree),
            ticket_id="PERF-1422B",
            agent_name="benchmark",
            phase="benchmark",
            operation="read",
            path="escape/passwd",
        )
    )
    assert result["found"] is False
    assert result["reason"] == "controller_path_outside_root"


@pytest.mark.asyncio
async def test_missing_file_is_not_found(controller_tree, tmp_path):
    result = json.loads(
        await controller_context_gateway(
            ssh=_ssh(controller_tree, tmp_path),
            controller_host="controller",
            mcp_adapter=None,
            install_root=str(controller_tree),
            ticket_id="PERF-1422B",
            agent_name="benchmark",
            phase="benchmark",
            operation="read",
            path="nope.md",
        )
    )
    assert result["found"] is False
    assert result["reason"] == "controller_document_not_found"


@pytest.mark.asyncio
async def test_directory_read_reports_is_directory(controller_tree, tmp_path):
    result = json.loads(
        await controller_context_gateway(
            ssh=_ssh(controller_tree, tmp_path),
            controller_host="controller",
            mcp_adapter=None,
            install_root=str(controller_tree),
            ticket_id="PERF-1422B",
            agent_name="benchmark",
            phase="benchmark",
            operation="read",
            path="docs",
        )
    )
    assert result["found"] is False
    assert result["reason"] == "controller_path_is_directory"


@pytest.mark.asyncio
async def test_list_over_ssh(controller_tree, tmp_path):
    result = json.loads(
        await controller_context_gateway(
            ssh=_ssh(controller_tree, tmp_path),
            controller_host="controller",
            mcp_adapter=None,
            install_root=str(controller_tree),
            ticket_id="PERF-1422B",
            agent_name="benchmark",
            phase="benchmark",
            operation="list",
            path="",
        )
    )
    assert result["found"] is True
    paths = {entry["path"] for entry in result["results"]}
    assert "AGENTS.md" in paths
    assert "docs" in paths
    # Excluded paths never appear in listings.
    assert "secrets" not in paths


@pytest.mark.asyncio
async def test_metadata_selectors_still_require_mcp(controller_tree, tmp_path):
    """endpoints/tools/benchmarks are structured MCP metadata, not files."""
    result = json.loads(
        await controller_context_gateway(
            ssh=_ssh(controller_tree, tmp_path),
            controller_host="controller",
            mcp_adapter=None,
            install_root=str(controller_tree),
            ticket_id="PERF-1422B",
            agent_name="benchmark",
            phase="benchmark",
            operation="read",
            path="endpoints",
        )
    )
    assert result["found"] is False
    assert result["reason"] == "controller_metadata_unavailable"


@pytest.mark.asyncio
async def test_install_root_resolution_is_provider_owned():
    """Non-crucible harnesses get a controller source only through their
    provider-configured install root — never by guessing /opt/<name>."""
    from agents.skill_gateway import _controller_install_root

    class Provider:
        async def get_all_private_config(self, harness):
            if harness == "kube-burner":
                return {"provisioning": {"install_target_path": "/opt/kb"}}
            return {}

    assert (
        await _controller_install_root(Provider(), "harness/kube-burner") == "/opt/kb"
    )
    # No configured root: no controller source.
    assert await _controller_install_root(Provider(), "harness/k8s-netperf") == ""
    # Crucible resolves through the controller contract (CRUCIBLE_HOME).
    assert (
        await _controller_install_root(Provider(), "harness/crucible")
        == "/opt/crucible"
    )
    # Non-harness subjects never resolve a root.
    assert await _controller_install_root(Provider(), "platform/kernel") == ""


@pytest.mark.asyncio
async def test_no_adapter_and_no_root_is_unsupported(tmp_path):
    result = json.loads(
        await controller_context_gateway(
            ssh=None,
            controller_host="controller",
            mcp_adapter=None,
            ticket_id="PERF-1422Z",
            agent_name="benchmark",
            phase="benchmark",
            operation="read",
            path="AGENTS.md",
        )
    )
    assert result["found"] is False
    assert result["reason"] == "unsupported_controller"


@pytest.mark.asyncio
async def test_security_verdict_is_final_over_mcp(controller_tree, tmp_path):
    """A host confinement or exclusion verdict must not be overridden by an
    MCP resource exposed under an alias — MCP is only a fallback when the
    host read itself could not classify the path."""
    ssh = _ssh(controller_tree, tmp_path)

    class ServingMCP:
        async def read_resource(self, uri):
            return json.dumps({"contents": [{"text": "leaked"}]})

    result = json.loads(
        await controller_context_gateway(
            ssh=ssh,
            controller_host="controller",
            mcp_adapter=ServingMCP(),
            install_root=str(controller_tree),
            ticket_id="PERF-1422K",
            agent_name="benchmark",
            phase="benchmark",
            operation="read",
            path="escape/passwd",
        )
    )
    assert result["found"] is False
    assert result["reason"] == "controller_path_outside_root"


@pytest.mark.asyncio
async def test_transport_failure_falls_back_to_mcp(controller_tree, tmp_path):
    """When the host read cannot classify the path (transport failure), MCP
    still serves the read."""
    ssh = _ssh(controller_tree, tmp_path)

    async def fail_all(self, host, command, timeout=30, **kwargs):
        return SimpleNamespace(stdout="", stderr="connection refused", exit_code=255)

    ssh.run = fail_all  # type: ignore[method-assign]

    class ServingMCP:
        async def read_resource(self, uri):
            return json.dumps({"contents": [{"text": "# from mcp\n" + "y" * 20}]})

    result = json.loads(
        await controller_context_gateway(
            ssh=ssh,
            controller_host="controller",
            mcp_adapter=ServingMCP(),
            install_root=str(controller_tree),
            ticket_id="PERF-1422L",
            agent_name="benchmark",
            phase="benchmark",
            operation="read",
            path="AGENTS.md",
        )
    )
    assert result["found"] is True


@pytest.mark.asyncio
async def test_secrets_descendants_via_symlink_alias_are_excluded(
    controller_tree, tmp_path
):
    """A benign-looking in-root symlink pointing at an excluded directory
    must not expose the directory's contents: exclusions apply to
    descendants, not just the directory itself."""
    (controller_tree / "docs" / "private").symlink_to("../secrets")
    (controller_tree / "secrets" / "notes.txt").write_text("secret notes")

    result = json.loads(
        await controller_context_gateway(
            ssh=_ssh(controller_tree, tmp_path),
            controller_host="controller",
            mcp_adapter=None,
            install_root=str(controller_tree),
            ticket_id="PERF-1422M",
            agent_name="benchmark",
            phase="benchmark",
            operation="read",
            path="docs/private/notes.txt",
        )
    )
    assert result["found"] is False
    assert result["reason"] == "controller_path_excluded"


@pytest.mark.asyncio
async def test_skill_gateway_serves_crucible_documents_from_host(
    controller_tree, tmp_path, monkeypatch
):
    """Integration: the model-facing skill_context_gateway path resolves
    crucible's controller root (CRUCIBLE_HOME) and serves installed
    documents with no MCP adapter at all."""
    monkeypatch.setenv("CRUCIBLE_HOME", str(controller_tree))

    from providers.skills.gateway import OrganizationSkillResolver

    class Provider:
        organization_resolver = OrganizationSkillResolver.from_instance_config({})

    from agents.skill_gateway import skill_context_gateway

    result = json.loads(
        await skill_context_gateway(
            Provider(),
            ssh=_ssh(controller_tree, tmp_path),
            controller_host="controller",
            ticket_id="PERF-1422N",
            agent_name="benchmark",
            phase="benchmark",
            subject="harness/crucible",
            operation="read",
            ref="skill://software/controller/harness/crucible/AGENTS.md",
        )
    )
    assert result["found"] is True
    content = json.dumps(result)
    assert "controller guide" in content
