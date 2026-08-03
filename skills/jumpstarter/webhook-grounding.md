# Webhook Grounding: Resolving Directives from Horreum Alerts

When a ticket is created by a Horreum webhook, the gathering
context agent must resolve hardware directives from the alert
data so downstream agents can provision and benchmark.

## Resolving run metadata

Call `get_run_info` with the `run_id` or `dataset_id` from the
ticket's `anomaly_context`. This returns the target, OS info,
and a `labels` dict with all Horreum dataset labels.

## Horreum label reference

| Label | Purpose | Example |
|---|---|---|
| `RHIVOS Target` | Image target for provisioning. Pass through as-is. | `ride4_sa8775p_sx_r3`, `ebbr` |
| `RHIVOS OS ID` | OS type (rhivos vs autosd) | `rhivos`, `autosd` |
| `RHIVOS Mode` | Image build type | `bootc`, `ostree`, `raw` |
| `RHIVOS Release` | Version/release identifier | `latest-RHIVOS-2-202607240103` |
| `RHIVOS Build` | Build number | `16609661.875426a8` |
| `RHIVOS image name` | Build variant (optional) | `ps`, `qe` |

## Directive resolution from labels

- **`harness`**: Infer from `anomaly_context.test_name` or the
  Horreum test name (e.g., `boot-time-verbose` → `boot-time`).
- **`board_selector`**: Use the Jumpstarter board-type label
  that corresponds to the `RHIVOS Target`. The `get_run_info`
  `target` field may help, but the board selector label is
  a Jumpstarter concept — check available boards if unsure.
- **`image_version`**: Derive from `RHIVOS Release` or
  `RHIVOS OS ID`. Examples:
  - `latest-RHIVOS-2-...` → `RHIVOS-2`
  - `autosd` OS ID → `AutoSD-10` (check release label)

## Important

- `ebbr` is a shared image type used across multiple boards
  (e.g., R-Car S4, S32G). It is NOT a board type.
- The `RHIVOS Target` label is the image manifest key for
  provisioning. For EBBR-compatible boards, this will be `ebbr`.
- Pass all resolved values through the `directives` dict in
  your `submit_gathering_context_result` call.

## Example directives

```json
{
  "directives": {
    "board_selector": "board-type=qc8775",
    "image_version": "RHIVOS-2",
    "harness": "boot-time"
  }
}
```
