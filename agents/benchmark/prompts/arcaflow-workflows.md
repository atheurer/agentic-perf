## Arcaflow Workflow Execution

This ticket uses an Arcaflow workflow executed through the
Arcaflow MCP engine. The engine runs on the orchestrator node
and targets the remote system directly — no external controller
is needed.

### Input Discovery and Construction

1. Call `workflow_load` with the workflow source from the
   ticket directives (`workflow_source`, and `workflow_name`
   when present) to discover the workflow schema.
2. Use `workflow_input_build` to construct inputs from the
   schema and the ticket's requested parameters.
3. Call `workflow_input_validate` to verify correctness;
   fix any reported errors before proceeding.

### Execution

4. Call `execute_arcaflow_workflow` with:
   - `workflow_source`: the workflow source URL
   - `workflow_name`: workflow name/path (if applicable)
   - `input`: the validated input parameters as a dict

   The tool loads the workflow, exports the input, runs the
   Arcaflow engine, and polls until completion. It returns
   the workflow output for `submit_benchmark_result`.
