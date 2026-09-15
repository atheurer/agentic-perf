# Live full-agent ticket gate

This opt-in gate starts from a prepared isolated instance and processes one real
ticket through the production state store, orchestrator, full LLM agents, MCP
servers, and a live Crucible controller. The workload is one five-second
`sleep` sample with client ID 1 on one separately approved system under test.

The gate is intentionally not part of public CI. It uses private infrastructure
and a configured LLM provider. Hostnames are command arguments and are never
committed. SSH and LLM settings come from the isolated instance configuration.

## Controlled execution

Enter the prepared development-instance shell so `AGENTIC_PERF_HOME`, the
state-store URL, deployment token, checkout, and runtime configuration agree.
Export the configured LLM provider credential, then run:

```bash
python3 scripts/live-ticket-gate.py \
  CONTROLLER SYSTEM_UNDER_TEST --live
```

That is the normal interface: two hostnames plus the explicit acknowledgement
that they are live systems. Optional flags can change the five-second duration,
total timeout, artifact destination, or reuse services that are already running.
By default the gate manages the isolated instance services and writes artifacts
under its private runtime home.

The runner first verifies SSH access, rejects an active
`crucible-rickshaw-run` container, and snapshots the controller run directory.
It passes only when the ticket reports completion and exactly one matching run
directory was added to the controller inventory. The ticket's authoritative
status trail must include triage, resource, provision, benchmark approval and
execution, review, teardown, retrospective, and closure; every execution-plan
agent must complete. Managed services must then stop cleanly, and their logs
must contain none of the gate's fatal runtime signatures.

Before approving the benchmark, the gate rejects any run file that is not
exactly one `sleep` benchmark using client ID 1 bound to the configured system
under test, one bounded sample, the configured duration, and no enabled
collection tools. The endpoint setting `disable-tools` is accepted only when it
is the JSON boolean `true`; `host-mounts` remains prohibited. Any other
human-guidance pause fails the run.

The artifact directory is created mode 0700 and contains the tested Git SHA,
status trail, ticket, approved run file, service logs, and final result. Treat it
as private because tickets and logs contain infrastructure identities.

For stabilization replay, first require three successful runs at the pre-wave
control commit `2eed4735b87e1ad7bad1cf75cf95314517c5b689`. Then run each Wave
0-8 merge checkpoint in historical order, stopping and filing a focused issue at
the first failure. The ordered refs and their issue/PR identities are recorded in
`scripts/live-ticket-replay.json`; follow-up fixes are explicit checkpoints rather
than being silently folded into a later wave.
