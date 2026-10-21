# Crucible run-file workflow notes

Use the current controller-sourced schema, benchmark subproject documents, and
tool metadata through get_skill_context to construct a run file. The
agentic-perf validator enforces its own run-file contract; pass the exact file
that validated to execute_benchmark, and do not modify it between validation
and execution.

Do not copy a generic run-file example's parameters into every benchmark. In
particular, per-ID remotehost configuration is benchmark-specific. For a
normal Crucible uperf client, peer discovery comes from Crucible's endpoint
metadata; do not add a client remotehost override unless the installed
bench-uperf documentation requires it. The uperf server's ifname option
selects the interface address it advertises; it is a server-side interface
choice, not a substitute for a client remotehost value. Follow the installed
benchmark subproject documentation when its behavior differs.

If validation rejects a required role, profiler, or endpoint combination,
inspect the returned schema and current controller guidance. Do not invent
engine IDs or endpoints to work around the validator. Ask for clarification if
the supported configuration cannot be established.
