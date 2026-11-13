# Design: LLM-Driven Run-File Generation

## Problem

The current benchmark agent uses a "telephone game" architecture for run-file creation:

1. User describes what they want in natural language
2. Triage agent extracts `parsed_specs` and `benchmark_suite`
3. Benchmark agent LLM calls `generate_run_file` with high-level params
4. `generate_run_file` handler resolves hostnames, merges defaults
5. `CrucibleSkillProvider.generate_runfile()` loads an example template and patches it

Each hop loses fidelity. The template-based generator (`providers/skills/crucible.py:137-202`) either loads a canned example and substitutes endpoints, or builds a minimal skeleton. It doesn't understand the full richness of run-file parameters — `mv-params` sets, multi-benchmark configs, per-engine settings, tool-params, etc. Every new use case requires code changes to the generator.

Meanwhile, the LLM is already good at generating structured JSON from natural language — it just needs the right reference material and validation feedback.

## Proposed Architecture

Remove `generate_run_file` as an abstraction layer. The LLM constructs the complete run.json directly, validated by schema and blockbreaker before execution.

```
User → natural language → Benchmark LLM → complete run.json
→ validate_benchmark (controller MCP validate_run) → present to user
→ execute_benchmark (controller MCP prepare_run + start_run)
```

### New Tools for the Benchmark Agent

**Replace `generate_run_file` with:**

1. **`get_runfile_schema`** — returns the run-file JSON schema
   - Source: `/opt/crucible/subprojects/core/rickshaw/schema/run-file.json`
   - The LLM uses this to understand what's structurally valid

2. **`get_benchmark_params`** — returns the multiplex.json for a specific benchmark
   - Source: `/opt/crucible/subprojects/benchmarks/{name}/multiplex.json`
   - Contains `presets` (named parameter sets) and `validations` (allowed values per arg)
   - This is the definitive source for what `mv-params` args are valid and what values they accept

3. **`get_example_runfile`** — returns an example run-file for a benchmark
   - Source: `/opt/crucible/subprojects/docs/examples/runfile/{name}/*.json`
   - Gives the LLM a concrete pattern to follow
   - Multiple examples may exist per benchmark (remotehosts, k8s, etc.)

4. **`present_runfile_for_approval`** — shows the user the generated run.json and asks for approval
   - Uses the existing `request_clarification` pattern
   - User can approve, modify, or reject
   - Returns the (potentially user-modified) run-file

5. **`validate_runfile`** (keep existing) — schema validation before execution
   - Already exists in crucible.py:215-233
   - Called automatically by execute_benchmark, but also available as a standalone tool
     so the LLM can check its work before presenting to the user

6. **`execute_benchmark`** (keep existing) — plans and starts the exact
   validated document through controller MCP contract v3
   - Controller-side schema and semantic validation happen through
     `validate_run` and `prepare_run`

### Keep `generate_run_file` as a Fallback

Don't delete `generate_run_file` immediately. Keep it as an optional tool the LLM can call if it wants a starting point. But the prompt should guide the LLM to construct the run-file directly when it has enough information.

### Updated Benchmark Agent Prompt

The prompt should guide the LLM through:

1. Call `get_execution_config(harness)` — learn harness requirements
2. Call `get_benchmark_params(benchmark)` — learn valid parameters and presets
3. Optionally call `get_example_runfile(benchmark)` — see a concrete example
4. Construct the complete run.json, resolving hostnames to IPs
5. Call `validate_runfile(run_file)` — check schema compliance
6. If validation fails, fix the run-file and re-validate
7. Call `present_runfile_for_approval(run_file)` — user reviews
8. Call `execute_benchmark(controller, validation_id)` — send the exact
   validated document to controller MCP

The prompt should also teach the LLM:
- The endpoint structure (remotehosts type, remotes array, engines with roles)
- How `mv-params` work (global-options with named groups, sets that include groups)
- How `ids` work (instance numbering: "1", "1-4", "1+3+5")
- Common pitfalls (tags must be `{string: string}` objects, not arrays)

### Clarification Flow

The LLM should ask the user to clarify when:
- The benchmark has multiple valid configurations and the request is ambiguous
  (e.g., "test network" — TCP or UDP? stream or request-response?)
- Hardware-specific parameters are needed but not provided
  (e.g., trafficgen needs PCI device addresses, server-devices)
