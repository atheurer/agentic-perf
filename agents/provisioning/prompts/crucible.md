## Crucible Provisioning Notes

Crucible uses a dedicated controller host where the framework is
installed. Target/endpoint hosts do not need the harness installed —
Crucible SSHes to them and runs workloads in containers.

### Controller-only installation

Always set `controller_host` on install-related tools (install_harness,
ensure_harness_installed, uninstall_harness, verify_harness_install,
check_existing_install, update_install) so the harness is only
installed on the controller, not on target hosts.

Set `controller_host` on ensure_prerequisites so harness prereqs
(podman, git, jq, curl) are installed only on the controller.

### Provisioning config

Use `get_private_config(harness_name="crucible", key="provisioning")`
as the source of truth for installation. Provisioning does not need
benchmark repositories, benchmark parameters, run-file semantics, or
benchmark-specific documentation. Those are resolved later by the
benchmark agent after the controller is prepared. The controller
remains authoritative for installed-runtime facts.
