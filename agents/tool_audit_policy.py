"""Reviewed audit policy for every agent-visible tool entry point.

The source-level CI check in ``tests/test_tool_audit_policy.py`` compares this
manifest with the AST-discovered MCP and native registrations.  Keep the
entries explicit: a new action is intentionally a review event, not an
implicit consequence of the server it happens to live in.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

ToolClassification = Literal["read_only", "side_effecting"]


@dataclass(frozen=True)
class FixtureExemption:
    """A reviewed reason not to execute a real production handler in CI."""

    owner: str
    expires_on: str
    reason: str


@dataclass(frozen=True)
class ToolAuditPolicy:
    """Classification and test contract for one externally callable tool."""

    registration: str
    classification: ToolClassification
    fixture_exemption: FixtureExemption
    operation_owner: str | None = None


@dataclass(frozen=True)
class AuditBypass:
    """A temporary reviewed exception to a canonical registration boundary."""

    path: str
    symbol: str
    owner: str
    scope: str
    reason: str
    expires_on: str


def _fixture_exemption(
    registration: str, *, native: bool, chat: bool
) -> FixtureExemption:
    """Make remote-test exemptions explicit, single-registration review items.

    The policy must never grant a server- or surface-wide fixture exception:
    adding a new tool creates a distinct expiring declaration which the CI test
    proves is consumed by the canonical boundary test.
    """
    if chat:
        owner = "chat-maintainers"
        boundary = "ChatToolAudit.invoke"
    elif native:
        owner = "observability-maintainers"
        boundary = "AgentBase._execute_tool"
    else:
        owner = "observability-maintainers"
        boundary = "MCPAuditMiddleware.on_call_tool"
    return FixtureExemption(
        owner=owner,
        expires_on="2027-12-31",
        reason=(
            f"{registration} may require provider credentials, ticket state, or a "
            f"remote host. CI intercepts its actual {boundary} entry/terminal "
            "boundary with a schema-valid harmless fixture; this exception avoids "
            "only the remote effect and expires with this exact registration."
        ),
    )


def _read_only(
    *registrations: str, native: bool = False, chat: bool = False
) -> tuple[ToolAuditPolicy, ...]:
    return tuple(
        ToolAuditPolicy(
            registration=registration,
            classification="read_only",
            fixture_exemption=_fixture_exemption(
                registration, native=native, chat=chat
            ),
        )
        for registration in registrations
    )


def _side_effecting(
    owner: str, *registrations: str, native: bool = False, chat: bool = False
) -> tuple[ToolAuditPolicy, ...]:
    return tuple(
        ToolAuditPolicy(
            registration=registration,
            classification="side_effecting",
            operation_owner=owner,
            fixture_exemption=_fixture_exemption(
                registration, native=native, chat=chat
            ),
        )
        for registration in registrations
    )


# ``registration`` is ``relative/path.py:advertised_tool_name``.  Advertised
# names, rather than Python function names, make FastMCP aliases first-class.
TOOL_AUDIT_POLICY = (
    *_read_only(
        "agents/analyze/server.py:read_skills",
        "agents/analyze/server.py:list_skill_docs",
        "agents/analyze/server.py:get_ticket_results",
        "agents/analyze/server.py:search_tickets",
    ),
    *_side_effecting(
        "state_store.ticket_transition",
        "agents/analyze/server.py:submit_analysis_result",
        "agents/evaluate/server.py:submit_evaluation_result",
        "agents/gathering_context/server.py:submit_gathering_context_result",
        "agents/platform/server.py:submit_platform_result",
        "agents/synthesis/server.py:submit_synthesis_result",
    ),
    *_read_only(
        "agents/benchmark/server.py:read_skills",
        "agents/benchmark/server.py:list_harness_docs",
        "agents/benchmark/server.py:read_harness_doc",
        "agents/benchmark/server.py:get_execution_config",
        "agents/benchmark/server.py:get_runfile_schema",
        "agents/benchmark/server.py:get_benchmark_params",
        "agents/benchmark/server.py:get_crucible_benchmark_context",
        "agents/benchmark/server.py:get_tool_params",
        "agents/benchmark/server.py:get_example_runfile",
        "agents/benchmark/server.py:get_run_logs",
    ),
    *_side_effecting(
        "providers.ssh.SSHExecutor",
        "agents/benchmark/server.py:setup_passwordless_ssh",
        "agents/benchmark/server.py:validate_benchmark",
        "agents/benchmark/server.py:execute_boot_time_test",
    ),
    *_side_effecting(
        "agents.mcp_audit.MCPAuditMiddleware.operation_transition",
        "agents/benchmark/server.py:execute_benchmark",
    ),
    *_read_only(
        "agents/evaluate/server.py:list_benchmark_artifacts",
        "agents/evaluate/server.py:read_benchmark_artifact",
        "agents/infra/server.py:set_ssh_context",
        "agents/infra/server.py:check_host",
        "agents/infra/server.py:read_remote_file",
        "agents/infra/server.py:list_controller_userenvs",
        "agents/infra/server.py:run_crucible_command",
        "agents/infra/server.py:get_ethtool_info",
        "agents/infra/server.py:get_sysctl_values",
        "agents/infra/server.py:get_hardware_topology",
        "agents/infra/server.py:get_cache_topology",
        "agents/infra/server.py:verify_ssh_path",
        "agents/infra/server.py:list_interfaces",
        "agents/infra/server.py:get_interface_inventory",
        "agents/infra/server.py:check_hosts",
        "agents/infra/server.py:test_port_connectivity",
    ),
    *_side_effecting(
        "providers.ssh.SSHExecutor",
        "agents/infra/server.py:write_remote_file",
        "agents/infra/server.py:deploy_secret",
        "agents/infra/server.py:transfer_file",
    ),
    *_side_effecting(
        "providers.execution.AuditedFilesystem",
        "agents/infra/server.py:read_remote_dir",
    ),
    *_read_only(
        "agents/investigation/server.py:query_investigation_records",
        "agents/investigation/server.py:get_investigation_record",
    ),
    *_side_effecting(
        "providers.investigation.repository",
        "agents/investigation/server.py:create_investigation_record",
        "agents/investigation/server.py:append_build_history",
        "agents/investigation/server.py:link_jira_ticket",
        "agents/investigation/server.py:close_investigation_record",
    ),
    *_side_effecting(
        "providers.resource.jumpstarter_lifecycle",
        "agents/platform/server.py:provision_platform",
    ),
    *_side_effecting(
        "state_store.ticket_transition",
        "agents/platform/server.py:request_clarification",
    ),
    *_read_only(
        "agents/provisioning/server.py:check_platform_contract",
        "agents/provisioning/server.py:check_host_prerequisites",
        "agents/provisioning/server.py:verify_harness_install",
        "agents/provisioning/server.py:check_existing_install",
        "agents/provisioning/server.py:list_skill_docs",
        "agents/provisioning/server.py:read_skills",
        "agents/provisioning/server.py:verify_host_tuning",
        "agents/provisioning/server.py:nm_show_connection",
        "agents/provisioning/server.py:nm_verify_interface",
        "agents/provisioning/server.py:get_private_config",
    ),
    *_side_effecting(
        "providers.ssh.SSHExecutor",
        "agents/provisioning/server.py:install_packages",
        "agents/provisioning/server.py:ensure_prerequisites",
        "agents/provisioning/server.py:install_harness",
        "agents/provisioning/server.py:update_install",
        "agents/provisioning/server.py:uninstall_harness",
        "agents/provisioning/server.py:install_k3s",
        "agents/provisioning/server.py:disable_firewall",
        "agents/provisioning/server.py:open_firewall_port",
        "agents/provisioning/server.py:tune_nic",
        "agents/provisioning/server.py:configure_flow_steering",
        "agents/provisioning/server.py:reset_flow_steering",
        "agents/provisioning/server.py:tune_tcp",
        "agents/provisioning/server.py:pin_irq",
        "agents/provisioning/server.py:reset_irq_pinning",
        "agents/provisioning/server.py:tune_hosts",
        "agents/provisioning/server.py:nm_set_mtu",
        "agents/provisioning/server.py:nm_set_ip",
        "agents/provisioning/server.py:nm_set_dhcp",
        "agents/provisioning/server.py:ensure_harness_installed",
    ),
    *_read_only(
        "agents/resource/server.py:parse_host_config",
        "agents/resource/server.py:list_resource_providers",
        "agents/resource/server.py:check_available_resources",
        "agents/resource/server.py:get_reservation_status",
        "agents/resource/server.py:validate_host",
        "agents/resource/server.py:get_host_inventory",
        "agents/resource/server.py:get_accumulated_metadata",
        "agents/resource/agent.py:get_accumulated_metadata",
    ),
    *_side_effecting(
        "providers.resource.ResourceProvider.reserve",
        "agents/resource/server.py:reserve_resources",
    ),
    *_read_only(
        "agents/retrospective/server.py:get_transcript_analysis",
        "agents/review/server.py:read_skills",
        "agents/review/server.py:get_crucible_benchmark_context",
        "agents/review/server.py:list_harness_docs",
        "agents/review/server.py:read_harness_doc",
        "agents/review/server.py:read_run_results",
        "agents/review/server.py:get_run_summary",
        "agents/review/server.py:cdm_api_requests",
        "agents/review/server.py:compare_results",
        "agents/review/server.py:get_review_config",
        "agents/triage/server.py:read_skills",
        "agents/triage/server.py:list_benchmarks",
        "agents/triage/server.py:get_benchmark_details",
        "agents/triage/server.py:resolve_benchmark",
        "agents/workspace/server.py:jq_file_from_workspace",
        "agents/workspace/server.py:grep_file_from_workspace",
        "agents/workspace/server.py:read_file_from_workspace",
        "agents/workspace/server.py:list_files_from_workspace",
        "agents/workspace/server.py:read_document_from_workspace",
        "agents/workspace/server.py:search_documents_from_workspace",
    ),
    *_side_effecting(
        "providers.workspace.manager.WorkspaceManager",
        "agents/workspace/server.py:generate_chart_from_workspace",
    ),
    *_read_only(
        "agents/workspace/tools.py:jq_file_from_workspace",
        "agents/workspace/tools.py:grep_file_from_workspace",
        "agents/workspace/tools.py:read_file_from_workspace",
        "agents/workspace/tools.py:list_files_from_workspace",
        "agents/workspace/tools.py:read_document_from_workspace",
        "agents/workspace/tools.py:search_documents_from_workspace",
        native=True,
    ),
    *_side_effecting(
        "providers.workspace.manager.WorkspaceManager",
        "agents/workspace/tools.py:generate_chart_from_workspace",
        native=True,
    ),
    *_read_only(
        "agents/base.py:jq_file_from_workspace",
        "agents/base.py:grep_file_from_workspace",
        "agents/base.py:read_file_from_workspace",
        "agents/base.py:list_files_from_workspace",
        "agents/base.py:read_document_from_workspace",
        "agents/base.py:search_documents_from_workspace",
        native=True,
    ),
    *_side_effecting(
        "providers.workspace.manager.WorkspaceManager",
        "agents/base.py:generate_chart_from_workspace",
        native=True,
    ),
    *_side_effecting(
        "state_store.ticket_transition",
        "agents/benchmark/agent.py:submit_benchmark_result",
        "agents/benchmark/agent.py:present_runfile_for_approval",
        "agents/benchmark/agent.py:request_clarification",
        "agents/provisioning/agent.py:request_clarification",
        "agents/provisioning/agent.py:submit_provisioning_result",
        "agents/resource/agent.py:submit_resource_result",
        "agents/retrospective/agent.py:submit_retrospective",
        "agents/review/agent.py:request_clarification",
        "agents/review/agent.py:submit_review_result",
        "agents/triage/agent.py:request_clarification",
        "agents/triage/agent.py:submit_triage_result",
        native=True,
    ),
    # Chat is a native LLM tool surface too.  Keep it in this inventory rather
    # than treating its direct execute_tool dispatch as an out-of-band UI path.
    *_read_only(
        "agents/chat/tools.py:search_tickets",
        "agents/chat/tools.py:get_ticket",
        "agents/chat/tools.py:list_field_options",
        "agents/chat/tools.py:list_skills",
        "agents/chat/tools.py:read_skill",
        "agents/chat/tools.py:read_doc",
        "agents/chat/tools.py:list_users",
        chat=True,
    ),
    *_side_effecting(
        "agents.chat.tools._create_ticket",
        "agents/chat/tools.py:create_ticket",
        chat=True,
    ),
    *_side_effecting(
        "agents.chat.tools._start_ticket",
        "agents/chat/tools.py:start_ticket",
        chat=True,
    ),
    *_side_effecting(
        "agents.chat.tools._send_interjection",
        "agents/chat/tools.py:send_interjection",
        chat=True,
    ),
    *_side_effecting(
        "agents.chat.tools._reply_to_guidance",
        "agents/chat/tools.py:reply_to_guidance",
        chat=True,
    ),
    *_side_effecting(
        "agents.chat.tools._update_ticket_fields",
        "agents/chat/tools.py:update_ticket_fields",
        chat=True,
    ),
    *_side_effecting(
        "agents.chat.tools._stop_ticket", "agents/chat/tools.py:stop_ticket", chat=True
    ),
    *_side_effecting(
        "agents.chat.tools._create_user", "agents/chat/tools.py:create_user", chat=True
    ),
    *_side_effecting(
        "agents.chat.tools._rotate_user_token",
        "agents/chat/tools.py:rotate_user_token",
        chat=True,
    ),
)

POLICY_BY_REGISTRATION = {policy.registration: policy for policy in TOOL_AUDIT_POLICY}

# Every mutating classification points to a concrete implementation that owns
# the effect.  The CI checker validates this map against both the classified
# registration and its canonical audit/operation boundary; it prevents a
# plausible-looking owner label from becoming dead documentation.
OPERATION_OWNER_CONTRACTS = {
    "state_store.ticket_transition": "agents/base.py:_transition_ticket",
    "providers.ssh.SSHExecutor": "providers/ssh.py:SSHExecutor",
    "agents.mcp_audit.MCPAuditMiddleware.operation_transition": (
        "agents/mcp_audit.py:MCPAuditMiddleware"
    ),
    "providers.execution.AuditedFilesystem": (
        "providers/execution/filesystem.py:AuditedFilesystem"
    ),
    "providers.investigation.repository": "providers/investigation:file",
    "providers.resource.jumpstarter_lifecycle": "providers/resource:provision",
    "providers.resource.ResourceProvider.reserve": "providers/resource:reserve",
    "providers.workspace.manager.WorkspaceManager": (
        "providers/workspace/manager.py:WorkspaceManager"
    ),
    "agents.chat.tools._create_ticket": "agents/chat/tools.py:_create_ticket",
    "agents.chat.tools._start_ticket": "agents/chat/tools.py:_start_ticket",
    "agents.chat.tools._send_interjection": "agents/chat/tools.py:_send_interjection",
    "agents.chat.tools._reply_to_guidance": "agents/chat/tools.py:_reply_to_guidance",
    "agents.chat.tools._update_ticket_fields": "agents/chat/tools.py:_update_ticket_fields",
    "agents.chat.tools._stop_ticket": "agents/chat/tools.py:_stop_ticket",
    "agents.chat.tools._create_user": "agents/chat/tools.py:_create_user",
    "agents.chat.tools._rotate_user_token": "agents/chat/tools.py:_rotate_user_token",
}

# Exceptions are intentionally empty.  Do not add a broad module exemption:
# an exception must identify the exact symbol and have an accountable expiry.
AUDIT_BYPASS_ALLOWLIST: tuple[AuditBypass, ...] = ()
