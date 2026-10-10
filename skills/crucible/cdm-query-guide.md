# CDM query workflow for agentic-perf

Use the review agent's cdm_api_requests tool for CDM queries. Use the run
summary's reported metric inventory to check which sources and types are
available before querying. Read Crucible's current CDM documentation through
get_skill_context for request fields, filters, aggregation, and breakout
semantics; these details can vary by installed version and metric.

For each source/type combination, first inspect the available breakouts returned
by CDM, then request only dimensions needed to answer the ticket. Do not reuse a
breakout or aggregation assumption from a different metric. For multi-pair
results, use the run file and the returned metric metadata to map values to
benchmark IDs and hosts. If the API or metric metadata does not establish that
mapping, present the ambiguity for clarification rather than guessing.

Use read_run_results only for data that is not indexed in CDM or when CDM
cannot provide the requested evidence. Prefer bounded queries and cite the
returned values and period IDs in the review.
