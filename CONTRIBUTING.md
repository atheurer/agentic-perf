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
3. **Commit** — hooks run `scripts/validate.sh` automatically
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
commits, the pre-commit hook runs lint + tests. If anything fails, the
commit is rejected and the agent sees the error output, fixes the issue,
and commits again. This creates a self-correcting loop. Do NOT run
`scripts/validate.sh` manually before committing — the hook does it for
you, and running it manually just duplicates the work.

## Scripts

| Script | Purpose | When to use |
|---|---|---|
| `scripts/dev-setup.sh` | Install hooks + deps | Once after clone |
| `scripts/lint.sh` | Run ruff lint + format check | Before commit |
| `scripts/test.sh` | Run pytest with coverage | Before commit |
| `scripts/validate.sh` | Run lint + test | Pre-commit hook calls this |

These scripts are the source of truth — CI, hooks, and developers all
use the same scripts.

## Code Standards

- **Python 3.12+** with type hints on all function signatures
- **Line length:** 88 characters (ruff enforced)
- **Formatter:** `ruff format`
- **Linter:** `ruff check`
- **Tests:** pytest + pytest-asyncio + pytest-cov
- **Coverage:** baseline ~41%, threshold 40% (increase as we improve)

See [AGENTS.md](AGENTS.md) for complete standards, architecture
principles, and key file paths.

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
