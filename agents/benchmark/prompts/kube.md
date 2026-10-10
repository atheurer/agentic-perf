## Kube Endpoints

For kube endpoints (endpoint_type: "kube"):
- Read the harness's endpoint documentation through its available context or
  documentation tools for the format required by the selected endpoint
- The controller serves as both the benchmark controller and the K8s
  cluster host. Use the controller's private IP as the kube host address
- Targets may be empty — workloads run as pods, not on separate hosts
