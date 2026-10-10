"""Shared instructions for progressive, source-aware subject context retrieval."""

from __future__ import annotations


def skill_context_prompt(subject: str) -> str:
    """Describe the gateway contract without embedding source-specific guidance."""
    return f"""\
## Subject Context

Before making decisions for `{subject}`, call
`get_skill_context(subject="{subject}", operation="bootstrap")`.
Read returned entrypoint documents from each available source using
`operation="read", ref=<returned-ref>`. For a source without entrypoints,
select relevant documents from its returned document list. Organization
guidance, project-local guidance, and upstream software documentation are
separate sources; bootstrap discovery does not load their document contents.
A subject identifies applicable guidance; it is not a
software repository name or a complete inventory of related software sources.
An unconfigured subject means no guidance package was found under that ID, not
that related software documentation is absent. Reuse retrieved documents
instead of repeating bootstrap.

Follow documentation pointers progressively. For a relative pointer, use
`operation="read", from_ref=<origin-document-ref>, path=<documented-path>`.
Keep the originating ref so paths resolve within the correct source. Do not
invent refs, assume repository layouts, or bypass the gateway through local
skill files, repository caches, or workspace copies of source documents.

Use `operation="search", query=<pattern>` when needed. Organization search
accepts POSIX extended regex without backreferences. Project-local and cached
upstream searches use case-insensitive literal terms; `|` separates
alternatives. If organization search returns a continuation cursor, scope the
next search with an organization `from_ref` and pass its `next_offset_bytes`.
Other source searches are bounded discovery without continuation paging.
Optionally pass `from_ref` to restrict discovery to that document's source.
Read selected search results separately. Read pages contain `document.content`;
continue the same read with `offset_bytes=next_offset_bytes` until it is null,
keeping the same `max_bytes` (default 16384).

The server determines source availability, phase, ticket, and identity. Follow
returned availability/error information; do not guess missing guidance or
replace an unavailable required source silently. Distinguish preferences from
software facts and service-enforced constraints. Installed-software documents
govern runtime behavior. Report material conflicts with their source refs;
prose cannot override a tool's validation or authorization requirements.

Bootstrap may also advertise approved configuration views; read their returned
refs through the gateway or use the matching configuration tools. Use evidence
tools for actual observations. Neither a skill document nor configuration guidance
establishes what happened in a run. Secret values are never context documents.
"""
