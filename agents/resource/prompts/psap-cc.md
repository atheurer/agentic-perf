## GPU Cluster Providers (psap-cc)

GPU cluster providers are different from bare-metal and cloud providers:
- They provide access to pre-existing K8s/OpenShift clusters with GPUs
- The reservation gives you exclusive use of a cluster, not individual hosts
- Access is via kubeconfig/API server, not SSH
- The `hosts` field will be empty — cluster access info is in provider_metadata
- These are best for AI/ML workloads that need GPUs (inference, training benchmarks)

When using a GPU cluster provider:
1. Call check_available_resources to see available clusters and their GPU inventory
2. Select a cluster based on GPU type/count matching the ticket requirements
3. Call reserve_resources with the cluster_id from the options
4. Call submit_resource_result with assigned_hardware_ips={}, empty ssh_user/ssh_key_path,
   and the cluster info in resource_provider_metadata
