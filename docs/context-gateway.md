# Context gateway

The context gateway is agentic-perf's shared retrieval path for reusable,
subject-specific guidance and software references. It lets an agent request the
knowledge relevant to its subject and phase, while the service controls which
sources apply and preserves their scope and provenance.

**Current status:** the active branch implements administrator-managed
organization sources, the initial `harness/crucible` subject, and a
manifest-scoped project source for the eight audited Crucible workflow guides
in this repository. The wider migration across harnesses and other subjects is
not complete: the bundled project adapter currently serves Crucible only, and
other subjects still need their sources inventoried and connected. Authenticated
user-owned skill sources are future work. See
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
  implemented.
- **Showing provenance and disagreement.** Responses preserve source, scope,
  revision, and document identity. Multiple organization repositories can
  contribute to a subject. Exact duplicates are identified; differing content
  at the same path is surfaced as a potential conflict. Source order does not
  select a winner, and material conflicts that agents cannot resolve should be
  raised through HITL. Conflicting runtime configurations stop affected
  consumers rather than being silently merged.
- **Supporting repeatable tickets.** Organization documents and configuration
  are pinned for a ticket so later phases can use the same captured revision;
  newly submitted tickets can see updated source content.
- **Loading context when it is needed.** Agents retrieve bounded entrypoints,
  follow relevant document pointers, and search the subject's material instead
  of putting every subject's documents into every prompt. This helps keep
  irrelevant text and repeated document payloads out of agent conversations.
- **Reducing onboarding work.** An administrator can configure one organization
  source root, and subject directories in it are discovered by layout. Named
  sources support independently maintained repositories without adding a
  location setting for every subject.

For soft guidance addressing the same claim, the intended order is
authenticated user, organization, upstream, then bundled project-local
documents. Project-local documents are a temporary fallback and have the lowest
default authority. This order applies only within each source's domain:
organization guidance sets shared practice, upstream references describe
general software behavior, and installed controller/version evidence
establishes behavior present on that system. A user preference cannot override
mandatory organization policy or deterministic security requirements. Ticket
text can clarify task intent but cannot waive those constraints. Sources at the
same level have no implicit winner; unresolved material conflicts should reach
HITL with the source ids and document paths.

## Examples: why this improves on separate readers

### One task needs several kinds of knowledge

**Before:** Each agent or phase can load a different subset of prompt text,
local skills, and software documentation. It can be hard to tell whether a
difference comes from the source, the installed version, or that agent's
retrieval path.

**With the gateway:** The agent requests a subject such as `harness/crucible`.
The service returns applicable organization and installed-software sources with
their scope and provenance kept distinct. Deterministic tools still enforce
schemas and runtime behavior.

### Two organization sources disagree

**Before:** Guidance may be copied together or one reader may happen to win,
hiding where the advice came from.

**With the gateway:** The response preserves each source. It identifies
duplicates, surfaces conflicting material, and does not use source order to
choose a winner. Unresolved material conflicts can go to HITL; runtime
configuration conflicts block use.

### A ticket passes from execution to review while guidance changes

**Before:** A later phase may read a newer copy than the phase being reviewed,
making the result hard to reproduce.

**With the gateway:** The ticket uses its captured organization revision across
phases. New tickets can capture later revisions.

### A new installation needs organization guidance

**Before:** Administrators may need to provision local files or per-topic paths
on every install, creating dependencies that are easy to miss.

**With the gateway:** The administrator configures the organization source once.
The gateway discovers subjects by the documented directory layout and reports
source availability.

These examples describe retrieval and source-management behavior, not a claim
that the gateway replaces all tools. Agents still use typed APIs for live ticket
state, execution, metrics, and large artifacts, and installed software remains
authoritative for what it can actually do.

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
| Credentials and secret values | Service-only secret references resolved by the local, vault, or Git-backed secrets provider; never returned by the model-facing context gateway |
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
| Repository `skills/` documents and `read_skills` | Provide static skill text for harnesses and domains. During migration, keep needed documents in the local repository and expose them as a project-scoped gateway source. Once a subject uses the gateway, direct document readers are no longer the path for that subject; unmigrated subjects may retain their legacy path. |
| `SkillProvider` and catalog tools | Supply structured benchmark discovery, templates, and harness-specific contracts. Keep typed discovery and validation behavior. Where their reference content needs shared scoped retrieval, connect the source through a subject adapter rather than turning operational contracts into free-form prose. |
| Crucible controller documentation | The existing controller reader follows installed documentation pointers. The Crucible gateway adapter exposes the applicable controller entrypoint for benchmark and review phases. |
| Upstream GitHub catalog access | Crucible triage retains bounded catalog and benchmark-detail operations that work before a controller is available. These structured operations remain distinct from full document retrieval. |
| `private-skills/<harness>.json` | Legacy instance-wide structured settings are consumed by services. For a migrated subject, select a canonical validated configuration source; do not silently merge it with new organization settings. |
| Ticket, workspace, result, and metric tools | Retrieve task-specific or potentially large operational data. They are not reusable subject guidance and should remain accessible through their purpose-built tools. |

