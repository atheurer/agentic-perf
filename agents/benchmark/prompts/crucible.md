## Crucible Benchmark Execution

This benchmark uses the Crucible harness with a **dedicated controller
host** that runs the Crucible framework. The orchestrator relays
commands to the controller, which SSHes to target hosts to execute
workloads.

### Controller Context

First call the benchmark-independent bootstrap operation
`get_crucible_benchmark_context(operation="bootstrap")`. Read the
returned `AGENTS.md` document, then follow its documentation pointers
iteratively. Pass controller-relative paths exactly as documented to
`get_crucible_benchmark_context(operation="read", path=<path>)`. Do
not ask the gateway to interpret Crucible repository metadata or
invent a namespace. Use `operation="search"` with a literal or regex
`query` to discover candidate controller-relative paths when AGENTS.md
does not identify the needed file; search returns paths, snippets, and
`size_bytes`, so read selected files separately. Search queries are
interpreted as regular expressions; whitespace is literal, not an
AND/OR separator. For independent alternatives, use `|`, such as
`ethtool|multiplex\\.json|flow steering`.

Reads return `document.content` with at most 16384 bytes by default;
use `max_bytes` to choose a smaller page, then repeat the same read
with `offset_bytes` set to `next_offset_bytes` until it is null.

Never construct or pass a `workspace://` path and never select a
source explicitly. The context gateway applies the phase-appropriate
authority dynamically. The controller remains authoritative for
installed-runtime facts such as userenv availability and actual
installed behavior.

### SSH and Network Model

For Crucible `remotehosts`, `remotes[].config.host` is the
controller-to-remote control-plane SSH address. It is independent from
benchmark data-plane addressing: select the benchmark interface with
`ifname` and its discovered test address according to benchmark
guidance; never infer one address from the other.

**SSH Key Setup** — Call `setup_passwordless_ssh` with:
- source: the controller's SSH-reachable IP (`ssh_hardware_ips.controller`)
- targets: the endpoint identities from the resource assignment
- target_ssh_hosts: the endpoint addresses verified for
  controller-to-host SSH

This generates a key on the controller and injects it through the
verified access path. Do not substitute benchmark dataplane addresses
for the SSH addresses.

### Run-File Construction

a. **MANDATORY — Bootstrap and discover context FIRST.** Call
   `get_crucible_benchmark_context(operation="bootstrap")`. Read the
   returned `AGENTS.md`, then follow its documentation pointers
   iteratively with `operation="read"`. When it identifies the
   `subprojects/benchmarks/` layout, use the known benchmark name
   to request `subprojects/benchmarks/<benchmark>/AGENTS.md`, then
   metadata files — including `multiplex.json` and `rickshaw.json`
   when present — through the context gateway. Use
   `operation="search"` to discover candidate paths.

b. Read the controller-sourced run-file schema and relevant tool
   metadata through `get_crucible_benchmark_context`. This includes
   the schema itself, tool definitions, and benchmark metadata. Do
   not use legacy schema, parameter, or example-runfile lookup tools.

c. Read the harness's run-file documentation for format details.

f. **Choose remote hosts from verified SSH reachability.** For
   `remotehosts`, each `remotes[].config.host` is the address the
   Crucible controller uses for SSH, file transfer, and container
   orchestration. Use the hostname or IP address that
   `verify_ssh_path` confirms from the controller. Do not infer
   this address from the benchmark interface or dataplane IP.

### Validation and Execution

- **Validate:** `validate_benchmark(controller, run_file, harness)`.
  This performs the controller-side `crucible validate` checks
  without deploying or running anything. Save `validation_id`.

- **Execute:** `execute_benchmark(controller, validation_id, harness,
  run_command)`. Do not pass the run-file: Crucible execution accepts
  only the exact run-file saved by the successful validation.

- **Verify results:** If status is "completed" AND `result_summary`
  is present, submit with status "completed". Include the
  `validation_id` returned by `execute_benchmark` when submitting.

  If `result-summary.json` is missing, the run did not produce
  usable results even though crucible exited cleanly. Read the
  `run_log` to understand why. Based on the log:
  - If the failure is transient (network timeout, container pull
    error), retry once.
  - If the failure indicates a configuration problem (bad
    parameters, missing endpoints, schema errors), call
    `request_clarification` to escalate.
  - If you cannot determine the cause, call
    `request_clarification` with the relevant log excerpt.

  If exit_code is non-zero, submit as "failed" immediately. Do
  NOT call `get_run_logs`, do NOT attempt to read files from the
  run directory, do NOT query OpenSearch. There are no results to
  extract from a failed run.
  Exception: if exit_code is non-zero but `run_id` is present and
  the message indicates only the indexing step failed (not the
  benchmark itself), call `request_clarification` to let the user
  decide.

### Common Pitfalls

- For `remotehosts`, use the controller-verified SSH address in each
  remote's `config.host`; do not use a dataplane address unless it
  has independently been verified as the controller's SSH access path.
- `tags` must be an object `{"key": "val"}`, NOT an array
- `ids` values must be strings: `"1"` not `1`
- Do NOT set `controller-ip-address` unless crucible cannot resolve
  it itself. Setting the wrong IP breaks the run.
- `userenv` must be a real userenv name — `"default"` is NOT valid.
  Call `list_controller_userenvs(controller)` and use the controller's
  installed benchmark metadata discovery to determine which userenv.
- `osruntime: podman` needs `host-mounts` for DPDK workloads
- Every benchmark object MUST include `mv-params`
- Tools in `tool-params` use `tool` plus optional
  `params: [{"arg": ..., "val": ...}]`
- `num-samples` belongs in `run-params` (top level), NOT inside a
  benchmark object

### Important Notes

- The controller host runs the benchmark framework. For remotehosts,
  it is NOT an endpoint unless the benchmark has only a "client" role.
  For kube endpoints, workloads run as pods on the controller's K8s
  cluster.
- Endpoints are the target hosts where the actual workload runs.
- If the benchmark needs only 1 host (client role only), use the
  first target host as the endpoint. If no targets exist, the
  controller itself can be the endpoint.
