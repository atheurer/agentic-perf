# Adding a New Benchmark Harness

This guide walks through adding a new benchmark harness to agentic-perf.
The design philosophy calls this "the acid test" — if the layering is right,
adding a new harness requires no changes to agent code.

In practice, adding a harness involves five deliverables:

1. Skill provider (Python class)
2. Skill documentation (markdown files)
3. Registration in the orchestrator
4. Tests
5. Config and repo references

## 1. Skill Provider

Create a new file at `providers/skills/<harness>.py` that extends
`SkillProvider`. This is the main deliverable — it teaches the system what
your harness can do.

### Required Methods

```python
from providers.skills.base import BenchmarkSuite, RunfileTemplate, SkillProvider

class MyHarnessSkillProvider(SkillProvider):

    async def list_benchmarks(self) -> list[BenchmarkSuite]:
        """Return all benchmarks this harness supports."""
        ...

    async def get_benchmark(self, name: str) -> BenchmarkSuite | None:
        """Return a single benchmark by name, or None."""
        ...

    async def resolve_benchmark(self, requirements: dict) -> str | None:
        """Match natural-language requirements to a benchmark name."""
        ...

    async def generate_runfile(self, benchmark: str, params: dict) -> RunfileTemplate:
        """Produce a run-file template for the benchmark."""
        ...
```

### BenchmarkSuite Fields

Each benchmark is described by a `BenchmarkSuite`:

| Field | Type | Purpose |
|---|---|---|
| `name` | str | Unique identifier (e.g., `"vstorm-containerdisk"`) |
| `description` | str | Human-readable description of what it tests |
| `supported_params` | dict | Parameters the benchmark accepts (name → type/default/description) |
| `endpoint_types` | list[str] | `"remotehosts"`, `"kube"`, or both |
| `roles` | list[str] | Host roles needed (e.g., `["client", "server"]`) |
| `min_hosts` | int | Minimum hosts required |
| `harness` | str | Harness name (must match registration key) |

### Optional Methods

These enable the LLM-driven run-file construction pipeline. Implement them
to get better run-file quality:

```python
    async def get_default_config(self) -> dict:
        """Return provisioning and execution config for this harness.

        This is the public default. Organization-specific overrides go in
        private-skills configs (~/.agentic-perf/private-skills/<harness>.json).
        """
        return {
            "provisioning": {
                "install_method": "git_clone",  # or "package", "script"
                "git_url": "https://github.com/...",
                "install_target_path": "/opt/myharness",
                "verify_command": "/opt/myharness/bin/myharness --version",
                "on_existing_install": "skip",  # skip, update, reinstall
            },
            "execution": {
                "controller_required": True,
                "run_command": "/opt/myharness/bin/run",
                "endpoint_type": "remotehosts",
                "run_file_format": "json",  # or "yaml", "cli_args"
            },
        }

    async def get_runfile_schema(self) -> dict | None:
        """JSON schema for the harness run-file format."""
        ...

    async def get_benchmark_params(self, benchmark: str) -> dict | None:
        """Valid parameters for a specific benchmark."""
        ...

    async def get_example_runfile(self, benchmark: str, endpoint_type: str) -> dict | None:
        """An example run-file the LLM can use as a reference."""
        ...

    async def validate_runfile(self, run_file: dict, harness: str | None = None) -> dict:
        """Validate a run-file. Return {"valid": bool, "errors": [str]}."""
        ...
```

### Keyword Resolution

The `resolve_benchmark` method maps natural language to benchmark names.
The standard pattern uses a keyword map:

```python
KEYWORD_MAP = {
    "network": ["myharness-iperf", "myharness-netperf"],
    "storage": ["myharness-fio"],
    "io": ["myharness-fio"],
    "latency": ["myharness-netperf"],
}

async def resolve_benchmark(self, requirements: dict) -> str | None:
    description = str(requirements.get("description", "")).lower()
    workload_type = str(requirements.get("workload_type", "")).lower()
    search_text = f"{description} {workload_type}"

    scores: dict[str, int] = {}
    for keyword, benchmarks in KEYWORD_MAP.items():
        if keyword in search_text:
            for bench in benchmarks:
                scores[bench] = scores.get(bench, 0) + 1

    if not scores:
        return None
    return max(scores, key=scores.get)
```

### Complete Example

See `providers/skills/vstorm.py` for a clean, self-contained example.
It defines three benchmarks with a keyword map, parameter schemas,
default config, and validation — all in about 280 lines.

## 2. Skill Documentation

