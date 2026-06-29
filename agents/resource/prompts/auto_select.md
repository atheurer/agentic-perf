## Auto-Select Resource Provider

No specific provider was requested. Choose the best provider:

1. Call list_resource_providers to see what is configured.
2. Prefer bare-metal providers for performance testing — they offer
   dedicated hardware without virtualization overhead.
3. If bare-metal is unavailable or cannot satisfy requirements, try cloud
   providers.
4. Call check_available_resources with the chosen provider and requirements
   from the ticket.
5. Call reserve_resources with the selected options. Always include the
   ticket_id for traceability.
6. Validate each host with validate_host.
7. Call submit_resource_result with the reservation details.
