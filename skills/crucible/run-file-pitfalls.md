# Crucible Run-File Construction

Run-file structure, endpoint semantics, benchmark parameters, and
environment-specific behavior must come from the controller-sourced Crucible
documentation through `get_crucible_benchmark_context`. This local document is
not a source of benchmark or endpoint guidance.

Before approval, construct the run file from the controller-sourced schema and
validate it with `validate_benchmark`. Do not execute an unvalidated run file.

## General structural reminders

- `benchmarks` and `endpoints` are top-level sections.
- Every benchmark entry must include `mv-params` when required by the
  controller-sourced schema.
- Tool arguments use the controller-sourced tool metadata format; do not invent
  additional fields.
- Client/server engines that form a benchmark pair must use the ID pairing
  defined by that benchmark's controller-sourced documentation.

If a run-file detail is not established by the current controller context,
search and read the relevant Crucible or benchmark repository document. Do not
rely on this file for that detail.
