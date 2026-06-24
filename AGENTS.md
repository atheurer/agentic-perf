# agentic-perf — AI Agent Guidelines

## Project Overview

Multi-agent system for autonomous performance testing. Engineers submit
natural-language requests; specialized AI agents triage, acquire hardware,
install tooling, execute benchmarks, and deliver structured analysis.

**Stack:** Python 3.12+, FastAPI, asyncio, MCP, Pydantic, httpx.
**License:** Apache 2.0.

## Key File Paths

| Path | Purpose |
|---|---|
| `agents/base.py` | `AgentBase` — LLM loop, tool dispatch, state store client |
| `agents/{name}/agent.py` | Agent implementation (extends AgentBase) |
| `agents/{name}/prompts.py` | System prompt for the agent |
| `agents/{name}/mcp_server.py` | MCP tools scoped to this agent |
| `agents/mcp_client.py` | MCP client for multi-server tool routing |
| `orchestrator/dispatcher.py` | Status → agent mapping, dispatch logic |
| `orchestrator/main.py` | Poll loop, provider initialization |
| `state_store/models.py` | Ticket model, state machine, valid transitions |
| `state_store/store.py` | In-memory ticket store with transition enforcement |
| `providers/llm/` | LLM providers (Claude, OpenAI-compat, mock) |
| `providers/skills/` | Benchmark harness skill providers |
| `providers/resource/` | Hardware resource providers (QUADS, AWS, PSAP) |
| `providers/events.py` | EventBus — audit trail (JSONL per ticket) |
| `providers/ssh.py` | Async SSH executor |
| `skills/` | Per-harness docs read by agents at runtime |
| `tests/conftest.py` | Test fixtures: MockSkillProvider, MockSSHExecutor |

## Development Standards

### Always Do

1. **Write tests and docs alongside code** — new functionality includes
   tests, docstrings, and relevant doc updates in the same commit.
2. **Commit and let hooks validate** — the pre-commit hook runs lint +
   tests automatically. Do NOT run `scripts/validate.sh` manually;
   that duplicates the hook. Just commit; fix and retry on failure.
3. **Use type hints** on all function signatures.
4. **Handle errors explicitly** — don't silently swallow exceptions.
5. **Explain "why" in comments** — not "what."

### Never Do

1. **Never commit secrets** — they live in `~/.agentic-perf/secrets/`.
2. **Never bypass hooks without justification** — document why in the
   commit message if you use `--no-verify`.
3. **Never skip or work around hook failures** — fix the problem. If a
   tool is missing, install it rather than working around the absence.
4. **Never modify agent prompts for harness-specific knowledge** — that
   belongs in skill providers (`providers/skills/`).
5. **Never put state in agents** — agents are stateless; state goes on
   the ticket `custom_fields`.

### Code Style

- **Line length:** 88 characters (ruff)
- **Formatter:** `ruff format` / **Linter:** `ruff check` (E, F, W, I)
- **Async:** use `async/await`, not threads
- **Quotes:** double quotes
- **Module header:** `from __future__ import annotations`
- **Imports:** three groups separated by blank lines:
  1. Standard library
  2. Third-party
  3. First-party (`agents`, `orchestrator`, `providers`, `state_store`)
- **No unused imports or variables** — use `_` for discards
- **Trailing commas** in multi-line collections and arguments

Wrap long lines cleanly:
```python
# Good
self._emit(
    ticket_id,
    "agent_started",
    {
        "system_prompt": system_prompt,
        "initial_messages": messages,
    },
)

# Bad
self._emit(ticket_id, "agent_started", {"system_prompt": system_prompt, "initial_messages": messages})
```

### Commit Messages

Conventional commits with thorough descriptions:
```
feat(agents): add evaluation logic for benchmark results

Implement result evaluation that assesses convergence conditions
after benchmark execution and decides whether to refine parameters
or finalize the analysis.

Closes #N
AI-assisted-by: Claude Opus 4.6
```

Always include `AI-assisted-by: <model>` when AI was used.

### Testing

- **Framework:** pytest + pytest-asyncio (asyncio_mode = auto)
- **Coverage:** pytest-cov, threshold 40% (baseline ~41%)
- **Mock LLM:** `providers/llm/mock.py` — no API keys needed
- **Scripts:** `./scripts/test.sh`, `./scripts/lint.sh`,
  `./scripts/validate.sh` (CI and debugging use only)

### Git Hooks

Hooks are the primary quality gate — they run `scripts/validate.sh`
automatically on every commit (ruff check, ruff format, pytest).

**Setup:** `./scripts/dev-setup.sh` (one-time after clone)

The scripts exist for CI and manual debugging, not for routine
pre-commit use.

## Architecture Principles

1. **LLM decides intent; code enforces invariants.**
2. **Multiple focused agents over one omniscient agent.**
3. **Skills for capabilities, prompts for reasoning, code for
   correctness.** Each piece of knowledge has exactly one home.
4. **The ticket is the single source of truth.** Agents are stateless.
5. **Contracts over hardcoded procedures.**
6. **Human on the loop, not in the loop.**

See `docs/design-philosophy.md` for full rationale.
