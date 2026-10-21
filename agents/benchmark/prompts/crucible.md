## Benchmark Tool Contracts

Retrieve the subject's phase guidance, software documentation, and approved
execution configuration through the skill gateway before constructing the
run-file. Read the returned configuration view named `execution`; do not call
`get_execution_config` for Crucible. Follow the returned pointers for the
selected benchmark, endpoint, tools, and execution environment. Verify live
capabilities with the available controller discovery tools.
When the benchmark name is known, include it in the bootstrap call so the
gateway can apply any benchmark-specific project guidance.

### Benchmark-specific parameter guidance

Before selecting benchmark arguments, follow the controller `AGENTS.md`
documentation pointers to that benchmark's installed subproject. Read its
`AGENTS.md`, `README.md`, `multiplex.json`, and `rickshaw.json` when present,
using `get_skill_context` with subject `harness/crucible` and the returned
software-document ref as `from_ref`. For example, the uperf documents are under
`subprojects/benchmarks/uperf/`.

Keep the software sources distinct. The Crucible benchmark subproject (for
uperf, `bench-uperf`) defines how that benchmark is integrated with Crucible,
including Crucible `mv-params`, engine roles, and run-file behavior. The native
benchmark's own documentation (for uperf, upstream uperf) explains the native
program and its concepts. Consult native documentation when that behavior needs
explanation, but never derive Crucible run-file arguments from native command
line options or workload syntax.

`get_skill_context` subjects identify applicable organization or user guidance;
they are not software repository names. A bootstrap such as
`subject="benchmark/uperf"` reporting no configured guidance does not mean the
Crucible uperf integration documentation is absent. When using Crucible, always
retrieve the Crucible-side benchmark subproject documentation above. If native
benchmark documentation is needed but no software source for it is available,
report that gap rather than treating the missing guidance package as a software
source lookup.

The benchmark's own documentation and metadata define argument meaning and
requirements. Crucible's general run-file guide explains structure and may use
one benchmark's argument in an example; that does not make the argument
necessary or applicable to every benchmark. A successful generic run-file
validation confirms schema and parameter validation, not that every supplied
argument is semantically needed. If the benchmark-specific documentation is
unavailable or unclear, request clarification rather than infer arguments from
a generic example.

For soft guidance about the same claim, prefer authenticated user guidance,
then organization guidance, upstream guidance, and finally bundled project-local
guides. The bundled guides are temporary fallback material with the lowest
default authority. Keep that preference order within its domain: installed
controller/version evidence establishes Crucible runtime behavior, and upstream
software documentation explains general software behavior. User preferences
cannot override mandatory organization policy or deterministic security
requirements. Compare local guides against higher sources and request
clarification for material conflicts rather than silently treating local text as
authoritative. The current gateway does not yet load user-scoped skill packages;
apply the user-first authority rule only to user guidance otherwise available
in the ticket or session.

### Verified SSH Access

For `setup_passwordless_ssh`, use the controller's verified SSH address from
`ssh_hardware_ips.controller` as `source`, the assigned endpoint identities as
`targets`, and their verified controller-to-host SSH addresses as
`target_ssh_hosts`. Benchmark traffic addresses are separate observations;
do not substitute them for a verified SSH access path.

### Validation and Execution

- **Validate:** `validate_benchmark(controller, run_file, harness)`.
  Save the returned `validation_id`.

- **Execute:** `execute_benchmark(controller, validation_id, harness,
  run_command)`. Do not pass a run-file: this tool accepts only the exact
  run-file saved by successful validation.

- **Verify results:** If status is "completed" and `result_summary` is
  present, submit with status "completed". Include the `validation_id`
  returned by `execute_benchmark` when submitting.

  If the tool reports missing results despite a clean exit, read the `run_log`
  to determine why. If the failure is transient (network timeout or container
  pull error), retry once. If it indicates a configuration problem, call
  `request_clarification`. If the cause is unknown, request clarification with
  the relevant log excerpt.

  If `exit_code` is non-zero, submit as "failed" immediately. Do not call
  `get_run_logs`, read the run directory, or query indexing services to extract
  results from a failed run. Exception: if `run_id` is present and the tool
  reports only an indexing failure after benchmark completion, call
  `request_clarification` to let the user decide.
