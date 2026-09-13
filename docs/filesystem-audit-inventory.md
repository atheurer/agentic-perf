# Filesystem audit inventory

`tests/test_filesystem_inventory.py` parses every Python module under `agents`,
`orchestrator`, `providers`, and `state_store`, plus `paths.py`. It recognizes
path mutation methods, write-mode `open`/`fdopen`/`tarfile.open`, OS and shutil
mutators, and temporary-file constructors. Its fixed `file:line:call` manifest
must exactly equal the discovered set, so additions, removals, and moved calls
require review.

Ticket-owned paths must use `AuditedFilesystem`: ticket persistence/archive
(`state_store/store.py`), workspaces (`providers/workspace/manager.py`), artifact
directory creation (`paths.py`), artifact export archives
(`state_store/api/artifacts.py`), and boot-time generated metadata/results
(`agents/benchmark/server.py`).  Ticket-aware callers without a state-store
recorder use the process-managed, fsyncing trace spool and fail closed if it is
unavailable.

The manifest entries fall into these reviewed classes:

* `providers/execution/filesystem.py` contains the reviewed low-level mutation
  primitives. Calls through `filesystem`, `staging`, `artifact_filesystem`,
  `log_filesystem`, or `self._filesystem` are audited facade calls.
* Ticket-aware branches in `agents/benchmark/server.py`,
  `agents/infra/server.py`, `providers/resource/jumpstarter_provision.py`,
  `providers/workspace/manager.py`, `state_store/store.py`,
  `state_store/api/artifacts.py`, and `paths.py` use that facade. Direct calls in
  those modules are explicit no-ticket compatibility fallbacks.
* `providers/tracing/{spool,payloads,fingerprints}.py`, `providers/events.py`,
  `providers/quota.py`, `state_store/audit.py`, and `state_store/trace_store.py`
  are audit transport or process-log internals; recursively auditing them would
  make the durable recorder depend on itself.
* `state_store/auth.py`, `state_store/identity.py`,
  `providers/secrets/bitwarden.py`, and `providers/resource/jumpstarter.py`
  manage operator identity, credentials, or provider configuration. They are
  deliberately excluded from ticket-owner traces.
* `providers/image_build/caib.py`, `providers/skills/arcaflow_plugins.py`,
  `providers/skills/repo_cache.py`, and `providers/investigation/file.py` own
  build caches or global provider records without a ticket ownership contract.
* `orchestrator/main.py` lock mutations and remaining agent scratch-directory
  constructors are process coordination/bootstrap operations without a ticket.

Operator diagnostics may retain physical paths only in local process logs.  Trace
events, spool frames, payload blobs, exports, and state-store rows contain only
logical `workspace://`, `artifact://`, or `ticket://` references and bounded
digest/type/code metadata.

The literal manifest lives beside the scanner so failures print exact missing
and added entries. Reviewers must classify every delta against the classes above
before updating it; broad module or directory wildcards are not accepted.
