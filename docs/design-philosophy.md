# Agentic-Perf Design Philosophy

## Why This Document Exists

Agentic-perf is not a conventional automation framework with AI bolted on.
It is a system designed around a specific insight: **ad-hoc performance work
is too variable for traditional automation but too structured for unguided AI.**
The design decisions below explain where we draw lines — what the LLM does,
what it doesn't, what goes in skills vs. agent prompts vs. deterministic code,
and why.

These aren't arbitrary choices. Each one addresses a failure mode we either
hit or anticipated. If you're extending the system, understanding the *why*
behind each boundary will help you make the right call when the existing
patterns don't quite fit.

---

## 1. The LLM Decides Intent; Code Enforces Invariants

The most fundamental design line in the system:

- **The LLM handles ambiguity.** When a user says "compare RHEL 9 and 10 on
  network tests," the LLM figures out which network benchmark to use, that
  it needs two host pairs, that CX7 implies 100Gb+. No amount of if/else
  trees can cover the space of natural-language performance requests. This
  is where LLMs are irreplaceable.

- **Code handles correctness.** Once the LLM produces a run-file, deterministic
  code validates it against the harness's own JSON schema before anything
  touches real hardware. If validation fails, the LLM gets the specific
  errors and can fix and retry — but the invalid run-file never reaches a
  controller. A 30-minute benchmark run wasted on a malformed config is
  unacceptable; a 2-second schema check prevents it.

### Where the line sits in practice

| Layer | Decided by LLM | Enforced by code |
|---|---|---|
| **Triage** | What the user wants to test, the hypothesis, which benchmark fits | Structured output schema for the triage result |
| **Resource** | Whether to use user-provided hosts or reserve from QUADS | Validation that required fields (IPs, SSH keys) are present |
| **Provisioning** | Whether to skip, update, or reinstall an existing harness | Contract-based install flow — secret injection, pre/post commands |
| **Benchmark** | Run-file generation (endpoints, parameters, tags) | Schema validation against harness spec, hostname-to-IP resolution |
| **Review** | Statistical interpretation, verdict, recommendations | Metric query interfaces, structured report format |

### The principle

If a mistake would waste time or resources (bad run-file, missing secret,
wrong host), catch it in code. If a decision requires understanding context
(which benchmark to use, how to interpret results, whether to retry), let
the LLM make it. Don't ask the LLM to be reliable at things code is
reliable at. Don't ask code to be flexible at things the LLM is flexible at.

---

## 2. Why Multiple Agents, Not One Big Agent

The instinct is to give one powerful agent all the tools and let it figure
everything out. We tried this mentally and rejected it for three reasons:

### Trust boundaries

The provisioning agent can SSH into hosts and deploy credentials. The review
agent queries metric stores. These are fundamentally different security
domains. A single agent with all tools means a prompt injection in a
benchmark result could theoretically trigger SSH commands. Separate agents
with separate MCP tool sets make this structurally impossible — the review
agent literally does not have an SSH tool.

### Replaceability

When we added the second benchmark harness, we didn't touch the triage,
resource, or review agents. The benchmark agent didn't change either — it
reads execution config from the skill provider. The only new code was a
new `SkillProvider` subclass. If everything lived in one agent's prompt and
toolset, adding a harness would mean modifying the monolith and hoping
nothing else breaks.

### Debuggability

When something goes wrong, you can look at exactly one agent's ticket
contributions. "The benchmark agent generated a run-file with the wrong
endpoint format" is actionable. "The agent did something wrong somewhere
in its 47-tool, 200-line reasoning chain" is not.

### The tradeoff

Multi-agent adds orchestration complexity. There's a dispatcher, a state
machine, agent-to-agent communication via the ticket. This is real cost.
We accept it because the alternative — a monolith that works for demos
but can't be maintained, secured, or extended — is worse for a system that
needs to run against real infrastructure with real credentials.

---

## 3. Skills vs. Agent Prompts vs. Code

This is the layering that makes the system maintainable. Every piece of
knowledge or behavior lives in exactly one of three places:

