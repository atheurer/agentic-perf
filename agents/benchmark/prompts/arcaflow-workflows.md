## Arcaflow Workflow Execution (mandatory)

This ticket supplies an Arcaflow workflow. Do not construct a
plugin-image run-file and do not call `execute_benchmark` for
this ticket. Use the configured Arcaflow MCP tools in this
order:

1. Call `workflow_load` for the supplied source (and workflow
   name/path when present — check directives for
   `workflow_source` and `workflow_name`).
2. Use `workflow_input_build` to construct inputs from the
   workflow schema and the ticket's requested parameters.
3. Call `workflow_input_validate`; correct any reported input
   errors before continuing.
4. Call `workflow_input_export` to obtain the immutable input
   payload, then call `workflow_execute` with the loaded workflow
   and exported input.
5. Use the workflow status/output tools until execution reaches
   a terminal state, then submit the result with its workflow run
   ID.
