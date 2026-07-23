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
| `agents/introspection/agent.py` | Continuous passive observer (not AgentBase) |
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

## Running Services

### Interactive (foreground)

```bash
./start.sh      # state store (background) + orchestrator (foreground)
                # Ctrl+C stops both
```

### Background (for AI agents and scripted testing)

```bash
./scripts/start-bg.sh           # start both services
./scripts/start-bg.sh status    # check if running
./scripts/start-bg.sh stop      # stop both services
```

**Why a separate script?** Background processes started from AI agent
tool calls or compound shell commands inherit the parent's
stdout/stderr. When the parent exits, writes to those file descriptors
fail with SIGPIPE, which can kill the orchestrator even though it
ignores SIGPIPE. `start-bg.sh` uses `nohup` with explicit log file
redirection to fully isolate the processes.

**Do NOT** start services with bare `&` in compound commands:
```bash
# BAD — orchestrator will die when the bash command completes
python3 -m orchestrator.main 2>&1 &
sleep 30 && curl http://localhost:8090/...

# GOOD — use the helper script
./scripts/start-bg.sh
```

Logs are written to `~/.agentic-perf/logs/orchestrator.log` and
`~/.agentic-perf/logs/state-store.log`.

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
6. **Guardrails at every LLM/infrastructure boundary.** Validate
   before executing. Use structured tool calls, not free-text
   parsing — tool call schemas are enforced by the model's
   tool-use training; free-text schemas are enforced by hope.
7. **Agents should be boring.** One job each. If an agent seems
   to need cross-boundary knowledge, that knowledge belongs in a
   skill provider, not in the agent.
8. **Human on the loop, not in the loop.**
9. **The new-harness test.** If adding a benchmark harness requires
   changing agent prompts, the orchestrator, or the state machine,
   the knowledge is in the wrong layer. Refactor until it doesn't.

See `docs/design-philosophy.md` for full rationale.

### Knowledge Layering

When adding something new, use this test to decide where it goes:

| Question | Home |
|---|---|
| Would a different org using the same harness need this? | **Public skill** (`skills/` or `providers/skills/`) |
| Is it specific to how our org deploys this harness? | **Private skill** (`~/.agentic-perf/private-skills/`) |
| Is it about how an agent reasons through its task? | **Agent prompt** (`agents/{name}/prompts.py`) |
| Can getting it wrong waste resources or compromise security? | **Deterministic code** |

If ambiguous, prefer code over prompts, and skills over hardcoded
values in either. Never put harness-specific knowledge in agent
prompts — that is always a skill provider's job.

### Token Efficiency

Tool results accumulate in conversation history and are re-sent
with every LLM call. Trim aggressively:

- **Strip unused fields** from tool responses.
- **Summarize large outputs** on success; keep full output on failure.
- **Use on-demand tools** instead of returning everything upfront.
- **Handle predictable errors in code** (e.g., stale SSH keys)
  so the LLM never wastes iterations on them.
- **Scope prompts** to the current harness/board type.

### Guardrails Checklist

Every place where LLM output touches real infrastructure must have
a guardrail. Existing examples to follow:

- Run-file schema validation before benchmark execution
- Hostname-to-IP resolution (FQDNs cause container timeouts)
- SSH key comment conventions (harness key cleaners)
- Run-file tamper detection between generate and execute
- Platform contract checks before install attempts
- PID lock files against duplicate orchestrators
- `submit_*` structured tool calls as the mandatory agent output
  format (never parse free-text for structured results)
- Jumpstarter board selector passthrough (code overrides LLM
  selector substitution with directive's `board_selector`)
- Jumpstarter IP validation (provisioning rejects completion
  when `hosts_provisioned` contains non-IP entries or when
  `j tcp address` resolution fails)

### Security Model & Current Limitations

The system uses defense-in-depth, but several controls are not yet
hardened to the level of a full sandbox. Contributors (human and AI)
should understand what is enforced today and what is not.

**What is enforced:**

- **Command policy** (`agents/infra/command_policy.py`) — per-agent
  binary allowlists, blocked patterns, and shell-bypass detection
  (chaining, subshells, interpreter payloads). This catches
  accidental LLM hallucinations and trivial evasion attempts.
- **Bearer token auth** (`state_store/auth.py`) — every API request
  requires a token generated on first run and shared via env var.
- **Dispatch claims** — ticket-level leases prevent duplicate agent
  dispatch on orchestrator restart.
- **Secrets isolation** — secrets live in `~/.agentic-perf/secrets/`
  and are served to agents via the infra MCP server; the LLM never
  sees raw credentials.

**What is NOT enforced (known limitations):**

- **The command policy is advisory, not a sandbox.** It cannot catch
  base64-encoded payloads, obfuscated commands, or commands that are
  individually safe but dangerous in combination. Container/seccomp
  isolation is planned but not yet implemented.
- **The state store binds to 0.0.0.0.** While bearer-token-protected,
  it should be bound to localhost or placed behind a firewall on
  untrusted networks. The token is a shared deployment secret, not
  per-user auth.
- **Background command output** is written to per-run private temp
  directories (mode 0700 via `mktemp -d`), but the remote commands
  themselves run as root over SSH with no additional confinement.
- **Audit logging** (JSONL event files) is best-effort — a disk-full
  condition will log a warning but not block execution.
