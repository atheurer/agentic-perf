## Kube Endpoints

For kube endpoints (endpoint_type: "kube"):
- Read the harness's kube endpoint skill doc (e.g., `kube-endpoints.md`)
  for the correct endpoint format — it differs from remotehosts
- The controller serves as both the benchmark controller and the K8s
  cluster host. Use the controller's private IP as the kube host address
- Targets may be empty — workloads run as pods, not on separate hosts
