## Auto-Select Resource Provider

No specific provider was requested. Choose the best provider:

1. Call `list_resource_providers` to see what is configured.
   **Only attempt providers returned by this call.** Do NOT try
   providers that were not in the list — they will fail.
2. If only one provider is configured, use it. If multiple are
   available, prefer bare-metal providers for performance testing
   — they offer dedicated hardware without virtualization overhead.
3. If the ticket directives include a `board_selector`, the
   provider is Jumpstarter.
4. Call `check_available_resources` with the chosen provider.
5. Call `reserve_resources` with the selected options. Always
   include the `ticket_id` for traceability.
6. Validate each host with `validate_host` (skip for Jumpstarter
   — boards are not yet provisioned).
7. Call `submit_resource_result` with the reservation details.
