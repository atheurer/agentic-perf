# Crucible subject context

Crucible guidance is retrieved through get_skill_context, the shared context
gateway. A bootstrap may return three distinct scopes:

- project: agentic-perf workflow and validation contracts maintained here;
- organization: administrator-maintained deployment practices;
- software: upstream or installed Crucible documentation.

Read applicable entrypoints from every returned scope and follow their refs.
Project documents are explicitly listed in skills/context-manifest.json and
filtered by phase, agent, and optional benchmark. They are migration context,
temporary fallback, and the lowest default authority for overlapping soft
guidance. The intended soft-guidance order is user, organization, upstream,
then bundled project-local docs. User-scoped context is not implemented yet.
Installed controller/version evidence establishes behavior present on that
controller; upstream references explain general software behavior. A user
preference cannot override mandatory organization policy or deterministic
security requirements. Use installed/upstream software references for their
respective behavior domains.

If guidance disagrees materially, compare the source claims and use
request_clarification when the conflict affects the run. Do not retrieve these
Crucible documents with read_skills or another direct document tool.
