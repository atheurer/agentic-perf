# Multi-instance deployment and claims

Status: current operator guide. Give each deployment a distinct home, service
port, instance name, token, logs, and PID file.

```bash
AGENTIC_PERF_HOME=/srv/ap-a AGENTIC_PERF_INSTANCE_NAME=lab-a \
  ./start.sh   # generated config contains URL/port 8091
AGENTIC_PERF_HOME=/srv/ap-b AGENTIC_PERF_INSTANCE_NAME=lab-b \
  ./start.sh   # generated config contains URL/port 8092
```

The generated `config.json` is the default source of truth for the state-store
URL and port. The launcher, orchestrator, web UI, and CLI all resolve their
defaults from that configuration, so no per-command port flags are needed.
`STATE_STORE_URL` and `STORE_PORT` remain available as explicit overrides.

`AGENTIC_PERF_HOME` controls config, tickets, logs, cache, and PID state.
`AGENTIC_PERF_SECRETS`, `AGENTIC_PERF_SKILLS`, and
`AGENTIC_PERF_ARTIFACTS` can separate credentials, private skills, and
artifacts. `AGENTIC_PERF_INSTANCE_NAME` overrides `config.json`'s
`instance_name`, which otherwise falls back to the short hostname. Use
separate `state_store.port`/`state_store.url` values and inspect each generated
`secrets/api-token` independently.

## Managed development instances

Use `scripts/dev-instance.sh prepare` for development work. It writes an
identity manifest containing the instance name, canonical runtime home and
worktree, URL/port, and a tamper-detecting digest. Before `start`, `shell`,
`status`, `test`, `validate`, or `commit`, the script verifies that the
manifest, config, selected worktree, and endpoint still agree. A copied or
edited runtime home therefore fails with expected-versus-actual diagnostics;
identity fields are not silently repaired. Port 8090 is rejected for managed
instances unless `--dangerous-default` is explicitly supplied for a deliberate
collision test. The state-store process lock and lease remain authoritative.

Never recursively copy a live `~/.agentic-perf` or managed runtime home into
another instance. That copies credentials, leases, claims, logs, and process
state and can make an operator act on the wrong endpoint.

To reproduce a ticket safely, select explicit source and destination managed
instances and ticket IDs. The command is a dry-run unless `--apply` is present:

```bash
./scripts/dev-instance.sh import-state \
  --source issue-123 --destination issue-456 --ticket PERF-123
./scripts/dev-instance.sh import-state \
  --source issue-123 --destination issue-456 --ticket PERF-123 --apply
```

Only selected ticket fixtures are transferred. Config, secrets, PID/lock files,
logs, audit files, caches, reservations, claims, leases, approvals, and
in-progress operation data are excluded. Imported tickets are marked
`awaiting_customer_guidance`, recorded as non-dispatchable fixtures, and carry
provenance without copying credentials. Existing destination IDs fail before
any write.

The dispatcher claims tickets with its instance name and a lease (default 300
seconds), renews at half the lease, and releases the claim on completion. A
restart can reacquire an expired claim. A stale claim is not proof that the
old process is dead: check its PID/logs and provider work before taking over.
The claim endpoints are documented in [rest-api-reference.md](rest-api-reference.md).

AWS resources carry instance/deployment identity tags. Keep instance names
unique and run cleanup against the intended instance; `cleanup --all-instances`
is deliberately broader and should be used only after reviewing matches.
Run the two `start.sh` commands from controlled service units or separate
terminals, and keep each instance's environment visible in its logs.

`list --include-default` prints warnings for duplicate configured endpoints or
token fingerprints. Fingerprints are truncated hashes; token values are never
printed.
