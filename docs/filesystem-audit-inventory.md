# Filesystem mutation policy

`tests/test_filesystem_inventory.py` parses first-party Python files in the
repository, including `cli.py` and `scripts/*.py`. It excludes tests, vendored
code, generated output, and virtual environments. CI fails if it finds a
recognized raw filesystem mutation anywhere outside
`providers/execution/filesystem.py`. The failure lists the current
`file:line:call` and enclosing lexical scope. Physical line numbers are
diagnostics only; source movement does not require inventory edits.

The check runs identically for pull requests, pushes, and local test runs. It
does not compare Git diffs, require PR context, call GitHub APIs, or use an LLM.
Duplicate calls remain visible as separate locations/counts. Imports and
read-only file access are allowed.

## Mutation boundary

`AuditedFilesystem` is the single first-party Python mutation boundary. It owns
the raw create, write, append, replace, rename, link, permission, temporary
file/directory, descriptor, archive, and cleanup primitives. Add new low-level
operations to this facade, then route their callers through it.

Use a critical facade with an emitter for ticket-owned data. Ticket workspace
and artifacts use durable trace recording so a ticket action cannot mutate its
files without a requested and terminal audit event. Use
`AuditedFilesystem.system(...)` for process-level state, caches, credentials,
transport staging, and no-ticket scratch paths. This explicitly selects the
non-audited runtime context. Do not add raw-call exceptions or per-site
allowlists to the inventory.

The gate protects source structure. It does not sandbox arbitrary Python code
or remove operating-system filesystem access. Its scanner recognizes common
first-party APIs including `Path` mutators, write-mode `open`/`fdopen` and
`tarfile.open`, write-capable `os.open` flags, OS descriptor writes/truncation
and mode changes, write-mode `zipfile.ZipFile` and `writestr`, temporary-file
constructors, and common `shutil` mutators. It recognizes `Path.replace` for
known `Path` bindings and unannotated calls matching its one-target signature;
numeric targets and known date/time receivers remain allowed, as do ordinary
`str.replace(old, new)` calls. It resolves imported and typed `ZipFile`
instances so `ZipFile.open(name, mode)` checks the correct mode argument and
its default read mode. For unknown bound `.open()` calls, it parses exact mode
strings in likely mode positions and fails closed on dynamic modes instead of
matching characters in filenames. The scanner also resolves import aliases and
straightforward local assignments such as `delete = os.unlink`. It fails
closed when raw mutator function objects are passed or returned.

The static scan cannot reliably follow dynamic lookup (`getattr`, reflection,
computed names), arbitrary rebinding or alias flows, or all third-party APIs.
Writes performed by subprocesses, native extensions, and database engines
(including SQLite) are outside this Python-call scanner. Ticket-owned writes
delegated to an external process still need an explicit product-level audit
boundary, and hostile-code isolation requires operating-system controls.

## Implementation notes

`AuditedFilesystem` records ticket-owned mutations using logical paths such as
`workspace://`, `artifact://`, and `ticket://`. Critical ticket mutations fail
closed when the durable trace recorder is unavailable. System-context
operations intentionally emit no ticket events, while still using the same
reviewed mutation implementation.

The facade is also used inside tracing persistence and spool code. That
dependency is deliberate: trace storage is itself system-context data, and
keeping its file mutations in the facade avoids an exception for the audit
transport. SQLite's own on-disk writes remain the documented database-engine
boundary.
