## Auto-Select Resource Provider

No specific provider was requested. Choose the best provider:

1. Call list_resource_providers to see what is configured.
2. Prefer bare-metal providers for performance testing — they offer
   dedicated hardware without virtualization overhead.
3. **If Jumpstarter is available** and the ticket targets embedded/bare-metal
   hardware, call `list_jumpstarter_targets` to see available hardware
   types. Match the user's platform description (e.g., "R-Car S4",
   "SA8775P", "Qualcomm Ride4") to the correct target selector. If NO
   target matches the user's platform request, call `request_clarification`
   — do NOT fall back to a different platform. Getting the wrong board
   type wastes a physical hardware lease.
4. Call check_available_resources with the chosen provider, including
   `jumpstarter_selector` for Jumpstarter providers.
5. Call reserve_resources with the selected options. Always include the
   ticket_id for traceability.
6. Validate each host with validate_host (skip for Jumpstarter — boards
   are not yet provisioned).
7. Call submit_resource_result with the reservation details.
