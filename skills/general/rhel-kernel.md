# RHEL Kernel Transitions

**By default a kernel step is run without model reasoning.** When the
current execution-plan step has `params.kernel`, the provisioning agent
executes the fixed sequence below in code (inventory → prepare →
approval pause → install → select → reboot/verify → submit) and only the
human approval is a decision. The ticket directive
`kernel_step_mode: "llm"` switches to the tool-driven model workflow
described here; `kernel_reboot_policy: "if_needed"` skips the reboot of
a host already on the expected kernel (default `"always"`).

When an execution-plan step carries `params.kernel`, your job is the
kernel transition — not a harness install. Every command, target address
and state decision is made by the tools; you supply the kernel release
the plan already names, the ticket's full host list, and the approval id.

## Model workflow

Retrieve this guidance with `get_skill_context(subject="platform/kernel")`,
then read the returned project entrypoint ref. The deterministic runner follows
its fixed code path and submits after kernel verification; the following tool
sequence describes the optional model workflow.

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
   Every action is authorized against the current plan and allocation under
   the state-store lock, including retries after initial consumption.
   A tool called out of order, or for an action the intent does not
   require, is refused.
5. In the model workflow, use `verify_harness_install` to check the
   harness after the kernel change when applicable. The deterministic runner
   does not perform this additional check; its completion gate proves the
   kernel transition, not harness readiness.
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
with what is actually running — it never reboots and needs no approval. The approved reboot policy also has to be satisfied. Under `always`, an
issued reboot must have a nonempty changed boot ID; merely observing the
expected kernel does not complete the step. Under explicit `if_needed`, a
host already running the kernel can verify only if no issued reboot is pending. If a host needs
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

### Approval identity and recovery

A prepared intent records the validated literal SSH address for each assigned
host. Its digest binds those addresses, the full kernel specification (including
companion packages), the plan step, and the ordered actions. Every mutation
checks the current assigned targets against those approved pins. A changed DNS
answer, host set, or specification requires preparation and approval again.
Older intents without address pins fail closed.

Reboot permission is reserved once, durably, by the approval API before a reboot
command can leave the controller. A timeout, cancellation, missing reconnect, or
unchanged boot ID never permits an automatic second command. Repeated calls
observe the issued reboot; they can reconcile a new boot ID without approval.
A crash after permission reservation but before sending also requires a new
explicit decision rather than guessing whether the command was sent.

`verify_kernel_state` invalidates every required host's previous evidence before
probing. Unreachable or unparsable hosts remain durably indeterminate, including
when all probes fail or reconciliation is cancelled. A later successful full
reconciliation can restore verification. Provisioning submission, plan advance,
and benchmark handoff require matching planned kernel and package, exact host
coverage, current address pins, and verified host records.

### Durable control records

Kernel intents, transition evidence, and approval records are server-owned.
Ordinary create-ticket and field-update requests cannot supply or replace them.
The kernel control API requires trusted service or administrator authentication,
the current provisioning status and kernel step, and the exact active session,
epoch, and ticket claim. The service token is a shared deployment credential; it
does not authenticate a distinct MCP-server identity. Each record update checks
the caller's observed version under the state-store lock, persists a cloned
ticket durably, and rejects stale versions without retrying them automatically.

Transition evidence binds the full kernel specification, including companions,
and records each verified host's observed kernel and boot ID. A changed or legacy
specification requires an explicit recovery decision; it never discards an
in-flight reboot checkpoint automatically. IPv4-mapped IPv6 addresses use their
IPv4 identity for self, controller, assignment, and duplicate checks. Two assigned
aliases sharing an address are refused rather than rebooting one machine twice.

### Pending reboot evidence and changed plans

Before a reboot command can leave, its durable host checkpoint records
`reboot_issued=true` and `reboot_pending=true`, with the pre-reboot boot ID.
Timeout, cancellation, crash, and reconciliation invalidation preserve that
evidence. A matching kernel on the unchanged boot cannot verify; missing prior
boot identity also remains indeterminate. Only a live nonempty changed boot ID
clears pending status. Legacy ambiguous evidence fails closed. Ordinary failed
read-only reconciliation carries no pending reboot and remains recoverable.

The immutable intent and digest include the reboot policy, which appears in the
approval presentation. Current ticket policy and the reboot tool argument must
match that approved policy. Deterministic approval recognizes an exact affirmative
whole reply; qualifications and extra lines do not grant approval. Structured
approval remains supported.

A completed nonpending transition can be superseded after a changed plan spec or
policy, using its observed record version. The server archives the old transition
in `previous_transitions`. An unresolved old reboot blocks new intents and earlier
install/default actions; changing the plan cannot discard it. Revert to the old
plan binding to reconcile such pending work before approving a new binding.

Self protection also attempts an unconnected UDP bind to each literal target
address at an ephemeral port, then immediately closes the socket. This sends no
packets and detects local interface addresses absent from hostname DNS. Only
`EADDRNOTAVAIL` establishes a nonlocal address; other errors refuse the target.
Nonlocal-bind kernel settings can cause conservative refusals. IPv6 scope IDs
are retained.
