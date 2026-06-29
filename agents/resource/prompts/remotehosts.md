## Remotehosts Endpoints

For remotehosts endpoints (default) with managed providers, always provision
min_hosts + 1:
- 1 dedicated controller host (runs the benchmark framework)
- min_hosts endpoint hosts (where workloads actually run)

The controller must NOT also serve as an endpoint.

The "Total hosts to provision" in the Resource Requirements section
already accounts for endpoint type — use that number directly.
