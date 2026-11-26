## Benchmark Tool Contracts

Retrieve the subject's phase guidance and software documentation through the
skill gateway before constructing the run-file. Follow the returned pointers
for the selected benchmark, endpoint, tools, and execution environment. Use
`get_execution_config` for the approved execution settings view; verify runtime
capabilities with the available controller discovery tools. Crucible gateway
responses also include the provider-owned `runfile_contract`; satisfy its
required top-level fields, including `tags` as `{}` when no tags apply.

### Benchmark-specific parameter guidance

Use the controller MCP capabilities for benchmark context. Do not depend on
installed subproject file paths, and do not treat a failed document read as a
blocking condition.

- **Discover** through `get_skill_context(subject="harness/crucible")`. The
  bootstrap reports applicable documentation entrypoints and metadata
  selectors. Read the refs it reports:
  - `benchmarks` — installed benchmark inventory;
  - `benchmark/<name>` — that benchmark's description, CDM/output metadata,
    and `parameter_validation` rules (the `describe_benchmark` contract);
  - `endpoints` — installed endpoint implementations and their schemas;
  - `tools` and `tools/<tool>` — installed tool metadata.
- **Structure** — `docs/run-files` is the structural reference for the run
  file: top-level `benchmarks`, `endpoints`, `tool-params`, `run-params`, and
  `tags`, plus the `mv-params` field rules including `role`, `id`, and `ids`.
  Read `docs/endpoints` for endpoint settings and `docs/benchmark-execution`
  for execution behavior.
- **Parameter values** — take benchmark arguments and roles from the ticket's
  explicit parameters and choose only values that `parameter_validation`
  accepts. When `parameter_validation.complete` is false, treat the rules as a
  partial guide. Named presets are optional and are not required to author a
  run file; parameters that are not supplied keep their preset defaults
  automatically.
- **Parameter roles** — `role` is optional. Omit it when a parameter applies
  to every engine, and set it only when the documentation or ticket identifies
  a specific role.
- **Tool parameters** — `docs/tool-collection` documents the default tools and
  their common arguments. Omit `tool-params` to use the default set.
- **Required fields** — the gateway-provided `runfile_contract` is
  authoritative for agentic-perf's required top-level fields, including
  `tags` as `{}` when no tags apply.

The Crucible benchmark subproject (for uperf, `bench-uperf`) defines how that
benchmark is integrated with Crucible, including Crucible `mv-params`, engine
roles, and run-file behavior. The native benchmark's own documentation (for
uperf, upstream uperf) explains the native program and its concepts. Consult
native documentation when that behavior needs explanation, but never derive
Crucible run-file arguments from native command line options or workload
syntax.

`get_skill_context` subjects identify applicable organization or user guidance;
they are not software repository names. A bootstrap such as
`subject="benchmark/uperf"` reporting no configured guidance does not mean the
Crucible uperf integration documentation is absent. When using Crucible,
retrieve benchmark facts through the refs above. If native benchmark
documentation is needed but no software source for it is available, report
that gap rather than treating the missing guidance package as a software
source lookup.

The benchmark's own documentation and metadata define argument meaning and
requirements. Crucible's general run-file guide explains structure and may use
one benchmark's argument in an example; that does not make the argument
necessary or applicable to every benchmark. A successful generic run-file
validation confirms schema and parameter validation, not that every supplied
argument is semantically needed. Prefer the discovered benchmark metadata and
the ticket over a generic example.

Do not read a subproject's `AGENTS.md`, `CLAUDE.md`, or `rickshaw.json` to
decide arguments, roles, or structure: those are maintainer and integration
files, not run-file authoring sources. A subproject `README.md` may be read
when the gateway serves it, but only for usage notes that `docs/run-files`,
`docs/endpoints`, and the benchmark metadata do not already cover.

If any context read fails, it is non-blocking. Record which ref or document
was unavailable, continue with the context that is available, and do not pause
the ticket for missing documentation. Call `request_clarification` only when
the ticket itself lacks benchmark, endpoint, or parameter information needed
to construct the run file.

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
