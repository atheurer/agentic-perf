## Arcaflow Workflow Execution

This ticket uses an Arcaflow workflow executed through the
Arcaflow MCP engine. Follow this sequence:

1. Call `workflow_load` with the workflow source from the
   ticket directives (`workflow_source`, and `workflow_name`
   when present).
2. Use `workflow_input_build` to construct inputs from the
   workflow schema and the ticket's requested parameters.
3. Call `workflow_input_validate`; correct any reported input
   errors before continuing.
4. Call `workflow_input_export` to obtain the immutable input
   payload, then call `workflow_execute` with the loaded
   workflow and exported input.
5. Monitor with `workflow_execution_status` until execution
   reaches a terminal state, then submit the result with its
   workflow run ID.
