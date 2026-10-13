# RHEL Kernel Transitions

When an execution-plan step carries `params.kernel`, your job is the
kernel transition — not a harness install. Every command, target address
and state decision is made by the tools; you supply the kernel release
the plan already names, the ticket's full host list, and the approval id.

## Tool order

1. `get_kernel_inventory` — what is running, installed and available on
   each host. Read-only.
2. `prepare_kernel_change` — validates the host list against the plan and
   the ticket, takes a fresh inventory, computes `actions_required`
   (`install`, `select_default`, `reboot`) and persists an immutable
   intent record. Returns the `intent_id`. It refuses when any host's
   inventory is unavailable, so fix reachability first.
3. `present_kernel_change_for_approval` — creates the human-in-the-loop
   approval bound to that intent and waits for the user. Interpret the
   reply with `resolve_kernel_approval`. The approval id it returns is
   what every mutating tool needs.
4. After approval, run exactly the actions in `actions_required`, in
   order, each with the approval id:
   - `install_kernel` — idempotent `dnf install`; already-installed
     hosts are reported without running dnf.
   - `select_default_kernel` — idempotent `grubby --set-default`;
     verifies `vmlinuz` and `initramfs` exist first.
   - `reboot_hosts_and_verify` — serial reboot with fresh-session
     reconnect polling and `uname -r` / `boot_id` verification.
     Long-lived; progress is posted to the ticket.
   The first of these consumes the approval; the later ones verify it.
   A tool called out of order, or for an action the intent does not
   require, is refused.
5. `verify_harness_install` — confirm the harness still works on the
   new kernel.
6. `submit_provisioning_result` with `provisioning_complete=true` only
   when the reboot tool reported `status: "verified"`. The submission is
   rejected unless the step's checkpoint is verified for every host; the
   verification details are derived from the checkpoint, not from your
   summary.

## Host identity

The host list is an identity, never a selection: it must be exactly the
ticket's assigned benchmark hosts (`assigned_hardware_ips.targets`
minus the harness controller). A subset, the controller, the
orchestrator, an unassigned host, or an unresolvable name is refused
(`host_set_mismatch` with `missing` / `extra` / `refused`). The tools
SSH only to the canonical address that passed validation.

## What the reboot tool does for you

- Every invocation probes each host first. A reboot that already
  happened in this step (new `boot_id`, expected kernel) is reconciled,
  never repeated — safe after a crash, a pause or a restart.
- The checkpoint is written *before* the reboot command is sent, with
  the pre-reboot `boot_id`.
- Hosts are rebooted one at a time; the next host starts only after the
  previous one is verified.
- `reboot_policy: "always"` (default) gives every host a fresh boot even
  if it already runs the expected kernel — parity between A and B.
  `"if_needed"` skips those hosts.
- Any outcome other than `verified` pauses the ticket and returns
  `status: "paused"`. The remaining hosts are `not_attempted`.

## After a pause

Use `verify_kernel_state` (with the full host list — it refuses a
subset so a stale record can never hide) to reconcile the checkpoint
with what is actually running — it never reboots and needs no approval. If every host
is now on the expected kernel the step can complete. If a host needs
another reboot, run `prepare_kernel_change` again and get a new approval;
the previous approval was consumed.

## Safety rules

- **Never target the controller.** The harness controller is never
  rebooted as part of a kernel transition.
- **Never call `install_packages` for kernels.**
- **Approval is mandatory.** The approval tools, not
  `request_clarification`, are how kernel changes get authorised.
- **The kernel must match the plan step** — release and package
  (`kernel` vs `kernel-rt`). Tools refuse anything else.

## States and failure classes

Inventory / install / select, per host:

| State | Meaning | Action |
|---|---|---|
| `installed` / `already_installed` | Release is in the rpm list | Proceed |
| `available` | Release is in the dnf repos | Install |
| `not_found` | Not in any enabled repo | Ask the user |
| `not_checked` / `unreachable` | The host could not be queried | Fix access; prepare refuses until resolved |
| `install_failed` / `selection_failed` | The command failed | Report; do not retry blindly |
| `not_bootable` | `vmlinuz` or `initramfs` missing | Report; do not select |
| `indeterminate` | A command timed out and the outcome could not be confirmed | Re-run the same tool; it re-reads the host first |
| `already_default` / `ok` | Boot entry is set | Proceed to reboot |

Reboot, per host:

| State | Meaning |
|---|---|
| `verified` | New `boot_id`, expected kernel running |
| `already_verified` | Verified earlier in this step; live probe still matches |
| `precheck_failed` | Unreachable, or the default entry is not the expected kernel |
| `fallback_boot` | Rebooted but came back on the previous kernel |
| `kernel_mismatch` | Rebooted but came back on some other kernel |
| `reconnect_timeout` | No SSH within the time budget |
| `reboot_not_observed` | Stayed up on the old boot; the reboot command was ignored |
| `indeterminate` | Probe unreadable, or the operation was cancelled mid-reboot |
| `not_attempted` | Serial run stopped on an earlier host |
| `refused` | Controller, self, unassigned or unresolvable host |