The migration target is for reusable subject knowledge to have a gateway path,
even when a specialized tool remains the right interface for structured
discovery or execution. The gateway can identify and retrieve the applicable
reference; typed tools can continue to return benchmark catalogs, validated
settings, or live data in their existing contracts. A local document adapter is
distinct from an administrator-configured local path: the former serves
versioned project documentation, while the latter is an organization-owned
source configured by the administrator.

The gateway combines Crucible project guidance with organization guidance and
controller documentation; triage also retains bounded Crucible catalog
operations. The project adapter reads only documents explicitly listed in
`skills/context-manifest.json` and scopes them by phase, agent, and optional
benchmark. This first adapter handles Crucible; extending it to other
`skills/<subject>/` directories remains part of the inventory and migration
plan. Do not copy project workflow guides into an organization package unless
their ownership audit says they are organization practices.

For Crucible, the gateway currently combines manifest-scoped project guidance,
organization guidance, and the installed controller documentation where that
phase has a controller. Triage
can use the bounded upstream Crucible catalog before a controller exists. A
Crucible benchmark that uses a named workload can require both Crucible's
benchmark integration documentation and, when needed, the native workload's
own documentation; the gateway must not treat the latter as a replacement for
the harness-specific parameter contract. See the [Crucible source and migration
details](design-skill-gateway.md#one-benchmark-can-require-several-software-sources).

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
Track the full file-to-subject-to-agent inventory and migration sequence in
[issue #1153](https://github.com/atheurer/agentic-perf/issues/1153).

### 3. Bridge local documents through the gateway

Keep useful local documents in the project while their content is being
verified and moved. Register them as a distinct project-scoped source, with a
stable source id, local path, applicable subject/phase, and content revision
over the manifest and mapped files (or the installed agentic-perf commit). The gateway should return these
documents alongside applicable organization and software references, preserving
their source and revision. Do not label bundled project material as
organization-owned guidance or let repository ordering resolve conflicts.

For each subject, switch agents to gateway retrieval only after the local source
is available to every required phase and the gateway preserves provenance and
conflicts. Until then, legacy direct readers may remain for that subject. Once
the gateway path is validated, remove the direct document-loading path for that
subject while keeping the local files in place as the temporary source. Typed
tools for validation, execution, live state, and bulk data remain available;
this step retires only tools whose purpose is to load reusable documents.

### 4. Prepare replacement sources and adapters

Place shared organization guidance and approved structured settings in an
administrator-controlled source. Use the documented subject layout so the
service can discover packages without per-subject source configuration. Keep
upstream and installed software sources separate, version-aware, and explicit
about what each establishes. Preserve specialized catalog interfaces when
agents need structured results or controller-independent discovery.

### 5. Wire every consumer for that subject

For every affected agent and phase, expose the same subject retrieval contract
and ensure it receives the applicable entrypoints and provenance. Update the
relevant service-side settings consumer to read the canonical validated
configuration. Check prompts, tool availability, ticket snapshots, workspace
references, audits, and diagnostics together; adding a document reader alone
does not migrate a subject if runtime configuration or another agent still
reads the old path.

### 6. Compare old and new behavior before cutover

Run representative tickets through all consuming phases. Compare the context
each phase receives, source revisions, software-version fit, structured
settings, and behavior under missing or conflicting sources. Verify that
same-level conflicts are visible and can trigger HITL, and that an unresolved
runtime-configuration conflict blocks use. Keep the legacy path enabled for
that subject until the new path meets the agreed coverage; do not let an
implicit fallback or merge obscure which source won.

### 7. Retire local copies only after replacement is proven

After direct readers have been removed for a subject, keep its local documents
as a gateway source until each section has a replacement in the appropriate
upstream, organization, or (when available) user source. Delete a local copy
only after representative tickets show that every required phase can retrieve
the replacement, the source and version are visible, conflicts reach HITL when
unresolved, and no consumer still depends on the local path. Preserve a migration
record of moved content and links to authoritative references. Other subjects
continue their own migration independently.

### 8. Add user scope only after identity controls are ready

User-level context is not merely another directory. The service must derive the
user from authenticated ticket state, enforce self-or-admin access, and isolate
documents and snapshots across tickets and users. Once that exists, user
preferences can participate in locality-based resolution. They cannot override
mandatory organization policy or verified software behavior, and conflicting
user sources at the same level still need HITL when material and unresolved.

## Crucible is the first migration, not the final coverage

The current branch supports one or more administrator organization sources
(local paths or Git URLs), subject discovery, source provenance, duplicate and
same-path conflict reporting, and ticket-scoped organization snapshots. For
Crucible, it also routes eight audited local guides through a manifest-scoped
project source, alongside phase-appropriate installed controller references
for benchmark/review and bounded upstream catalog tools for triage. Conflicting
organization runtime configurations are configuration errors rather than merge
candidates.

This does **not** mean that all agentic-perf context already uses the gateway.
Other harnesses and subjects still have existing skill-provider,
local-document, prompt, or private-settings paths. Extending the project source
to their manifests, adding user-owned packages, adding native workload software
adapters beyond the current Crucible sources, and migrating each remaining
subject are future work. [Issue #1153](https://github.com/atheurer/agentic-perf/issues/1153)
tracks the detailed inventory and checks required before rollout; the
[design](design-skill-gateway.md) describes the source contracts.
