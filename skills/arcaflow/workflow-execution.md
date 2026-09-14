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
     deployer_config={}
   )
   ```
   This returns an `execution_id` immediately.

   **Do NOT construct deployer_config.** Pass an empty
   object — the system injects the correct deployer
   configuration deterministically from the ticket's
   assigned hardware (SSH user, IP, key). The engine
   uses podman-over-SSH to run plugin containers on
   the provisioned board.

5. **Poll for completion:**
   ```
   workflow_execution_status(execution_id="...")
   ```
   Repeat until status is "completed" or "failed".

6. **Get results** via `workflow_results_load` if needed.

7. **Submit results** via `submit_benchmark_result`.

## Deployer Config

The deployer config is **code-enforced** — you do not
need to build it. The system automatically:

- Reads the target IP from `assigned_hardware_ips.controller`
- Reads `ssh_user` and `ssh_key_path` from the ticket
- Sets up a podman SSH connection to the target board
- Injects the correct deployer config into every
  `workflow_execute` call

This prevents format errors and ensures plugins always
run on the provisioned board, not locally.

## Source Resolution

- Git repos (`.git` suffix, github.com, gitlab.com):
  `source.kind = "git"`
- Raw YAML file URLs: `source.kind = "url"`

## Important

- Do NOT run the arcaflow engine directly — use the
  MCP's `workflow_execute` tool
- Do NOT construct workflow YAML — the workflow comes
  from the user's `workflow_source`
- Do NOT construct `deployer_config` — it is injected
  automatically from ticket data
- The `workflow_execute` call is async — it returns
  immediately. Poll `workflow_execution_status` for
  completion.
- If `workflow_name` is not specified, check
  `workflow_list` to find available workflows in the
  source
