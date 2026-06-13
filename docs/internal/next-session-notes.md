# Next Session Notes

## ~~LLM-Driven Run-File Generation~~ (Done)

Implemented: benchmark agent constructs run.json directly from natural language via LLM instead of `generate_run_file` templates. Commit `bb77ac2` on main. Design doc: [design-llm-runfile-generation.md](design-llm-runfile-generation.md).

## ~~Triage Directives~~ (Done)

Implemented: `directives` field in triage submit schema with `on_existing_install`,
`harness`, `user_pre_run_approval`, `host_cleanup`, plus arbitrary keys for extensibility.
Provisioning and benchmark agents check directives before falling back to skill config defaults.
Benchmark agent now asks for user approval before executing (unless `user_pre_run_approval: false`).

## Persist Validated Run-File in Ticket (Priority: Medium)

When the benchmark agent validates a run-file, save it to `custom_fields.validated_run_file`. On re-dispatch (e.g., after a transient failure like valkey), the agent checks for an existing validated run-file and skips straight to execution instead of rebuilding from scratch. Saves time and LLM iterations on retries.

## ~~Clean Up Stale Valkey~~ (Done)

Implemented as a pre-flight check in `execute_benchmark` (benchmark agent) right before `crucible run`. Stops `crucible-valkey` if running with no active `crucible-rickshaw-run`. Placed here instead of provisioning to also catch stale state from failed retries within the same ticket.

## Migrate Crucible Cleanup to Crucible Project (Priority: Low)

The `cleanup_harness` function in `agents/provisioning/mcp_server.py` has crucible-specific uninstall logic (stop containers, discover and remove auth tokens from registries.json, remove system artifacts). This should eventually become a `crucible uninstall` command upstream so any consumer can use it. For now it lives here.

## Harness Update Directive (Priority: Medium)

Allow a triage directive like `update_harness: true` that tells the provisioning agent to update the harness after install check (e.g., `crucible update`). The execution config already has `update_command` for crucible and `on_existing_install` for zathras — wire these into the provisioning flow so the agent respects them. Triggered by: the PERF-B51A2E61 test showed crucible was 19 commits behind, which could cause failures.

## ~~Remote Skills Loading~~ (Phase 1 Done)

Currently CrucibleSkillProvider and ZathrasSkillProvider need local repo clones. The architecture should be:
- **Before install**: lightweight benchmark catalog in private skill config (names, roles, min_hosts)
- **After install**: benchmark agent loads detailed skills from the controller's harness installation (schemas, example run-files, test_defs.yml)

This decouples the orchestrator machine from needing harness repos locally.

## Checkpoint/Restart on Tickets

Instead of resubmitting from scratch when a phase fails, support rewinding a ticket to a previous state. The ticket has all accumulated context (hardware IPs, SSH key, harness version). A `rewind` CLI command would transition the ticket back and let the dispatcher re-run just the failed phase. Need to define which custom_fields to clear on rewind.

## QUADS Orphaned Assignments

The QUADS terminate API returns 500 for assignments with expired schedules (0 hosts). The teardown agent should:
1. Try to delete host schedules first
2. Then terminate the assignment
3. Catch 500 and log as warning, not failure

Also file QUADS issue — RFE #605 mentions auto-cleanup of orphaned assignments.

## Zathras-Specific Issues

- `bin/install.sh` doesn't install `gh` (GitHub CLI) — needed for version checks
- `--skip_test_version_check` is a workaround; upstream should make version checks optional or not require `gh`
- `dnf config-manager --add-repo` doesn't work on dnf5 (Fedora 41+) — zathras is RHEL-only in practice
- Install script should be more self-contained about its deps
