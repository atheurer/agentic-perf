# Releases, changelog, and upgrades

Status: operator policy. Until tagged releases are introduced, the Git commit
and `origin/main` revision are the compatibility identifiers. Every release
should publish a changelog entry covering configuration, API/schema, storage,
dependency, and behavior changes; breaking changes require a migration note.
Deprecations are announced in the changelog, retained for one documented
release where practical, and removed only after the removal is recorded.

## Upgrade checklist

1. Record the current commit, Python version, dependency lockfiles, and config.
2. Stop dispatch and wait for active tickets to reach a safe boundary; do not
   delete the state directory.
3. Back up `$AGENTIC_PERF_HOME` including tickets, logs, artifacts,
   investigation records, and secrets (protect the backup separately).
4. Review config changes, especially auth, iteration, model, provider, paths,
   and retention settings; migrate manually as documented by the release.
5. Install the new locked dependencies and restart the state store and
   orchestrator with the same instance identity.
6. Verify `/health`, `/openapi.json`, token access, dashboard access, and a
   mock/smoke ticket before resuming production work.
7. Check old active tickets, claims, event logs, artifacts, and usage; preserve
   them if an older schema is not explicitly declared migratable.

Closed tickets and JSONL events are expected to remain readable across nearby
versions, but compatibility is not promised across undocumented schema
changes. Archive backups before upgrades and test recovery before deleting
anything.
