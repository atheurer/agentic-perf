# Verbatim Agent Directives

## Problem

The triage agent summarizes ticket descriptions when building `scoped_context`
for downstream agents. This summarization can lose critical verbatim instructions.
For example, "32 combined queues required" and "assign IP to base interface if
missing" were both dropped when triage condensed a provisioning section to
"400G interface configuration, TCP tuning, and IRQ affinity." The provision
agent then fell back to its own defaults, setting 1 queue and skipping IP
assignment.

## Solution

Mark verbatim content in the ticket description using a fenced block with an
agent-target header. These blocks are parsed **deterministically at ticket
creation** (no LLM) and delivered byte-for-byte to the named agent.

## Syntax

````
```agent:<target>
<directives>
```
````

`<target>` is one of the agent keys: `provision`, `benchmark`, `review`,
`resource`.

**Example:**

````
Run uperf with 8 threads, RHEL9, 400G NIC.

```agent:provision
- that interface must have exactly 32 combined queues
- if the interface does not have an IPv4 address on a private network, add one
  - do not use VLAN interfaces — configure the base interface
- TCP congestion control: cubic, qdisc: fq_codel
- IRQs pinned round-robin to 32 CPUs on the NIC's NUMA node, irqbalance banned
- firewall disabled
- verify all settings before proceeding
```
````

## Multiple Targets

A single block can target multiple agents using a comma-separated list:

````
```agent:provision,benchmark
- use the 400G interface on NUMA node 0
```
````

The block is delivered independently to each named agent.

## Multiple Blocks

Multiple blocks targeting the same agent are joined with a blank line:

````
```agent:provision
- 32 combined queues
```

```agent:provision
- disable firewall
```
````

Both directives reach the provision agent.

## How it works

1. **Ticket creation** — `state_store/store.py` calls
   `parse_verbatim_directives(description)` and stores the result in
   `custom_fields["verbatim_directives"]`. This field is never written by any
   agent — write-protection is structural.

2. **Triage** — The triage agent sees the pre-parsed blocks with an explicit
   instruction not to re-summarize them in `scoped_context`. Its
   `scoped_context` entries should only cover supplemental context that the
   verbatim blocks do not already address.

3. **Agent dispatch** — `AgentBase._get_scoped_context` injects verbatim
   content under an authoritative header ahead of any triage-generated
   supplemental context:

   ```
   ## Directives (authoritative — follow exactly):
   <verbatim block>

   ## Additional context:
   <triage supplemental>
   ```

4. **No alias mapping** — use the exact agent key. The canonical keys are
   `provision`, `benchmark`, `review`, `resource`.
