# Fleet investigation runbook

Status: current for Jumpstarter fleet investigations. Fleet mode is enabled
with `custom_fields.fleet_investigation: {"enabled": true}` and a provider
selector such as `custom_fields.directives.board_selector`. Jumpstarter is the
provider that currently enumerates devices and consumes `exclude_hosts` for
all-device iteration. QUADS, AWS, and user-provided hosts can support resource
acquisition, but do not provide the same untested-device inventory contract.

```bash
python3 cli.py submit "Boot time across S32G boards" \
  -d "Run a fleet investigation with Jumpstarter across boards of type nxp-s32g-vnp-rdb3. Exclude boards already tested and continue until the fleet is exhausted."
```

For exact structured placement, create the ticket through the API with
`custom_fields.fleet_investigation` and `custom_fields.directives`; the CLI
does not have a `--directive` option.

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
