# Capabilities and tool inventory

Status: current implementation reference (audited against `origin/main`,
commit `909f298`). The source of truth for registered MCP tools is each
`agents/*/server.py` `@mcp.tool()` declaration plus the native workspace tools
registered by `agents/base.py`.

## Dispatchers and agents

The deterministic orchestrator polls the state store, claims a ticket, and
dispatches one handler for the current status. The status-to-handler mapping is
in `orchestrator/dispatcher.py`; the complete lifecycle is in
[status-lifecycle.md](status-lifecycle.md).

| Handler | Status | LLM-driven | Purpose |
|---|---|---:|---|
| `triage` | `triage_pending` | yes | classify request and choose a path |
| `resource_create` | `awaiting_hardware` | yes | discover, reserve, and validate resources |
| `platform` | `preparing_platform` | no | deterministic platform/image preparation |
| `image_builder` | `building_image` | no | deterministic custom image build |
| `provisioning` | `awaiting_provision` | yes | install and configure harnesses |
| `benchmark` | `executing_benchmark` | yes | construct, validate, and execute runs |
| `review` | `awaiting_review` | yes | inspect results and recommend next action |
| `resource_teardown` | `awaiting_teardown` | yes | release resources and clean hosts |
| `retrospective` | `retrospective_pending` | yes | record post-run findings |
| `gathering_context` | `gathering_context` | yes | collect prior records and change context |
| `planning_investigation` | `planning_investigation` | yes | produce an investigation plan |
| `evaluating_convergence` | `evaluating_convergence` | yes | assess results and route the next step |
| `fleet_coordinator` | `coordinating_fleet` | no | record a host result and select the next host |
| `synthesizing_results` | `synthesizing_results` | yes | write the final investigation record |
| introspection | out-of-band | configurable | observe events and write guidance/summary |

`new`, `awaiting_customer_guidance`, and `closed` are state-machine statuses,
not dispatch targets. The coordinator also handles claims, lease renewal,
stop requests, and terminal cleanup.

## MCP tools by server

These are the registered tools. Every agent also receives the native workspace
tools listed below. Provider-specific tools are attached only when configured.

| Server/role | Registered tools |
|---|---|
| Triage | `read_skill`, `list_benchmarks`, `get_benchmark_details`, `resolve_benchmark` |
| Resource | `parse_host_config`, `list_resource_providers`, `check_available_resources`, `reserve_resources`, `get_reservation_status`, `validate_host`, `get_host_inventory`, `get_accumulated_metadata` |
| Platform | `provision_platform`, `submit_platform_result` |
| Provisioning | `check_platform_contract`, `check_host_prerequisites`, `install_packages`, `ensure_prerequisites`, `install_harness`, `verify_harness_install`, `check_existing_install`, `update_install`, `uninstall_harness`, `install_k3s`, `list_skill_docs`, `read_skill`, `read_skills`, `disable_firewall`, `open_firewall_port`, `tune_nic`, `configure_flow_steering`, `reset_flow_steering`, `tune_tcp`, `pin_irq`, `reset_irq_pinning`, `verify_host_tuning`, `tune_hosts`, `nm_set_mtu`, `nm_set_ip`, `nm_set_dhcp`, `nm_show_connection`, `nm_verify_interface`, `ensure_harness_installed`, `get_private_config` |
| Benchmark | `read_skill`, `read_skills`, `list_harness_docs`, `read_harness_doc`, `get_execution_config`, `get_runfile_schema`, `get_benchmark_params`, `get_tool_params`, `get_example_runfile`, `setup_passwordless_ssh`, `execute_benchmark`, `get_run_logs`, `execute_boot_time_test` |
| Analyze | `read_skill`, `list_skill_docs`, `get_ticket_results`, `search_tickets`, `submit_analysis_result` |
| Evaluate | `submit_evaluation_result`, `list_benchmark_artifacts`, `read_benchmark_artifact` |
| Review | `read_skill`, `list_harness_docs`, `read_harness_doc`, `read_run_results`, `get_run_summary`, `cdm_api_requests`, `compare_results`, `get_review_config` |
| Investigation | `query_investigation_records`, `get_investigation_record`, `create_investigation_record`, `append_build_history`, `link_jira_ticket`, `close_investigation_record` |
| Gathering context | `submit_gathering_context_result` |
| Synthesis | `submit_synthesis_result` |
| Retrospective | `get_transcript_analysis` |
| Infrastructure | `set_ssh_context`, `check_host`, `write_remote_file`, `read_remote_file`, `read_remote_dir`, `get_ethtool_info`, `get_sysctl_values`, `get_hardware_topology`, `get_cache_topology`, `verify_ssh_path`, `list_interfaces`, `get_interface_inventory`, `deploy_secret`, `transfer_file`, `check_hosts`, `test_port_connectivity` |

Agent-specific filtering is assembled when the dispatcher creates the agent
MCP client. A tool in this table is not evidence that every agent can call it.
For a live deployment, inspect the tool list in the agent startup event.

## Native workspace tools

All agents can use `jq_query`, `grep_file`, `read_file_slice`,
`list_workspace_files`, and `generate_chart_from_workspace`. They are scoped
to the current ticket workspace; see [workspaces-and-charts.md](workspaces-and-charts.md).

## Explicit non-capabilities

There are no current tools named `ssh_execute`, `get_harness_schema`,
`submit_benchmark`, `get_benchmark_status`, `get_benchmark_results`, or
`query_metrics`. SSH, run execution, result reading, and CDM requests are
exposed under the names above and constrained by the agent server.
