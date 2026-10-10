## Cloud Provider Context

Before making AWS-specific resource decisions, call
`get_skill_context(subject="resource/aws", operation="bootstrap")` and read
the returned local and organization guidance. This resource subject contains
agentic-perf provider behavior; it does not contain AWS credentials. Keep using
the deterministic resource tools for availability checks, reservations,
validation, and teardown.
