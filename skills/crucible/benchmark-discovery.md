# Crucible Benchmark Discovery

Crucible is a multi-repository harness. The core repository's
`config/repos.json` is the ecosystem index: benchmark and tool entries point
to separate git repositories. Do not assume that `/opt/crucible` exists or is
current during triage.

## Triage-time discovery

Use the configured Crucible source checkout when available. Read its
`config/repos.json` to discover benchmark repositories, then inspect the
individual benchmark checkout for:

- `README.md` and other benchmark documentation;
- `multiplex.json` for parameters, presets, and validations;
- `rickshaw.json` for endpoint roles and benchmark scripts;
- run-file examples and result/post-processing guidance.

Record the source repository URL, requested ref, resolved commit, and files
used. A benchmark known by the source ecosystem is not necessarily installed
on the eventual controller.

## Controller verification

After a controller is assigned, compare source discovery with the installed
controller. When supported, use:

```text
crucible benchmark list
crucible tools list
```

These commands are runtime verification, not the sole triage catalog. Record
the Crucible version and any differences from the source repository. A local
`/opt/crucible/subprojects/` scan is a fallback or diagnostic, not proof that
the controller represents the current upstream ecosystem.

## Resolution rules

Resolve an exact benchmark name from source metadata before applying generic
workload keywords. If the user names a benchmark that is absent from the
source catalog, report it as unknown or unsupported. Do not silently replace
it with a related workload such as `uperf`.