Create a directory at `skills/<harness>/` with markdown files that agents
read at runtime. These are the "skills" layer — agents learn about your
harness by reading these docs, not from hardcoded prompts.

Recommended files:

| File | Purpose | Used By |
|---|---|---|
| `workloads.md` | Available benchmarks, what they test, key parameters | Triage, Benchmark |
| `config-guide.md` | Configuration reference, CLI flags, file formats | Benchmark |

Additional files for complex harnesses:

| File | Purpose |
|---|---|
| `run-file-pitfalls.md` | Common mistakes and solutions |
| `endpoints.md` | Endpoint types and configuration |
| `result-format.md` | How to retrieve and interpret results |

Agents discover these via `list_harness_docs` and read them via
`read_harness_doc`. Write them as if the reader is an LLM that needs
to construct a valid configuration — be explicit about formats, required
fields, and common errors.

## 3. Registration

### Orchestrator (`orchestrator/main.py`)

Import and register your provider:

```python
from providers.skills.myharness import MyHarnessSkillProvider

# In poll_loop(), add to the harnesses dict:
harnesses["myharness"] = MyHarnessSkillProvider()
```

### Config (`orchestrator/config.py`)

Add the git repo to the default repos so the repo cache can clone it:

```python
default_repos = {
    # ... existing repos ...
    "myharness": "https://github.com/.../myharness.git",
}
```

### Private Skills Template

Create `sample-private-skills/myharness.json` with organization-specific
overrides:

```json
{
    "provisioning": {
        "container_registry": "registry.example.com",
        "install_flags": "--with-gpu-support"
    }
}
```

Users copy this to `~/.agentic-perf/private-skills/myharness.json` and
customize for their environment.

## 4. Tests

Create `tests/test_myharness_provider.py`:

```python
import pytest
from providers.skills.myharness import MyHarnessSkillProvider

@pytest.fixture
def provider() -> MyHarnessSkillProvider:
    return MyHarnessSkillProvider()

@pytest.mark.asyncio
async def test_list_benchmarks(provider):
    benchmarks = await provider.list_benchmarks()
    assert len(benchmarks) > 0
    for b in benchmarks:
        assert b.harness == "myharness"
        assert b.name
        assert b.description

@pytest.mark.asyncio
async def test_harness_field(provider):
    benchmarks = await provider.list_benchmarks()
    for b in benchmarks:
        assert b.harness == "myharness"

@pytest.mark.asyncio
async def test_resolve_benchmark(provider):
    result = await provider.resolve_benchmark({"description": "your keyword here"})
    assert result is not None

@pytest.mark.asyncio
async def test_generate_runfile(provider):
    result = await provider.generate_runfile("your-benchmark-name", {})
    assert result.benchmark == "your-benchmark-name"
    assert result.template

@pytest.mark.asyncio
async def test_get_benchmark_not_found(provider):
    result = await provider.get_benchmark("nonexistent")
    assert result is None

@pytest.mark.asyncio
async def test_resolve_no_match(provider):
    result = await provider.resolve_benchmark({"description": "xyzzy"})
    assert result is None
```

Run tests: `python3 -m pytest tests/test_myharness_provider.py -v`

## 5. Checklist

Before submitting:

- [ ] Provider implements all four required methods
- [ ] `harness` field on every `BenchmarkSuite` matches the registration key
- [ ] `get_default_config()` returns provisioning and execution config
- [ ] Keyword map covers the harness's natural-language domain
- [ ] `skills/<harness>/workloads.md` describes all benchmarks
- [ ] `skills/<harness>/config-guide.md` documents configuration
- [ ] Provider registered in `orchestrator/main.py`
- [ ] Git repo added to `orchestrator/config.py` default repos
- [ ] Tests pass: `pytest tests/test_<harness>_provider.py -v`
- [ ] `sample-private-skills/<harness>.json` template created
- [ ] Full test suite still passes: `pytest tests/ -v`

## How It All Connects

When a user submits "run a network performance test":

1. **Triage agent** calls `list_benchmarks()` → sees benchmarks from all
   registered harnesses → calls `resolve_benchmark({"description": "network"})` →
   your keyword map matches → triage selects your benchmark
2. **Resource agent** reads `min_hosts` and `roles` from the benchmark suite →
   acquires the right number of hosts
3. **Provisioning agent** reads `get_default_config()` for install instructions →
   installs your harness via SSH
4. **Benchmark agent** calls `list_harness_docs("myharness")` → reads your
   skill docs → constructs a run-file using the schema and examples →
   executes via the configured run command
5. **Review agent** reads results using the harness's result retrieval method

No agent code changes required. The skill provider is the only code you write.
