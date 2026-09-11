# Arcaflow Workflow Execution

## When to Use

Use the Arcaflow MCP workflow tools when the ticket has a
`workflow_source` directive pointing to a git repo or
workflow file URL. This runs a multi-plugin Arcaflow
workflow on the provisioned host.

For single-plugin benchmarks without a workflow (e.g.,
just fio or stress-ng), use `execute_benchmark` with
the arcaflow-plugins harness instead.

## How It Works

The Arcaflow MCP server manages the workflow engine. You
call MCP tools to load, configure, execute, and monitor
the workflow. The MCP handles engine lifecycle — you
do NOT run the engine directly.

## Workflow Execution Steps

1. **Load the workflow:**
   ```
   workflow_load(
     source={kind: "git", location: "<workflow_source>"},
     selector={path: "<workflow_name>.yaml"}
   )
   ```

2. **Build input** (if parameters are needed):
   ```
   workflow_input_build(
     source=...,
     selector=...,
     values={key: value, ...}
   )
   ```

3. **Validate input:**
   ```
   workflow_input_validate(source=..., input=...)
   ```

4. **Execute the workflow:**
   ```
   workflow_execute(
     source={kind: "git", location: "<workflow_source>"},
     selector={path: "<workflow_name>.yaml"},
     input={...},
     deployer_config={
       deployers: {
         image: {
           deployer_name: "podman",
           podman: {
             path: "/usr/bin/podman",
             connection: {
               host: "ssh://<ssh_user>@<controller_ip>"
             }
           }
         }
       }
     }
   )
   ```
   This returns an `execution_id` immediately.

5. **Poll for completion:**
   ```
   workflow_execution_status(execution_id="...")
   ```
   Repeat until status is "completed" or "failed".

6. **Get results** via `workflow_results_load` if needed.

7. **Submit results** via `submit_benchmark_result`.

## Deployer Config

The deployer config tells the engine how to run plugin
containers on the target host. Build it from the ticket's
assigned hardware:

- `controller_ip`: from `assigned_hardware_ips.controller`
- `ssh_user`: from ticket's `ssh_user` field
- `ssh_key_path`: from ticket's `ssh_key_path` field

Use podman as the deployer for bare-metal hosts.

## Source Resolution

- Git repos (`.git` suffix, github.com, gitlab.com):
  `source.kind = "git"`
- Raw YAML file URLs: `source.kind = "url"`

## Important

- Do NOT run the arcaflow engine directly — use the
  MCP's `workflow_execute` tool
- Do NOT construct workflow YAML — the workflow comes
  from the user's `workflow_source`
- The `workflow_execute` call is async — it returns
  immediately. Poll `workflow_execution_status` for
  completion.
- If `workflow_name` is not specified, check
  `workflow_list` to find available workflows in the
  source
