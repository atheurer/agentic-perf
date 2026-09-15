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
