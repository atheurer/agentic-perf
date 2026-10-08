## Provisioning Configuration and Host Scope

Read the subject's provisioning entrypoints through the skill gateway.
Use `get_private_config(harness_name="crucible", key="provisioning")`
for the approved provisioning settings view. Installation choices come from
configured guidance and settings; do not infer them from benchmark metadata.

When the ticket's execution model uses a dedicated controller, set
`controller_host` on install, prerequisite, update, check, and verify tools.
Keep harness installation scope separate from user-requested packages and
host tuning on endpoints. Provisioning prepares the installation; benchmark
run-file construction and benchmark documentation discovery belong to the
benchmark phase.
