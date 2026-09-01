# Fleet investigation runbook

Status: current for the implemented resource providers. Fleet mode is enabled
with `custom_fields.fleet_investigation: {"enabled": true}` and a provider
selector such as `custom_fields.directives.board_selector`. Supported provider
choices depend on configuration: Jumpstarter, QUADS, AWS, and user-provided
hosts are implemented resource paths; verify availability first.

```bash
agentic-perf submit "Boot time across S32G boards" \
  -d "Use Jumpstarter; selector board-type=nxp-s32g-vnp-rdb3" \
  --directive resource_provider=jumpstarter \
  --directive board_selector=board-type=nxp-s32g-vnp-rdb3
```

The triage/resource stages set and enforce the selector. The flow is:
`awaiting_hardware → (preparing_platform) → awaiting_provision →
executing_benchmark → coordinating_fleet`. The deterministic fleet coordinator
records each host result, excludes tested hosts on the next acquisition, and
routes to `awaiting_hardware` or `evaluating_convergence`.

`custom_fields.fleet_investigation.tested_hosts` records completed, partial,
or failed hosts and their KPIs. A failed host is data, not automatically a
reason to discard the investigation. Hard exhaustion means the inventory has
been exhausted; soft exhaustion means no remaining device is currently
available. Soft exhaustion can return to hardware acquisition after a retry or
user intervention. No-device, provider, or flash failures should be left in
the record and may route to customer guidance.

Convergence routes to analysis or a refined plan, re-provisioning, synthesis,
or guidance. Synthesis writes the investigation record, then teardown releases
resources. Review/evaluation and the dashboard expose per-host results and
events; use `show`, `watch -v`, or the events API when diagnosing a partial run.

Do not assume every provider supports the same fleet metadata. Run
`list_resource_providers` and `check_available_resources` first, and treat
“not configured”, “temporarily unavailable”, and “unsupported” as distinct
outcomes.
