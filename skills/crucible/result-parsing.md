# Crucible result retrieval workflow

The review agent should first check ticket artifacts and the result summary.
Use the summary's metric inventory to decide which data can be queried through
CDM. Query CDM with cdm_api_requests; use the run summary for indexed metadata
and read_run_results for raw result files when the requested source is
unavailable in CDM.
Use get_skill_context with subject harness/crucible for the installed
controller's result and CDM documentation instead of relying on old API routes
or hard-coded controller paths.

Do not retrieve benchmark results by SSHing directly to client or server hosts.
Keep queries bounded, inspect tool responses for workspace references when
they are spilled, and support each conclusion with the returned data and its
source. If the run summary and available data disagree, report that discrepancy
instead of silently selecting one representation.
