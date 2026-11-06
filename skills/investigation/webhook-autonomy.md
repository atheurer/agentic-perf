# Autonomous Investigation Rules for Webhook-Triggered Benchmarks

When this benchmark step is part of a webhook-triggered investigation
(`anomaly_context` is present in custom_fields), proceed autonomously
without escalating to HITL for decisions you can make yourself.

## Reproduction Decision

**Always reproduce** when:
- Analysis findings are absent (no analyze agent output yet), or
- `analysis_result.conclusive` is `false` — inconclusive analysis
  means more data is needed; reproduction is the data-gathering step.

**Use your judgment** when `analysis_result.conclusive` is `true`.
Read the conclusive finding. If it already explains the alert fully
(e.g., a known regression confirmed by log analysis), skip
reproduction and document why. If empirical validation would add
value (e.g., the finding is plausible but unverified), reproduce and
document that reasoning instead.

**Never ask the user whether to reproduce.** Make the call and add a
ticket comment explaining what you decided and why.

## Document All Autonomous Decisions

After each autonomous decision, add a ticket comment explaining the
reasoning so the investigation ledger captures why the agent
proceeded without escalating to HITL.
