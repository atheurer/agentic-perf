# RHEL Kernel Transitions

When an execution-plan step carries `params.kernel`, your job is the
kernel transition — not a harness install.

## Tool order

1. `get_kernel_inventory` — discover what is running, installed, and
   available on each host.
2. `prepare_kernel_change` — validates hosts against the plan, computes
   required actions (install, select_default, reboot), writes an intent
   record. Returns the `intent_id`.
3. `present_kernel_change_for_approval` — creates a human-in-the-loop
   approval request. Wait for the user to approve.
4. After approval: call the actions from `actions_required` in order:
   - `install_kernel` — idempotent `dnf install`; skips if already installed.
   - `select_default_kernel` — idempotent `grubby --set-default`; skips
     if already default.
   - `reboot_hosts_and_verify` — serial reboot with reconnect polling
     and kernel verification. Long-lived; progress updates are posted.
5. After reboot verification: `verify_harness_install` to confirm the
   harness is still functional.
6. `submit_provisioning_result` — only with `provisioning_complete=true`
   when the reboot tool reports `state: "verified"` for all hosts.

## Safety rules

- **Never target the controller.** The harness controller is never
  rebooted as part of a kernel transition.
- **Never call `install_packages` for kernels.** The kernel tools
  handle installation with validation and approval.
- **Approval is mandatory.** The approval tools (not `request_clarification`)
  are how kernel changes get authorised.
- **Hosts must be assigned SUTs.** Every host must be in
  `assigned_hardware_ips.targets`. Unassigned hosts are refused.
- **The kernel must match the plan step.** Tools refuse a kernel that
  differs from `execution_plan.steps[current_step].params.kernel.release`.

## Failure classes

| State | Meaning | Action |
|---|---|---|
| `installed` | Kernel is in rpm list | Proceed to select/reboot |
| `available` | Kernel is in dnf repos | Install first |
| `not_found` | Kernel is not in any repo | Ask the user |
| `not_checked` | dnf query failed | Report the error |
| `install_failed` | dnf install failed | Report; do not retry |
| `not_bootable` | vmlinuz or initramfs missing | Report; do not select |
| `selection_failed` | grubby --set-default did not stick | Report |
| `already_installed` | No action needed | Proceed |
| `already_default` | No action needed | Proceed to reboot |
