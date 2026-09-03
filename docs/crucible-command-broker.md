# Crucible command-broker MCP tool

The infrastructure MCP server exposes `run_crucible_command` for a small,
explicit set of read-only controller discovery operations. It is a broker,
not remote shell access: the operation name maps to a complete command in
server code and the `arguments` object is rejected unless it is empty.

Current operations:

| Operation | Controller command |
| --- | --- |
| `benchmark_list` | `crucible benchmark list` |
| `tools_list` | `crucible tools list` |
| `userenvs_list` | `crucible userenvs list` |

The response contains the operation, controller, exit code, stdout, and any
stderr. Unknown operations and non-empty arguments fail before SSH is called.
There is no command string, shell fragment, path, or arbitrary argument in
the input schema. Benchmark execution and mutating operations (`run`,
`update`, `install`, `stop`, and similar) remain separate tools and approval
paths.

The older `list_controller_userenvs` tool remains as a compatibility wrapper
and delegates to `userenvs_list`. New operations should be added only when
their Crucible CLI contract is verified, read-only, schema-defined, and
covered by rejection and dispatch tests.