- The request implies parameters the LLM isn't confident about
  (e.g., "run for a long time" — what's the actual duration?)

## Key Files and Their Roles

### Run-file Structure Reference

**Top-level keys** (from schema, `additionalProperties: false`):
- `benchmarks` (required, array, minItems: 1)
- `endpoints` (optional, array of endpoint objects)
- `tags` (optional, `{string: string}`)
- `tool-params` (optional, array of tool config objects)
- `run-params` (optional: `num-samples`, `max-sample-failures`, `test-order`, `name`, `email`)

**Benchmark object** (`additionalProperties: false`):
- `name` (required, string) — benchmark name matching subproject
- `ids` (required) — instance numbering: `"1"`, `"1-4"`, `"1+3+5"`, `[1, "2-4"]`
- `mv-params` (required) — multivariate parameters, either object or array

**Endpoint object** (remotehosts type):
```json
{
  "type": "remotehosts",
  "settings": {"user": "root", "userenv": "alma8"},
  "remotes": [
    {
      "engines": [{"role": "client", "ids": [1]}],
      "config": {
        "host": "10.1.2.3",
        "settings": {"osruntime": "podman", "controller-ip-address": "10.1.2.4"}
      }
    }
  ]
}
```

**mv-params structure** (the most complex part):
```json
{
  "global-options": [
    {
      "name": "my-globals",
      "params": [
        {"arg": "protocol", "vals": ["tcp"], "role": "client"},
        {"arg": "duration", "vals": ["120"], "role": "client"}
      ]
    }
  ],
  "sets": [
    {
      "include": "my-globals",
      "params": [
        {"arg": "test-type", "vals": ["stream"], "role": "client"},
        {"arg": "nthreads", "vals": ["1", "4", "8"], "role": "client"}
      ]
    }
  ]
}
```

### Per-Benchmark Parameter Definitions

Each benchmark's `multiplex.json` defines valid parameters:

**Location:** `/opt/crucible/subprojects/benchmarks/{name}/multiplex.json`

**Structure:**
```json
{
  "presets": {
    "basic": [
      {"arg": "test-type", "vals": ["stream"]},
      {"arg": "protocol", "vals": ["tcp"]}
    ]
  },
  "validations": {
    "test-types": {
      "description": "all possible test-types",
      "args": ["test-type"],
      "vals": "^stream$|^crr$|^rr$|^ping-pong$"
    }
  }
}
```

**Available benchmarks with multiplex.json:**
- `uperf` — network throughput/latency (TCP/UDP stream, request-response)
- `fio` — storage I/O
- `trafficgen` — DPDK packet forwarding (TRex + testpmd/bridge/router)
- `iperf` — network bandwidth
- `oslat` / `cyclictest` / `timerlat` / `hwlatdetect` / `hwnoise` / `osnoise` — latency/jitter
- `ilab` — InstructLab AI training
- `pytorch` — ML inference
- `sleep` — no-op (testing framework itself)

### Example Run-Files

**Location:** `/opt/crucible/subprojects/docs/examples/runfile/{name}/`

Available examples:
- `fio/fio.json` — storage benchmark with detailed mv-params
- `trafficgen/trafficgen-remotehosts-runfile.json` — DPDK packet gen with PCI devices, flow config
- `uperf/run-uperf.json` — network benchmark (note: this file has a malformed endpoints section)
- `oslat/oslat-remotehost-runfile.json`, `oslat/oslat-k8s-runfile.json`
- `timerlat/`, `hwlatdetect/`, `hwnoise/`, `osnoise/`, `sleep/`, `multibench/`, `ilab/`

### Run-File Schema

**Location:** `/opt/crucible/subprojects/core/rickshaw/schema/run-file.json`

### Blockbreaker

**Location:** `/opt/crucible/subprojects/core/rickshaw/util/blockbreaker.py`

Blockbreaker is a **post-generation** utility invoked by Crucible's controller
execution pipeline. It:
1. Validates the run-file against the JSON schema
2. Expands the run-file into CLI argument streams for rickshaw-run
3. Converts `benchmarks`, `endpoints`, `tags`, `run-params` into positional/flag arguments

It does NOT generate run-files — it consumes them. Validation errors from
blockbreaker surface through MCP `validate_run` and `prepare_run` failures.

### Current Code to Modify

| File | Current Role | Change |
|------|-------------|--------|
| `agents/benchmark/mcp_server.py` | Tool definitions + handlers | Add `get_runfile_schema`, `get_benchmark_params`, `get_example_runfile`, `present_runfile_for_approval`. Keep `generate_run_file` as optional fallback. |
| `agents/benchmark/prompts.py` | Agent instructions | Rewrite to guide LLM through direct run-file construction |
| `providers/skills/crucible.py` | `generate_runfile` + `validate_runfile` | Add methods to load multiplex.json and schema. Keep `generate_runfile` for backward compat. |
| `providers/skills/base.py` | Abstract interface | Add `get_benchmark_params()` and `get_runfile_schema()` to interface |

### Existing Safeguards to Preserve

1. **Schema validation** before execution (`execute_benchmark` line 354-364)
2. **Controller semantic validation** through MCP `validate_run` and
   `prepare_run` (catches errors beyond schema)
3. **Run-file stash** (`_last_generated_runfile`) — may no longer be needed if the LLM constructs directly, but keep until the transition is complete
4. **Hostname → IP resolution** — move into a utility tool the LLM can call explicitly, or keep as part of execute_benchmark

## Known Pitfalls

From our testing history (see MEMORY.md):
- `tags` must be `{"name": "val"}` (object), NOT `[{"name":"x","val":"y"}]` (array)
- Use IP addresses, not hostnames (IPv6 link-local timeout in paramiko)
- `controller-ip-address` must be set when controller is also an endpoint
- `userenv` should be `alma8` for trafficgen (not `default` or `rhubi9`)
- `osruntime: podman` needs `host-mounts` for DPDK (`/dev/hugepages`)
- `ids` format is strict: `"1"` not `1` for string form

## Testing Strategy

1. **Unit tests:** LLM-generated run-files pass schema validation
2. **Integration test:** Generate a run-file for each major benchmark (uperf, fio, trafficgen),
   validate with blockbreaker on a controller host
3. **E2E test:** Full ticket lifecycle where the LLM constructs the run-file from a natural
   language request, user approves, and the benchmark executes successfully

## Migration Path

1. Add the new tools (`get_runfile_schema`, `get_benchmark_params`, `get_example_runfile`)
2. Update the prompt to guide LLM toward direct construction
3. Keep `generate_run_file` as a fallback tool
4. Run side-by-side comparison: LLM-generated vs template-generated run-files
5. Once confidence is high, deprecate `generate_run_file`
