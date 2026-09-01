# Adding a Benchmark Harness

Adding a harness is a contract exercise. Harness-specific knowledge belongs
in a `SkillProvider` and its `skills/<harness>/` documents; agent prompts,
dispatcher logic, and the state machine should not need harness branches.

## Provider contract

Subclass `providers.skills.base.SkillProvider`. These four methods are
required:

```python
async def list_benchmarks(self) -> list[BenchmarkSuite]: ...
async def get_benchmark(self, name: str) -> BenchmarkSuite | None: ...
async def resolve_benchmark(self, requirements: dict[str, Any]) -> str | None: ...
async def generate_runfile(
    self, benchmark: str, params: dict[str, Any]
) -> RunfileTemplate: ...
```

`BenchmarkSuite` must provide a stable name, description, supported parameter
schema, endpoint types (`remotehosts` and/or `kube`), roles, minimum host
count, and a `harness` value equal to the registration key. Set `visibility`
when the suite is private or has a public/private variant.

The optional methods are the rest of the public contract:

| Method | Implement when |
|---|---|
| `get_default_config()` | Always, for provisioning/execution defaults |
| `get_private_config(suite, key)` | The provider has private overrides (normally inherited) |
| `get_runfile_schema()` | The harness has a schema that can reject malformed run files |
| `get_benchmark_params(benchmark)` | Parameters differ by benchmark |
| `get_tool_params(tool)` | A tool has valid flags, presets, or subtools |
| `get_tool_metadata(tool)` | The tool exposes descriptions, units, or capability metadata |
| `list_tools()` | The harness has discoverable profiling/measurement tools |
| `get_example_runfile(benchmark, endpoint_type)` | An example materially improves generation |
| `validate_runfile(run_file, harness)` | Validation can catch errors before execution |

Use `get_tool_params` before constructing a tool invocation when the tool's
accepted values are not static or are supplied by a checked-out repository.
Use `get_tool_metadata` for human/LLM-facing semantics; `list_tools` is the
discovery gate. Do not invent flags from memory. `get_example_runfile` must
return the actual endpoint shape, and `validate_runfile` should return
`{"valid": bool, "errors": list[str]}`.

## A current provider shape

`providers/skills/k8s_netperf.py` is a useful reference because it supplies
benchmark parameters, defaults, a schema, an example, and validation:

```python
class MyHarnessSkillProvider(SkillProvider):
    async def get_default_config(self) -> dict[str, Any]:
        return {
            "provisioning": {
                "install_method": "binary_download",
                "controller_only_install": True,
                "install_command": "...",
                "install_target_path": "/usr/local/bin",
                "verify_command": "myharness --help",
                "on_existing_install": "skip",
            },
            "execution": {
                "controller_required": True,
                "run_command": "myharness",
                "endpoint_type": "remotehosts",
                "run_file_format": "json",
            },
        }

    async def get_benchmark_params(self, benchmark: str) -> dict[str, Any] | None: ...
    async def get_runfile_schema(self) -> dict[str, Any] | None: ...
    async def get_example_runfile(
        self, benchmark: str, endpoint_type: str = "remotehosts"
    ) -> dict[str, Any] | None: ...
    async def validate_runfile(
        self, run_file: dict[str, Any], harness: str | None = None
    ) -> dict[str, Any]: ...
```

The default config is merged with
`~/.agentic-perf/private-skills/<harness>.json`; keep organization URLs,
registries, credentials, supported OS constraints, and platform contracts in
that private config. Provisioning checks `platform_contract` before install;
document supported OS/repos/packages there rather than hiding them in a
prompt.

## Provisioning, execution, results, and review

`provisioning` describes how to install and verify the harness on the
controller. `execution` identifies the command, endpoint type, user, and
run-file format. The benchmark agent validates and executes the generated
file, then stores run status, run ID, and the file used on the ticket.

Result retrieval is harness-specific. The review agent first calls
`get_review_config(harness_name)`, which reads the private `review` contract:
result location/API, retrieval commands, parsing guidance, and interpretation
notes. Provide that configuration and document expected success/failure
artifacts; do not assume every harness has `result-summary.json`.

Results and intermediate files should be copied into the ticket workspace
(`workspace://...`) or the persistent artifact directory. The workspace
manager supports JSON/jq queries, grep, bounded reads, and chart generation.
Harness-specific chart adapters are optional: implement a
`BaseChartAdapter` (`can_handle` and `build_chart` returning `ChartSpec`) and
register it with the chart registry when generic JSON/CSV handling is not
enough. Review submissions should reference generated chart files rather than
embedding large numeric arrays.

## Availability and platform compatibility

`list_benchmarks` is the availability declaration: return only suites the
provider can actually run in the current checkout. `endpoint_types`, `roles`,
and `min_hosts` drive resource planning. `find_capable_harnesses` reports
these fields and supported parameters when multiple providers can satisfy a
request. Resolve only names discovered by the provider.

Put OS, repository, package, controller, Kubernetes, architecture, and
privilege requirements in merged private config (`constraints` and
`platform_contract`). The provisioning agent treats OS/repository mismatches
as hard failures and missing packages as installable warnings. Keep
availability checks deterministic and provider-backed; do not claim a cloud,
cluster, board, or tool is available merely because it is documented.

## Registration and synchronization

Import and register the provider in `orchestrator/main.py` (the current
registry constructs `CrucibleSkillProvider` first and adds optional providers
to the `harnesses` mapping):

```python
from providers.skills.myharness import MyHarnessSkillProvider

harnesses["myharness"] = MyHarnessSkillProvider()
skills = MultiHarnessSkillProvider(
    harnesses, PrivateSkillProvider(), default_harness="crucible"
)
```

Add the harness repository to the repo-cache defaults when provisioning needs
to clone it, and add a `sample-private-skills` template if it has private
URLs, credentials, or platform constraints.

Synchronize all of these surfaces:

- `skills/<harness>/workloads.md`, `config-guide.md`, and result/review docs;
- provider benchmark names, parameters, endpoints, tool metadata, and schemas;
- `capabilities`/agent-facing documentation and any required prompt context;
- provisioning `platform_contract`, private constraints, and `review` config;
- workspace/artifact paths and chart adapters;
- tests and the public README/docs index.

Coordinate setup and availability behavior with [#331](https://github.com/atheurer/agentic-perf/issues/331),
and use [#651](https://github.com/atheurer/agentic-perf/issues/651) as the
parity check: every provider must expose equivalent discovery, parameter,
execution, result, and review information where its harness supports it.

## Tests and checklist

At minimum test benchmark listing/fields, exact and unknown resolution,
run-file generation, schema validation, endpoint examples, tool discovery and
delegation, result/review configuration, platform constraints, and chart
handling where implemented. Run the focused provider tests and the full suite.

```bash
python3 -m pytest tests/test_myharness_provider.py -v
python3 -m pytest tests/ -v
```

- [ ] Four required methods implemented; every suite has matching `harness`.
- [ ] Defaults cover provisioning, verification, execution, endpoint, and format.
- [ ] Parameters, schema, examples, validation, tools, metadata, and retrieval are synchronized.
- [ ] Availability, roles, host count, visibility, OS/platform contract, and constraints are accurate.
- [ ] Workspace/artifact persistence and optional chart adapter are covered.
- [ ] `skills/` workload/config/result docs and capabilities are updated.
- [ ] Registration, repo cache, private template, prompts (only if truly shared), and README/docs are updated.
- [ ] Tests cover the provider and parity with #651; setup/availability follows #331.
