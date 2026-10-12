## Kube-burner Run-file Validation and Approval

- Read applicable kube-burner guidance through `get_skill_context` and use
  `get_runfile_schema` or `get_example_runfile` when available to construct
  the run-file.
- Do not call `validate_benchmark` for kube-burner. Agentic-perf's
  controller-side `validate_benchmark` tool is currently supported only for
  Crucible.
- Kube-burner's documented CLI has no separate, validation-only command.
  `kube-burner init` launches a benchmark; do not run it as a preflight or
  validation step.
- `execute_benchmark` accepts the complete kube-burner run-file directly.
  Agentic-perf performs basic structural checks on it before execution, but
  those checks are not equivalent to kube-burner's full config parsing or
  validation. The benchmark can still fail when kube-burner processes it.
- Include a local indexer in `config.metricsEndpoints`, with a relative
  `metricsDirectory`, so kube-burner writes measurement JSON for agentic-perf
  to return. Do not assume results are saved automatically when no indexer is
  configured.
- After execution, inspect `output`, `result_summary`, `result_directories`,
  and `result_files`. If logs are needed, call `get_run_logs` with the returned
  `run_id` and `harness="kube-burner"`. Report the command exit status
  separately from whether measurement artifacts were collected; a successful
  command with missing result files is a collection problem, not a non-zero
  kube-burner exit.
- If `user_pre_run_approval` is true (the default), and there is no immutable
  validation ID, call `request_clarification` with the complete exact
  run-file, the planned execution command/config, and the limits of the
  structural checks. Ask whether to proceed or stop, then wait for the reply.
  Do not claim that `present_runfile_for_approval` or digest-bound approval
  was used.
- After the user authorizes execution, pass the same complete run-file to
  `execute_benchmark`. Do not change it after asking for approval; if any
  changes are needed, show the revised run-file and request authorization
  again. If `user_pre_run_approval` is false, execute the constructed
  run-file directly.
