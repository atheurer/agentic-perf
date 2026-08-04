# Webhook Grounding: Run Metadata Reference

When a ticket is created by a webhook, the orchestrator calls
`get_run_info` from the Domain MCP to resolve metadata for the
run that triggered the alert. The raw metadata is written to the
ticket as `run_metadata` for downstream agents and deterministic
code to use directly.

No configuration or label mapping is required — the metadata
passes through as-is.

## Run Metadata Fields

| Field | Description | Example |
|---|---|---|
| `target` | Board/platform target | `ride4_sa8775p_sx_r3`, `ebbr` |
| `os_id` | OS identifier | `rhivos`, `autosd` |
| `mode` | Image build type | `bootc`, `ostree`, `raw` |
| `build` | Build number | `16609661.875426a8` |
| `test_name` | Horreum test name | `boot-time-verbose` |
| `start_time` | Run start timestamp | `2026-08-03T00:12:45+00:00` |
| `stop_time` | Run end timestamp | `2026-08-03T00:45:50+00:00` |
| `labels` | Full Horreum dataset labels | (dict) |

## Key Labels

| Label | Purpose | Example |
|---|---|---|
| `RHIVOS Target` | Image manifest key for provisioning | `ride4_sa8775p_sx_r3`, `ebbr` |
| `RHIVOS OS ID` | OS type | `rhivos`, `autosd` |
| `RHIVOS Mode` | Image build type | `bootc`, `ostree` |
| `RHIVOS Release` | Version/release identifier | `latest-RHIVOS-2-202607240103` |
| `RHIVOS Build` | Build number | `16609661.875426a8` |
| `RHIVOS image name` | Build variant | `ps`, `qe` |

## Notes

- `ebbr` is a shared image type used across multiple boards
  (e.g., R-Car S4, S32G). It is NOT a board type.
- Board selection is handled by the resource agent using
  Jumpstarter exporter labels — no mapping config needed.
- Image version derivation from `RHIVOS Release` is handled
  by the image resolution code in the platform agent.
