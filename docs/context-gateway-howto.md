# Set up and verify the context gateway

Use this guide to publish organization guidance, connect it to an agentic-perf
instance, and check what a ticket actually retrieved. The examples use the
integrated `harness/crucible` subject and an existing working installation.
For purpose and migration scope, see the [overview](context-gateway.md);
for all settings, see the [configuration reference](configuration.md#organization-skill-gateway).

The administrator owns source configuration. Package authors maintain documents
in a separate organization repository. Agents request subjects and returned
references through `get_skill_context`; they do not choose repository URLs,
credentials, ticket identity, or phase. Authenticated user packages remain future
work. Discovering a subject directory does not wire it into every agent; see
[adding another subject](#add-another-subject).

## 1. Create a subject package

In an organization-controlled repository, create this layout:

```text
organization-context/
  skills/
    harness/
      crucible/
        SKILL.md
        skill.json
        review.md
```

Save the following as `skills/harness/crucible/SKILL.md`:

```markdown
---
name: crucible
description: Shared organization practices for Crucible tickets.
metadata:
  subject: harness/crucible
---

# Crucible practices

Record the effective benchmark parameters and software revisions with results.
Use the installed controller documentation to verify supported parameters.
For review, follow [the review checklist](review.md).
```

Save the following as `skills/harness/crucible/review.md`:

```markdown
# Review checklist

Check that the recorded parameters match the approved test plan. Identify missing
raw results before drawing conclusions from a summary.
```

Export both files with `skills/harness/crucible/skill.json`:

```json
{
  "schema_version": 1,
  "subject": "harness/crucible",
  "documents": [
    {"path": "SKILL.md", "entrypoint": true},
    {"path": "review.md", "entrypoint": true, "phases": ["review"]}
  ]
}
```

The directory layout and manifest must name the same subject. The frontmatter
`name` must match the package directory (`crucible`), use lowercase letters or
digits separated by single hyphens, and be at most 64 characters. A nonempty
`description` is required. Include `SKILL.md` in the export manifest; a directory
of Markdown files alone is insufficient.

Only exported Markdown documents are readable or searchable. Paths are relative
to the package and must stay inside it. Omitted `phases` means all phases;
the example's `review.md` is visible only during review. Organization manifests
use phase filtering; the separate bundled project manifest also supports agent
filtering. Keep credentials, raw service configuration, and bulk results outside
these documents. Link to software documentation for software facts and keep
organization procedures in the package.

## 2. Choose where runtime settings come from

This package supplies documents only. Current harness tools continue to use
their existing deterministic defaults and legacy runtime settings when no
organization service configuration is bound. This is a useful first step for an
installation that already works.

To migrate runtime settings too, add `service-config/harness/crucible.json` at
the repository root, outside `skills/`. Supply the validated structured settings
needed by the provisioning, execution, and review tools. The
[runtime configuration contracts](../providers/skills/runtime_config.py) define
accepted fields; keep secret values in the [secrets provider](secrets.md).
Do not create an empty file merely to enable guidance: once selected, this file
is canonical and is not overlaid with legacy private JSON or harness defaults.

The gateway discovers documents and service configuration independently.
A configuration-only subject can supply settings without document entrypoints.
Approved configuration views expose only registered fields for the requesting
phase; the raw service JSON is never a skill document.

During migration, an explicit subject override can retain legacy settings even
when a service-config file is present:

```json
{
  "harness/crucible": {
    "source_id": "team",
    "legacy_config": true
  }
}
```

Put this object under `skill_gateway.organization.subjects` alongside the
`sources` list below. Legacy settings selected this way are not pinned with the
package. Remove the override when the service-config migration is ready. Do not
combine `legacy_config: true` with a non-null explicit `service_config` override.

## 3. Connect the instance to the repository

Merge one of the following examples into the existing instance's
`$AGENTIC_PERF_HOME/config.json` (default `~/.agentic-perf/config.json`). Preserve
its other settings. `team` is a stable source identifier, not a subject name;
one repository can contain many subjects.

### Local directory

For development, point to the repository root, not the package directory:

```json
{
  "skill_gateway": {
    "organization": {
      "sources": [
        {
          "id": "team",
          "kind": "path",
          "path": "/srv/organization-context"
        }
      ]
    }
  }
}
```

Replace the example path with an absolute path accessible to all workers.
The layout discovers `harness/crucible`; no per-subject location entry is needed.
Keep the directory stable during active tickets, because local content is
captured on first subject use.

### Shared Git repository

Commit and publish the package to an organization-controlled repository, then
replace the path source with a Git source:

```json
{
  "skill_gateway": {
    "organization": {
      "sources": [
        {
          "id": "team",
          "kind": "git",
          "url": "ssh://git@git.example.com/performance/organization-context.git",
          "ref": "main"
        }
      ]
    }
  }
}
```

Use the real URL and branch. The service fetches and caches it; each developer
does not need to clone it manually. The service account must have repository
read access. SSH uses its existing OpenSSH identity or agent and known-hosts
configuration with strict host-key checking. Reuse the existing identity and
public key; establish the Git host's trusted key through your normal process.

For HTTPS token authentication, use a credential-free HTTPS clone URL and add
this `auth` object to the source descriptor:

```json
{
  "kind": "https-token",
  "username": "oauth2",
  "secret_ref": "organization/context-read-token"
}
```

Store the token through the existing [secrets mechanism](secrets.md), accessible
to the service, and choose the username required by your Git host. Never put
the token in the URL, package, or `config.json`. See the
[authentication reference](configuration.md#organization-skill-gateway) for
SSH key-secret and GitLab file-reference alternatives.

## 4. Inspect configuration and retrieve context

Apply the binding during a suitable maintenance window and restart the
deployment using its normal service controls so workers use the intended
configuration. Start verification with a new ticket. Changing source bindings
under an existing ticket fails closed rather than replacing its pins.

Run CLI commands in the same environment as the instance. For a managed
development instance, first enter its shell from the host:

```bash
./scripts/dev-instance.sh shell --name INSTANCE_NAME
```

Then, from the agentic-perf checkout, inspect the redacted configuration:

```bash
python3 cli.py config show
```

Check the effective instance paths, source ID, host/ref/auth method or local
root, and discovery status. A path source should discover `harness/crucible`.
Git discovery is offline here and can report `not_checked` until a worker
initializes it. This command does not fetch documents, validate the whole
package, test Git authentication, or prove a ticket used a particular revision.

Use a representative Crucible ticket through your normal submission workflow
([user guide](user-guide.md)). The following are **agent MCP tool arguments**,
not shell commands or ticket directives. In a ticket-bound agent phase,
bootstrap the subject:

```json
{"subject":"harness/crucible","operation":"bootstrap"}
```

Inspect the organization entry in `sources`: it should be `available` and name
`team`, its revision, capture time, applicable documents, and entrypoints.
Bootstrap offers references; it does not establish that an agent read the
documents. During review, both example documents should be offered. In earlier
phases only `SKILL.md` should be offered from this package.

Read an entrypoint using the exact returned reference:

```json
{"subject":"harness/crucible","operation":"read","ref":"<returned-file-ref>"}
```

During review, follow the relative link using the returned `SKILL.md` reference:

```json
{"subject":"harness/crucible","operation":"read","from_ref":"<SKILL.md-ref>","path":"review.md"}
```

Search applicable sources, or narrow to the organization source with an origin:

```json
{"subject":"harness/crucible","operation":"search","query":"parameters","from_ref":"<SKILL.md-ref>"}
```

Organization directories do not support `list`; use bootstrap's exported
document inventory. Other adapters can return directory refs for `list`.
For read pagination, reuse the ref with `offset_bytes` set to the returned
`next_offset_bytes`. Continue list/search pages with each source's `next_cursor`
and the same subject, operation, query, and original scope. Check completeness
and source errors before treating a missing search match as absence. See the
[operation reference](design-skill-gateway.md#gateway-operations) for limits
and source-specific search syntax.

## 5. Verify what the ticket used

Inspect the agent's tool calls and results in its transcript:

```bash
python3 cli.py transcript TICKET_ID --json
```

Find `get_skill_context` bootstrap and subsequent read/search calls for the
subject. Compare the returned source IDs, refs, revisions, phase, and status
across consumers. Verify an actual read of the example text, and check review
separately to confirm the phase-specific document is available there.

For durable trace inspection, including available payloads, use:

```bash
python3 cli.py trace --ticket-id TICKET_ID --include-payloads --json
```

Follow `next_cursor` with `--cursor` if more trace pages remain. See
[trace querying](tracing-query.md) and [audit logging](audit-logging-and-tracing.md)
for access and retention details. Configuration discovery, offered references,
and fetched document bodies are different evidence; check the latter before
claiming guidance reached an agent. Verify controller availability separately:
benchmark/review use installed Crucible references, while triage can discover
benchmarks before a controller exists.

## Update guidance and handle conflicts

Publish updates to the configured Git branch and verify them with a new ticket.
Organization Git commits are resolved during ticket provider setup; later phases
reuse those pins. Local organization content is captured on first subject use.
Other project/local/upstream captures are also lazy. Restarting workers does
not refresh an existing ticket's captured documents. There is no gateway refresh
command; do not delete pins to force an update. After confirmed ticket closure,
the orchestrator cleans up ticket-owned captures; shared immutable organization
Git checkouts can remain for other tickets.

To add another repository, add a descriptor with a distinct stable `id` to
`organization.sources`. The gateway preserves every applicable source. Identical
documents are reported as duplicates; differing content at the same path is
reported in `overlaps`. Source order does not choose a winner, and this check
does not detect every semantic disagreement. Agents should raise material
unresolved conflicts through `request_clarification`, citing source IDs and
document paths. Verified software behavior and mandatory policy constrain
preferences regardless of source order.

Conflicting service configurations are not merged. Keep one canonical settings
file. If another source must retain its documents, disable its service-config
counterpart with a subject override containing its `source_id` and
`service_config: null`, following the
[binding reference](configuration.md#organization-skill-gateway). Verify the
result with a new ticket.

## Troubleshooting

| Observation | Check or action |
| --- | --- |
| Organization source is `unconfigured` | Confirm the instance home, repository layout, and exact subject. No binding exists for that subject; this does not say its software documentation is absent. |
| Git discovery is `not_checked` | `config show` is offline. Inspect the first worker/provider initialization and ticket retrieval before concluding authentication or package validation succeeded. |
| Configured source is unavailable | Check repository access, Git ref, trusted host key, service secret access, or worker filesystem permissions. A missing, unreadable, or empty configured repository is an error. Required sources default to blocking affected operations. |
| Subject is discovered but documents fail | Check frontmatter, matching subject/name, manifest exports, valid UTF-8 Markdown, contained paths, and size limits. Discovery checks metadata; capture validates content. |
| A document is missing in one phase | Check `phases` and the caller's server-owned phase. Confirm the document is exported and read through the correct source ref. |
| Guidance changed but an active ticket sees old text | Check its captured revision. Use a new ticket to test updates; existing pins intentionally persist. |
| An active ticket fails after a config edit | Restore the expected binding or use a new ticket with the new configuration. Missing or corrupt captures fail closed and are not silently rebuilt. |
| Runtime defaults disappeared after adding service-config | The selected service JSON is canonical. Supply required settings there or explicitly select legacy configuration during migration. |
| Organization `list` reports unsupported | Use bootstrap's document inventory and `read`/`search`; package directory enumeration is not exposed. |
| A search page reports incomplete or `page_too_small` | Follow source cursors or increase `max_bytes` up to 16384. An incomplete scan cannot confirm absence. |

## Add another subject

Add `skills/<namespace>/<name>/SKILL.md` and `skill.json`, with matching metadata,
to the configured repository. Add paired `service-config/<namespace>/<name>.json`
only when migrating runtime settings. Discovery requires no additional location
setting, but consumption still needs a gateway registration, phase policy, and
any software adapter or configuration-view schema for that subject. Validate
every consuming phase before retiring its old reader. The
[migration plan](context-gateway.md#migration-plan) describes that process;
publishing a directory alone does not migrate a harness.
