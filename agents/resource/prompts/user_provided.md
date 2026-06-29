## User-Provided Hosts

The ticket contains explicit hostnames or IP addresses.

1. Call parse_host_config to extract structured host info.
2. Validate each host with validate_host.
3. Call submit_resource_result with resource_provider="user_provided".
