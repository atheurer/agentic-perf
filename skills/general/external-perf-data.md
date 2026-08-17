# Historical Performance Data (External Tools)

You have access to external tools that provide historical performance baselines. Use them to add quantitative context to your analysis.

## When to use

- **During analysis (Step 4):** Call `get_baseline_stats` with the target platform to understand historical norms before interpreting the current results.
- **During investigation (Step 5):** Call `compare_run_to_baseline` with observed metric values to quantify how the current run deviates from history (z-scores, deviation percentages, assessments).
- **In the review (Step 6):** Include baseline context in your analysis so the reader can see how results compare to historical norms.

## Query guidance

- If `get_baseline_stats` returns no data, check the `available_targets` field for valid target names.
- Use `from_timestamp` (e.g. '30d') to scope to recent history.

## Token efficiency

- **Always prefer `get_baseline_stats`** for summaries (~2-3 KB).
- **Avoid `get_key_metrics`** for bulk data retrieval — raw responses are 800 KB-2 MB and cannot be reasoned about effectively.