### Skills: "What can we do?"

Skills are the **capability catalog**. They define what benchmarks exist,
what parameters they accept, what host roles they need, how to generate
a run-file, and how to install the harness.

Skills are split into public and private:

- **Public skills** (from the harness's git repo or a shared registry): benchmark
  definitions, parameter schemas, run-file templates. These are shareable —
  they contain no org-specific information. Example: "Harness X supports
  storage benchmarks with roles [client], min 1 host."

- **Private skills** (from `~/.agentic-perf/private-skills/`): org-specific
  install configuration — internal registry URLs, install flags, pre/post
  commands, vault references. Example: "our install of Harness X uses an
  internal container registry at registry.internal:5000."

**Why the split matters:** A performance team at a different org can use the
same public skills with their own private config. They don't fork the skill
definitions — they overlay their org config. And critically, **secrets are
never in skill configs.** Private skills reference vault paths; the secrets
provider resolves them at runtime.

**When to add to skills:** When the knowledge is about a *benchmark harness* —
what it can do, how to install it, how to configure it. If a different org
using the same harness would need the same information, it's a public skill.
If it's specific to how *your* org deploys that harness, it's a private skill.

### Agent prompts: "How do you reason about your job?"

Agent prompts define the *decision-making process* for each role. They tell
the LLM what to do in what order, what tools to call, how to handle edge
cases, and when to escalate to the user.

Prompts should be **harness-agnostic.** The benchmark agent's prompt says
"call `get_execution_config` to learn how to run this harness" — it doesn't
hardcode any specific harness's run command. This is what lets the same
agent handle any registered harness without prompt changes.

**When to add to prompts:** When the knowledge is about *how an agent
should think* — the decision tree, the order of operations, the criteria for
escalating vs. proceeding. If it's about the agent's role rather than a
specific harness, it belongs in the prompt.

**What not to put in prompts:** Benchmark-specific parameters, host
requirements, or install procedures. These change when you add a new harness
and should live in skills. If you find yourself editing an agent prompt
because you added a new benchmark, the knowledge is in the wrong layer.

### Code: "What must never go wrong?"

Code handles the invariants — the things that must hold regardless of what
the LLM decides:

- **Schema validation** before benchmark execution
- **Hostname-to-IP resolution** (some harnesses time out on FQDNs inside
  containers)
- **SSH key management** with specific key comments that survive harness
  key-cleanup routines
- **Run-file integrity** — the `execute_benchmark` handler compares what
  the LLM passes to what `generate_run_file` produced, and uses the original
  if the LLM modified it
- **Secret injection** at runtime, scoped to the agent that needs it
- **PID locking** to prevent duplicate orchestrator instances

**When to add to code:** When it's a correctness invariant that should never
depend on LLM judgment. If getting it wrong wastes a benchmark run, exposes
a credential, or corrupts state, it's code.

### The layering test

When you're adding something new, ask:

1. Would a different org using the same harness need this? → **Public skill**
2. Is it specific to how our org deploys this harness? → **Private skill**
3. Is it about how an agent reasons through its task? → **Agent prompt**
4. Can getting it wrong waste resources or compromise security? → **Code**

If the answer is ambiguous, prefer code over prompts, and skills over
hardcoded values in either.

---

## 4. The Ticket Is the Single Source of Truth

Agents are stateless. They can crash, be killed, time out, or be replaced
between runs. All durable state lives on the ticket — a JSON document
(local state store today, Jira later) that every agent reads from and
writes to.

### Why not message passing?

Message-based architectures (queues, pub/sub) create invisible state.
If an agent crashes after reading a message but before acting on it, the
message is gone. If you want to debug what happened, you're reconstructing
state from log fragments.

With ticket-as-truth:

- **Any agent can resume.** The provisioning agent crashes mid-install?
  When it restarts, it reads the ticket, sees `provisioning_complete: false`,
  and picks up where it left off.

- **Humans can inspect.** At any point, a human can read the ticket and
  understand the complete state: what was requested, what was planned, what
  was provisioned, what ran, what the results were. No log archaeology.

- **Negotiation is natural.** During the planning phase, benchmark and
  resource agents both write to the ticket. Each can read what the other
  posted. Conflict detection is just reading two fields and comparing them.
  No special messaging protocol needed.

### What goes on the ticket

- User's original request
- Triage intent and hypothesis
- Benchmark plan (harness, suite, parameters, resource requirements)
- Resource plan (provider, hosts, availability, constraints)
- Plan status (proposing / conflict / agreed)
- Assigned hardware (IPs, SSH user, key path)
- Provisioning status and harness version
- Run ID, run-file used, benchmark status
- Review summary, verdict, detailed analysis, key metrics

### What does NOT go on the ticket

- Credentials (resolved at runtime from the secrets provider)
- Agent-internal reasoning (the LLM's chain of thought stays in the agent's
  session)
- Intermediate tool call results (logged, but not persisted to the ticket
  unless they're relevant to other agents)

---

## 5. Contracts Over Configuration

When the provisioning agent installs a benchmark harness, it doesn't follow
a hardcoded procedure. It executes a **contract** defined in the private
skill config:

```
secret_files → resolved from secrets provider, deployed to host
pre_install_commands → run before install (mask firewall, etc.)
install script → fetched from upstream (curl | bash, git clone, etc.)
install flags → passed to the install script
post_install_commands → run after install (add private registry, etc.)
```

### Why contracts?

The alternative is scripting each harness install as a custom code path.
This works for one or two harnesses. It doesn't scale to ten, and it
definitely doesn't work when different orgs install the same harness
differently.

Contracts let you:

- **Add a harness without writing install code.** Define the contract in
  the private skill config. The provisioning agent follows the same flow
  for every harness.

- **Customize installs per org.** The public skill says "Harness X is
  installed via its upstream install script." The private contract adds
  "also configure our internal registry and deploy these auth tokens."

- **Handle uninstall/reinstall cleanly.** The contract defines
  `pre_uninstall_commands` (stop services before removal) and the
  `on_existing_install` policy (skip, update, reinstall, or ask the user).
  These are encoded once and enforced consistently.

### The contract is not the LLM's job

The LLM decides *whether* to install (is there an existing install? does
the policy say skip or reinstall?). The contract defines *how* to install.
The LLM orchestrates; the contract executes. This means a bug in install
logic is a contract bug, not a prompt bug — and you fix it by editing a
config file, not by rewording an instruction to an AI.

---

## 6. Guardrails Are Not Optional

Every place where the LLM's output touches real infrastructure has a
guardrail:

| Guardrail | What it catches |
|---|---|
| **Run-file schema validation** | Malformed benchmark configs before they waste a 30-min run |
| **Hostname-to-IP resolution** | FQDNs that cause paramiko timeouts inside containers |
| **SSH key comment convention** | Keys that would be deleted by the harness's own key cleaner |
| **Run-file tamper detection** | LLM modifying the run-file between generate and execute |
| **Platform contract check** | OS/package incompatibilities before attempting install |
| **PID lock file** | Duplicate orchestrator instances stepping on each other |
| **Structured output (submit_result)** | Every agent ends by calling a structured tool, not free-text |

### Why structured output matters

Early in development, agents would sometimes produce their result as
free-text in their final message. Parsing free-text from an LLM is fragile.
The `submit_result` tool pattern solves this: every agent's final action is
calling a tool with a defined schema. The tool validates the schema, the
orchestrator reads the structured result, and there's no parsing ambiguity.

This is a general principle: **anywhere you need reliable structured data
from the LLM, make it a tool call, not a text response.** Tool call schemas
are enforced by the model's tool-use training; free-text schemas are
enforced by hope.

---

## 7. Agents Should Be Boring

Each agent does exactly one thing:

- **Triage** understands what the user wants. It doesn't pick hosts or
  generate run-files.
- **Resource** finds and reserves hardware. It doesn't know what any specific benchmark is.
- **Provisioning** installs software on hosts. It doesn't run benchmarks.
- **Benchmark** executes a test. It doesn't analyze results.
- **Review** interprets data. It doesn't SSH into anything.

This is intentional. A "smart" agent that crosses boundaries is harder to
debug, harder to secure, and harder to replace. A "boring" agent that does
one thing well is composable.

### When an agent seems to need cross-boundary knowledge

This usually means the knowledge should be in a skill, not in the agent.
When the benchmark agent needs to know how to install a harness's dependencies
on the SUT (not the controller), that's not the benchmark agent's job — it's
a skill-defined pre-run step that the benchmark agent executes without
understanding the details.

When the triage agent seems to need deep benchmark knowledge to resolve
the user's request, that's the skill provider's `resolve_benchmark` doing
the matching — the triage agent calls it as a tool, it doesn't carry the
knowledge itself.

---

## 8. The Human Stays in Control

The system is designed to remove the human from the *execution* loop, not
from the *decision* loop. At defined points, the system pauses and asks:

- **Ambiguous requests:** Triage can't determine what the user wants →
  `request_clarification`
- **Resource conflicts:** Benchmark needs 2 hosts, QUADS has 1 → escalate
  to user with the options
- **Install policy:** Existing install found, policy says "ask_user" →
  present skip/update/reinstall choices
- **Unexpected results:** Review finds anomalies it can't explain →
  recommend follow-up tests, don't silently proceed

The goal is human-on-the-loop (reviewing and steering) rather than
human-in-the-loop (manually executing each step). The agents do the work;
the human owns the judgment calls that the agents shouldn't be making alone.

This is the CEO's vision applied specifically: *"Letting AI handle execution
so our people can solve problems, ensure system output is accurate, and
drive innovation."*

---

## 9. Adding a New Benchmark Harness: The Acid Test

The design philosophy is validated by how much work it takes to add a new
harness. When we added the second benchmark harness, here's what changed:

| What changed | Where | Lines |
|---|---|---|
| New `SkillProvider` subclass | `providers/skills/` | New file (~100 lines) |
| Private skill config | `~/.agentic-perf/private-skills/` | New JSON file |
| Harness-specific execution path | `agents/benchmark/mcp_server.py` | ~60 lines |

What did NOT change:

- Triage agent prompt or tools
- Resource agent prompt or tools
- Provisioning agent prompt (harness-agnostic by design)
- Review agent prompt or tools
- Orchestrator or dispatcher
- State machine or ticket schema
- The first harness's code or configuration

**This is the test.** If adding a new harness requires touching agent prompts,
the orchestrator, or the state machine, something is in the wrong layer.

---

## 10. Summary of Principles

1. **LLM decides intent; code enforces invariants.** Don't ask the LLM to
   be reliable at what code is reliable at.

2. **Multiple focused agents over one omniscient agent.** Trust boundaries,
   replaceability, and debuggability outweigh orchestration complexity.

3. **Skills for capabilities, prompts for reasoning, code for correctness.**
   Each piece of knowledge has exactly one home.

4. **The ticket is the single source of truth.** Agents are stateless;
   state lives where everyone can read it.

5. **Contracts over hardcoded procedures.** Installation, configuration,
   and teardown follow declarative contracts, not imperative scripts.

6. **Guardrails at every boundary.** Validate before executing. Structured
   tool calls, not free-text parsing.

7. **Agents should be boring.** One job each. Cross-boundary knowledge goes
   in skills.

8. **Human on the loop, not in the loop.** AI handles execution; humans
   own judgment calls.

9. **The new-harness test.** If adding a harness means changing agents,
   refactor until it doesn't.

---

*Note: The Perf & Scale ecosystem includes many benchmark harnesses —
arcaflow, benchmark-runner, cluster-buster, crucible, forge, fournos,
k8s-netperf, kube-burner, and zathras among them. The design principles
above are validated against this diversity: any of these should be
addable as a skill provider without modifying agent logic.*
