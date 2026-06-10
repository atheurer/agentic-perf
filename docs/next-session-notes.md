# Next Session Notes

## Platform & Install Contracts (Priority: High)

The install_contract pattern works for secrets. Extend it to platform requirements:

**Crucible platform contract:**
```json
"platform_contract": {
    "packages": ["git", "podman", "jq", "curl"],
    "os": ["rhel8", "rhel9", "rhel10", "fedora"]
}
```

**Zathras platform contract:**
```json
"platform_contract": {
    "os": ["rhel8", "rhel9"],
    "repos": ["epel"],
    "notes": "dnf5 (Fedora 41+) breaks install.sh. Zathras installs its own deps (ansible, terraform, yq, pip) but needs EPEL enabled first."
}
```

The provisioning agent validates platform_contract before attempting install. If the platform doesn't match, it escalates back to the resource agent to find a compatible host — this is the negotiation loop between agents.

Key design question: how far do we go? Validate OS + repos (simple), or also validate individual packages (complex, overlaps with what the install script should do)?

## Triage Directives (Priority: Medium)

The triage agent should extract operational directives from natural language:
- `on_existing_install: reinstall` — "reinstall crucible before running"
- `fresh_host: true` — inferred when QUADS is used
- `user_pre_run_approval: true/false` — "don't ask me for approval to start the tests"
- `harness: zathras` — "use zathras, not crucible"

Some of these already work naturally (triage extracted `reinstall_crucible: true` from request text). Formalize with a `directives` field in the triage submit schema.

## Remote Skills Loading (Priority: Medium)

Currently CrucibleSkillProvider and ZathrasSkillProvider need local repo clones. The architecture should be:
- **Before install**: lightweight benchmark catalog in private skill config (names, roles, min_hosts)
- **After install**: benchmark agent loads detailed skills from the controller's harness installation (schemas, example run-files, test_defs.yml)

This decouples the orchestrator machine from needing harness repos locally.

## Run-File Integrity (Priority: Medium)

The LLM modifies the run-file between `generate_run_file` and `execute_benchmark`. The schema guardrail catches invalid modifications, but the LLM shouldn't need to modify it at all. Options:
- Have `execute_benchmark` use the run-file directly from `generate_run_file` internally
- Tell the benchmark agent via prompt to pass the run-file through unmodified
- Make `generate_run_file` produce a complete, valid run-file every time (it mostly does, but the LLM adds extra fields like `endpoint_user`)

## Crucible GitHub Issue

File an issue on perftool-incubator/crucible about `_ssh-key-cleaner` removing non-crucible keys. The legacy cleanup (`sed -i "/${LEGACY_KEY_COMMENT}/d"`) deletes any key with comment `root@$(hostname -f)`, which is the default ssh-keygen comment. This breaks any automation that adds SSH keys to a crucible controller.

## Checkpoint/Restart on Tickets

Instead of resubmitting from scratch when a phase fails, support rewinding a ticket to a previous state. The ticket has all accumulated context (hardware IPs, SSH key, harness version). A `rewind` CLI command would transition the ticket back and let the dispatcher re-run just the failed phase. Need to define which custom_fields to clear on rewind.

## QUADS Orphaned Assignments

The QUADS terminate API returns 500 for assignments with expired schedules (0 hosts). The teardown agent should:
1. Try to delete host schedules first
2. Then terminate the assignment
3. Catch 500 and log as warning, not failure

Also file QUADS issue — RFE #605 mentions auto-cleanup of orphaned assignments.

## SSH Key Cleanup

After a run completes and the ticket closes, the teardown agent should remove the `agentic-perf-controller-key` from authorized_keys on all hosts. We're currently responsible tenants but not cleaning up after ourselves.

## Zathras-Specific Issues

- `bin/install.sh` doesn't install `gh` (GitHub CLI) — needed for version checks
- `--skip_test_version_check` is a workaround; upstream should make version checks optional or not require `gh`
- `dnf config-manager --add-repo` doesn't work on dnf5 (Fedora 41+) — zathras is RHEL-only in practice
- Install script should be more self-contained about its deps
