# Arcaflow Workflow Execution

## When to Use

Use `execute_arcaflow_workflow` when the ticket has a
`workflow_source` directive pointing to a git repo or
workflow file URL. This runs a multi-plugin Arcaflow
workflow on the provisioned host.

For single-plugin benchmarks without a workflow (e.g.,
just fio or stress-ng), use `execute_benchmark` with
the arcaflow-plugins harness instead.

## How It Works

1. The tool clones/downloads the workflow from the source
2. Generates a deployer config for podman-over-SSH to
   the provisioned host
3. Runs the arcaflow engine with the workflow and config
4. Collects structured JSON output and saves artifacts

## Parameters

- `workflow_source`: The git repo URL or raw file URL
  from the ticket's `directives.workflow_source`
- `workflow_name`: Optional — name or path of the specific
  workflow file within the repo. Use when the repo has
  multiple workflows.
- `input_overrides`: Optional dict of workflow input
  parameter overrides
- `timeout_seconds`: Maximum execution time (default 3600)

## Example

For a ticket with:
```json
{
  "directives": {
    "workflow_source": "https://gitlab.com/.../arcaflow-workflow-auto-perf.git",
    "workflow_name": "workflow-fio"
  }
}
```

Call:
```
execute_arcaflow_workflow(
  workflow_source="https://gitlab.com/.../arcaflow-workflow-auto-perf.git",
  workflow_name="workflow-fio"
)
```

## Important

- Do NOT construct run-files or input YAML manually —
  the arcaflow engine handles input from the workflow
  schema
- Do NOT SSH to the host to run plugins directly —
  the engine handles deployment via its deployer config
- The workflow source is the user's responsibility —
  if the URL is wrong or the workflow fails to load,
  report the error clearly
