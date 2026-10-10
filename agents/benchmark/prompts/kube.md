## Kube Endpoints

For kube endpoints (endpoint_type: "kube"):
- Before selecting endpoint settings, retrieve `harness/kube-burner` guidance
  through `get_skill_context(subject="harness/kube-burner", operation="bootstrap")`
  and read applicable returned documents
- The controller serves as both the benchmark controller and the K8s
  cluster host. Use the controller's private IP as the kube host address
- Targets may be empty — workloads run as pods, not on separate hosts
