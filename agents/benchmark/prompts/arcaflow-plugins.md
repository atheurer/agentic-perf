## Arcaflow Plugin Execution

This ticket uses a direct Arcaflow plugin container. The plugin
runs via `podman run` on the target host.

1. Call `get_plugin_schema` with the plugin image to discover
   available steps and input parameters.
2. Build the input based on the schema and ticket parameters.
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
