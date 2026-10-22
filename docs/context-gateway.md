# Context gateway

The context gateway is agentic-perf's shared retrieval path for reusable,
subject-specific guidance and software references. It lets an agent request the
knowledge relevant to its subject and phase, while the service controls which
sources apply and preserves their scope and provenance.

**Current status:** this implementation slice addresses the gateway integration
in [#1118](https://github.com/atheurer/agentic-perf/issues/1118) and the broader
context migration inventory in
[#1153](https://github.com/atheurer/agentic-perf/issues/1153). It includes
administrator-managed organization sources, the `harness/crucible` subject,
explicit Crucible project guidance, and ticket-bound captures for the existing
kube-burner, k8s-netperf, and AWS local-document maps. Crucible catalog and child
repository reads are pinned to exact upstream commits for ticket-bound calls.
This is a narrow project-local migration bridge, not a general project package
service. Zathras and Arcaflow retain their existing paths; authenticated
user-level sources and migration of other subjects remain future work. See
[configuration](configuration.md#organization-skill-gateway) for current setup
and [the detailed design](design-skill-gateway.md) for contracts and
implementation boundaries.

## Why a gateway

Without a common retrieval path, the same topic can be described in prompts,
repository documents, private JSON settings, upstream repositories, and the
installed software. That makes it hard to know which copy an agent used, who it
applies to, whether it matches the installed version, or whether another agent
received different instructions. A gateway helps by:

- **Giving agents one retrieval contract.** Agents request a stable subject such
  as `harness/crucible` rather than deciding which local path, repository, or
  legacy reader to use. The service selects available sources for the agent's
  phase.
- **Keeping different kinds of knowledge distinct.** Upstream references
  describe general software behavior; installed, version-matched references
  establish what the active software supports; organization packages describe
  shared local procedures and defaults. A preference cannot redefine a schema
  or capability.
- **Making ownership and access explicit.** The administrator configures
  organization sources once for the instance. The model cannot choose another
  user's identity, a source URL, a repository path, or credentials. User-level
  sources require authenticated identity and storage isolation and are not yet
  implemented. Bundled project guidance is limited to entries in the explicit
  repository manifest and is filtered by agent and phase.
- **Showing provenance and disagreement.** Responses preserve source, scope,
  revision, and document identity. Multiple organization repositories can
  contribute to a subject. Exact duplicates are identified; differing content
  at the same path is surfaced as a potential conflict. Source order does not
  select a winner, and material conflicts that agents cannot resolve should be
  raised through HITL. Conflicting runtime configurations stop affected
  consumers rather than being silently merged.
- **Supporting repeatable tickets.** Configured organization Git sources are
  resolved to commits during ticket provider setup. Manifest, local-map, and
  upstream source content is captured lazily on its first applicable gateway
  or catalog request. Later phases reuse those revisions while new tickets can
  see updates. Captures and commit metadata remain in service-only storage, with
  their revision and capture time returned as provenance. Cleanup runs only
  after a direct ticket lookup confirms closure; stable locks and small closed
  markers fence late readers and captures.
- **Loading context when it is needed.** Agents retrieve bounded entrypoints,
  follow relevant document pointers, and search the subject's material instead
  of putting every subject's documents into every prompt. This helps keep
  irrelevant text and repeated document payloads out of agent conversations.
- **Reducing onboarding work.** An administrator can configure one organization
  source root, and subject directories in it are discovered by layout. Named
  sources support independently maintained repositories without adding a
  location setting for every subject.

For contextual claims and preferences, locality is a useful default signal:
upstream context is the baseline, organization context normally carries more
weight for shared environment-specific practices, and authenticated user
context is intended to carry more weight for that user's preferences. This is
not a universal authority order. Verified software behavior and mandatory
organization policy remain constraints, ticket text can clarify the task, and
conflicts between sources at the same level have no implicit winner. If a
material conflict remains unresolved, the agent should ask for human guidance
and identify the competing sources.

## What belongs behind the gateway

Use the gateway for reusable instructions and reference material whose
applicability depends on a subject, scope, phase, or software version. Examples
include harness procedures, organization defaults, benchmark integration
documentation, and version-matched product references.

The gateway does not replace every agent tool or every layer of the system:

| Knowledge or data | Appropriate home |
| --- | --- |
| Reusable organization procedures and preferences | Organization-scoped subject package |
| Future personal preferences | Authenticated user-scoped subject package, after identity and isolation support exists |
| General software facts | Authoritative upstream documentation, retrieved through an appropriate software adapter |
| Behavior supported by the installed software | Version-matched installed documentation and runtime evidence |
| Agent role, reasoning process, and output contract | Agent prompt, except harness-specific procedures that belong in subject context |
| Schema validation, authorization, safe execution, and enforced invariants | Deterministic code |
| Credentials and secret values | Existing secrets provider; skill packages may refer to secrets but do not contain their values |
| Live ticket state, hardware discovery, benchmark results, metrics, and large artifacts | Their typed APIs and workspace/artifact tools; do not ingest bulk result data as skill text |

Structured runtime settings may be sourced from an organization subject package
where a registered schema and allowlisted view exist. The gateway keeps raw
service configuration and resolved secrets out of model-readable documents;
runtime consumers continue to use validated service-side configuration.

## Current source paths

Context currently reaches agents through several mechanisms. They serve
different purposes and should be inventoried before a subject is migrated:

| Existing mechanism | Current role and migration treatment |
| --- | --- |
| Agent prompts | Include role instructions and, in some cases, harness-specific prose. Keep general reasoning and tool protocol here; classify reusable harness instructions before moving them. |
| Repository `skills/` documents and `read_skills` | Provide static skill text for harnesses and domains. Crucible project guidance is selected from `skills/context-manifest.json` and retrieved through the gateway; other subjects retain legacy access until migrated. |
| `SkillProvider` and catalog tools | Supply structured benchmark discovery, templates, and harness-specific contracts. Keep typed discovery and validation behavior. Where their reference content needs shared scoped retrieval, connect the source through a subject adapter rather than turning operational contracts into free-form prose. |
| Crucible controller documentation | The existing controller reader follows installed documentation pointers. The Crucible gateway adapter exposes the applicable controller entrypoint for benchmark and review phases. |
| Upstream GitHub catalog access | Ticket-bound Crucible catalog and document reads use a ticket-owned core checkout and exact commit pins for selected child repositories. No-ticket catalog compatibility remains unpinned and is identified as such. |
| Local harness and resource documents | kube-burner, k8s-netperf, and AWS expose the existing approved Markdown subsets through ticket-owned content captures. These adapters retain their existing refs and phase visibility. |
| `private-skills/<harness>.json` | Legacy instance-wide structured settings are consumed by services. For a migrated subject, select a canonical validated configuration source; do not silently merge it with new organization settings. |
| Ticket, workspace, result, and metric tools | Retrieve task-specific or potentially large operational data. They are not reusable subject guidance and should remain accessible through their purpose-built tools. |

The migration target is for reusable subject knowledge to have a gateway path,
even when a specialized tool remains the right interface for structured
discovery or execution. The gateway can identify and retrieve the applicable
reference; typed tools can continue to return benchmark catalogs, validated
settings, or live data in their existing contracts.

For Crucible, the gateway combines explicitly mapped project guidance,
organization guidance, and installed controller documentation where that
phase has a controller. The bundled project documents have the lowest default
authority for overlapping soft guidance and are scoped by phase and agent.
Direct Crucible skill and harness-document readers route callers to the gateway.
Triage can use the bounded upstream Crucible catalog before a controller exists. A
Crucible benchmark that uses a named workload can require both Crucible's
benchmark integration documentation and, when needed, the native workload's
own documentation; the gateway must not treat the latter as a replacement for
the harness-specific parameter contract. See the [Crucible source and migration
details](design-skill-gateway.md#one-benchmark-can-require-several-software-sources).

## Ticket source lifetime

Ticket identity comes from the trusted server context. The model cannot choose a
ticket ID, repository, branch, or capture path. A first applicable request
captures a complete manifest package or the approved local Markdown subset
before filtering documents for the caller. Upstream adapters resolve the
configured ref to a commit and read only from a ticket-owned checkout. If the
first upstream capture fails, the source stays unavailable until a later retry;
it never substitutes a stale shared cache. Once an existing pin is missing or
corrupt, reads fail closed rather than capturing the current source again.

The source epoch stores content, revisions, and capture timestamps outside
ticket workspaces and artifacts. The Crucible catalog adapter pins the core
catalog and each selected child repository on first use. A catalog-only call
without trusted ticket context keeps the legacy unpinned discovery behavior and
must not be treated as ticket provenance. Existing organization source-binding
changes also fail closed for that ticket; updated bindings apply to new tickets.
Upstream document exposure is bounded to 256 documents and 32 MiB per request,
with a 1 MiB per-document limit. Crucible reports an incomplete inventory when
those limits omit documents; kube-burner and k8s-netperf reject an oversized
source listing. These limits bound retrieved guidance, not the upstream Git
checkout used to pin its revision.

On closure, the orchestrator confirms `status == "closed"` with the state store
before cleanup. Stable per-ticket locks serialize capture and deletion. A
service-only closed marker remains after content cleanup so an in-flight worker
or stale MCP process cannot recreate a snapshot. The periodic sweep leaves data
alone when the ticket lookup is missing, fails, or returns an unknown status.

## Migration plan

Migration is incremental. The goal is for reusable context to have a clear
subject, owner, source, role, and retrieval path, while each subject moves only
after its consumers can retrieve equivalent or better information through the
gateway. Do not disable legacy retrieval globally as subjects are added.

### 1. Inventory current context and consumers

For each subject, list every prompt fragment, skill document, private setting,
provider/catalog result, upstream reference, and installed-software reader. For
each item, record its actual consuming agents and phases, intended audience,
source and revision, and whether it is prose, software fact, preference,
runtime setting, or enforced behavior. Identify duplicated and conflicting
claims. Confirm coverage in upstream and installed documentation rather than
assuming a local copy is needed. If upstream software documentation is missing
or unclear, track a correction with the owning software project. Keep any
temporary local workaround tied to an affected version, its reason, and a
condition for removal.

### 2. Assign a stable subject and classify each item

Use a subject such as `harness/crucible`, `harness/zathras`, or
`benchmark/uperf`. Decide whether each item belongs in upstream documentation,
organization guidance, future user guidance, an agent prompt, deterministic
code, or an operational data tool. An item stays in the public skill tree only
if it is a reusable reference that still needs to be maintained there; do not
copy upstream documentation into an organization package merely because it is
convenient. Keep private organization content and its detailed migration
inventory in administrator-controlled storage; the public repository should
describe generic contracts and link to authoritative software documentation.

### 3. Prepare source packages and adapters

Place shared organization guidance and approved structured settings in an
administrator-controlled source. Use the documented subject layout so the
service can discover packages without per-subject source configuration. Keep
upstream and installed software sources separate, version-aware, and explicit
about what each establishes. Preserve specialized catalog interfaces when
agents need structured results or controller-independent discovery.

### 4. Wire every consumer for that subject

For every affected agent and phase, expose the same subject retrieval contract
and ensure it receives the applicable entrypoints and provenance. Update the
relevant service-side settings consumer to read the canonical validated
configuration. Check prompts, tool availability, ticket snapshots, workspace
references, audits, and diagnostics together; adding a document reader alone
does not migrate a subject if runtime configuration or another agent still
reads the old path.

### 5. Compare old and new behavior before cutover

Run representative tickets through all consuming phases. Compare the context
each phase receives, source revisions, software-version fit, structured
settings, and behavior under missing or conflicting sources. Verify that
same-level conflicts are visible and can trigger HITL, and that an unresolved
runtime-configuration conflict blocks use. Keep the legacy path enabled for
that subject until the new path meets the agreed coverage; do not let an
implicit fallback or merge obscure which source won.

### 6. Retire old copies only after replacement is proven

Remove duplicate prompt prose, skill files, and legacy configuration access
only when every required consumer has a tested gateway path and the replacement
source is available in all supported deployment phases. Preserve a migration
record of moved content and links to authoritative software references. Other
subjects continue using their current path until they complete the same process.

### 7. Add user scope only after identity controls are ready

User-level context is not merely another directory. The service must derive the
user from authenticated ticket state, enforce self-or-admin access, and isolate
documents and snapshots across tickets and users. Once that exists, user
preferences can participate in locality-based resolution. They cannot override
mandatory organization policy or verified software behavior, and conflicting
user sources at the same level still need HITL when material and unresolved.

## Crucible is the first migration, not the final coverage

The current implementation draft supports one or more administrator
organization sources (local paths or Git URLs), subject discovery, source
provenance, duplicate and same-path conflict reporting, and ticket-scoped
organization snapshots. Crucible project guidance is separately declared in
`skills/context-manifest.json`; each document names its phase and agent scope.
The Crucible subject also retains phase-appropriate software sources: the
installed controller documentation for benchmark/review and ticket-pinned
upstream catalog repositories for ticket-bound discovery. The current local
document maps for kube-burner, k8s-netperf, and AWS are captured per ticket;
their legacy interfaces remain while this migration bridge is reviewed.
Conflicting organization runtime configurations are configuration errors
rather than merge candidates. Retire the project-local bridge only after
equivalent upstream or organization coverage is available to every consuming
phase.

This does **not** mean that all agentic-perf context already uses the gateway.
Zathras and Arcaflow retain their existing legacy paths. Authenticated user
packages, a general project package service, native workload software adapters
beyond the current Crucible sources, and migration of every other subject
remain future work. The
[detailed design](design-skill-gateway.md)
tracks the Crucible migration inventory and the checks required before rollout.
