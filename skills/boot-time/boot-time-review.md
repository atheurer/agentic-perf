# Boot Time Review Notes

## Result Location

Boot-time test results are stored on the **orchestrator host**,
not the SUT. The SUT's `/tmp` is cleared on every reboot cycle,
so searching the SUT filesystem for results will find nothing.

The benchmark agent's completion comment contains all KPIs:
- `avg_total_boot_s`, `avg_kernel_s`, `avg_initrd_s`, `avg_userspace_s`
- `sample_count`, `samples_collected`
- `output_dir` (local path on orchestrator)

If `output_dir` is set on the ticket, use `list_benchmark_artifacts`
and `read_benchmark_artifact` to access the merged results JSON.
If not, rely on the KPIs from the benchmark completion comment —
they contain the same data.

Do NOT search the SUT's `/tmp` or `/var/log` for boot-time results.
They will not be there.
