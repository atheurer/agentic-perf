# Contributing to agentic-perf

## Quick Start

```bash
git clone https://github.com/dustinblack/agentic-perf.git
cd agentic-perf
./scripts/dev-setup.sh    # Install hooks + dev dependencies
./scripts/validate.sh     # Verify everything works
```

## Development Workflow

1. **Create a feature branch** from `main`
2. **Make changes** — write tests alongside code, update docs
3. **Commit** — the pre-commit hook runs scoped lint and tests for staged
   Python files. It skips Python validation for documentation-only commits.
4. **Push and open a PR**

## AI-Assisted Development

We embrace AI-assisted development. This project is built with AI coding
agents, and we encourage contributors to use AI tools effectively.

**Required:**
- Tag AI-assisted commits: `AI-assisted-by: <model name>` in the commit
  message
- Review all AI-generated code before committing — you are responsible
  for what you submit
- Follow [AGENTS.md](AGENTS.md) standards — AI agents working on this
  project should read it first

**Git hooks enforce quality automatically.** When an AI agent (or human)
commits, the pre-commit hook runs scoped lint and auto-discovered tests for
staged Python files. If anything fails, the commit is rejected and the agent
sees the error output, fixes the issue, and commits again. For a full local
check, use the commands below. The current CI workflow runs the full suite for
pull requests targeting `main` or `local-pr-tests`.

## Scripts

| Script | Purpose | When to use |
|---|---|---|
| `scripts/dev-setup.sh` | Install hooks + deps | Once after clone |
| `scripts/lint.sh` | Run ruff lint + format check across the repository | Full local check |
| `scripts/test.sh` | Run the full pytest suite serially with coverage | Debugging / serial check |
| `scripts/test-parallel.sh` | Run the full pytest suite with isolated workers | Full local check; CI |
| `scripts/audit.sh` | Run audit & trace verification suite | Periodic / before audit gate |
| `scripts/validate.sh` | Run lint + the full serial test suite | Manual full validation |

The CI workflow runs `scripts/lint.sh` and `scripts/test-parallel.sh`, plus
security checks. The pre-commit hook runs scoped validation for staged Python
files.

## Code Standards

- **Python 3.12+** with type hints on all function signatures
- **Line length:** 88 characters (ruff enforced)
- **Formatter:** `ruff format`
- **Linter:** `ruff check`
- **Tests:** pytest + pytest-asyncio + pytest-cov
- **Coverage:** baseline ~41%, threshold 40% (increase as we improve)

See [AGENTS.md](AGENTS.md) for complete standards, architecture
principles, and key file paths.

## Reviewing Changes

See [docs/reviewing.md](docs/reviewing.md) for project-specific guidance on
reviewing orchestration, configuration, and agent workflow changes.

## Agent Skills

Repository-scoped contributor skills live under `.agents/skills/`. They are
separate from `skills/`, which contains runtime capabilities loaded by
agentic-perf agents.

- [PR cycle](.agents/skills/agentic-perf-pr-cycle/SKILL.md) — review, update,
  validate, and land an existing pull request.
- [Orchestration development](.agents/skills/agentic-perf-orchestration/SKILL.md)
  — develop and review changes to agent context, prompts, handoffs, dispatch,
  and ticket state.

These skills complement this guide and [AGENTS.md](AGENTS.md); the repository
docs and scripts remain the source of truth for project-specific rules and
commands.

## Commit Messages

Use conventional commits with thorough descriptions:

```
type(scope): brief description

Detailed explanation of the change, rationale, and trade-offs.

Closes #N
AI-assisted-by: Claude Sonnet 4
```

**Types:** `feat`, `fix`, `docs`, `test`, `refactor`, `chore`

## Adding a Benchmark Harness

The project's design is validated by how easy it is to add a new harness.
See [docs/adding-a-harness.md](docs/adding-a-harness.md) for the guide.
If adding a harness requires changing agent prompts or the orchestrator,
something is in the wrong layer.

## License

By contributing, you agree that your contributions will be licensed
under the Apache License 2.0.
