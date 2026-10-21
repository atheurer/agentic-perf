## Arcaflow Plugin Execution

This ticket uses a direct Arcaflow plugin container. The plugin
runs via `podman run` on the target host.

1. Call `get_plugin_schema` with the plugin image to discover
   available steps and input parameters.
   If the response says `spilled_to_workspace`, use
   `jq_file_from_workspace` on its `file_ref` to inspect only the selected
   step's input schema (for example, `.schema.steps.workload.input`). Do not
   guess from a truncated preview or try to emit the entire schema.
2. Build the input from that schema and the ticket parameters. For the
   stress-ng plugin, the `workload` input has a required `timeout` and a
   required `stressors` array. Each CPU entry looks like
   `{"stressor":"cpu","workers":1}`; `stressor` is inside an array item,
   not a top-level input field.
3. Call `execute_arcaflow_plugin` with:
   - `plugin_image`: full container image ref
   - `plugin_step`: step name (e.g. 'sysbenchcpu', 'uperf')
   - `input`: plugin input parameters as a dict

4. Call `submit_benchmark_result` after execution. Put the exact useful
   measurements from `result_summary` in `notes`, including units and sample
   counts, and include any execution errors. Keep the summary concise; the
   review agent receives this field because raw tool output is not retained
   as a retrievable artifact.

Community plugins from quay.io/arcalot are typically multi-arch
(amd64 + arm64). Do NOT manually install workload binaries on
the host — the plugin container is self-contained.
