# Ticket status lifecycle

Status: current. This table is derived from `state_store/models.py` and
`orchestrator/dispatcher.py`. The API rejects transitions not listed here.

## Statuses and dispatch mapping

| Status | Dispatch target | Meaning |
|---|---|---|
| `new` | — | newly created ticket |
| `triage_pending` | `triage` | request classification |
| `awaiting_hardware` | `resource_create` | acquire or retry resources |
| `preparing_platform` | `platform` | flash/provision platform |
| `awaiting_provision` | `provisioning` | install/configure harness |
| `executing_benchmark` | `benchmark` | execute benchmark |
| `awaiting_review` | `review` | inspect results and choose next action |
| `awaiting_teardown` | `resource_teardown` | clean up resources |
| `awaiting_customer_guidance` | — | paused pending user input |
| `retrospective_pending` | `retrospective` | post-run analysis |
| `closed` | — | terminal status |
| `analyzing` | `analyze` | data-only analysis |
| `building_image` | `image_builder` | custom image build |
| `gathering_context` | `gathering_context` | dedup/change context |
| `planning_investigation` | `planning_investigation` | plan investigation |
| `evaluating_convergence` | `evaluating_convergence` | convergence decision |
| `coordinating_fleet` | `fleet_coordinator` | record/select fleet host |
| `synthesizing_results` | `synthesizing_results` | final investigation record |

There are 18 statuses, 15 dispatch mappings, one out-of-band introspection
agent, one paused status, and one terminal status.

## Valid paths

```text
new → triage_pending
triage_pending → awaiting_hardware | analyzing | building_image |
                  gathering_context | awaiting_customer_guidance
awaiting_hardware → preparing_platform | awaiting_provision |
                    coordinating_fleet | gathering_context | awaiting_customer_guidance
preparing_platform → awaiting_provision | coordinating_fleet | awaiting_customer_guidance
building_image → awaiting_hardware | awaiting_customer_guidance
analyzing → awaiting_review | awaiting_hardware | building_image | awaiting_customer_guidance
gathering_context → analyzing | planning_investigation |
                    retrospective_pending | awaiting_customer_guidance
planning_investigation → awaiting_provision | awaiting_hardware | awaiting_customer_guidance
awaiting_provision → executing_benchmark | awaiting_hardware | awaiting_review |
                     awaiting_teardown | awaiting_customer_guidance
executing_benchmark → awaiting_review | evaluating_convergence | coordinating_fleet |
                     awaiting_provision | awaiting_teardown | awaiting_hardware |
                     awaiting_customer_guidance
coordinating_fleet → awaiting_hardware | evaluating_convergence | awaiting_customer_guidance
evaluating_convergence → analyzing | planning_investigation | preparing_platform |
                         awaiting_provision | synthesizing_results | awaiting_customer_guidance
synthesizing_results → awaiting_teardown | awaiting_customer_guidance
awaiting_review → awaiting_teardown | synthesizing_results | analyzing |
                 triage_pending | executing_benchmark | awaiting_hardware |
                 awaiting_provision | awaiting_customer_guidance
awaiting_teardown → retrospective_pending | closed | awaiting_hardware |
                   awaiting_provision | executing_benchmark | awaiting_review |
                   awaiting_customer_guidance
retrospective_pending → closed
```

`awaiting_customer_guidance` has no static outgoing edge: a user reply or
control action supplies the next transition. `closed` has no outgoing edge.
The plan loop can intentionally return to analysis, hardware, provisioning,
execution, review, or teardown.

## Controls and terminal behavior

- `agentic-perf reply TICKET MESSAGE` adds guidance and resumes a paused ticket;
  `--remember` restores the previous conversation context.
- `agentic-perf stop TICKET` requests a graceful stop; `--hard` cancels the
  task immediately. `stop-all` applies the same choice to active tickets.
- `agentic-perf abort TICKET [REASON]` records an abort and routes cleanup.
  `force-close` is the administrative escape hatch and may bypass normal
  teardown; verify resources independently afterward.
- Normal completion reaches teardown, then retrospective when configured, and
  finally `closed`. Stopping a task does not mean the external resource is
  released; inspect the ticket and provider after a hard stop.

To detect documentation drift, compare this file with the enum and maps during
release review; `/api/v1/tickets/{id}/transitions` exposes the valid next states
for a live ticket.
