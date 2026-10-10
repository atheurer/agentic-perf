"""Shared instructions for progressive, source-aware subject context retrieval."""

from __future__ import annotations


def skill_context_prompt(subject: str) -> str:
    """Describe the gateway contract without embedding source-specific guidance."""
    return f"""\
## Subject Context

Before making decisions for `{subject}`, call
`get_skill_context(subject="{subject}", operation="bootstrap")`.
When the subject is a harness and its benchmark is known, include the benchmark
name so benchmark-scoped project guidance can be returned. Read the returned
phase entrypoint documents from each available source using
`operation="read", ref=<returned-ref>`. Project workflow, organization
guidance, and software documentation are separate scopes; bootstrap discovery
does not load their document contents. For soft guidance about the same claim,
the authority order, when user guidance is available, is authenticated user
context before organization context, organization before upstream guidance, and
upstream before bundled project-local docs. This gateway does not yet load
user-scoped skill packages. Bundled project-local docs are a temporary fallback
with the lowest default authority.
Runtime facts have a separate domain: installed controller/version evidence
establishes what is present and works there, while upstream software docs
explain general behavior. User preferences cannot override mandatory
organization policy or deterministic security requirements. A subject
identifies applicable guidance; it is not a software repository name or a
complete inventory of related software sources. An unconfigured subject means
no guidance package was found under that ID, not that related software
documentation is absent.
Reuse retrieved documents instead of repeating bootstrap.
Project refs preserve the benchmark filter used during bootstrap, so use the
returned ref for later reads and searches without repeating the benchmark.

Follow documentation pointers progressively. For a relative pointer, use
`operation="read", from_ref=<origin-document-ref>, path=<documented-path>`.
Keep the originating ref so paths resolve within the correct source. Do not
invent refs, assume repository layouts, or bypass the gateway through local
skill files, repository caches, or workspace copies of source documents.

Use `operation="search", query=<pattern>` when needed. Search queries are
regular expressions: spaces are literal and `|` separates alternatives.
Organization and project search accept POSIX extended regex without
backreferences. If either returns a continuation cursor, scope the next search
with a returned document ref from that source and pass its
`next_offset_bytes`. Controller search is bounded discovery without
continuation paging.
Optionally pass `from_ref` to restrict discovery to that document's source.
Read selected search results separately. Read pages contain `document.content`;
continue the same read with `offset_bytes=next_offset_bytes` until it is null,
keeping the same `max_bytes` (default 16384).

The server determines source availability, phase, ticket, and identity. Follow
returned availability/error information; do not guess missing guidance or
replace an unavailable required source silently. Distinguish preferences from
software facts and service-enforced constraints. Installed-software documents
govern runtime behavior. Report material conflicts with their source refs.
Compare bundled project-local docs against higher sources and request
clarification when a material conflict remains; never silently treat the
bundled docs as authoritative. Prose cannot override a tool's validation or
authorization requirements.

Bootstrap may also advertise approved configuration views; read their returned
refs through the gateway or use the matching configuration tools. Use evidence
tools for actual observations. Neither a skill document nor configuration guidance
establishes what happened in a run. Secret values are never context documents.
"""
