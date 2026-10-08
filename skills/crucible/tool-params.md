# Crucible tool configuration workflow

Tool availability, parameters, placement, and collected metrics are runtime
and version dependent. Discover the installed controller's tool metadata and
documentation through get_skill_context; do not use a static table or assume
that a tool can run in a client/server engine. Omitted tool-params may invoke
controller defaults, while explicit entries select a run-specific tool set;
confirm the current controller behavior before changing the defaults.

Use only parameters and roles supported by the installed tool metadata and
benchmark endpoint schema. If a requested collector cannot be represented by
the supported run file, do not invent a profiler ID or move it to an arbitrary
host. Surface the schema/documentation conflict and ask for clarification.

For result interpretation, use the run summary's metric inventory and current
CDM metadata. Do not assume that a tool's output is indexed or that an absent
metric means the tool did not run until the run artifacts and post-processing
status have been checked.
