# Filesystem audit inventory

The checked inventory command is:

```bash
rg -n 'write_text\(|write_bytes\(|\.unlink\(|NamedTemporaryFile|mkstemp|os\.replace|\.rename\(' \
  agents providers state_store paths.py --glob '*.py'
```

Ticket-owned paths must use `AuditedFilesystem`: ticket persistence/archive
(`state_store/store.py`), workspaces (`providers/workspace/manager.py`), artifact
directory creation (`paths.py`), artifact export archives
(`state_store/api/artifacts.py`), and boot-time generated metadata/results
(`agents/benchmark/server.py`).  Ticket-aware callers without a state-store
recorder use the process-managed, fsyncing trace spool and fail closed if it is
unavailable.

The remaining direct filesystem calls are intentionally excluded:

* `providers/tracing/{spool,payloads,fingerprints}.py`: trace transport internals
  cannot recursively emit trace filesystem events; their own checksummed atomic
  protocol is the durable recorder.
* `state_store/auth.py`, `state_store/identity.py`, and
  `providers/resource/jumpstarter.py`: instance/operator credentials or provider
  configuration, never ticket-owned content.  They must not appear in owner
  traces.
* `providers/image_build/caib.py`: a build-tool manifest temporary, with no
  ticket identifier or artifact ownership contract.
* `providers/investigation/file.py`: global investigation records, which have
  their own provider lifecycle and are not ticket workspace/artifact state.
* `agents/{infra,benchmark}/server.py` temporary SCP runfiles: process-local
  transport staging files, removed immediately after transfer; the remote copy is
  already covered by the audited SSH/SCP boundary.  They contain no persisted
  owner artifact and therefore cannot be represented as a stable logical ref.
* `orchestrator/main.py` lock file and `providers/image_build` cleanup: process
  coordination/build internals, not ticket state.

Operator diagnostics may retain physical paths only in local process logs.  Trace
events, spool frames, payload blobs, exports, and state-store rows contain only
logical `workspace://`, `artifact://`, or `ticket://` references and bounded
digest/type/code metadata.
