# Crucible benchmark discovery workflow

Triage uses the typed benchmark catalog to identify candidate benchmarks. For
a Crucible benchmark, the benchmark agent then uses get_skill_context with
subject harness/crucible and the optional benchmark name. Follow the returned
installed-software pointer to the benchmark subproject documentation and
metadata. That Crucible-side integration defines run-file parameters and
endpoint roles. If the native benchmark's own behavior also matters, consult
its software documentation separately; do not derive Crucible parameters from
native command-line options.

Triage can establish that a benchmark exists in the source catalog, but that
does not prove it is installed on the eventual controller. The benchmark agent
must use the assigned controller's available discovery tools and installed
documentation before constructing a run file. If the requested benchmark or
its integration docs cannot be verified, report the gap instead of substituting
a similar workload.
