# Crucible Result Retrieval & Parsing Guide (CDM-First Methodology)

This document guides the Review Agent on how to locate, retrieve, and interpret benchmark and tool data.

---

> [!IMPORTANT]
> **CDM-FIRST PRINCIPLE: ALWAYS QUERY CDM FIRST**
> 1. Sourcing benchmark and tool data should **first and foremost** come from CDM (Common Data Model) API queries using the `cdm_api_request` tool.
> 2. CDM automatically extracts, normalizes, and indexes steady-state metrics (including throughput, cpu-utilization, and interrupt rates) from both benchmarks and system tools.
> 3. **Manual raw file inspection is a secondary fallback.** You should only inspect and read raw files on the controller filesystem when:
>    * The benchmark or tool did not index its data into CDM (i.e. no CDM source exists).
>    * The CDM API is unreachable or returned an unrecoverable error.
> 4. Resorting to filesystem exploration and raw parsing when CDM contains the data wastes LLM iterations, consumes massive token limits, and risks rate-limiting (429) errors.
> 5. **DIRECT WORKLOAD HOST SSH'ING FOR RESULT HARVESTING IS FORBIDDEN:** Direct SSH connections to benchmark client/server hosts are strictly prohibited for result data retrieval. All performance and system metrics must be queried through CDM or read from the controller's run result files via `read_run_results`.

---

### Check CDM availability FIRST

Before querying CDM for any metric source, check the `metrics` array from
`get_run_summary`. This array lists every source and type that was successfully
post-processed and indexed. If a source (e.g., turbostat) is NOT in this list,
skip CDM entirely for that source and use `read_run_results` for raw file access.

## 1. Primary Path: CDM API Queries

Always try to retrieve your performance data using `cdm_api_request` to query the controller's port 3000 REST API.

### 🗺️ Complete CDM API Route Map
The following are the valid endpoints on the CDM service:
* **Get Run Configurations & Metadata:**
  `GET /api/v1/run/{run_id}`
* **Get Iterations for a Run:**
  `GET /api/v1/run/{run_id}/iterations` — List all iterations and active phases for the specific run.
* **Get Period Groups & Periods:**
  `GET /api/v1/run/{run_id}/period-groups` — List steady-state periods, warmup, and cooldown intervals with their unique Period UUIDs.
* **Get Normalized Metric Values (Recommended):**
  `POST /api/v1/run/{run_id}/metric-values`
  * **Note:** For metric post requests, always specify the `source` (e.g., `mpstat`, `procstat`, `uperf`) and the `type` (e.g., `Busy-CPU`, `Gbps`, `interrupts-sec`).

### ⚠️ Crucial Router & Query Constraints
* **HTML Responses on Port 3000 indicate a Router Mismatch:** The CDM REST server serves a Single Page Application (SPA). If you hit a 404 or specify an incorrect API route (e.g., omitting the `/run/{run_id}/` prefix), the port 3000 web server will return an HTML page fallback. **An HTML response means your API path is wrong — do not believe the API is down or fall back to raw curl debugging.** Correct the URL path and retry.
* **Avoid Timestamp-based Bounds (Negative IPC Bug):** Do not query metrics using raw start/end timestamps. Due to aggregation bugs, timestamp-based metrics for CPU execution events like `perf-stat::ipc` can return massive, impossible negative numbers. **Always specify the Period UUID in your JSON query body instead of timestamp bounds.** Period UUIDs aggregate correctly and return accurate values.

### Essential Skills Reference
To learn how to discover breakouts, apply threshold filters on high-core systems, perform per-pair parallel analyses, and construct optimized CDM query payloads, read the sibling guide:
* **Tool Call:** `read_skill(harness="crucible", filename="cdm-query-guide.md")`

### Quick CDM Queries
* **To find active iterations and periods:**
  `GET /api/v1/run/{run_id}/iterations`
* **To retrieve normalized metrics (e.g., Gbps, Busy-CPU):**
  `POST /api/v1/run/{run_id}/metric-values` with source/type parameters.

---

## 2. Secondary Fallback: Raw File Access via `read_run_results`

If certain metric or profiler logs are not indexed by CDM, you must access the raw results. **Do not write ad-hoc Python decompression or grep scripts.** Instead, use the dedicated, purpose-built `read_run_results` tool.

### A. Discovering Available Result Files (Listing Mode)
To see all available raw tool and metric files for a run, call `read_run_results` with **only** the `run_id` and `controller`:
```json
{
  "run_id": "<run-uuid>",
  "controller": "<controller-ip>"
}
```
This returns a list of absolute file paths for all logs under the run directory (such as turbostat, perf-stat, and sar-net metrics).

### B. Reading & Decompressing Files (Reading Mode)
To view the contents of any file returned in listing mode, call `read_run_results` specifying the `file_path`:
```json
{
  "run_id": "<run-uuid>",
  "controller": "<controller-ip>",
  "file_path": "/var/lib/crucible/run/.../turbostat-stdout.txt.xz",
  "max_bytes": 4000
}
```
* **Automatic Decompression:** If the file ends with `.xz`, the tool automatically decompresses it on the fly and returns the plain-text contents.
* **Size Control:** Use `max_bytes` to limit the chunk size and protect your context window.

---

## 3. Deep Fallback: Local Parsing Recipes

In rare situations where you need to run complex multi-file calculations or
statistical aggregations on tool-data directories, use
`read_remote_dir(host, remote_path)` to copy the directory locally, then read
individual files with the local Read tool. The following Python snippets show
how to process the files once you have them locally.

### Recipe 1: Extracting Turbostat Data
```python
import lzma
with lzma.open("/var/lib/crucible/run/<run-id>/run/tool-data/profiler/remotehosts-1-kernel-1/kernel/turbostat-stdout.txt.xz", "rt") as f:
    for line in f:
        line = line.strip()
        if not line or "Time_Of_Day_Seconds" in line:
            continue
        parts = line.split()
        if parts and parts[0].replace('.', '', 1).isdigit():
            pass # process core values...
```

### Recipe 2: Extracting Perf Stat (IPC / CPI)
```python
import lzma, re
with lzma.open("/var/lib/crucible/run/<run-id>/run/tool-data/profiler/remotehosts-1-kernel-1/kernel/perf-stat-stdout.txt.xz", "rt") as f:
    for line in f:
        if "instructions" in line:
            match = re.search(r'#\s+([\d\.]+)\s+insn per cycle', line)
            if match:
                print(f"IPC: {match.group(1)}")
```

---

## 4. Aligning Intervals with Steady-State Timestamps

Always filter your raw tool timestamps to match the steady-state period. Fetch the precise start/end epoch millisecond timestamps of each iteration from:
```bash
cat /var/lib/crucible/run/<run-id>/run/roadblock-msgs/start-tools-end.json
```
Only include tool data points whose timestamps fall strictly within these steady-state ranges.
