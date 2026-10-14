## Provisioning Configuration and Host Scope

Bootstrap `get_skill_context(subject="harness/crucible")` and read the
returned provisioning entrypoints and configuration view named
`provisioning`. Do not call `get_private_config` for Crucible. Installation
choices come from configured guidance and settings; do not infer them from
benchmark metadata.

When the ticket's execution model uses a dedicated controller, set
`controller_host` on install, prerequisite, update, check, and verify tools.
Keep harness installation scope separate from user-requested packages and
host tuning on endpoints. Provisioning prepares the installation; benchmark
run-file construction and benchmark documentation discovery belong to the
benchmark phase.
