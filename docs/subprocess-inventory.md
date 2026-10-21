# Local subprocess inventory

`providers/ssh.py` is intentionally excluded: it is the SSH/SCP transport
boundary, not a local tool execution path.  Test mocks are also excluded.

Ticket-scoped local process callers are to use
`providers.execution.AuditedSubprocessRunner`: workspace helpers, resource
providers (QUADS, AWS, Jumpstarter), image builders, skill providers, and MCP
tool implementations.  The only permitted non-ticket contexts are topology
read-only host discovery and bootstrap/service-management scripts, which do
not have a ticket context and must remain argv-only.

The `lscpu`/`ip` snippets in `agents/infra/topology.py` and the `pgrep
irqbalance` snippet in `agents/provisioning/server.py` execute inside remote
SSH scripts, not in this process; they are SSH-transport exceptions.

All `ssh`, `scp`, and `sshpass` executable invocations in AWS, QUADS, infra,
and provisioning paths are #789 transport exceptions.  Local `ssh-keygen`,
`git`, `jq`, `jmp`, `podman`, and helper-script calls remain #790-owned.

This document is the exception allowlist until each historical helper is
migrated; additions require a documented reason and an inventory test update.

`providers/skills/gateway.py:search` uses the existing audited subprocess runner
for GNU grep. Its argv is constructed without a shell; POSIX extended
expressions reject backreferences and run with a two-second deadline. The
input is a temporary export containing only phase-visible pinned Markdown,
with no runtime configuration or credentials. The temporary file is created
and removed through the ticket's critical filesystem facade. This is an
audited caller, with no raw subprocess exception. Deployment images must
provide GNU grep.
